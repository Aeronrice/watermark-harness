from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from hermes_plugin.watermark_file import WATERMARK_FILE_SCHEMA
import watermark_harness.core as watermark_module
from watermark_harness.cli import main as cli_main
from watermark_harness.core import watermark_file, watermark_file_tool


def _json_result(payload: str) -> dict:
    return json.loads(payload)


def test_image_watermark_creates_new_file_and_preserves_original(tmp_path: Path) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (420, 280), "white").save(input_path)
    original_bytes = input_path.read_bytes()

    result = _json_result(
        watermark_file(
            input_path=str(input_path),
            watermark_text="内部文件",
            spacing=120,
            opacity=0.35,
            allow_style_overrides=True,
        )
    )

    assert result["success"] is True
    assert result["format"] == "image"
    assert result["images_processed"] == 1
    output_path = Path(result["output_path"])
    assert output_path.exists()
    assert output_path != input_path
    assert input_path.read_bytes() == original_bytes

    with Image.open(input_path) as original, Image.open(output_path) as watermarked:
        assert watermarked.size == original.size
        assert ImageChops.difference(original.convert("RGB"), watermarked.convert("RGB")).getbbox() is not None


def test_output_path_must_not_overwrite_input(tmp_path: Path) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (80, 80), "white").save(input_path)

    result = _json_result(
        watermark_file_tool(
            {
                "input_path": str(input_path),
                "watermark_text": "secret",
                "output_path": str(input_path),
            }
        )
    )

    assert result["success"] is False
    assert result["code"] == "invalid_output_path"
    assert "different" in result["error"]


def test_sensitive_credential_paths_are_rejected_before_processing(tmp_path: Path) -> None:
    secret_dir = tmp_path / ".ssh"
    secret_dir.mkdir()
    secret_pdf = secret_dir / "secret.pdf"
    secret_pdf.write_bytes(b"not really a pdf but should be rejected before parsing")

    result = _json_result(watermark_file(input_path=str(secret_pdf), watermark_text="secret"))

    assert result["success"] is False
    assert result["code"] == "unsafe_or_unsupported_input"
    assert "sensitive" in result["error"]


def test_existing_output_path_gets_unique_suffix(tmp_path: Path) -> None:
    input_path = tmp_path / "source.png"
    preferred_output = tmp_path / "source.watermarked.png"
    Image.new("RGB", (100, 100), "white").save(input_path)
    preferred_output.write_bytes(b"already here")

    result = _json_result(
        watermark_file(
            input_path=str(input_path),
            watermark_text="copy",
            output_path=str(preferred_output),
        )
    )

    assert result["success"] is True
    assert Path(result["output_path"]) != preferred_output
    assert Path(result["output_path"]).exists()
    assert "Output existed" in "\n".join(result["warnings"])
    assert preferred_output.read_bytes() == b"already here"


def test_pdf_watermark_preserves_page_count(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    input_path = tmp_path / "source.pdf"
    c = canvas.Canvas(str(input_path), pagesize=(240, 180))
    c.drawString(20, 100, "hello")
    c.showPage()
    c.drawString(20, 100, "world")
    c.save()
    original_bytes = input_path.read_bytes()

    result = _json_result(
        watermark_file(
            input_path=str(input_path),
            watermark_text="机密",
            spacing=90,
            opacity=0.3,
            allow_style_overrides=True,
        )
    )

    assert result["success"] is True
    assert result["format"] == "pdf"
    assert result["pages_processed"] == 2
    output_path = Path(result["output_path"])
    assert output_path.exists()
    assert input_path.read_bytes() == original_bytes
    assert len(pypdf.PdfReader(str(output_path)).pages) == 2
    assert output_path.stat().st_size > input_path.stat().st_size


def test_tool_rejects_non_standard_style_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (160, 120), "white").save(input_path)
    monkeypatch.setattr(
        watermark_module,
        "_call_ollama_chat",
        lambda *args, **kwargs: pytest.fail("preflight must not run for rejected style args"),
    )

    result = _json_result(
        watermark_file_tool(
            {
                "input_path": str(input_path),
                "watermark_text": "内部分享禁止外传CVC",
                "angle": 45,
                "font_size": 72,
                "spacing": 80,
                "opacity": 0.7,
            }
        )
    )

    assert result["success"] is False
    assert result["code"] == "watermark_style_locked"
    assert result["locked_standard"] == {
        "angle": 45.0,
        "font_size": 13.0,
        "font_family": "Microsoft YaHei",
        "spacing": 200,
        "opacity": 0.2,
    }
    assert result["rejected_overrides"] == {
        "angle": 45,
        "font_size": 72,
        "spacing": 80,
        "opacity": 0.7,
    }
    assert not (tmp_path / "source.watermarked.png").exists()


def test_tool_uses_locked_standard_without_exposing_style_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (160, 120), "white").save(input_path)
    monkeypatch.setattr(
        watermark_module,
        "_call_ollama_chat",
        lambda *args, **kwargs: json.dumps(
            {"valid_text": True, "style_override_requested": False, "warning": ""}
        ),
    )

    result = _json_result(
        watermark_file_tool(
            {
                "input_path": str(input_path),
                "watermark_text": "内部分享禁止外传CVC",
            }
        )
    )

    assert result["success"] is True
    assert result["standard_locked"] is True
    assert result["angle"] == 45.0
    assert result["font_size"] == 13.0
    assert result["font_family"] == "Microsoft YaHei"
    assert result["spacing"] == 200
    assert result["opacity"] == 0.2
    assert result["preflight"]["status"] == "passed"
    assert result["preflight"]["provider"] == "ollama-local"
    assert result["preflight"]["model"] == "gemma4:e4b-mlx"
    assert result["preflight"]["style_override_requested"] is False
    assert result["preflight"]["watermark_text_length"] == len("内部分享禁止外传CVC")
    assert "watermark_text_sha256" in result["preflight"]
    assert Path(result["output_path"]).exists()


def test_tool_preflight_warns_but_keeps_locked_standard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (160, 120), "white").save(input_path)
    monkeypatch.setattr(
        watermark_module,
        "_call_ollama_chat",
        lambda *args, **kwargs: json.dumps(
            {
                "valid_text": True,
                "style_override_requested": True,
                "warning": "用户似乎要求调整样式，但水印标准已锁定。",
            }
        ),
    )

    result = _json_result(
        watermark_file_tool(
            {
                "input_path": str(input_path),
                "watermark_text": "内部文件，请把水印放大",
            }
        )
    )

    assert result["success"] is True
    assert result["font_size"] == 13.0
    assert result["spacing"] == 200
    assert result["opacity"] == 0.2
    assert result["preflight"]["status"] == "warning"
    assert result["preflight"]["style_override_requested"] is True
    assert "锁定" in result["preflight"]["warning"]


def test_tool_preflight_skips_when_local_ollama_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (160, 120), "white").save(input_path)

    def _raise_unavailable(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(watermark_module, "_call_ollama_chat", _raise_unavailable)

    result = _json_result(
        watermark_file_tool(
            {
                "input_path": str(input_path),
                "watermark_text": "内部分享禁止外传CVC",
            }
        )
    )

    assert result["success"] is True
    assert result["preflight"]["status"] == "skipped"
    assert result["preflight"]["provider"] == "ollama-local"
    assert result["preflight"]["model"] == "gemma4:e4b-mlx"
    assert result["preflight"]["reason"] == "ollama_unavailable"
    assert Path(result["output_path"]).exists()


def test_tool_preflight_skips_when_ollama_response_is_bad_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (160, 120), "white").save(input_path)

    def _raise_bad_json(*args, **kwargs):
        raise json.JSONDecodeError("bad response", "", 0)

    monkeypatch.setattr(watermark_module, "_call_ollama_chat", _raise_bad_json)

    result = _json_result(
        watermark_file_tool(
            {
                "input_path": str(input_path),
                "watermark_text": "内部分享禁止外传CVC",
            }
        )
    )

    assert result["success"] is True
    assert result["preflight"]["status"] == "skipped"
    assert result["preflight"]["reason"] == "ollama_unavailable"
    assert Path(result["output_path"]).exists()


def test_local_preflight_request_forces_ollama_json_without_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {"message": {"content": "{\"valid_text\":true,\"style_override_requested\":false,\"warning\":\"\"}"}}
            ).encode("utf-8")

    def _fake_open_local_ollama_request(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr(watermark_module, "_open_local_ollama_request", _fake_open_local_ollama_request)

    content = watermark_module._call_ollama_chat([{"role": "user", "content": "x"}], timeout=3.0)

    assert content == "{\"valid_text\":true,\"style_override_requested\":false,\"warning\":\"\"}"
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["timeout"] == 3.0
    body = captured["body"]
    assert body["model"] == "gemma4:e4b-mlx"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == "json"
    assert body["options"]["temperature"] == 0
    assert body["options"]["num_predict"] == 200


def test_local_preflight_coerces_model_string_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watermark_module,
        "_call_ollama_chat",
        lambda *args, **kwargs: json.dumps(
            {"valid_text": "false", "style_override_requested": "true", "warning": ""}
        ),
    )

    result = watermark_module._run_watermark_preflight("内部文件")

    assert result["status"] == "warning"
    assert result["valid_text"] is False
    assert result["style_override_requested"] is True
    assert "锁定" in result["warning"]


def test_tool_schema_does_not_expose_style_configuration() -> None:
    properties = WATERMARK_FILE_SCHEMA["parameters"]["properties"]

    assert set(properties) == {"input_path", "watermark_text", "output_path"}
    assert WATERMARK_FILE_SCHEMA["parameters"]["additionalProperties"] is False


def test_cli_creates_output_without_preflight(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / "source.png"
    Image.new("RGB", (160, 120), "white").save(input_path)

    exit_code = cli_main([str(input_path), "内部文件", "--pretty"])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is True
    assert "preflight" not in result
    assert Path(result["output_path"]).exists()
