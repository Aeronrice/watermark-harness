# Watermark Harness

Standalone harness for applying a locked-standard, repeated diagonal watermark
to local PDF, image, Word, Excel, and PPT files.

This repository packages the watermark functionality extracted from the local Hermes Agent `watermark_file` plugin, plus a Hermes adapter that can be copied back into a Hermes plugin directory.

## Contract

Agent/model-facing calls may provide only:

- `input_path`
- `watermark_text`
- optional `output_path`

The visual style is intentionally locked in code:

- angle: `45` degrees
- font size: `19.5 pt`
- preferred font: `Microsoft YaHei`
- spacing: `200`
- opacity: `0.2`

For high-resolution scanned images, the image renderer keeps the public locked
standard at `19.5 pt` but scales the actual raster font size and spacing from
the image short side so watermarks remain visible on A4/letter-sized scans.

Style parameters are not exposed in the tool schema. If a caller tries to pass style arguments to `watermark_file_tool`, the call is rejected with `watermark_style_locked`.

Supported inputs are fixed in code:

- PDF
- PNG/JPG/JPEG
- Word: `.doc`, `.docx`
- Excel: `.xls`, `.xlsx`
- PPT: `.ppt`, `.pptx`

Office inputs are converted locally to PDF first, then watermarked with the
same PDF renderer. Office outputs are always `.pdf`.

For `.xlsx` inputs, the harness may normalize print scaling on a temporary copy
before LibreOffice conversion when worksheets do not already define explicit
scaling. The source workbook is never mutated.

## Local preflight

`watermark_file_tool` runs a local-only Ollama preflight with `gemma4:e4b-mlx` before rendering.

Implementation invariants:

- direct call to `http://127.0.0.1:11434/api/chat`
- `stream=false`
- `think=false`
- `format=json`
- `temperature=0`
- `num_predict=200`
- proxy-disabled `urllib` opener for localhost
- no Hermes `auxiliary_client`
- no provider auto-detection
- no cloud fallback

If Ollama is unavailable or returns bad JSON, rendering still proceeds and records preflight status as skipped/failed. If the watermark text asks for style changes, the preflight returns a warning while the renderer keeps the locked standard.

## PDF compatibility

PDF watermarking mutates writer-owned pages via `PdfWriter(clone_from=...)` before `merge_page(...)`. Keep this pattern when syncing with Hermes Agent; mutating `PdfReader` pages directly triggers a pypdf 7 removal warning around `PageObject.replace_contents()`.

## Install

```bash
python3 -m pip install -e '.[dev]'
```

For PDF support in runtime environments, install the PDF extra:

```bash
python3 -m pip install -e '.[pdf]'
```

For Office input support, install LibreOffice so `soffice` or `libreoffice` is
available on `PATH`.

## CLI

```bash
python3 -m watermark_harness input.png '内部分享禁止外传CVC' --pretty
python3 -m watermark_harness input.pdf '机密' --output-path output.pdf --local-preflight --pretty
python3 -m watermark_harness input.docx '内部分享禁止外传CVC' --pretty
```

## Python API

```python
from watermark_harness import watermark_file_tool

result_json = watermark_file_tool({
    "input_path": "/path/to/input.png",
    "watermark_text": "内部分享禁止外传CVC",
})
```

## Hermes adapter

The `hermes_plugin/watermark_file` directory contains a Hermes-compatible adapter:

- `plugin.yaml`
- `__init__.py` with `WATERMARK_FILE_SCHEMA` and `register(ctx)`
- `watermark.py` compatibility re-export

The adapter imports the standalone package, so install this package in the Hermes runtime or vendor `watermark_harness` alongside the plugin.

## Tests

```bash
python3 -m py_compile watermark_harness/core.py watermark_harness/cli.py hermes_plugin/watermark_file/__init__.py tests/test_watermark_harness.py
python3 -m pytest -q
```
