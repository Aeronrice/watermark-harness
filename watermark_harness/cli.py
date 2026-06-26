"""Command-line entry point for the watermark harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .core import watermark_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watermark-harness",
        description="Apply the locked-standard watermark to a PDF/PNG/JPG/JPEG file.",
    )
    parser.add_argument("input_path", help="Local PDF/PNG/JPG/JPEG file to watermark.")
    parser.add_argument("watermark_text", help="Text to repeat across the file.")
    parser.add_argument("-o", "--output-path", help="Optional output path. Defaults to a .watermarked sibling.")
    parser.add_argument(
        "--local-preflight",
        action="store_true",
        help="Run local-only Ollama gemma4:e4b-mlx preflight before rendering.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON result.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result_text = watermark_file(
        input_path=args.input_path,
        watermark_text=args.watermark_text,
        output_path=args.output_path,
        run_local_preflight=args.local_preflight,
    )
    if args.pretty:
        print(json.dumps(json.loads(result_text), ensure_ascii=False, indent=2))
    else:
        print(result_text)
    result = json.loads(result_text)
    output_path = result.get("output_path")
    if result.get("success") and output_path and Path(output_path).exists():
        return 0
    return 0 if result.get("success") else 1
