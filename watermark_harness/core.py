"""Watermark generation helpers for the ``watermark_file`` plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_OFFICE_SUFFIXES = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
SUPPORTED_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | SUPPORTED_PDF_SUFFIXES | SUPPORTED_OFFICE_SUFFIXES
OFFICE_OUTPUT_SUFFIX = ".pdf"
DEFAULT_ANGLE = 45.0
DEFAULT_FONT_SIZE = 13.0
DEFAULT_FONT_FAMILY = "Microsoft YaHei"
DEFAULT_SPACING = 200
DEFAULT_OPACITY = 0.2
MAX_INPUT_BYTES = 100 * 1024 * 1024
STYLE_LOCKED_ERROR = (
    "Watermark style is locked to the configured standard. Omit style "
    "parameters; the tool applies the standard automatically: 45 degree "
    "angle, 13 pt Microsoft YaHei font, 200 pt/px spacing, and 0.2 opacity."
)
LOCKED_STANDARD = {
    "angle": DEFAULT_ANGLE,
    "font_size": DEFAULT_FONT_SIZE,
    "font_family": DEFAULT_FONT_FAMILY,
    "spacing": DEFAULT_SPACING,
    "opacity": DEFAULT_OPACITY,
}
STYLE_ARGUMENT_KEYS = frozenset(LOCKED_STANDARD)
PREFLIGHT_MODEL = "gemma4:e4b-mlx"
PREFLIGHT_PROVIDER = "ollama-local"
PREFLIGHT_BASE_URL = "http://127.0.0.1:11434"
PREFLIGHT_TIMEOUT_SECONDS = 5.0
STYLE_OVERRIDE_TERMS = (
    "angle",
    "font_size",
    "font family",
    "font_family",
    "opacity",
    "spacing",
    "字号",
    "字號",
    "字体",
    "字體",
    "透明度",
    "间距",
    "間距",
    "角度",
    "加深",
    "加粗",
    "放大",
    "调大",
    "調大",
    "更大",
    "更密",
)

SENSITIVE_PARTS = {
    ".1password",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".docker",
    ".gnupg",
    ".kube",
    ".ssh",
    ".vault-token",
    "authorized_keys",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}
SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "auth.json",
    "config.json",
    "token.json",
}


@dataclass(frozen=True)
class FontChoice:
    name: str
    path: Optional[str]
    source: str


def _json_error(message: str, *, code: str, **extra: Any) -> str:
    payload = {"success": False, "error": message, "code": code}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _json_success(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _locked_standard_payload() -> Dict[str, Any]:
    return dict(LOCKED_STANDARD)


def _coerce_float(value: Any, default: float, *, min_value: float, max_value: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return min(max(result, min_value), max_value)


def _coerce_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return min(max(result, min_value), max_value)


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return default


def _float_changed(value: float, default: float) -> bool:
    return abs(value - default) > 1e-9


def _rejected_style_overrides(
    *,
    angle: float,
    font_size: float,
    font_family: str,
    spacing: int,
    opacity: float,
) -> Dict[str, Any]:
    rejected: Dict[str, Any] = {}
    if _float_changed(angle, DEFAULT_ANGLE):
        rejected["angle"] = angle
    if _float_changed(font_size, DEFAULT_FONT_SIZE):
        rejected["font_size"] = font_size
    if font_family != DEFAULT_FONT_FAMILY:
        rejected["font_family"] = font_family
    if spacing != DEFAULT_SPACING:
        rejected["spacing"] = spacing
    if _float_changed(opacity, DEFAULT_OPACITY):
        rejected["opacity"] = opacity
    return rejected


def _watermark_text_audit_fields(text: str) -> Dict[str, Any]:
    return {
        "watermark_text_length": len(text),
        "watermark_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _preflight_payload(
    *,
    text: str,
    status: str,
    style_override_requested: bool = False,
    valid_text: bool = True,
    warning: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": status,
        "provider": PREFLIGHT_PROVIDER,
        "model": PREFLIGHT_MODEL,
        "style_override_requested": bool(style_override_requested),
        "valid_text": bool(valid_text),
        **_watermark_text_audit_fields(text),
    }
    if warning:
        payload["warning"] = warning[:240]
    if reason:
        payload["reason"] = reason[:120]
    return payload


def _looks_like_style_override_request(text: str) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    return any(term.lower().replace(" ", "") in compact for term in STYLE_OVERRIDE_TERMS)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _call_ollama_chat(messages: List[Dict[str, str]], *, timeout: float = PREFLIGHT_TIMEOUT_SECONDS) -> str:
    body = json.dumps(
        {
            "model": PREFLIGHT_MODEL,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": 200,
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{PREFLIGHT_BASE_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open_local_ollama_request(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise RuntimeError("Ollama response did not include message.content")
    return content


def _open_local_ollama_request(request: urllib.request.Request, *, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def _run_watermark_preflight(text: str) -> Dict[str, Any]:
    """Run local-only Gemma preflight for model-visible watermark calls.

    This intentionally talks directly to local Ollama instead of Hermes'
    auxiliary client so there is no provider auto-detection or cloud fallback.
    Any failure is recorded as skipped/failed and the fixed-standard watermark
    render continues.
    """
    heuristic_style_override = _looks_like_style_override_request(text)
    locked_warning = "水印标准已锁定；调用时只支持修改水印内容，不支持修改角度、字号、字体、间距或透明度。"
    messages = [
        {
            "role": "system",
            "content": (
                "You are a local-only preflight classifier for a locked "
                "watermark tool. Return only compact JSON with keys: "
                "valid_text (boolean), style_override_requested (boolean), "
                "warning (string). The renderer style is fixed; never suggest "
                "font, angle, spacing, opacity, or size changes."
            ),
        },
        {
            "role": "user",
            "content": f"watermark_text:\n{text}",
        },
    ]

    try:
        raw = _call_ollama_chat(messages)
    except (OSError, TimeoutError, urllib.error.URLError, RuntimeError, json.JSONDecodeError):
        if heuristic_style_override:
            return _preflight_payload(
                text=text,
                status="warning",
                style_override_requested=True,
                warning=locked_warning,
                reason="ollama_unavailable",
            )
        return _preflight_payload(text=text, status="skipped", reason="ollama_unavailable")

    parsed = _extract_json_object(raw)
    if parsed is None:
        if heuristic_style_override:
            return _preflight_payload(
                text=text,
                status="warning",
                style_override_requested=True,
                warning=locked_warning,
                reason="invalid_model_json",
            )
        return _preflight_payload(text=text, status="failed", reason="invalid_model_json")

    valid_text = _coerce_bool(parsed.get("valid_text", True), True)
    style_override_requested = heuristic_style_override or _coerce_bool(
        parsed.get("style_override_requested", False),
        False,
    )
    warning = _normalize_text(parsed.get("warning"))[:240]
    if style_override_requested:
        warning = locked_warning if not warning else f"{warning} {locked_warning}"[:240]
    status = "warning" if style_override_requested or not valid_text or warning else "passed"
    return _preflight_payload(
        text=text,
        status=status,
        style_override_requested=style_override_requested,
        valid_text=valid_text,
        warning=warning,
    )


def _resolve_existing_file(path_value: Any) -> Tuple[Optional[Path], Optional[str]]:
    if not isinstance(path_value, str) or not path_value.strip():
        return None, "input_path is required"
    try:
        path = Path(path_value).expanduser()
        path = path.resolve(strict=True)
    except FileNotFoundError:
        return None, f"Input file does not exist: {path_value}"
    except OSError as exc:
        return None, f"Could not resolve input path: {exc}"
    if not path.is_file():
        return None, f"Input path is not a file: {path}"
    return path, None


def _contains_sensitive_part(path: Path) -> bool:
    lower_parts = [part.lower() for part in path.parts]
    joined = "/".join(lower_parts)
    if path.name.lower() in SENSITIVE_FILENAMES:
        return True
    return any(part in lower_parts or part in joined for part in SENSITIVE_PARTS)


def _validate_input(path: Path) -> Optional[str]:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return "Unsupported file type. Supported types: PDF, PNG, JPG, JPEG, Word, Excel, PPT."
    if _contains_sensitive_part(path):
        return "Refusing to process files in credential or sensitive configuration paths."
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"Could not stat input file: {exc}"
    if size <= 0:
        return "Input file is empty."
    if size > MAX_INPUT_BYTES:
        return f"Input file is too large ({size} bytes); limit is {MAX_INPUT_BYTES} bytes."
    return None


def _default_output_path(input_path: Path) -> Path:
    if input_path.suffix.lower() in SUPPORTED_OFFICE_SUFFIXES:
        return input_path.with_name(f"{input_path.stem}.watermarked{OFFICE_OUTPUT_SUFFIX}")
    return input_path.with_name(f"{input_path.stem}.watermarked{input_path.suffix}")


def _with_unique_suffix(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
    digest = hashlib.sha256(os.urandom(16)).hexdigest()[:8]
    return path.with_name(f"{path.stem}-{digest}{path.suffix}")


def _resolve_output_path(input_path: Path, output_value: Any) -> Tuple[Optional[Path], List[str], Optional[str]]:
    warnings: List[str] = []
    output_suffix = OFFICE_OUTPUT_SUFFIX if input_path.suffix.lower() in SUPPORTED_OFFICE_SUFFIXES else input_path.suffix
    if isinstance(output_value, str) and output_value.strip():
        raw = Path(output_value).expanduser()
        if raw.suffix == "":
            raw = raw.with_suffix(output_suffix)
        try:
            parent = raw.parent.resolve(strict=True)
        except FileNotFoundError:
            return None, warnings, f"Output directory does not exist: {raw.parent}"
        except OSError as exc:
            return None, warnings, f"Could not resolve output directory: {exc}"
        output = (parent / raw.name).resolve(strict=False)
    else:
        output = _default_output_path(input_path).resolve(strict=False)

    if output == input_path:
        return None, warnings, "Output path must be different from input_path."
    if output.suffix.lower() != output_suffix.lower():
        if input_path.suffix.lower() in SUPPORTED_OFFICE_SUFFIXES:
            return None, warnings, "Office watermark outputs must be PDF files."
        return None, warnings, "Output file extension must match the input file extension."
    if _contains_sensitive_part(output):
        return None, warnings, "Refusing to write output into credential or sensitive configuration paths."

    unique = _with_unique_suffix(output)
    if unique != output:
        warnings.append(f"Output existed; wrote to {unique} instead.")
    return unique, warnings, None


def _font_candidate_paths(font_family: str) -> Iterable[Path]:
    family = font_family.strip() or DEFAULT_FONT_FAMILY
    maybe_path = Path(family).expanduser()
    if maybe_path.suffix.lower() in {".ttf", ".otf", ".ttc"}:
        yield maybe_path

    names = []
    compact = re.sub(r"\s+", "", family).lower()
    if "yahei" in compact or "微软雅黑" in compact or family == DEFAULT_FONT_FAMILY:
        names.extend([
            "Microsoft YaHei.ttf",
            "Microsoft YaHei UI.ttf",
            "msyh.ttc",
            "msyh.ttf",
        ])
    names.extend([
        "PingFang.ttc",
        "PingFang SC.ttf",
        "STHeiti Light.ttc",
        "NotoSansCJK-Regular.ttc",
        "NotoSansCJK-Regular.otf",
        "Noto Sans CJK Regular.ttc",
        "Arial Unicode.ttf",
    ])

    roots = [
        Path.home() / "Library" / "Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path("C:/Windows/Fonts"),
    ]
    for root in roots:
        for name in names:
            yield root / name


def _choose_pil_font(font_family: str, size_px: int, warnings: List[str]) -> Tuple[ImageFont.ImageFont, FontChoice]:
    for path in _font_candidate_paths(font_family):
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size=size_px), FontChoice(path.stem, str(path), "file")
        except OSError:
            continue
    warnings.append("Preferred CJK font was not found for image output; using Pillow default font fallback.")
    return ImageFont.load_default(), FontChoice("Pillow default", None, "fallback")


def _register_pdf_font(font_family: str, warnings: List[str]) -> FontChoice:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _font_candidate_paths(font_family):
        try:
            if not path.is_file() or path.suffix.lower() not in {".ttf", ".otf"}:
                continue
            font_name = "HermesWatermark_" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
            try:
                pdfmetrics.getFont(font_name)
            except KeyError:
                pdfmetrics.registerFont(TTFont(font_name, str(path)))
            return FontChoice(font_name, str(path), "file")
        except Exception:
            continue

    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        except Exception:
            warnings.append("CJK PDF font fallback unavailable; using Helvetica, which may not render CJK text.")
            return FontChoice("Helvetica", None, "fallback")
    warnings.append("Microsoft YaHei TTF/OTF was not found for PDF output; using ReportLab STSong-Light CJK fallback.")
    return FontChoice("STSong-Light", None, "cid-fallback")


def _draw_repeated_pdf_text(canvas: Any, text: str, width: float, height: float, *, angle: float, font_name: str, font_size: float, spacing: int, opacity: float) -> None:
    canvas.saveState()
    try:
        canvas.setFillAlpha(opacity)
    except Exception:
        pass
    canvas.setFont(font_name, font_size)
    canvas.setFillColorRGB(0, 0, 0)
    span = max(width, height)
    x_start = -span
    x_end = width + span
    y_start = -span
    y_end = height + span
    x = x_start
    while x <= x_end:
        y = y_start
        while y <= y_end:
            canvas.saveState()
            canvas.translate(x, y)
            canvas.rotate(angle)
            canvas.drawString(0, 0, text)
            canvas.restoreState()
            y += spacing
        x += spacing
    canvas.restoreState()


def _watermark_pdf(input_path: Path, output_path: Path, *, text: str, angle: float, font_size: float, spacing: int, opacity: float, font_family: str, warnings: List[str]) -> Dict[str, Any]:
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError(
            "PDF watermarking requires pypdf and reportlab. Install the watermark plugin dependencies first."
        ) from exc

    reader = PdfReader(str(input_path))
    if getattr(reader, "is_encrypted", False):
        raise RuntimeError("Encrypted PDFs are not supported without a password.")

    writer = PdfWriter(clone_from=str(input_path))
    font_choice = _register_pdf_font(font_family, warnings)

    page_count = 0
    for page in writer.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        packet = BytesIO()
        overlay_canvas = canvas.Canvas(packet, pagesize=(width, height))
        _draw_repeated_pdf_text(
            overlay_canvas,
            text,
            width,
            height,
            angle=angle,
            font_name=font_choice.name,
            font_size=font_size,
            spacing=spacing,
            opacity=opacity,
        )
        overlay_canvas.save()
        packet.seek(0)
        overlay_reader = PdfReader(packet)
        page.merge_page(overlay_reader.pages[0])
        page_count += 1

    if reader.metadata:
        metadata = {str(k): str(v) for k, v in reader.metadata.items() if v is not None}
        if metadata:
            writer.add_metadata(metadata)

    with output_path.open("wb") as fh:
        writer.write(fh)

    return {
        "format": "pdf",
        "pages_processed": page_count,
        "font_used": font_choice.name,
        "font_source": font_choice.source,
        "font_path": font_choice.path,
    }


def _find_office_converter() -> Optional[str]:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    for candidate in (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/opt/homebrew/bin/soffice",
        "/usr/local/bin/soffice",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _convert_office_to_pdf(input_path: Path, output_dir: Path) -> Path:
    converter = _find_office_converter()
    if not converter:
        raise RuntimeError("Office watermarking requires LibreOffice/soffice for PDF conversion.")

    proc = subprocess.run(
        [
            converter,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(input_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Office to PDF conversion failed: {detail[:240] or 'unknown error'}")

    expected = output_dir / f"{input_path.stem}.pdf"
    if expected.exists():
        return expected

    candidates = sorted(output_dir.glob("*.pdf"))
    if candidates:
        return candidates[0]
    raise RuntimeError("Office to PDF conversion completed but did not produce a PDF.")


def _watermark_office(input_path: Path, output_path: Path, *, text: str, angle: float, font_size: float, spacing: int, opacity: float, font_family: str, warnings: List[str]) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="watermark-office-") as temp_dir:
        converted_pdf = _convert_office_to_pdf(input_path, Path(temp_dir))
        details = _watermark_pdf(
            converted_pdf,
            output_path,
            text=text,
            angle=angle,
            font_size=font_size,
            spacing=spacing,
            opacity=opacity,
            font_family=font_family,
            warnings=warnings,
        )

    return {
        **details,
        "format": "office_pdf",
        "source_format": input_path.suffix.lower().lstrip("."),
        "converted_format": "pdf",
    }


def _text_image(text: str, font: ImageFont.ImageFont, *, opacity: float) -> Image.Image:
    probe = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), text, font=font)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    padding = max(12, height)
    label = Image.new("RGBA", (width + padding * 2, height + padding * 2), (0, 0, 0, 0))
    label_draw = ImageDraw.Draw(label)
    alpha = int(round(255 * opacity))
    label_draw.text((padding - bbox[0], padding - bbox[1]), text, fill=(0, 0, 0, alpha), font=font)
    return label


def _watermark_image(input_path: Path, output_path: Path, *, text: str, angle: float, font_size: float, spacing: int, opacity: float, font_family: str, warnings: List[str]) -> Dict[str, Any]:
    with Image.open(input_path) as original:
        image_format = original.format or input_path.suffix.lstrip(".").upper()
        dpi = original.info.get("dpi") or (96, 96)
        try:
            dpi_x = float(dpi[0]) if isinstance(dpi, tuple) else 96.0
        except (TypeError, ValueError):
            dpi_x = 96.0
        font_px = max(1, int(round(font_size * max(dpi_x, 72.0) / 72.0)))
        font, font_choice = _choose_pil_font(font_family, font_px, warnings)
        base = original.convert("RGBA")
        label = _text_image(text, font, opacity=opacity)
        rotated = label.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        step = max(1, spacing)
        for y in range(-rotated.height, base.height + rotated.height, step):
            for x in range(-rotated.width, base.width + rotated.width, step):
                overlay.alpha_composite(rotated, dest=(x, y))
        watermarked = Image.alpha_composite(base, overlay)

        save_kwargs: Dict[str, Any] = {}
        if original.info.get("dpi"):
            save_kwargs["dpi"] = original.info["dpi"]
        if input_path.suffix.lower() in {".jpg", ".jpeg"}:
            rgb = Image.new("RGB", watermarked.size, (255, 255, 255))
            rgb.paste(watermarked, mask=watermarked.getchannel("A"))
            rgb.save(output_path, format="JPEG", quality=95, **save_kwargs)
        else:
            watermarked.save(output_path, format=image_format if image_format != "JPG" else "JPEG", **save_kwargs)

    return {
        "format": "image",
        "images_processed": 1,
        "font_used": font_choice.name,
        "font_source": font_choice.source,
        "font_path": font_choice.path,
        "image_font_px": font_px,
    }


def watermark_file(
    *,
    input_path: str,
    watermark_text: str,
    output_path: Optional[str] = None,
    angle: Any = DEFAULT_ANGLE,
    font_size: Any = DEFAULT_FONT_SIZE,
    font_family: str = DEFAULT_FONT_FAMILY,
    spacing: Any = DEFAULT_SPACING,
    opacity: Any = DEFAULT_OPACITY,
    allow_style_overrides: bool = False,
    run_local_preflight: bool = False,
) -> str:
    """Apply a watermark and return a JSON string result.

    Agent-visible calls are locked to the standard watermark style. Direct
    harness/tests may opt into custom styling with ``allow_style_overrides``;
    that switch is intentionally not exposed in the model tool schema.
    """
    text = _normalize_text(watermark_text)
    if not text:
        return _json_error("watermark_text is required", code="missing_watermark_text")

    source, path_error = _resolve_existing_file(input_path)
    if path_error or source is None:
        return _json_error(path_error or "Invalid input_path", code="invalid_input_path")

    validation_error = _validate_input(source)
    if validation_error:
        return _json_error(validation_error, code="unsafe_or_unsupported_input", input_path=str(source))

    warnings: List[str] = []
    target, output_warnings, output_error = _resolve_output_path(source, output_path)
    warnings.extend(output_warnings)
    if output_error or target is None:
        return _json_error(output_error or "Invalid output_path", code="invalid_output_path", input_path=str(source))

    angle_value = _coerce_float(angle, DEFAULT_ANGLE, min_value=-360.0, max_value=360.0)
    font_size_value = _coerce_float(font_size, DEFAULT_FONT_SIZE, min_value=1.0, max_value=144.0)
    spacing_value = _coerce_int(spacing, DEFAULT_SPACING, min_value=20, max_value=2000)
    opacity_value = _coerce_float(opacity, DEFAULT_OPACITY, min_value=0.01, max_value=1.0)
    font_family_value = _normalize_text(font_family) or DEFAULT_FONT_FAMILY
    rejected_overrides = _rejected_style_overrides(
        angle=angle_value,
        font_size=font_size_value,
        font_family=font_family_value,
        spacing=spacing_value,
        opacity=opacity_value,
    )
    if rejected_overrides and not allow_style_overrides:
        return _json_error(
            STYLE_LOCKED_ERROR,
            code="watermark_style_locked",
            input_path=str(source),
            locked_standard=_locked_standard_payload(),
            rejected_overrides=rejected_overrides,
        )

    preflight_result = _run_watermark_preflight(text) if run_local_preflight else None

    try:
        if source.suffix.lower() in SUPPORTED_PDF_SUFFIXES:
            details = _watermark_pdf(
                source,
                target,
                text=text,
                angle=angle_value,
                font_size=font_size_value,
                spacing=spacing_value,
                opacity=opacity_value,
                font_family=font_family_value,
                warnings=warnings,
            )
        elif source.suffix.lower() in SUPPORTED_OFFICE_SUFFIXES:
            details = _watermark_office(
                source,
                target,
                text=text,
                angle=angle_value,
                font_size=font_size_value,
                spacing=spacing_value,
                opacity=opacity_value,
                font_family=font_family_value,
                warnings=warnings,
            )
        else:
            details = _watermark_image(
                source,
                target,
                text=text,
                angle=angle_value,
                font_size=font_size_value,
                spacing=spacing_value,
                opacity=opacity_value,
                font_family=font_family_value,
                warnings=warnings,
            )
    except Exception as exc:
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        extra: Dict[str, Any] = {}
        if preflight_result is not None:
            extra["preflight"] = preflight_result
        return _json_error(
            str(exc),
            code="watermark_failed",
            input_path=str(source),
            output_path=str(target),
            **extra,
        )

    payload = {
        "input_path": str(source),
        "output_path": str(target),
        "watermark_text_length": len(text),
        "angle": angle_value,
        "font_size": font_size_value,
        "font_family": font_family_value,
        "spacing": spacing_value,
        "opacity": opacity_value,
        "standard_locked": not allow_style_overrides,
        "locked_standard": _locked_standard_payload(),
        "warnings": warnings,
        **details,
    }
    if preflight_result is not None:
        payload["preflight"] = preflight_result
    return _json_success(**payload)


def watermark_file_tool(args: Dict[str, Any], task_id: str | None = None) -> str:
    """Registry handler wrapper for ``watermark_file``."""
    if not isinstance(args, dict):
        return _json_error("Tool arguments must be an object", code="invalid_arguments")
    provided_style_args = {
        key: args.get(key)
        for key in STYLE_ARGUMENT_KEYS
        if key in args
    }
    if provided_style_args:
        return _json_error(
            STYLE_LOCKED_ERROR,
            code="watermark_style_locked",
            locked_standard=_locked_standard_payload(),
            rejected_overrides=provided_style_args,
        )
    return watermark_file(
        input_path=args.get("input_path", ""),
        watermark_text=args.get("watermark_text", ""),
        output_path=args.get("output_path"),
        run_local_preflight=True,
    )
