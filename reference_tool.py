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
from pathlib import Path

from core.verify import VerifyWeights
from src.models import PipelineConfig
from src.downloader import load_config_file, run_pipeline


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
    parser.add_argument("--verify-title-rename", dest="verify_title_rename", action="store_true", default=True, help="Verify downloaded PDF title and rename on match (default: on)")
    parser.add_argument("--no-verify-title-rename", dest="verify_title_rename", action="store_false", help="Disable title verification and renaming")
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

    cookies_path = Path(args.cookies) if args.cookies and Path(args.cookies).exists() else None

    cfg = PipelineConfig(
        input_pdf=Path(args.input),
        output_dir=Path(args.output),
        pdf_parser=args.pdf_parser,
        header_margin=args.header_margin,
        footer_margin=args.footer_margin,
        timeout=args.timeout,
        lookup_timeout=args.lookup_timeout,
        retries=args.retries,
        cookies_path=cookies_path,
        verify_title_rename=bool(args.verify_title_rename),
        verify_rename_mode=str(getattr(args, "verify_rename_mode", "number_and_original")),
        verify_title_threshold=float(args.verify_title_threshold),
        verified_subdir=str(getattr(args, "verified_subdir", "verified_pdfs")),
        meta_subdir=str(getattr(args, "meta_subdir", "meta")),
        landing_subdir=str(getattr(args, "landing_subdir", "landing_urls")),
        mismatch_subdir=str(getattr(args, "mismatch_subdir", "mismatch_pdfs")),
        workers=args.workers,
        max_per_domain=args.max_per_domain,
        min_domain_delay_ms=args.min_domain_delay_ms,
        user_agent=args.user_agent,
        download_log=args.download_log,
        unpaywall_email=str(getattr(args, "unpaywall_email", "") or ""),
        max_candidates_per_item=args.max_candidates_per_item,
        skip_doi=args.skip_doi,
        download_max=args.download_max,
        secondary_lookup=args.secondary_lookup,
        secondary_max=args.secondary_max,
        secondary_top_k=int(args.secondary_top_k),
        secondary_cache=str(getattr(args, "secondary_cache", "") or ""),
        generic_download_sites=getattr(args, "generic_download_sites", []),
        domain_cookies_file=str(getattr(args, "domain_cookies_file", "domain_cookies.json")),
        no_download=args.no_download,
        resume=bool(getattr(args, "resume", True)),
        show_progress=not args.no_progress,
        interactive=str(getattr(args, "interactive", "auto")),
        api_concurrency=int(getattr(args, "api_concurrency", 1)),
        api_min_delay_ms=int(getattr(args, "api_min_delay_ms", 500)),
        neurips_proceedings=str(getattr(args, "neurips_proceedings", "true")).lower() == "true",
    )

    verify_weights = VerifyWeights(
        title_weight=float(getattr(args, "verify_title_weight", 1.0)),
        line_weight=float(getattr(args, "verify_line_weight", 1.0)),
        year_hit_bonus=float(getattr(args, "verify_year_hit_bonus", 0.0)),
        year_miss_multiplier=float(getattr(args, "verify_year_miss_mult", 0.95)),
        author_hit_bonus=float(getattr(args, "verify_author_hit_bonus", 0.0)),
        author_miss_multiplier=float(getattr(args, "verify_author_miss_mult", 0.97)),
    )

    run_pipeline(cfg, verify_weights=verify_weights)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
