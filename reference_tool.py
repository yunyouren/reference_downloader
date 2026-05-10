#!/usr/bin/env python3
"""
Extract, number, and download references from a paper PDF.

中文说明（概览）
----------------
这个脚本用于从论文 PDF 中：
1) 提取"参考文献/References"章节文本；
2) 将每条参考文献进行分段与编号；
3) 从条目文本中提取 DOI/URL；
4) 尝试自动下载 PDF（或保存落地页链接）；
5) 生成结构化输出（Markdown / CSV / JSON）。

整体流程（main）
--------------
读取 PDF 文本 -> 定位参考文献章节 -> 解析条目 -> 初次下载（可选）
-> 失败项二次检索（可选，Crossref/OpenAlex）-> 写入输出文件。

Features:
- Multiple parsing modes: numeric [1], 1., (1), and author/year-like entries (APA/MLA heuristics)
- Concurrent download with requests.Session() connection reuse
- Optional progress bars via tqdm
- Optional pdfplumber parser to reduce header/footer interference
"""

from __future__ import annotations

import argparse
import sys
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from core.verify import VerifyWeights
from src.models import ReferenceItem, DownloadLogger, SecondaryLookupCache
from src.parsers import read_pdf_text, extract_references_section, split_references
from src.candidates import iter_candidate_urls_with_generic_sites, normalize_generic_download_sites
from src.downloader import (
    make_session, load_config_file, load_cookies_txt,
    apply_resume_state, resolve_downloads_subdir,
    run_initial_download_phase, enrich_failed_references,
    load_domain_cookies_config, save_domain_cookies_config,
    load_domain_cookies, suggest_cookies_configuration,
)
from src.output import write_outputs


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（CLI）。"""
    parser = argparse.ArgumentParser(
        description="Extract references from PDF, number them, and download where possible."
    )
    parser.add_argument("--config", help="JSON config file path")
    parser.add_argument("--input", "-i", required=False, help="Input paper PDF path")
    parser.add_argument("--output", "-o", default="references_output", help="Output directory")
    parser.add_argument(
        "--pdf-parser",
        choices=["pypdf", "pdfplumber"],
        default="pdfplumber",
        help="PDF text parser backend (default: pdfplumber; falls back to pypdf if missing)",
    )
    parser.add_argument("--header-margin", type=float, default=40.0, help="Top margin for pdfplumber crop")
    parser.add_argument("--footer-margin", type=float, default=40.0, help="Bottom margin for pdfplumber crop")

    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds")
    parser.add_argument("--lookup-timeout", type=int, default=6, help="Secondary lookup API timeout seconds")
    parser.add_argument("--retries", type=int, default=1, help="Retries per candidate URL")
    parser.add_argument("--cookies", help="cookies.txt (Netscape) path for authenticated downloads")
    parser.add_argument("--verify-title-rename", action="store_true", help="Verify downloaded PDF title and rename on match")
    parser.add_argument(
        "--verify-rename-mode",
        choices=["original", "number_only", "number_and_original"],
        default="number_and_original",
        help="Rename mode when verify-title-rename is enabled",
    )
    parser.add_argument("--verify-title-threshold", type=float, default=0.55, help="Title match threshold (Jaccard)")
    parser.add_argument("--verify-title-weight", type=float, default=1.0, help="Verify score: weight for PDF title match")
    parser.add_argument("--verify-line-weight", type=float, default=1.0, help="Verify score: weight for first-page best line match")
    parser.add_argument("--verify-year-hit-bonus", type=float, default=0.0, help="Verify score: add bonus when year appears on first page")
    parser.add_argument("--verify-year-miss-mult", type=float, default=0.95, help="Verify score: multiply when year missing on first page")
    parser.add_argument("--verify-author-hit-bonus", type=float, default=0.0, help="Verify score: add bonus when author surname appears on first page")
    parser.add_argument("--verify-author-miss-mult", type=float, default=0.97, help="Verify score: multiply when author surname missing on first page")
    parser.add_argument("--verified-subdir", default="verified_pdfs", help="Put verified PDFs into downloads/<subdir> (empty disables)")
    parser.add_argument("--meta-subdir", default="meta", help="Put meta txt files into downloads/<subdir> (empty disables)")
    parser.add_argument("--landing-subdir", default="landing_urls", help="Put landing url txt files into downloads/<subdir> (empty disables)")
    parser.add_argument("--mismatch-subdir", default="mismatch_pdfs", help="Put mismatched PDFs into downloads/<subdir> (empty disables)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker count")
    parser.add_argument("--max-per-domain", type=int, default=2, help="Max concurrent requests per domain (0 means unlimited)")
    parser.add_argument("--min-domain-delay-ms", type=int, default=0, help="Minimum delay per domain between requests")
    parser.add_argument("--user-agent", default="ReferenceDownloader/1.1", help="HTTP User-Agent header")
    parser.add_argument("--download-log", default="download_log.csv", help="Write download attempt log CSV (empty to disable)")
    parser.add_argument("--unpaywall-email", default="", help="Your email for Unpaywall API (required for OA lookup)")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bars")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", dest="resume", action="store_true", help="Resume using existing output directory state")
    resume_group.add_argument("--no-resume", dest="resume", action="store_false", help="Disable resume behavior")
    parser.set_defaults(resume=True)

    parser.add_argument(
        "--max-candidates-per-item",
        type=int,
        default=3,
        help="Max URLs tried per item (0 means unlimited).",
    )
    parser.add_argument("--skip-doi", action="store_true", help="Skip DOI URL attempts in initial phase")
    parser.add_argument(
        "--download-max",
        "--initial-max",
        dest="download_max",
        type=int,
        default=0,
        help="Max references to attempt downloading (0 means all).",
    )

    parser.add_argument(
        "--secondary-lookup",
        action="store_true",
        help="For failed items, query Crossref/OpenAlex and retry.",
    )
    parser.add_argument("--secondary-max", type=int, default=40, help="Secondary phase max failed items")
    parser.add_argument("--secondary-top-k", type=int, default=2, help="Secondary lookup: keep top K title-similar candidates (0 means all)")
    parser.add_argument("--secondary-cache", default="cache/secondary_lookup_cache.json", help="Secondary lookup cache file (relative to output; empty disables)")
    parser.add_argument(
        "--generic-download-sites",
        nargs="*",
        default=[],
        help=(
            "Extra generic download site URL templates, e.g. "
            "'https://sci-hub.se/{doi}' or 'https://example.org/search?q={title_encoded}'"
        ),
    )
    parser.add_argument("--no-download", action="store_true", help="Only extract and number references")

    # 域名cookies配置
    parser.add_argument("--domain-cookies-file", default="domain_cookies.json", help="Per-domain cookies config file")
    parser.add_argument(
        "--interactive",
        choices=["auto", "true", "false"],
        default="auto",
        help="Interactive mode: auto (detect TTY), true, or false (batch mode)",
    )
    # API限速与NeurIPS proceedings注入
    parser.add_argument("--api-concurrency", type=int, default=1, help="API concurrency for secondary lookups")
    parser.add_argument("--api-min-delay-ms", type=int, default=500, help="Min delay between API calls (ms)")
    parser.add_argument(
        "--neurips-proceedings",
        choices=["true", "false"],
        default="true",
        help="Enable NeurIPS proceedings PDF injection in secondary lookup",
    )
    return parser


def main() -> None:
    """
    命令行入口。

    - no_download: 仅解析与导出，不进行任何网络下载；
    - skip_doi: 初次下载阶段不尝试 DOI（减少跳转开销或避免被出版方拦截）；
    - secondary_lookup: 对失败项启用二次检索并重试下载。
    """
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config")
    config_args, _ = config_parser.parse_known_args(sys.argv[1:])

    parser = build_arg_parser()
    if config_args.config:
        config_path = Path(config_args.config)
        config = load_config_file(config_path)
        valid_dests = {a.dest for a in parser._actions}
        filtered = {k: v for k, v in config.items() if k in valid_dests}
        parser.set_defaults(**filtered)
    args = parser.parse_args()
    if not args.input:
        parser.error("--input is required (or set 'input' in --config JSON)")
    cookies_jar: MozillaCookieJar | None = None
    if args.cookies:
        cookies_path = Path(args.cookies)
        if cookies_path.exists():
            cookies_jar = load_cookies_txt(cookies_path)
    input_pdf = Path(args.input)
    output_dir = Path(args.output)
    downloads_dir = output_dir / "downloads"
    meta_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "meta_subdir", "meta")))
    landing_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "landing_subdir", "landing_urls")))
    mismatch_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "mismatch_subdir", "mismatch_pdfs")))
    verified_dir: Path | None = None
    if bool(args.verify_title_rename):
        verified_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "verified_subdir", "verified_pdfs")))
    verify_weights = VerifyWeights(
        title_weight=float(getattr(args, "verify_title_weight", 1.0)),
        line_weight=float(getattr(args, "verify_line_weight", 1.0)),
        year_hit_bonus=float(getattr(args, "verify_year_hit_bonus", 0.0)),
        year_miss_multiplier=float(getattr(args, "verify_year_miss_mult", 0.95)),
        author_hit_bonus=float(getattr(args, "verify_author_hit_bonus", 0.0)),
        author_miss_multiplier=float(getattr(args, "verify_author_miss_mult", 0.97)),
    )

    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")
    if not input_pdf.is_file():
        raise ValueError(f"Input PDF is not a file: {input_pdf}")
    if input_pdf.stat().st_size <= 0:
        raise ValueError(f"Input PDF is empty: {input_pdf}")

    # 1) 读取全文文本（不同后端对页眉页脚/分栏的抗噪能力不同）
    try:
        full_text = read_pdf_text(
            input_pdf,
            parser=args.pdf_parser,
            header_margin=args.header_margin,
            footer_margin=args.footer_margin,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to read input PDF: {input_pdf} ({exc})") from exc
    # 2) 截取参考文献章节，并按风格分段成条目
    ref_section = extract_references_section(full_text)
    refs = split_references(ref_section)

    # 3) 域名分析与交互式配置
    from site_handlers.domain_analyzer import analyze_reference_domains
    from src.interactive_ui import should_run_interactive, display_domain_summary, configure_cookies_interactively

    # 加载域名cookies配置
    domain_cookies_file = Path(getattr(args, "domain_cookies_file", "domain_cookies.json"))
    if not domain_cookies_file.is_absolute():
        domain_cookies_file = output_dir / domain_cookies_file
    domain_cookies_config = load_domain_cookies_config(domain_cookies_file)

    # 分析域名
    domain_info = analyze_reference_domains(refs, domain_cookies_config)

    # 显示域名摘要（始终显示）
    display_domain_summary(domain_info)

    # 检测是否应该进入交互模式
    interactive_setting = str(getattr(args, "interactive", "auto"))
    is_interactive = should_run_interactive(interactive_setting)

    if is_interactive:
        # 交互式配置cookies
        new_config = configure_cookies_interactively(domain_info, domain_cookies_config)
        if new_config != domain_cookies_config:
            domain_cookies_config = new_config
            # 保存配置
            save_domain_cookies_config(domain_cookies_config, domain_cookies_file)
            print(f"\n已保存域名cookies配置到: {domain_cookies_file}")

    # 加载域名cookies
    domain_cookies = load_domain_cookies(domain_cookies_config, Path.cwd())
    generic_download_sites = normalize_generic_download_sites(getattr(args, "generic_download_sites", []))

    output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    if bool(getattr(args, "resume", True)):
        apply_resume_state(refs, output_dir=output_dir, downloads_dir=downloads_dir)

    if not args.no_download:
        logger = DownloadLogger()
        secondary_cache: SecondaryLookupCache | None = None
        cache_str = str(getattr(args, "secondary_cache", "") or "").strip()
        if cache_str:
            cache_path = Path(cache_str)
            if not cache_path.is_absolute():
                cache_path = output_dir / cache_path
            secondary_cache = SecondaryLookupCache(cache_path)
        initial_refs = refs[: args.download_max] if args.download_max > 0 else refs
        run_initial_download_phase(
            initial_refs,
            downloads_dir=downloads_dir,
            meta_dir=meta_dir,
            landing_dir=landing_dir,
            mismatch_dir=mismatch_dir,
            timeout=args.timeout,
            retries=args.retries,
            use_doi=not args.skip_doi,
            max_candidates_per_item=args.max_candidates_per_item,
            workers=args.workers,
            show_progress=not args.no_progress,
            user_agent=args.user_agent,
            max_per_domain=args.max_per_domain,
            min_domain_delay_ms=args.min_domain_delay_ms,
            logger=logger,
            cookies_jar=cookies_jar,
            verify_title_rename=bool(args.verify_title_rename),
            verify_title_threshold=float(args.verify_title_threshold),
            verify_rename_mode=str(getattr(args, "verify_rename_mode", "number_and_original")),
            verify_weights=verify_weights,
            verified_dir=verified_dir,
            domain_cookies=domain_cookies,
            generic_download_sites=generic_download_sites,
        )
        if args.secondary_lookup:
            # 4) 二次检索：只针对失败项补全 DOI/URL 再下载
            limited_refs = initial_refs
            enrich_failed_references(
                limited_refs,
                timeout=args.timeout,
                lookup_timeout=args.lookup_timeout,
                retries=args.retries,
                downloads_dir=downloads_dir,
                meta_dir=meta_dir,
                landing_dir=landing_dir,
                mismatch_dir=mismatch_dir,
                max_items=args.secondary_max,
                max_candidates_per_item=args.max_candidates_per_item,
                secondary_top_k=int(args.secondary_top_k),
                workers=args.workers,
                show_progress=not args.no_progress,
                user_agent=args.user_agent,
                max_per_domain=args.max_per_domain,
                min_domain_delay_ms=args.min_domain_delay_ms,
                logger=logger,
                cookies_jar=cookies_jar,
                verify_title_rename=bool(args.verify_title_rename),
                verify_title_threshold=float(args.verify_title_threshold),
                verify_rename_mode=str(getattr(args, "verify_rename_mode", "number_and_original")),
                verify_weights=verify_weights,
                verified_dir=verified_dir,
                secondary_cache=secondary_cache,
                unpaywall_email=str(getattr(args, "unpaywall_email", "") or ""),
                generic_download_sites=generic_download_sites,
                api_concurrency=int(getattr(args, "api_concurrency", 1)),
                api_min_delay_ms=int(getattr(args, "api_min_delay_ms", 500)),
                neurips_proceedings=str(getattr(args, "neurips_proceedings", "true")).lower() == "true",
            )
        if args.download_log:
            logger.write_csv(output_dir / args.download_log)

    # 5) 写出最终结果（refs 里含每条的下载状态与文件名）
    write_outputs(refs, output_dir)

    # 6) 简单汇总输出，便于快速判断成功率
    total = len(refs)
    ok_pdf = sum(1 for r in refs if r.download_status == "downloaded_pdf")
    ok_landing = sum(1 for r in refs if r.download_status == "saved_landing_url")
    failed = sum(1 for r in refs if r.download_status == "failed")
    print(f"Done. Parsed {total} references.")
    print(f"PDF downloaded: {ok_pdf}, landing URLs saved: {ok_landing}, failed: {failed}")
    print(f"Output directory: {output_dir.resolve()}")

    # 7) 提示用户配置机构 cookies
    if failed > 0:
        suggest_cookies_configuration(refs, domain_cookies_config, output_dir)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
