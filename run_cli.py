#!/usr/bin/env python3
"""CLI entry point for PDF conversion (no GUI needed)."""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.core.pipeline import Pipeline, PipelineConfig
from backend.models.schema import PdfJob


def main():
    parser = argparse.ArgumentParser(
        description="Convert PDF files to HTML and/or Markdown"
    )
    parser.add_argument("input", help="PDF file or folder path")
    parser.add_argument("-o", "--output", required=True, help="Output directory")
    parser.add_argument(
        "-f", "--formats",
        nargs="+",
        default=["html", "markdown"],
        choices=["html", "markdown"],
        help="Output formats (default: html markdown)",
    )
    parser.add_argument(
        "-c", "--config",
        default="config/pipeline_config.yaml",
        help="Pipeline config file path",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Max parallel workers",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=None,
        help="Rendering DPI (default: 300)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Process subfolders recursively (batch mode)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load config
    if os.path.exists(args.config):
        config = PipelineConfig.from_yaml(args.config)
    else:
        config = PipelineConfig()

    if args.workers is not None:
        config.max_workers = args.workers
    if args.dpi is not None:
        config.dpi = args.dpi
    config.output_formats = args.formats

    def progress(msg: str, pct: float):
        bar_len = 30
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r[{bar}] {pct*100:5.1f}% {msg}", end="", flush=True)
        if pct >= 1.0:
            print()

    pipeline = Pipeline(config=config, progress_callback=progress)

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if input_path.is_dir():
        print(f"Batch processing folder: {input_path}")
        results = pipeline.process_folder(
            input_path, output_dir, recursive=args.recursive
        )
        print(f"\nCompleted: {len(results)} files processed")
    elif input_path.is_file():
        print(f"Processing: {input_path.name}")
        job = PdfJob(
            input_path=input_path,
            output_dir=output_dir,
            filename=input_path.stem,
            output_formats=args.formats,
        )
        result = pipeline.process(job)
        print(f"\nDone: {result.total_pages} pages → {output_dir}")
    else:
        print(f"Error: {input_path} not found", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
