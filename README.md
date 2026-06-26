# Watermark Harness

Standalone harness for applying a locked-standard, repeated diagonal watermark to local PDF and image files.

This repository packages the watermark functionality extracted from the local Hermes Agent `watermark_file` plugin, plus a Hermes adapter that can be copied back into a Hermes plugin directory.

## Contract

Agent/model-facing calls may provide only:

- `input_path`
- `watermark_text`
- optional `output_path`

The visual style is intentionally locked in code:

- angle: `45` degrees
- font size: `13 pt`
- preferred font: `Microsoft YaHei`
- spacing: `200`
- opacity: `0.2`

Style parameters are not exposed in the tool schema. If a caller tries to pass style arguments to `watermark_file_tool`, the call is rejected with `watermark_style_locked`.

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

## Install

```bash
python3 -m pip install -e '.[dev]'
```

For PDF support in runtime environments, install the PDF extra:

```bash
python3 -m pip install -e '.[pdf]'
```

## CLI

```bash
python3 -m watermark_harness input.png '内部分享禁止外传CVC' --pretty
python3 -m watermark_harness input.pdf '机密' --output-path output.pdf --local-preflight --pretty
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
