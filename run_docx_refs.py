#!/usr/bin/env python3
"""
从 .docx 文件中提取参考文献并批量下载。

用法：
    python run_docx_refs.py --input "文献.docx" --output references_output

该文件的内容直接作为参考文献章节文本（无需"参考文献"标题）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docx import Document

from core.verify import VerifyWeights
from src.models import PipelineConfig
from src.parsers import split_references
from src.downloader import load_config_file, run_pipeline_from_refs


def extract_text_from_docx(docx_path: Path) -> str:
    doc = Document(str(docx_path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract references from .docx and download where possible."
    )
    parser.add_argument("--config", help="JSON config file path")
    parser.add_argument("--input", "-i", required=False, help="Input .docx file path")
    parser.add_argument("--output", "-o", default="references_output", help="Output directory")

    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds")
    parser.add_argument("--lookup-timeout", type=int, default=6, help="Secondary lookup API timeout seconds")
    parser.add_argument("--retries", type=int, default=1, help="Retries per candidate URL")
    parser.add_argument("--cookies", help="cookies.txt (Netscape) path for authenticated downloads")
    parser.add_argument("--verify-title-rename", dest="verify_title_rename", action="store_true", default=True, help="Verify downloaded PDF title and rename on match (default: on)")
    parser.add_argument("--no-verify-title-rename", dest="verify_title_rename", action="store_false", help="Disable title verification and renaming")
    parser.add_argument("--verify-rename-mode", choices=["original", "number_only", "number_and_original"], default="number_and_original")
    parser.add_argument("--verify-title-threshold", type=float, default=0.55)
    parser.add_argument("--verify-title-weight", type=float, default=1.0)
    parser.add_argument("--verify-line-weight", type=float, default=1.0)
    parser.add_argument("--verify-year-hit-bonus", type=float, default=0.0)
    parser.add_argument("--verify-year-miss-mult", type=float, default=0.95)
    parser.add_argument("--verify-author-hit-bonus", type=float, default=0.0)
    parser.add_argument("--verify-author-miss-mult", type=float, default=0.97)
    parser.add_argument("--verified-subdir", default="verified_pdfs")
    parser.add_argument("--meta-subdir", default="meta")
    parser.add_argument("--landing-subdir", default="landing_urls")
    parser.add_argument("--mismatch-subdir", default="mismatch_pdfs")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-per-domain", type=int, default=2)
    parser.add_argument("--min-domain-delay-ms", type=int, default=0)
    parser.add_argument("--user-agent", default="ReferenceDownloader/1.1")
    parser.add_argument("--download-log", default="download_log.csv")
    parser.add_argument("--unpaywall-email", default="")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--resume", dest="resume", action="store_true")
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.set_defaults(resume=True)
    parser.add_argument("--max-candidates-per-item", type=int, default=3)
    parser.add_argument("--skip-doi", action="store_true")
    parser.add_argument("--download-max", "--initial-max", dest="download_max", type=int, default=0)
    parser.add_argument("--secondary-lookup", action="store_true")
    parser.add_argument("--secondary-max", type=int, default=40)
    parser.add_argument("--secondary-top-k", type=int, default=2)
    parser.add_argument("--secondary-cache", default="cache/secondary_lookup_cache.json")
    parser.add_argument("--generic-download-sites", nargs="*", default=[])
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--domain-cookies-file", default="domain_cookies.json")
    parser.add_argument("--interactive", choices=["auto", "true", "false"], default="auto")
    parser.add_argument("--api-concurrency", type=int, default=1)
    parser.add_argument("--api-min-delay-ms", type=int, default=500)
    parser.add_argument("--neurips-proceedings", choices=["true", "false"], default="true")
    return parser


def main() -> None:
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

    docx_path = Path(args.input)
    if not docx_path.exists():
        raise FileNotFoundError(f"Input .docx does not exist: {docx_path}")

    # Extract text from .docx
    print(f"Reading .docx: {docx_path}")
    full_text = extract_text_from_docx(docx_path)
    if not full_text.strip():
        raise ValueError(f"No text extracted from {docx_path}")
    print(f"Extracted {len(full_text)} characters from document.")

    # Parse references (entire document is reference list)
    refs = split_references(full_text)
    print(f"Parsed {len(refs)} references.")

    # Build config
    cookies_path = Path(args.cookies) if args.cookies and Path(args.cookies).exists() else None

    cfg = PipelineConfig(
        input_pdf=docx_path,
        output_dir=Path(args.output),
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

    # Delegate to shared pipeline
    run_pipeline_from_refs(refs, cfg, verify_weights=verify_weights)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise
