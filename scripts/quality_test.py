#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
画風サンプラー（スタイル比較テスト）
=====================================
同じ風刺シーンを、全6画風プリセットで1枚ずつ生成して
quality-test/ フォルダに保存する。費用は1回あたり概ね$0.3〜0.4。

実行: GitHub Actions の「Image quality test」を手動実行するだけ。
結果: quality-test/ 内の style-*.jpg を見比べて、好みの画風を決める。
      全部好きなら何もしなくてよい（毎朝6画風が日替わりローテーションで
      5候補に割り当てられる）。1つに固定したい場合は、リポジトリの
      Settings → Secrets and variables → Actions → Variables に
      IMAGE_STYLE = プリセット名（例: retro-pop）を追加する。

品質(medium/high)を比べたい場合は、Variables に IMAGE_QUALITY を
設定してからこのテストを再実行すると、その品質でサンプルが作られる。
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

# generate_daily.py の画風プリセット定義をそのまま使う（定義の二重管理を避ける）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_daily import STYLE_PRESETS, SAFETY_SUFFIX  # noqa: E402

MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1.5")
SIZE = os.environ.get("IMAGE_SIZE", "1536x1024")
QUALITY = os.environ.get("IMAGE_QUALITY", "medium")
OUT_DIR = Path(__file__).resolve().parent.parent / "quality-test"

# 比較用の固定シーン（画風の違いが分かりやすい、人物+建物+小道具のある構図）
TEST_SCENE = (
    "Lawmakers in a relay race passing a giant 3,000-page bill like a baton, "
    "none of them looking at it, finish line labeled 'MIDNIGHT VOTE', "
    "the Capitol dome in the background."
)


def generate(style_desc: str) -> bytes:
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"model": MODEL, "prompt": TEST_SCENE + " " + style_desc + SAFETY_SUFFIX,
              "size": SIZE, "quality": QUALITY, "n": 1},
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
    # 前回のサンプルを掃除（プリセット名を変えた際に古い画像が残らないように）
    for old in OUT_DIR.glob("style-*.*"):
        old.unlink()
        print(f"[info] removed old sample: {old.name}")
    ext = ".jpg" if HAS_PIL else ".png"
    ok = 0
    for key, desc in STYLE_PRESETS:
        try:
            print(f"[info] generating style sample: {key} (quality={QUALITY})...")
            data = to_jpeg(generate(desc))
            path = OUT_DIR / f"style-{key}{ext}"
            path.write_bytes(data)
            ok += 1
            print(f"[ok] saved {path.name} ({len(data)//1024} KB)")
        except Exception as e:
            print(f"[warn] style {key} failed: {e}", file=sys.stderr)
    print(f"[done] {ok}/{len(STYLE_PRESETS)} style samples in quality-test/")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
