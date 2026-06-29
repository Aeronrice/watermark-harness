"""Hermes adapter for the standalone watermark harness."""

from __future__ import annotations

from typing import Any, Dict

from watermark_harness.core import watermark_file_tool


WATERMARK_FILE_SCHEMA: Dict[str, Any] = {
    "name": "watermark_file",
    "description": (
        "Apply a repeated diagonal watermark to a local PDF, PNG, JPG, JPEG, "
        "Word, Excel, or PPT file. Office inputs are converted to watermarked "
        "PDF output. Creates a new output file and never overwrites the input. "
        "The watermark style is locked to the standard: 45 degree angle, "
        "13 pt Microsoft YaHei preferred font, 0.2 opacity, 200 pt/px spacing. "
        "At call time, provide only the file, watermark text, and optional output path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "input_path": {
                "type": "string",
                "description": "Path to the local PDF/PNG/JPG/JPEG/Word/Excel/PPT file to watermark.",
            },
            "watermark_text": {
                "type": "string",
                "description": "Text to repeat across every page or image.",
            },
            "output_path": {
                "type": "string",
                "description": "Optional output path. If omitted, a .watermarked file is created beside the input.",
            },
        },
        "required": ["input_path", "watermark_text"],
        "additionalProperties": False,
    },
}


def register(ctx) -> None:
    """Register the watermark tool with Hermes' plugin tool registry."""
    ctx.register_tool(
        name="watermark_file",
        toolset="watermark",
        schema=WATERMARK_FILE_SCHEMA,
        handler=lambda args, **kwargs: watermark_file_tool(args, task_id=kwargs.get("task_id")),
        description="Apply repeated diagonal watermarks to PDFs, images, Word, Excel, and PPT files",
        emoji="🔖",
    )
