#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
画像品質の比較テスト（medium vs high）
=====================================
同じ風刺画プロンプトを medium と high の両方で1枚ずつ生成し、
quality-test/ フォルダに保存する。費用はテスト1回で概ね$0.2前後。

実行: GitHub Actions の「Image quality test」を手動実行するだけ。
結果: リポジトリの quality-test/medium.jpg と quality-test/high.jpg を
      並べて見比べて、毎朝の運用品質を決める。
"""

import base64
import io
import os
import sys
from pathlib import Path

import requests

try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1.5")
SIZE = os.environ.get("IMAGE_SIZE", "1536x1024")
OUT_DIR = Path(__file__).resolve().parent.parent / "quality-test"

# 比較用の固定プロンプト（アプリが生成する典型的なimage promptと同系統）
TEST_PROMPT = (
    "Editorial cartoon: lawmakers in a relay race passing a giant 3,000-page "
    "bill like a baton, none of them looking at it, finish line labeled "
    "'MIDNIGHT VOTE'. Editorial cartoon style, cross-hatching ink illustration, "
    "muted colors, newspaper satire aesthetic. Do not depict any real, "
    "identifiable person. No watermarks."
)


def generate(quality: str) -> bytes:
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"model": MODEL, "prompt": TEST_PROMPT, "size": SIZE,
              "quality": quality, "n": 1},
        timeout=300,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return base64.b64decode(resp.json()["data"][0]["b64_json"])


def to_jpeg(png_bytes: bytes) -> bytes:
    if not HAS_PIL:
        return png_bytes
    img = PILImage.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    return buf.getvalue()


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("[error] OPENAI_API_KEY is not set", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(exist_ok=True)
    ext = ".jpg" if HAS_PIL else ".png"
    for quality in ("medium", "high"):
        print(f"[info] generating {quality} sample (model={MODEL})...")
        data = to_jpeg(generate(quality))
        path = OUT_DIR / f"{quality}{ext}"
        path.write_bytes(data)
        print(f"[ok] saved {path.name} ({len(data)//1024} KB)")
    print("[done] compare the two files in the quality-test/ folder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
