#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
America Satire Desk — daily.json / daily.js 自動生成スクリプト
================================================================
毎朝、RSSからアメリカ関連ニュースを取得し、Claude APIで風刺向きの
5本を選定・初稿生成して、以下の3ファイルを出力する。

  daily.json                … アプリ(index.html)が読み込む当日データ
  daily.js                  … file://直開き用の同内容ラッパー
  archive/YYYY-MM-DD.json   … 日付付きの控え（履歴）

安全設計:
  - 5本揃わない / JSONが壊れている / 検証に失敗 → 何も書き込まずに
    終了コード1で失敗する（既存の daily.json は壊れない）
  - 書き込みは「一時ファイル → 置き換え」のアトミック方式
  - Substackへの投稿機能は存在しない（投稿は必ず手動）

環境変数:
  ANTHROPIC_API_KEY  … 必須。GitHub Secrets から渡す
  SATIRE_MODEL       … 任意。既定は claude-sonnet-4-6
"""

import base64
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from anthropic import Anthropic

try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ----------------------------------------------------------------
# 設定（ここを編集すればカスタマイズできる）
# ----------------------------------------------------------------

# --- 画像生成（OpenAI）の設定 ---
# OPENAI_API_KEY が未設定なら画像生成は自動スキップされ、
# アプリはこれまで通りプレースホルダーを表示する（安全なフォールバック）。
IMAGE_ENABLED = bool(os.environ.get("OPENAI_API_KEY"))
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1.5")
IMAGE_QUALITY = os.environ.get("IMAGE_QUALITY", "medium")  # low / medium / high
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "1536x1024")     # 横長（カードの形に合う）
IMAGE_KEEP_DAYS = 60   # これより古い日付の画像フォルダは自動削除（リポジトリ肥大防止）
# --- 画風プリセット（毎朝5候補に別々のスタイルを割り当て、日替わりでローテーション） ---
# ※ 実在アーティスト名はプロンプトに入れない方針（権利・倫理面 + API側で拒否されうるため）。
#   各作風のエッセンスを言語化した記述を使う。
# IMAGE_STYLE にプリセット名（例: "retro-pop"）を設定すると、その画風だけで固定できる。
STYLE_PRESETS = [
    ("classic-cartoon",
     "Classic editorial cartoon style: cross-hatching ink illustration, muted colors, "
     "vintage newspaper satire aesthetic."),
    ("retro-pop",
     "Retro pop advertising illustration: clean confident linework, flat vivid colors, "
     "stylish figures, 1980s Japanese city-pop magazine aesthetic, fashionable and airy."),
    ("watercolor-sketch",
     "Warm mid-century American storytelling illustration in light watercolor and "
     "pencil sketch: airy transparent washes, soft gentle brush touch, delicate visible "
     "pencil underdrawing, warm nostalgic palette, tender humane character expressions. "
     "Light and breathable like a study on paper — never heavy opaque oil paint."),
    ("anime-digital",
     "Polished digital illustration: anime-influenced character design, soft cinematic "
     "lighting, painterly color gradients, glossy modern finish."),
    ("editorial-modern",
     "Modern editorial op-ed illustration: conceptual and minimalist, sophisticated muted "
     "palette, generous negative space, clever visual metaphor, prestigious newspaper "
     "opinion-page style."),
    ("soft-3d",
     "Soft 3D rendered illustration: rounded stylized characters, gentle studio lighting, "
     "matte textures, contemporary tech-brand aesthetic."),
]
IMAGE_STYLE = os.environ.get("IMAGE_STYLE", "")  # 空=ローテーション / プリセット名=固定
SAFETY_SUFFIX = (
    " Do not depict any real, identifiable person. No watermarks. No text captions "
    "unless the scene requires a small sign or label."
)

def style_for(candidate_index: int, today: str) -> tuple[str, str]:
    """候補ごとの画風を返す。日付でローテーションが1つずつずれる。"""
    if IMAGE_STYLE:
        for key, desc in STYLE_PRESETS:
            if key == IMAGE_STYLE:
                return key, desc
        print(f"[warn] unknown IMAGE_STYLE '{IMAGE_STYLE}' — falling back to rotation")
    day_offset = sum(int(x) for x in today.replace("-", ""))  # 日付から決まる安定オフセット
    key, desc = STYLE_PRESETS[(day_offset + candidate_index) % len(STYLE_PRESETS)]
    return key, desc



# ニュースソース（無料RSS）。政治に偏らないよう分野を混ぜている。
# 追加・削除はこのリストを編集するだけでよい。
FEEDS = [
    # 総合・政治
    ("NPR News",        "https://feeds.npr.org/1001/rss.xml"),
    ("NPR Politics",    "https://feeds.npr.org/1014/rss.xml"),
    ("Politico",        "https://rss.politico.com/politics-news.xml"),
    ("The Guardian US", "https://www.theguardian.com/us-news/rss"),
    ("CBS News US",     "https://www.cbsnews.com/latest/rss/us"),
    # ビジネス・労働
    ("NPR Business",    "https://feeds.npr.org/1006/rss.xml"),
    ("CNBC Top News",   "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    # テック・SNS・都市生活・文化
    ("The Verge",       "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica",    "https://feeds.arstechnica.com/arstechnica/index"),
    ("NPR Culture",     "https://feeds.npr.org/1008/rss.xml"),
]

HOURS_BACK = 36           # 直近この時間内の記事だけを対象にする
MAX_ITEMS_TO_MODEL = 40   # Claudeに渡す記事の最大数（コスト管理）
NUM_PICKS = 5             # 必ず5本
MAX_ATTEMPTS = 2          # 生成が崩れた場合のリトライ回数
MODEL = os.environ.get("SATIRE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 8000

JST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent  # リポジトリのルート
OUT_JSON = ROOT / "daily.json"
OUT_JS = ROOT / "daily.js"
ARCHIVE_DIR = ROOT / "archive"
IMAGES_DIR = ROOT / "images"

# ----------------------------------------------------------------
# 1. RSS取得
# ----------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")

def strip_html(text: str) -> str:
    return TAG_RE.sub("", text or "").replace("&nbsp;", " ").strip()


def fetch_news() -> list[dict]:
    """全フィードから直近の記事を集め、重複を除いて新しい順に返す。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    items: list[dict] = []
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:  # フィード1本の失敗で全体を止めない
            print(f"[warn] feed failed: {source}: {e}")
            continue
        for e in feed.entries[:30]:
            title = strip_html(getattr(e, "title", ""))
            link = getattr(e, "link", "")
            if not title or not link:
                continue
            # 公開日時（無ければ現在時刻扱い）
            published = None
            for key in ("published_parsed", "updated_parsed"):
                t = getattr(e, key, None)
                if t:
                    published = datetime(*t[:6], tzinfo=timezone.utc)
                    break
            if published and published < cutoff:
                continue
            summary = strip_html(getattr(e, "summary", ""))[:300]
            items.append({
                "title": title,
                "url": link,
                "source": source,
                "date": (published or datetime.now(timezone.utc)).astimezone(JST).strftime("%Y-%m-%d"),
                "snippet": summary,
                "_ts": (published or datetime.now(timezone.utc)).timestamp(),
            })
        print(f"[info] {source}: fetched")

    # タイトルの正規化で重複除去（同じニュースが複数フィードに載るため）
    seen: set[str] = set()
    unique: list[dict] = []
    for it in sorted(items, key=lambda x: x["_ts"], reverse=True):
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    print(f"[info] collected {len(unique)} unique items (last {HOURS_BACK}h)")
    return unique[:MAX_ITEMS_TO_MODEL]


# ----------------------------------------------------------------
# 2. Claude APIで5本選定＋初稿生成
# ----------------------------------------------------------------

SYSTEM_PROMPT = """あなたは「America Satire Desk」の編集AIです。アメリカのニュースから、風刺コンテンツの初稿を作ります。最終確認と投稿は必ず人間の編集者が行います。

【選定方針】
- アメリカ国内のニュース / 時事ネタのみを選ぶ。米国と無関係な記事は選ばない。
- 政治だけに偏らせない。政治・企業・テック・裁判・教育・労働・文化・SNS・都市生活などを混ぜ、5本で分野のバランスを取る。
- 「風刺として強いもの」を優先する: 建前と実態のギャップ、制度が自分の目的と矛盾する構図、数字や場所選びが皮肉になっている事例。
- 悲劇そのもの（死者・災害・暴力事件・個人の不幸）は選ばない。笑いの対象にできない。
- 真偽が不確かな情報、匿名SNS投稿由来の話、陰謀論的な話題、扇動的な記事は選ばない。リストにあっても無視する。

【風刺のルール（厳守）】
- 風刺の対象は、制度・企業・組織・社会構造・文化・矛盾。実在の個人への人格攻撃はしない。
- 政治家等の公人に触れる場合も、個人の外見・家族・人格ではなく、役職としての行動や制度の構図を対象にする。
- 名誉毀損リスクのある断定（違法行為の断定、動機の決めつけ）はしない。
- NEWS/EVENT系フィールド（summary, newsEn）は事実のみ。皮肉・論評を混ぜない。
- COMMENTARY系フィールド（commentary, ironyEn）は論評・風刺として書き、事実の捏造をしない。

【出力形式（厳守）】
- 有効なJSONのみを出力する。前置き・後書き・コードフェンス・コメントは一切付けない。
- 記事は必ず与えられたリストの index で参照する。URLや出典を自分で作らない。
- picks は必ずちょうど5件。indexは重複させない。

JSONスキーマ:
{
  "picks": [
    {
      "index": <リスト内の記事番号(整数)>,
      "headline": "<英語見出し。元見出しを整えてよい>",
      "summary": "<日本語の事実要約。1〜2文。皮肉を混ぜない>",
      "newsEn": "<英語の事実説明。2〜4文。皮肉を混ぜない>",
      "commentary": [
        "<b>矛盾:</b> <日本語1〜2文>",
        "<b>滑稽さ:</b> <日本語1〜2文>",
        "<b>日本・海外から見ると:</b> <日本語1〜2文>"
      ],
      "ironyEn": [
        {"contradiction": "<英語1〜2文>", "absurdity": "<英語1〜2文>", "outside": "<英語1〜2文>"},
        {"contradiction": "<別表現>", "absurdity": "<別表現>", "outside": "<別表現>"}
      ],
      "imagePrompts": [
        "<英語。場面描写のみの画像プロンプト。構図・登場要素・皮肉の視覚化に集中し、画風（cartoon等のスタイル語）は書かない>",
        "<別アングル>",
        "<別アングル>"
      ],
      "captions": [
        "<英語パンチライン。スタンダップコメディ調、1〜2文、ミスディレクションあり>",
        "<別角度のジョーク>", "<別角度>", "<別角度>", "<別角度>"
      ],
      "captionsJa": [
        "<上記5本の自然な日本語訳。直訳ではなく皮肉のニュアンスを活かす>",
        "...", "...", "...", "..."
      ]
    }
  ]
}
captions と captionsJa は同じ順序で対応させること。"""


def build_user_prompt(items: list[dict]) -> str:
    lines = ["以下は本日のニュースリストです。この中から方針に沿って5本選び、指定スキーマのJSONだけを出力してください。\n"]
    for i, it in enumerate(items):
        lines.append(f"[{i}] ({it['source']}, {it['date']}) {it['title']}")
        if it["snippet"]:
            lines.append(f"    {it['snippet']}")
    return "\n".join(lines)


def extract_json(text: str) -> dict:
    """コードフェンスや前後の文が混ざっていても、JSON本体を取り出してパースする。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start:end + 1])


def call_claude(client: Anthropic, items: list[dict]) -> dict:
    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(items)}],
    )
    text = "".join(block.text for block in message.content if block.type == "text")
    usage = getattr(message, "usage", None)
    if usage:
        print(f"[info] tokens: in={usage.input_tokens} out={usage.output_tokens}")
    return extract_json(text)


# ----------------------------------------------------------------
# 3. 検証（壊れたデータは絶対に書き込まない）
# ----------------------------------------------------------------

def _req_str(v, name: str, min_len: int = 1) -> str:
    if not isinstance(v, str) or len(v.strip()) < min_len:
        raise ValueError(f"invalid field: {name}")
    return v.strip()


def validate_picks(data: dict, items: list[dict]) -> list[dict]:
    """モデル出力を検証し、RSS由来の確実な出典情報と合体させて候補を組み立てる。"""
    picks = data.get("picks")
    if not isinstance(picks, list) or len(picks) != NUM_PICKS:
        raise ValueError(f"picks must be exactly {NUM_PICKS} items")

    used_indices: set[int] = set()
    candidates: list[dict] = []
    for i, p in enumerate(picks):
        idx = p.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(items)) or idx in used_indices:
            raise ValueError(f"pick {i}: invalid or duplicate index: {idx!r}")
        used_indices.add(idx)
        src = items[idx]  # URL・出典・日付はRSSの実データを使う（捏造防止）

        commentary = p.get("commentary")
        if not isinstance(commentary, list) or len(commentary) != 3:
            raise ValueError(f"pick {i}: commentary must have 3 items")
        commentary = [_req_str(c, f"pick {i}: commentary", 8) for c in commentary]

        irony = p.get("ironyEn")
        if not isinstance(irony, list) or len(irony) < 1:
            raise ValueError(f"pick {i}: ironyEn missing")
        irony_norm = []
        for v in irony[:2]:
            irony_norm.append({
                "contradiction": _req_str(v.get("contradiction"), f"pick {i}: contradiction", 15),
                "absurdity":     _req_str(v.get("absurdity"),     f"pick {i}: absurdity", 15),
                "outside":       _req_str(v.get("outside"),       f"pick {i}: outside", 15),
            })

        prompts = p.get("imagePrompts")
        if not isinstance(prompts, list) or len(prompts) < 2:
            raise ValueError(f"pick {i}: imagePrompts needs >=2")
        prompts = [_req_str(x, f"pick {i}: imagePrompt", 20) for x in prompts[:3]]

        captions = p.get("captions")
        captions_ja = p.get("captionsJa")
        if (not isinstance(captions, list) or not isinstance(captions_ja, list)
                or len(captions) < 3 or len(captions) != len(captions_ja)):
            raise ValueError(f"pick {i}: captions/captionsJa mismatch")
        captions = [_req_str(x, f"pick {i}: caption", 8) for x in captions[:5]]
        captions_ja = [_req_str(x, f"pick {i}: captionJa", 4) for x in captions_ja[:5]]

        candidates.append({
            "id": f"d{i + 1}",
            "news": {
                "headline": _req_str(p.get("headline"), f"pick {i}: headline", 8),
                "source": src["source"],
                "date": src["date"],
                "url": src["url"],
                "summary": _req_str(p.get("summary"), f"pick {i}: summary", 10),
            },
            "commentary": commentary,
            "imagePrompts": prompts,
            "captions": captions,
            "captionsJa": captions_ja,
            "newsEn": _req_str(p.get("newsEn"), f"pick {i}: newsEn", 40),
            "ironyEn": irony_norm,
            "imageSeed": i + 1,
        })
    return candidates


# ----------------------------------------------------------------
# 3.5. 風刺画の実画像生成（OpenAI gpt-image）
#   - 各候補の imagePrompts[0] から1枚生成し、images/日付/ に保存
#   - 失敗しても daily.json の生成は止めない（画像なし＝プレースホルダー表示）
# ----------------------------------------------------------------

def openai_generate_image(prompt: str, style_desc: str = "") -> bytes:
    """OpenAIの画像APIで1枚生成し、PNGバイト列を返す。"""
    full_prompt = prompt + " " + style_desc + SAFETY_SUFFIX
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": IMAGE_MODEL,
            "prompt": full_prompt,
            "size": IMAGE_SIZE,
            "quality": IMAGE_QUALITY,
            "n": 1,
        },
        timeout=300,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"image API HTTP {resp.status_code}: {resp.text[:200]}")
    return base64.b64decode(resp.json()["data"][0]["b64_json"])


def compress_to_jpeg(png_bytes: bytes, max_width: int = 1280, quality: int = 82) -> bytes:
    """PNGを縮小JPEGに変換（リポジトリ肥大防止: 1枚 数MB → 100〜300KB程度）。
    Pillowが無い環境ではPNGのまま返す。"""
    if not HAS_PIL:
        return png_bytes
    img = PILImage.open(io.BytesIO(png_bytes)).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)),
                         PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def generate_images(candidates: list[dict], today: str) -> None:
    """候補ごとに1枚ずつ画像を生成。個別失敗はスキップ、全体は止めない。"""
    if not IMAGE_ENABLED:
        print("[info] OPENAI_API_KEY not set — skipping image generation (placeholders will be used)")
        return
    day_dir = IMAGES_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    ext = ".jpg" if HAS_PIL else ".png"
    ok = 0
    for i, c in enumerate(candidates, start=1):
        prompt = c["imagePrompts"][0]
        style_key, style_desc = style_for(i - 1, today)
        try:
            print(f"[info] generating image {i}/{len(candidates)} "
                  f"(model={IMAGE_MODEL}, quality={IMAGE_QUALITY}, style={style_key})")
            raw = openai_generate_image(prompt, style_desc)
            data = compress_to_jpeg(raw)
            path = day_dir / f"candidate-{i}{ext}"
            path.write_bytes(data)
            c["image"] = f"images/{today}/candidate-{i}{ext}"  # アプリが読む相対パス
            c["imageStyle"] = style_key  # 使った画風の記録（アプリ側は無視してOK）
            ok += 1
            print(f"[ok] image {i}: {path.name} ({len(data)//1024} KB)")
        except Exception as e:
            print(f"[warn] image {i} failed (placeholder will be shown): {e}", file=sys.stderr)
    print(f"[info] images generated: {ok}/{len(candidates)}")
    prune_old_images()


def prune_old_images() -> None:
    """IMAGE_KEEP_DAYS より古い日付フォルダを削除してリポジトリの肥大を防ぐ。"""
    if not IMAGES_DIR.exists():
        return
    cutoff = (datetime.now(JST) - timedelta(days=IMAGE_KEEP_DAYS)).strftime("%Y-%m-%d")
    for d in sorted(IMAGES_DIR.iterdir()):
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and d.name < cutoff:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            print(f"[info] pruned old images: {d.name}")


# ----------------------------------------------------------------
# 4. 出力（アトミック書き込み）
# ----------------------------------------------------------------

def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_outputs(candidates: list[dict], today: str) -> None:
    daily = {
        "version": 1,
        "date": today,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "auto (rss + claude)",
        "candidates": candidates,
    }
    payload = json.dumps(daily, ensure_ascii=False, indent=2)

    # 出力直前の最終チェック: 自分の出力をもう一度パースできるか
    json.loads(payload)

    atomic_write(OUT_JSON, payload + "\n")
    atomic_write(OUT_JS, "window.DAILY_DATA = " + payload + ";\n")
    ARCHIVE_DIR.mkdir(exist_ok=True)
    atomic_write(ARCHIVE_DIR / f"{today}.json", payload + "\n")
    print(f"[ok] wrote daily.json / daily.js / archive/{today}.json")


# ----------------------------------------------------------------
# main
# ----------------------------------------------------------------

def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[error] ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 1

    items = fetch_news()
    if len(items) < NUM_PICKS:
        print(f"[error] not enough news items ({len(items)}) — keeping existing daily.json", file=sys.stderr)
        return 1

    client = Anthropic()
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"[info] calling Claude (attempt {attempt}/{MAX_ATTEMPTS}, model={MODEL})")
            data = call_claude(client, items)
            candidates = validate_picks(data, items)
            today = datetime.now(JST).strftime("%Y-%m-%d")
            # 画像生成は「おまけ」扱い: 全滅しても daily.json は出す
            try:
                generate_images(candidates, today)
            except Exception as e:
                print(f"[warn] image stage failed entirely (placeholders will be shown): {e}",
                      file=sys.stderr)
            write_outputs(candidates, today)
            print("[done] generation succeeded")
            return 0
        except Exception as e:
            last_error = e
            print(f"[warn] attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(5)

    print(f"[error] all attempts failed: {last_error} — existing daily.json is untouched", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
