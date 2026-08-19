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

# --- 編集室（モノローグ下書き + SNS投稿候補）の設定 ---
# EDITORIAL_ENABLED=0 を設定すると生成をスキップできる（従来通りの動作に戻る）。
# 生成に失敗しても daily.json 本体は必ず出力される（editorial: null になるだけ）。
EDITORIAL_ENABLED = os.environ.get("EDITORIAL_ENABLED", "1") != "0"
EDITORIAL_MODEL = os.environ.get("EDITORIAL_MODEL", MODEL)
EDITORIAL_MAX_TOKENS = 8000

# --- 編集長レビュー（二段階チェック）の設定 ---
# 生成済みの全コンテンツを「辛口の編集長」としてもう一度読み直し、
# 弱いジョーク・AIっぽさ・画像プロンプトを書き直す第二パス。
# REVIEW_ENABLED=0 でスキップ可能。失敗しても一段階目の結果で出力される。
REVIEW_ENABLED = os.environ.get("REVIEW_ENABLED", "1") != "0"
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", MODEL)
REVIEW_MAX_TOKENS = 8000
NUM_NOTES = 8             # Substack Notes 候補（英語）の本数
NUM_X_POSTS = 8           # X（旧Twitter）候補（日本語）の本数
X_MAX_CHARS = 135         # X候補の最大文字数（140字制限に余白を持たせる）

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

【文体 — 「AIっぽさ」の排除（commentary / ironyEn / captions に適用。厳守）】
書き手は舞台に立つ人間のスタンダップコメディアン。機械的な整いは芸の敵。
- captions は「書き言葉」ではなく「舞台で口に出す一言」。英語は短縮形（don't, it's, they're）を使い、会話のリズムを優先する。
- 定型句を禁止: "Let that sink in" / "In a stunning display of..." / "Ah yes," / "In a world where..." / 「〜と言わざるを得ない」「まさに皮肉としか言いようがない」。
- きれいな三段並列・対句（AIの癖）を避ける。3つ並べるなら3つ目で予想を外す。
- 抽象的な総括で笑わせようとしない。具体的なモノ・数字・場面で笑わせる。
- 同じ記事の5本のcaptionsは、角度だけでなく「文の形」も変える（疑問形・言いかけ・断言・ぼやき・観客への呼びかけ、など）。

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
# 3.2. 編集室: モノローグ下書き（EN/JA）+ SNS投稿候補の生成
#   - 確定した5候補をもとに、2回目のClaude呼び出しで生成する
#   - スタンダップ構成: 掴み(opener) → 5本のくだり(beats) → 締め(closer)
#   - 失敗しても daily.json の生成は止めない（editorial: null で出力）
# ----------------------------------------------------------------

EDITORIAL_SYSTEM_PROMPT = """あなたは「America Satire Desk」の編集AIです。すでに選定済みの本日の風刺候補5本をもとに、ニュースレター用モノローグの下書き（英語版・日本語版）と、SNS投稿候補を作ります。これはあくまで下書きであり、最終的なリライト・事実確認・投稿は必ず人間の編集者が行います。

【この商品が売っているもの（全設計の前提）】
読者が買っているのは「ニュース」ではない。「角度」だ。この記事を読んだ人は、職場・学校・飲み会でこの話題が出たとき、気の利いた視点から一言言えるようになる——それがこの商品の価値。ニュースの要約は無料でどこにでもある。「皮肉の見出し方」「矛盾の突き方」という読み方の技術と、明日そのまま使える一言を持ち帰らせること。すべての見出し・本文・一言はこの前提で書く。

【ペルソナ（厳守）】
語り手は「東京でアメリカのニュースを毎朝読んで、頭を抱えている男」。
- 英語版: An ordinary guy in Tokyo reading American news every morning so you don't have to. 自虐的で、外部者ならではの困惑と好奇心がある。アメリカを見下すのではなく「うちの国も大概だけど、おたくの国は今日も一段と面白いね」という対等な立場のツッコミ。凝りすぎた慣用句の曲芸は不要。シンプルで明瞭な英語と観察の鋭さが武器。一人称は I。
- 日本語版: 同一人物。英語版の翻訳ではなく、同じ人物が日本の読者に向けて話し直したもの。日本の読者に馴染みが薄い前提（制度・役職・人名）には一言だけ補助線を入れる。文体は「です・ます」を基調に、ツッコミの瞬間だけ砕ける。

【文体 — 「AIっぽさ」の徹底排除（最重要）】
これは人間のスタンダップコメディアンの語りとして読まれる文章。機械的な整いは芸の敵。以下を厳守する。
- 文の長さを意図的にばらつかせる。長い文のあとに3語の短文を置く。ときには一語の文も。「で、です。」のような呼吸を恐れない。
- 教科書的な接続語を使わない: 「しかしながら」「一方で」「さらに」「つまり」 / "Moreover," "Furthermore," "However," "It's worth noting," "Interestingly,"。話し言葉の接続（「で、」「あと」「いや待って」 / "And look," "But here's my problem," "Anyway—no, wait."）で繋ぐ。
- きれいな三段並列・対句・「AがBなら、CはDだ」型の整いすぎた構文（AIの癖）を避ける。3つ並べるなら3つ目で必ず予想を外す。
- 抽象語で語らない。具体的な生活のディテールで語る: 冷めたコーヒー、朝5時のスマホの光、コンビニのレジ、山手線。ただし毎回同じ小道具を使い回さない。
- 観客に直接話しかける瞬間を作る: 「いや、聞いてください」「ここ、笑うところです」 / "Look," "I'm serious," "You think I'm making this up."
- 語り手自身の身体的リアクションを入れる: 二度読みした、記事を閉じてもう一度開いた、声が出た。
- 英語は簡潔な口語。短縮形（don't, it's, they're）を必ず使う。完璧な文法よりも会話のリズム。文頭の And / But / So は歓迎。
- 禁止する定型句: "Let that sink in." / "In a world where..." / "In a stunning display of..." / "And that, ladies and gentlemen, ..." / ダッシュ（—）の多用 / 「〜と言わざるを得ない」「まさに現代の縮図だ」。
- 完璧にまとめない。締めの一文は「うまいこと言った感」より「本音がこぼれた感」を優先する。

【スタンダップ構成（厳守）】
- opener: 掴み。今日の5本を貫く「一本の糸」を1つのジョークとして提示する。2〜4文。
- beats: 5本それぞれのくだり。必ず5候補すべてを1回ずつ使う。各beatの冒頭に前のくだりからのブリッジ（話題転換の一言）を含める。弱いネタから強いネタへ並べ、一番笑える候補を最後のbeatに置く。各2〜5文。
- closer: 締め。笑いから一段降りて、本音をひとこと。説教はしない。1〜3文。

【風刺のルール（厳守）】
- 事実は候補データに書かれているものだけを使う。新しい事実・数字・引用を発明しない。
- 風刺の対象は制度・企業・組織・社会構造・矛盾。実在個人の人格・外見・家族は対象にしない。
- 名誉毀損リスクのある断定（違法行為の断定、動機の決めつけ）はしない。
- 悲劇そのものを笑いにしない。

【SNS投稿候補】
- notesEn: Substack Notes用の英語投稿。各1〜3文。その投稿だけ読んで意味が分かる自己完結型にする（必要なニュースの前提を投稿内に含める）。ハッシュタグ・絵文字・URLなし。ペルソナの声で。
- xJa: X（旧Twitter）用の日本語投稿。各135字以内。自己完結型。ハッシュタグ・URLなし。ニュースを知らない日本の読者がそのまま笑える形にする。

【noteタイトル3案（titleJa + titleAltJa）】
noteでは読まれるかどうかの大半がタイトルで決まる。note公式の有料記事500件分析によれば、読まれるタイトルの共通点は「具体性」「読者にとっての価値の明確さ」「トレンド性」。風刺コラムでは次のように翻訳する:
- 具体性: 固有名詞・数字をタイトルにそのまま見せ、その違和感で引く（「最近思ったこと」型の曖昧タイトルは禁止）
- 価値: 読むと何が分かるか・どんな気分になれるかが一目で伝わる
- トレンド性: その日の話題語（人名・企業名・事件）を自然に入れる
本命titleJaに加え、titleAltJaに別角度の2案を作り、型を必ず変えること:
- 1案は「固有名詞・数字の違和感」型（例: 具体的な数字がそのままオチになっている）
- 1案は「これで語れる」型（読み終えたら何をどう語れるようになるかが伝わる。例:「『5000億ドルのAI投資』の話題が出たら、この一言を返せばいい」）
- 1案は「ぼやき・本音がこぼれた」型
titleEn / subtitleEn も同じ思想で: 英語の副題は「この記事を読むと何が語れるようになるか」を軽く匂わせる（説教くさくせず、ウィットで）。
釣らない。本文が答えられない約束をタイトルでしない。「〜がヤバい」「衝撃の」等の摩耗した釣り語は使わない。

【今日の使える一言（quipEn / quipJa）】
記事の締めに置く「持ち帰り」。読者が明日、会議や飲み会でこの話題が出たときに、そのまま口に出して使える気の利いた一言。
- 自己完結型（ニュースの前提を知らない相手にも通じる形）
- 1〜2文。暗記できる短さ。賢く聞こえるが、嫌味に聞こえない
- quipEnとquipJaは同じ趣旨でよいが、直訳ではなくそれぞれの言語で口に出して自然な形にする

【今日の運勢（fortuneJa / fortuneEn）】
記事のもう一つの持ち帰り。皮肉を読んだ読者を、最後に少しだけ元気にして帰す「ニュース連動型の前向き占い」。
- 必ずその日の「一本の糸」（今日の5本に通底するテーマ）に絡めること。汎用の占い文は禁止（例: 糸が「誰も読まない約款」の日なら→「3,000ページ読まない議員でも法律は通る。あなたの今日のタスクも、完璧に読み込んでから始めなくていい。仕事運は上向きです」）
- 星座別にしない。全読者向けに1本だけ
- 2〜3文。仕事運・恋愛運・金運のどれかに軽く触れてよい
- 構造は「ニュースの皮肉→だからあなたは大丈夫、という論理の飛躍→前向きな着地」。説教とスピリチュアル用語は禁止。自己肯定感がその日だけ少し上がる読後感に
- fortuneEnはThe Onionの風刺占いの伝統を意識しつつ、最後は必ず温かく着地させる（皮肉で終わらせない）

【出力形式（厳守）】
- 有効なJSONのみを出力する。前置き・後書き・コードフェンスは一切付けない。
- beats の ref は候補の id（d1〜d5）を使う。

JSONスキーマ:
{
  "thread": "<日本語1〜2文。今日の5本を貫く『一本の糸』（人間の編集者向けメモ）>",
  "titleEn": "<英語タイトル。ニュースレターの件名になる。punchyに、60文字以内>",
  "subtitleEn": "<英語の副題(dek)。1文・15語以内。タイトルの下に表示され、開封を後押しするフック>",
  "titleJa": "<日本語タイトル本命案。note記事の見出しになる>",
  "titleAltJa": ["<日本語タイトル別案（本命と型を変える）>", "<日本語タイトル別案（さらに別の型）>"],
  "leadJa": "<日本語。note記事の冒頭に置く掴み2〜3文。タイトルの約束をすぐ回収しつつ、続きを読ませる。ペルソナの声で>",
  "quipEn": "<英語1〜2文。読者が明日そのまま会話で使える、今日一番の気の利いた一言>",
  "quipJa": "<日本語1〜2文。同趣旨の日本語版。口に出して自然な形>",
  "fortuneEn": "<英語2〜3文。今日のニュースに絡めた前向き占い(Today's Forecast)。温かく着地>",
  "fortuneJa": "<日本語2〜3文。今日のニュースに絡めた前向き占い。自己肯定感が上がる着地>",
  "monologueEn": {
    "opener": "<英語>",
    "beats": [{"ref": "d1", "text": "<英語>"}, {"ref": "...", "text": "..."}, {"ref": "...", "text": "..."}, {"ref": "...", "text": "..."}, {"ref": "...", "text": "..."}],
    "closer": "<英語>"
  },
  "monologueJa": {
    "opener": "<日本語>",
    "beats": [{"ref": "d1", "text": "<日本語>"}, {"ref": "...", "text": "..."}, {"ref": "...", "text": "..."}, {"ref": "...", "text": "..."}, {"ref": "...", "text": "..."}],
    "closer": "<日本語>"
  },
  "notesEn": ["<英語>", "...", "...", "...", "...", "...", "...", "..."],
  "xJa": ["<日本語>", "...", "...", "...", "...", "...", "...", "..."]
}
monologueEn と monologueJa の beats は同じ順序（同じrefの並び）にすること。"""


def build_editorial_prompt(candidates: list[dict], today: str) -> str:
    """5候補を圧縮した素材リストにして渡す。"""
    lines = [f"本日 {today} の確定済み候補5本です。この素材だけを使って、"
             "指定スキーマのJSONだけを出力してください。\n"]
    for c in candidates:
        lines.append(f"[{c['id']}] {c['news']['headline']}  ({c['news']['source']})")
        lines.append(f"  事実(EN): {c['newsEn']}")
        lines.append(f"  事実(JA): {c['news']['summary']}")
        for point in c["commentary"]:
            lines.append(f"  視点: {point}")
        for cap in c["captions"][:3]:
            lines.append(f"  パンチライン案: {cap}")
        lines.append("")
    return "\n".join(lines)


def _validate_monologue(m: dict, name: str, candidates: list[dict],
                        min_len: int) -> dict:
    """モノローグ1言語分の構造検証。beatsが5候補を過不足なく使っているかも確認。"""
    if not isinstance(m, dict):
        raise ValueError(f"{name}: not an object")
    valid_ids = {c["id"] for c in candidates}
    beats = m.get("beats")
    if not isinstance(beats, list) or len(beats) != NUM_PICKS:
        raise ValueError(f"{name}: beats must be exactly {NUM_PICKS}")
    norm_beats, used = [], set()
    for i, b in enumerate(beats):
        ref = b.get("ref") if isinstance(b, dict) else None
        if ref not in valid_ids or ref in used:
            raise ValueError(f"{name}: beat {i}: invalid or duplicate ref: {ref!r}")
        used.add(ref)
        norm_beats.append({
            "ref": ref,
            "text": _req_str(b.get("text"), f"{name}: beat {i} text", min_len),
        })
    return {
        "opener": _req_str(m.get("opener"), f"{name}: opener", min_len),
        "beats": norm_beats,
        "closer": _req_str(m.get("closer"), f"{name}: closer", 10),
    }


def validate_editorial(data: dict, candidates: list[dict]) -> dict:
    """編集室データの検証・正規化。壊れた候補はここで弾く。"""
    thread = _req_str(data.get("thread"), "editorial: thread", 8)
    title_en = _req_str(data.get("titleEn"), "editorial: titleEn", 8)
    title_ja = _req_str(data.get("titleJa"), "editorial: titleJa", 5)
    mono_en = _validate_monologue(data.get("monologueEn"), "monologueEn", candidates, 40)
    mono_ja = _validate_monologue(data.get("monologueJa"), "monologueJa", candidates, 25)

    notes = data.get("notesEn")
    if not isinstance(notes, list):
        raise ValueError("editorial: notesEn missing")
    notes = [n.strip() for n in notes if isinstance(n, str) and len(n.strip()) >= 20]
    if len(notes) < 5:
        raise ValueError(f"editorial: notesEn needs >=5 usable items (got {len(notes)})")

    x_posts = data.get("xJa")
    if not isinstance(x_posts, list):
        raise ValueError("editorial: xJa missing")
    x_posts = [p.strip() for p in x_posts
               if isinstance(p, str) and 10 <= len(p.strip()) <= X_MAX_CHARS + 5]
    if len(x_posts) < 5:
        raise ValueError(f"editorial: xJa needs >=5 usable items within "
                         f"{X_MAX_CHARS} chars (got {len(x_posts)})")

    # タイトル別案は任意項目（無くても失敗にしない）
    title_alt = data.get("titleAltJa")
    if not isinstance(title_alt, list):
        title_alt = []
    title_alt = [t.strip() for t in title_alt
                 if isinstance(t, str) and len(t.strip()) >= 5][:3]

    # 副題(EN)とリード文(JA)も任意項目（無ければ空。フロント側でフォールバック）
    subtitle_en = data.get("subtitleEn")
    subtitle_en = subtitle_en.strip() if isinstance(subtitle_en, str) and len(subtitle_en.strip()) >= 8 else ""
    lead_ja = data.get("leadJa")
    lead_ja = lead_ja.strip() if isinstance(lead_ja, str) and len(lead_ja.strip()) >= 10 else ""
    quip_en = data.get("quipEn")
    quip_en = quip_en.strip() if isinstance(quip_en, str) and len(quip_en.strip()) >= 10 else ""
    quip_ja = data.get("quipJa")
    quip_ja = quip_ja.strip() if isinstance(quip_ja, str) and len(quip_ja.strip()) >= 8 else ""
    fortune_en = data.get("fortuneEn")
    fortune_en = fortune_en.strip() if isinstance(fortune_en, str) and len(fortune_en.strip()) >= 15 else ""
    fortune_ja = data.get("fortuneJa")
    fortune_ja = fortune_ja.strip() if isinstance(fortune_ja, str) and len(fortune_ja.strip()) >= 12 else ""

    return {
        "thread": thread,
        "titleEn": title_en,
        "subtitleEn": subtitle_en,
        "titleJa": title_ja,
        "titleAltJa": title_alt,
        "leadJa": lead_ja,
        "quipEn": quip_en,
        "quipJa": quip_ja,
        "fortuneEn": fortune_en,
        "fortuneJa": fortune_ja,
        "monologueEn": mono_en,
        "monologueJa": mono_ja,
        "notesEn": notes[:NUM_NOTES],
        "xJa": x_posts[:NUM_X_POSTS],
    }


def assemble_full_text(editorial: dict, candidates: list[dict]) -> None:
    """モノローグ各部を、そのまま貼れる1本のMarkdown下書きに組み立てる。
    末尾に beat の順で「今日の5本」の出典リスト（Today's Docket）を付ける。"""
    by_id = {c["id"]: c for c in candidates}

    def docket_en() -> list[str]:
        lines = ["---", "", "**Today's Docket** (in order of appearance)", ""]
        for i, b in enumerate(editorial["monologueEn"]["beats"], start=1):
            n = by_id[b["ref"]]["news"]
            lines.append(f"{i}. **{n['headline']}** — {n['source']} ([source]({n['url']}))")
        return lines

    def docket_ja() -> list[str]:
        lines = ["---", "", "**今日の5本**（登場順）", ""]
        for i, b in enumerate(editorial["monologueJa"]["beats"], start=1):
            n = by_id[b["ref"]]["news"]
            lines.append(f"{i}. **{n['headline']}**（{n['source']}） — {n['summary']} "
                         f"[記事]({n['url']})")
        return lines

    en = editorial["monologueEn"]
    parts_en = [f"# {editorial['titleEn']}", "", en["opener"], ""]
    for b in en["beats"]:
        parts_en += [b["text"], ""]
    parts_en += [en["closer"], ""]
    if editorial.get("fortuneEn"):
        parts_en += ["**Today's Forecast** (news-based, scientifically dubious, warmly meant):", "",
                     f"> {editorial['fortuneEn']}", ""]
    if editorial.get("quipEn"):
        parts_en += ["**Steal this line** — for your next meeting:", "",
                     f"> {editorial['quipEn']}", ""]
    parts_en += docket_en()
    editorial["fullEn"] = "\n".join(parts_en).strip() + "\n"

    ja = editorial["monologueJa"]
    parts_ja = [f"# {editorial['titleJa']}", "", ja["opener"], ""]
    for b in ja["beats"]:
        parts_ja += [b["text"], ""]
    parts_ja += [ja["closer"], ""]
    if editorial.get("fortuneJa"):
        parts_ja += ["**今日の運勢**（ニュース連動・非科学的・でも本気で応援）:", "",
                     f"> {editorial['fortuneJa']}", ""]
    if editorial.get("quipJa"):
        parts_ja += ["**今日の使える一言**（明日この話題が出たら、これをどうぞ）:", "",
                     f"> {editorial['quipJa']}", ""]
    parts_ja += docket_ja()
    editorial["fullJa"] = "\n".join(parts_ja).strip() + "\n"


def generate_editorial(client: Anthropic, candidates: list[dict],
                       today: str) -> dict | None:
    """編集室データを生成する。失敗したら None を返す（本体の生成は止めない）。"""
    if not EDITORIAL_ENABLED:
        print("[info] EDITORIAL_ENABLED=0 — skipping monologue generation")
        return None
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"[info] generating editorial draft "
                  f"(attempt {attempt}/{MAX_ATTEMPTS}, model={EDITORIAL_MODEL})")
            message = client.messages.create(
                model=EDITORIAL_MODEL,
                max_tokens=EDITORIAL_MAX_TOKENS,
                system=EDITORIAL_SYSTEM_PROMPT,
                messages=[{"role": "user",
                           "content": build_editorial_prompt(candidates, today)}],
            )
            text = "".join(b.text for b in message.content if b.type == "text")
            usage = getattr(message, "usage", None)
            if usage:
                print(f"[info] editorial tokens: in={usage.input_tokens} "
                      f"out={usage.output_tokens}")
            editorial = validate_editorial(extract_json(text), candidates)
            assemble_full_text(editorial, candidates)
            print(f"[ok] editorial draft ready: notes={len(editorial['notesEn'])} "
                  f"x-posts={len(editorial['xJa'])}")
            return editorial
        except Exception as e:
            last_error = e
            print(f"[warn] editorial attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(5)
    print(f"[warn] editorial generation failed entirely — daily.json will have "
          f"editorial: null ({last_error})", file=sys.stderr)
    return None


# ----------------------------------------------------------------
# 3.3. 編集長レビュー（二段階チェック）
#   - 一段階目の全出力を「辛口編集長」として読み直し、弱い箇所だけ書き直す
#   - 画像生成の前に実行する（改善済みの画像プロンプトを使うため）
#   - 失敗したら一段階目の結果をそのまま使う（安全なフォールバック）
# ----------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """あなたは「America Satire Desk」の編集長です。部下が作った本日の風刺コンテンツ一式を、初見の読者としてゼロから読み直し、弱い箇所だけを書き直します。あなたは褒めるためではなく、水準を上げるために存在します。

【審査基準】
1. ジョークの強度: 「説明」で終わっているcaptionは、オチのある一言に書き直す。良いジョークにはミスディレクション（読者の予想を最後の瞬間に裏切る構造）がある。状況をなぞっただけの文は不合格。
2. AIっぽさの検出: 整いすぎた対句・きれいな三段並列・定型句・均一な文の長さを見つけたら壊す。人間の呼吸にする。
3. ペルソナの一貫性: モノローグは「東京でアメリカのニュースを毎朝読んで頭を抱えている男」の声か。上から目線の講釈になっていたら、当事者性のあるぼやきに直す。
4. 事実の検品: 元データに無い事実・数字・引用が紛れ込んでいたら削除する。新しい事実を絶対に足さない。
5. 画像プロンプトの検品: 実在人物の顔・容姿に依存した構図は禁止（そもそも似ない上に権利リスクがある）。役職や制度の記号（大統領執務室の机、書類の山、議事堂のドーム、赤いネクタイの後ろ姿、報道陣のマイクの群れ）と状況の可視化で皮肉が伝わる構図に書き直す。人物を入れる場合は「顔が判別できない後ろ姿・シルエット・群衆」まで。
6. タイトルの検品: 曖昧・抽象的・釣り表現を弾く。具体的な固有名詞や数字の違和感で引くタイトルに。

【ルール（厳守）】
- 良いものは変えない。「確実により笑える／より人間らしい」と言える場合だけ書き直す。
- 元データに無い事実を足さない。ニュースの内容（headline, summary, newsEn, commentary）は変更対象外。
- xJaは135字以内を厳守。
- 有効なJSONのみを出力する。前置き・後書き・コードフェンス禁止。

【出力形式（差分方式・厳守）】
変更した箇所「だけ」を出力する。合格（無変更）の要素は出力に含めない。全体の書き直しは禁止——悪い箇所をピンポイントで直すのがあなたの仕事。何も直す必要がなければ candidatePatches は []、editorialPatch は {} とする。

JSONスキーマ:
{
  "reviewNotes": "<日本語1〜3文。今日どこを直したかの編集メモ。直す箇所が無ければ『合格』と書く>",
  "candidatePatches": [
    {"id": "<直す候補のid>",
     "captions": ["<英語5本。captionsを直す場合のみ・リスト全本を出す>"],
     "captionsJa": ["<日本語5本。captionsを出すときは必ずペアで同数>"],
     "imagePrompts": ["<英語3本。直す場合のみ・リスト全本を出す>"]}
  ],
  "editorialPatch": {
    "titleEn": "<変更する場合のみ>", "subtitleEn": "<同>", "titleJa": "<同>",
    "titleAltJa": ["<変更する場合のみ・全案>"], "leadJa": "<同>", "thread": "<同>",
    "quipEn": "<同>", "quipJa": "<同>", "fortuneEn": "<同>", "fortuneJa": "<同>",
    "notesEn": ["<変更する場合のみ・8本すべて>"], "xJa": ["<同・8本すべて>"],
    "monologueEn": {"opener": "<変更する場合のみ>",
                    "beats": [{"ref": "d3", "text": "<直すbeatだけref付きで差し替え>"}],
                    "closer": "<変更する場合のみ>"},
    "monologueJa": {"...同様に変更箇所のみ...": ""}
  }
}
- candidatePatches: 直す候補だけを入れる。フィールドも直すものだけ（captionsだけ、imagePromptsだけ、でよい）。
- monologueのbeatsは差分可: 直したいbeatのref+textだけを出す。opener/closerも変更時のみ。
- リスト型フィールド(captions/imagePrompts/notesEn/xJa/titleAltJa)は部分差し替え不可なので、直すならリスト全本を出す。"""


def build_review_prompt(candidates: list[dict], editorial: dict, today: str) -> str:
    """一段階目の全出力を審査対象として渡す。"""
    material = {
        "date": today,
        "candidates": [
            {
                "id": c["id"],
                "news": {"headline": c["news"]["headline"],
                         "summary": c["news"]["summary"]},
                "newsEn": c["newsEn"],
                "commentary": c["commentary"],
                "captions": c["captions"],
                "captionsJa": c["captionsJa"],
                "imagePrompts": c["imagePrompts"],
            } for c in candidates
        ],
        "editorial": {k: editorial.get(k) for k in
                      ("thread", "titleEn", "subtitleEn", "titleJa", "titleAltJa",
                       "leadJa", "quipEn", "quipJa", "fortuneEn", "fortuneJa",
                       "monologueEn", "monologueJa", "notesEn", "xJa")},
    }
    return ("本日の風刺コンテンツ一式です。審査基準に沿って読み直し、"
            "指定スキーマのJSONだけを出力してください。\n\n"
            + json.dumps(material, ensure_ascii=False, indent=1))


def _apply_candidate_patches(candidates: list[dict], patches) -> int:
    """差分パッチのcaptions/imagePromptsを検証して適用。戻り値は適用件数。
    フィールドは部分的でよい（captionsだけ、imagePromptsだけ等）。
    検証は先に全て行い、通った場合だけ反映する（中途半端な適用を防ぐ）。"""
    if not isinstance(patches, list):
        return 0
    by_id = {c["id"]: c for c in candidates}
    applied = 0
    for p in patches:
        if not isinstance(p, dict) or p.get("id") not in by_id:
            continue
        c = by_id[p["id"]]
        try:
            updates = {}
            captions = p.get("captions")
            captions_ja = p.get("captionsJa")
            if captions is not None or captions_ja is not None:
                if (isinstance(captions, list) and isinstance(captions_ja, list)
                        and 3 <= len(captions) <= 5 and len(captions) == len(captions_ja)):
                    updates["captions"] = [_req_str(x, "review caption", 8) for x in captions]
                    updates["captionsJa"] = [_req_str(x, "review captionJa", 4) for x in captions_ja]
                else:
                    raise ValueError("captions/captionsJa must be a same-length pair (3-5)")
            prompts = p.get("imagePrompts")
            if prompts is not None:
                if isinstance(prompts, list) and len(prompts) >= 2:
                    updates["imagePrompts"] = [_req_str(x, "review imagePrompt", 20)
                                               for x in prompts[:3]]
                else:
                    raise ValueError("imagePrompts shape")
            if not updates:
                continue
        except ValueError as e:
            print(f"[warn] review: patch for {p.get('id')} rejected: {e}", file=sys.stderr)
            continue  # このカードのパッチは破棄（元の文を残す）
        c.update(updates)
        applied += 1
    return applied


def _merge_editorial_patch(editorial: dict, patch: dict,
                           candidates: list[dict]) -> dict:
    """差分パッチを editorial にマージし、全体を再検証して返す。
    検証に失敗した場合は例外を投げる（呼び出し側で元のeditorialを維持）。"""
    import copy
    merged = copy.deepcopy(editorial)
    for k in ("fullEn", "fullJa", "reviewNotes"):
        merged.pop(k, None)
    # 単純置換フィールド（文字列・リストは丸ごと差し替え）
    simple_keys = ("thread", "titleEn", "subtitleEn", "titleJa", "titleAltJa",
                   "leadJa", "quipEn", "quipJa", "fortuneEn", "fortuneJa",
                   "notesEn", "xJa")
    for k in simple_keys:
        if k in patch and patch[k] is not None:
            merged[k] = patch[k]
    # モノローグはbeat単位の差分に対応
    for mkey in ("monologueEn", "monologueJa"):
        mp = patch.get(mkey)
        if not isinstance(mp, dict):
            continue
        tgt = merged.get(mkey) or {}
        if isinstance(mp.get("opener"), str) and mp["opener"].strip():
            tgt["opener"] = mp["opener"]
        if isinstance(mp.get("closer"), str) and mp["closer"].strip():
            tgt["closer"] = mp["closer"]
        if isinstance(mp.get("beats"), list):
            by_ref = {b.get("ref"): b for b in tgt.get("beats", [])}
            for pb in mp["beats"]:
                if (isinstance(pb, dict) and pb.get("ref") in by_ref
                        and isinstance(pb.get("text"), str) and pb["text"].strip()):
                    by_ref[pb["ref"]]["text"] = pb["text"]
    # マージ結果を通常の検証にかける（壊れたパッチはここで弾かれる）
    return validate_editorial(merged, candidates)


def review_and_polish(client: Anthropic, candidates: list[dict],
                      editorial: dict | None, today: str
                      ) -> tuple[list[dict], dict | None]:
    """二段階目: 編集長パス。失敗しても一段階目の結果をそのまま返す。"""
    if not REVIEW_ENABLED:
        print("[info] REVIEW_ENABLED=0 — skipping editor-in-chief pass")
        return candidates, editorial
    if editorial is None:
        print("[info] editorial missing — skipping editor-in-chief pass")
        return candidates, editorial
    try:
        print(f"[info] editor-in-chief review pass (model={REVIEW_MODEL})")
        message = client.messages.create(
            model=REVIEW_MODEL,
            max_tokens=REVIEW_MAX_TOKENS,
            system=REVIEW_SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": build_review_prompt(candidates, editorial, today)}],
        )
        text = "".join(b.text for b in message.content if b.type == "text")
        usage = getattr(message, "usage", None)
        if usage:
            print(f"[info] review tokens: in={usage.input_tokens} out={usage.output_tokens}")
        data = extract_json(text)

        applied = _apply_candidate_patches(candidates, data.get("candidatePatches"))
        print(f"[info] review: candidate patches applied to {applied} candidates")

        new_editorial = editorial
        ep = data.get("editorialPatch")
        if isinstance(ep, dict) and ep:
            try:
                new_editorial = _merge_editorial_patch(editorial, ep, candidates)
                assemble_full_text(new_editorial, candidates)
                print(f"[info] review: editorial patch merged "
                      f"({', '.join(sorted(ep.keys()))})")
            except Exception as e:
                print(f"[warn] review editorial patch rejected (keeping first draft): {e}",
                      file=sys.stderr)
                new_editorial = editorial
        elif applied:
            # captionsが変わった場合もfullEn/fullJaのドケット部分は影響を受けないため
            # 再組み立ては不要（プルクオートはフロント側で最新captionsを参照する）
            pass
        notes = data.get("reviewNotes")
        if isinstance(notes, str) and notes.strip():
            new_editorial["reviewNotes"] = notes.strip()
        print("[ok] editor-in-chief pass done")
        return candidates, new_editorial
    except Exception as e:
        print(f"[warn] review pass failed entirely (using first draft): {e}",
              file=sys.stderr)
        return candidates, editorial


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
    import shutil
    cutoff = (datetime.now(JST) - timedelta(days=IMAGE_KEEP_DAYS)).strftime("%Y-%m-%d")
    for d in sorted(IMAGES_DIR.iterdir()):
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)  # carousel等のサブフォルダごと削除
            print(f"[info] pruned old images: {d.name}")


# ----------------------------------------------------------------
# 3.7. カルーセル自動組版（TikTokフォトモード / Instagram用・英語）
#   - 表紙 + 風刺画5枚(パンチライン焼き込み) + CTA の7枚を毎朝生成
#   - 1080x1350 (4:5)。Pillowのみで組版。失敗しても本体は止めない
# ----------------------------------------------------------------

CAROUSEL_W, CAROUSEL_H = 1080, 1350
C_CREAM = (246, 241, 231)   # サイトと同じ配色
C_TEXT = (69, 63, 54)
C_NAVY = (95, 114, 145)
C_CORAL = (205, 107, 87)
C_LINE = (231, 222, 204)
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
NEWSLETTER_CTA = os.environ.get(
    "CAROUSEL_CTA", "Full monologue + the line to steal → link in bio")


def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"{FONT_DIR}/{name}", size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """ピクセル幅で折り返し。"""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_wrapped(draw, text, font, x, y, max_width, fill, line_gap=10) -> int:
    """折り返して描画し、次のy座標を返す。"""
    for line in _wrap_text(draw, text, font, max_width):
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_gap
    return y


def _slide_base():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (CAROUSEL_W, CAROUSEL_H), C_CREAM)
    d = ImageDraw.Draw(img)
    # ヘッダー(ブランド)とフッター
    d.text((60, 48), "JOKES YOU CAN USE", font=_font(34), fill=C_NAVY)
    d.line([(60, 104), (CAROUSEL_W - 60, 104)], fill=C_LINE, width=3)
    return img, d


def _slide_footer(d, page_label: str, today: str):
    d.line([(60, CAROUSEL_H - 96), (CAROUSEL_W - 60, CAROUSEL_H - 96)], fill=C_LINE, width=3)
    d.text((60, CAROUSEL_H - 76), f"THE VIEW FROM TOKYO  ·  {today}",
           font=_font(26, bold=False), fill=C_NAVY)
    w = d.textlength(page_label, font=_font(26))
    d.text((CAROUSEL_W - 60 - w, CAROUSEL_H - 76), page_label, font=_font(26), fill=C_CORAL)


def generate_carousel(candidates: list[dict], editorial: dict | None,
                      today: str) -> list[str]:
    """カルーセル7枚を生成し、相対パスのリストを返す。失敗時は空リスト。"""
    if not HAS_PIL:
        print("[info] Pillow not available — skipping carousel")
        return []
    from PIL import Image, ImageDraw
    try:
        out_dir = IMAGES_DIR / today / "carousel"
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        total = len(candidates) + 2

        # マスコット「Punchy」(images/ に置かれた画像があれば登場)
        #   mascot.png     → 表紙 / mascot-cta.png → 最終スライド(無ければmascot.png)
        #   reaction-1..5.png → 各ニューススライドに日替わりローテーションで登場
        def _load_png(name):
            p = ROOT / "images" / name
            if p.exists():
                try:
                    return Image.open(p).convert("RGBA")
                except Exception:
                    return None
            return None
        mascot = _load_png("mascot.png")
        mascot_cta = _load_png("mascot-cta.png") or mascot
        reactions = [r for r in (_load_png(f"reaction-{i}.png") for i in range(1, 6)) if r]
        # 同じ記事番号でも日によって表情が変わるよう、日付でオフセット
        try:
            reaction_offset = int(today[8:10]) % len(reactions) if reactions else 0
        except Exception:
            reaction_offset = 0

        # --- 表紙 ---
        img, d = _slide_base()
        # ベネフィットのストラップライン（毎日固定。ブランドの約束）
        d.text((60, 118), "THE IRONY INSIDE AMERICAN NEWS — AS A JOKE YOU CAN USE",
               font=_font(26), fill=C_CORAL)
        y = 360
        d.text((60, y - 120), "TODAY IN AMERICA", font=_font(30), fill=C_NAVY)
        title = (editorial or {}).get("titleEn") or f"Five stories, one raised eyebrow"
        y = _draw_wrapped(d, title, _font(72), 60, y, CAROUSEL_W - 120, C_TEXT, 16)
        sub = (editorial or {}).get("subtitleEn") or ""
        if sub:
            y += 30
            y = _draw_wrapped(d, sub, _font(40, bold=False), 60, y, CAROUSEL_W - 120, C_NAVY, 12)
        d.text((60, CAROUSEL_H - 240), "5 stories → swipe", font=_font(44), fill=C_CORAL)
        if mascot:
            mm = mascot.resize((250, 250), Image.LANCZOS)
            img.paste(mm, (CAROUSEL_W - 300, CAROUSEL_H - 370), mm)
        _slide_footer(d, f"1/{total}", today)
        p = out_dir / "slide-1.jpg"
        img.save(p, "JPEG", quality=88)
        paths.append(f"images/{today}/carousel/slide-1.jpg")

        # --- ニュース5枚 ---
        for i, c in enumerate(candidates, start=1):
            img, dd = _slide_base()
            # 風刺画(あれば): 上部に配置
            art_h = 640
            art_path = ROOT / c["image"] if c.get("image") else None
            if art_path and art_path.exists():
                art = Image.open(art_path).convert("RGB")
                ratio = (CAROUSEL_W - 120) / art.width
                art = art.resize((CAROUSEL_W - 120, int(art.height * ratio)), Image.LANCZOS)
                if art.height > art_h:
                    art = art.crop((0, (art.height - art_h) // 2,
                                    art.width, (art.height - art_h) // 2 + art_h))
                img.paste(art, (60, 140))
                text_y = 140 + art.height + 44
            else:
                dd.rectangle([60, 140, CAROUSEL_W - 60, 140 + art_h], fill=(243, 237, 224))
                dd.text((CAROUSEL_W // 2 - 20, 140 + art_h // 2), str(i),
                        font=_font(120), fill=C_LINE)
                text_y = 140 + art_h + 44
            # 見出し(小・事実) → パンチライン(大)
            # Punchyのリアクション(記事番号+日付で表情をローテーション)
            if reactions:
                rr = reactions[(i - 1 + reaction_offset) % len(reactions)]
                rr = rr.resize((175, 175), Image.LANCZOS)
                img.paste(rr, (CAROUSEL_W - 235, 128), rr)
            dd.text((60, text_y), f"STORY {i}", font=_font(28), fill=C_CORAL)
            text_y += 48
            text_y = _draw_wrapped(dd, c["news"]["headline"], _font(34, bold=False),
                                   60, text_y, CAROUSEL_W - 120, C_NAVY, 8)
            text_y += 28
            caption = (c.get("captions") or [""])[0]
            _draw_wrapped(dd, f"“{caption}”", _font(46), 60, text_y,
                          CAROUSEL_W - 120, C_TEXT, 12)
            _slide_footer(dd, f"{i + 1}/{total}", today)
            p = out_dir / f"slide-{i + 1}.jpg"
            img.save(p, "JPEG", quality=88)
            paths.append(f"images/{today}/carousel/slide-{i + 1}.jpg")

        # --- CTA ---
        img, d = _slide_base()
        y = 300
        d.text((60, y - 100), "THAT'S TODAY'S AMERICA.", font=_font(30), fill=C_CORAL)
        quip = (editorial or {}).get("quipEn") or ""
        if quip:
            y = _draw_wrapped(d, f"“{quip}”", _font(56), 60, y,
                              CAROUSEL_W - 120, C_TEXT, 14)
            y += 40
            d.text((60, y), "— steal this line for your next meeting",
                   font=_font(30, bold=False), fill=C_NAVY)
            y += 90
        y = max(y, 760)
        y = _draw_wrapped(d, NEWSLETTER_CTA, _font(44), 60, y, CAROUSEL_W - 120, C_CORAL, 12)
        y += 30
        _draw_wrapped(d, "Come back tomorrow. Your meetings will thank you.",
                      _font(30, bold=False), 60, y, CAROUSEL_W - 700, C_NAVY, 8)
        if mascot_cta:
            mm = mascot_cta.resize((300, 300), Image.LANCZOS)
            img.paste(mm, (CAROUSEL_W - 350, CAROUSEL_H - 430), mm)
        _slide_footer(d, f"{total}/{total}", today)
        p = out_dir / f"slide-{total}.jpg"
        img.save(p, "JPEG", quality=88)
        paths.append(f"images/{today}/carousel/slide-{total}.jpg")

        print(f"[ok] carousel: {len(paths)} slides generated")
        return paths
    except Exception as e:
        print(f"[warn] carousel generation failed (skipping): {e}", file=sys.stderr)
        return []


# ----------------------------------------------------------------
# 4. 出力（アトミック書き込み）
# ----------------------------------------------------------------

def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_outputs(candidates: list[dict], today: str,
                  editorial: dict | None = None,
                  carousel: list[str] | None = None) -> None:
    daily = {
        "version": 1,
        "date": today,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "auto (rss + claude)",
        "candidates": candidates,
        "editorial": editorial,  # モノローグ下書き+SNS候補（生成失敗時は null）
        "carousel": carousel or [],  # TikTok/Instagram用カルーセル画像の相対パス
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
            # 編集室（モノローグ+SNS候補）も「おまけ」扱い: 失敗しても本体は出す
            editorial = generate_editorial(client, candidates, today)
            # 二段階目: 編集長パス（画像生成の前に。改善済みプロンプトを使うため）
            candidates, editorial = review_and_polish(client, candidates, editorial, today)
            # 画像生成は「おまけ」扱い: 全滅しても daily.json は出す
            try:
                generate_images(candidates, today)
            except Exception as e:
                print(f"[warn] image stage failed entirely (placeholders will be shown): {e}",
                      file=sys.stderr)
            # カルーセル組版（風刺画の後。失敗しても本体は止めない）
            carousel = generate_carousel(candidates, editorial, today)
            write_outputs(candidates, today, editorial, carousel)
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
