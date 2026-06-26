from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from watermark_harness.core import watermark_file_tool


def main() -> int:
    root = Path("/private/tmp/watermark-harness-smoke")
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.png"
    Image.new("RGB", (160, 120), "white").save(source)
    result = json.loads(
        watermark_file_tool(
            {
                "input_path": str(source),
                "watermark_text": "内部分享禁止外传CVC",
            }
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
