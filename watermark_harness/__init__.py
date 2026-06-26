"""Standalone watermark harness with local-only Ollama preflight."""

from .core import LOCKED_STANDARD, watermark_file, watermark_file_tool

__all__ = ["LOCKED_STANDARD", "watermark_file", "watermark_file_tool"]
