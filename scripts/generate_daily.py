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

# --- 日本語文体パス（AI感ハンター）の設定 ---
# 完成した日本語だけを対象に「AIっぽい文」を検出し、その文だけを
# ラフな話し言葉に書き直す第三のパス。POLISH_JA_ENABLED=0 でスキップ可。
POLISH_JA_ENABLED = os.environ.get("POLISH_JA_ENABLED", "1") != "0"
POLISH_JA_MAX_TOKENS = 4000
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
- commentary(日本語3視点)とcaptionsJaは、記事にそのまま掲載される本文。書き言葉ではなく話し言葉で書く: 「〜なんですよ」「〜じゃないですか」等の文末、短い文で切る、体言止め、ツッコミの瞬間は常体(「読めるか。」)。翻訳調(「〜することができます」「〜と言えるでしょう」)は禁止。
- news.summary(日本語要約)は事実のみだが、翻訳調ではなく自然な日本語のニュース文体で(「〜と発表した。」「〜が明らかになった。」)。
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
# 3.2. 編集室: 記事パーツ（EN/JA）+ SNS投稿候補の生成
#   - 記事構成(モジュール型): 今日を占うよ〜(導入) → 5ニュースブロック → 今日のまとめジョーク/パンチライン
#   - 5ニュースブロックの本文(概要・3視点・ジョーク)は候補データをそのまま使うため、
#     ここで生成するのは導入・パンチライン・タイトル・SNS候補のみ
#   - 失敗しても daily.json の生成は止めない（editorial: null で出力）
# ----------------------------------------------------------------

EDITORIAL_SYSTEM_PROMPT = """あなたは「America Satire Desk」の編集AIです。すでに選定済みの本日の風刺候補5本をもとに、記事の導入と締め（英語版・日本語版）、タイトル、SNS投稿候補を作ります。記事は「導入（今日を占うよ〜）→ ニュース5ブロック（概要・どこが笑える？・ジョーク。これは候補データから自動組版）→ 今日のまとめジョーク/パンチライン（締め）」というモジュール構成です。これはあくまで下書きであり、最終的なリライト・事実確認・投稿は必ず人間の編集者が行います。

【この商品が売っているもの（全設計の前提）】
読者が買っているのは「ニュース」ではない。「角度」だ。この記事を読んだ人は、職場・学校・飲み会でこの話題が出たとき、気の利いた視点から一言言えるようになる——それがこの商品の価値。ニュースの要約は無料でどこにでもある。「皮肉の見出し方」「矛盾の突き方」という読み方の技術と、明日そのまま使える一言を持ち帰らせること。すべての見出し・本文・一言はこの前提で書く。

【ペルソナ（厳守）】
語り手は「東京でアメリカのニュースを毎朝読んで、頭を抱えている男」。
- 英語版: An ordinary guy in Tokyo reading American news every morning so you don't have to. 自虐的で、外部者ならではの困惑と好奇心がある。アメリカを見下すのではなく「うちの国も大概だけど、おたくの国は今日も一段と面白いね」という対等な立場のツッコミ。凝りすぎた慣用句の曲芸は不要。シンプルで明瞭な英語と観察の鋭さが武器。一人称は I。
- 日本語版: 同一人物。英語版の翻訳ではなく、同じ人物が日本の読者に向けて話し直したもの。日本の読者に馴染みが薄い前提（制度・役職・人名）には一言だけ補助線を入れる。文体は下の【日本語の文体】ルールに完全に従うこと。

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

【日本語の文体 — noteで読まれる“話し言葉”（最重要・introJa/leadJa/quipJa/xJaすべてに適用）】
書き言葉ではなく、親しい友人に話している声をそのまま文字にする。人気noteクリエイターの文章から学んだ以下のルールを厳守:
- 改行を惜しまない。1段落は1〜3文まで。段落の区切りは空行（\n\n）。感情が切り替わる瞬間には、1文だけの段落を置く。
- 短い文で切る。体言止め、一語文を恐れない（「無理。」「で、です。」「最高の朝。」）。長い文を書いたら、次は短く。
- 会話の接続で繋ぐ:「で、」「いや、」「まあ」「あと」「ちなみに」「というか」。言い直しを入れる（「すごい。いや、すごくはない。」「でも、いや、だからこそ」）。
- 自問自答を使う（「何なんだこれは。」「え、そこ?」）。読者への語りかけを使う（「聞いてください」「〜じゃないですか」「わかります?」）。
- 文末を散らす: です・ます基調に、「〜なんですよ」「〜んですよね」「〜と思う」「〜らしい」体言止めを混ぜる。同じ文末を3回続けない。
- ツッコミの瞬間だけ常体に落とす（「3,000ページを3時間で。読めるか。」）。括弧で小声の補足を入れてよい（「（誰も読んでいません）」）。
- ひらがなを多めに:「事→こと」「出来る→できる」「更に→さらに」「〜して下さい→〜してください」。
- 自虐を1箇所入れる（「こんなのを毎朝読んでいる私も私ですが」）。読者より自分を先に笑う。
- 禁止: 「〜と言えるでしょう」「〜ではないでしょうか」の連発、「まさに」「非常に」「〜することができます」などの翻訳調、説明のための説明。
- 数字や固有名詞は会話の中で自然に出す。プレゼン資料のような列挙にしない。
- 【使い古し表現の禁止（毎日同じ書き出しは読者に飽きられる）】以下は使用禁止:「声が出ました」「声が出た」「今日も読みました」「信じられますか」「〜という週です」で始める型。書き出しは毎日ちがう入り方にする。入り方の例（日替わりで型を変える）: ①一番強い事実をいきなり置く（「オンタリオ湖が今日から『レイク・アメリカ』です。」）②読者への質問から入る ③会話の再現から入る（「『それ本当？』って聞き返しました。」）④数字のツッコミから入る ⑤自分の行動から入る（「記事を閉じて、もう一度開きました。同じことが書いてありました。」）
- 【文末の温度】読者への指示形「〜してください」「〜でいてください」「〜しましょう」は使わない。友達に話す形に:「〜でいいんだよ」「〜でいいと思う」「〜だと思うよ」「〜じゃないかな」。占いの締めも命令ではなく、隣に座って言う感じで。
- 「〜んですよ」「〜ですよ」は効くが連発すると鼻につく。1つの段落に1回まで。

【導入「今日を占うよ〜 / Today's Forecast」（introJa / introEn）】
記事の顔。スタンダップコメディの開幕口上のように書く。想定読者は、ビジネスパーソン・学生・「努力が報われていない」と感じている人。構成:
1. 掴み: 今日の5本を貫く「一本の糸」を1つのジョークとして提示（2〜3文）
2. 予告: 今日の5本のテイストをちら見せする（具体的な固有名詞を1〜2個だけ出して期待を作る。全部は明かさない）
3. 占い: ニュース連動型の前向き占いをここに織り込む。「今日の5本はこんな話→つまり、あなたのその悩みはあなたのせいじゃない→だから今日は大丈夫」という論理の飛躍で、読者の自己肯定感をその日だけ少し上げて本編へ送り出す。汎用の占い文は禁止、必ずその日のニュースに絡める。星座別にしない。仕事運・恋愛運・金運のどれかに軽く触れてよい。説教とスピリチュアル用語は禁止
- 分量: introJaは日本語250〜400字(段落改行必須)、introEnは英語80〜130語
- 本文にコーナー名（「今日を占うよ〜」「Today's Forecast」）を書かない。見出しは組版時に自動で付くため、本文は1文目からいきなり始める
- introEnはThe Onionの風刺占いの伝統を意識しつつ、必ず温かく着地させる

【締め「今日のまとめジョーク/パンチライン / Today's Punchline」（quipJa / quipEn）】
5本すべてを読み終えた読者に渡す、総括の一撃。明日、職場や学校でこの話題が出たときにそのまま口に出せる一言。
- 今日の「一本の糸」を回収する内容にする（導入の占いと呼応すると美しい）
- 自己完結型（ニュースの前提を知らない相手にも通じる形）
- 1〜2文。暗記できる短さ。賢く聞こえるが、嫌味に聞こえない
- quipEnとquipJaは同じ趣旨でよいが、直訳ではなくそれぞれの言語で口に出して自然な形にする

【風刺のルール（厳守）】
- 事実は候補データに書かれているものだけを使う。新しい事実・数字・引用を発明しない。
- 風刺の対象は制度・企業・組織・社会構造・矛盾。実在個人の人格・外見・家族は対象にしない。
- 名誉毀損リスクのある断定（違法行為の断定、動機の決めつけ）はしない。
- 悲劇そのものを笑いにしない。

【SNS投稿候補】
- notesEn: Substack Notes用の英語投稿。各1〜3文。その投稿だけ読んで意味が分かる自己完結型にする（必要なニュースの前提を投稿内に含める）。ハッシュタグ・絵文字・URLなし。ペルソナの声で。
- xJa: X（旧Twitter）用の日本語投稿。各135字以内。自己完結型。ハッシュタグ・URLなし。ニュースを知らない日本の読者がそのまま笑える形にする。

【コメント弾（raidEn / raidJa）— 他人の投稿のコメント欄に置く一言】
フォロワー獲得の巡回コメント用。今日の5本それぞれに1本ずつ、計5本作る。
- raidEn: TikTok/Instagramで同じニュースを扱う他人の投稿のコメント欄に書く想定の英語1〜2文。そのコメント単体で笑えて、投稿主を立てる（投稿の否定・訂正をしない）。宣伝・ハッシュタグ・URL・「follow me」系は厳禁。プロフィールを見に来させる力はコメントの面白さだけに持たせる。
- raidJa: noteで同じ話題を扱う他人の記事のコメント欄に書く想定の日本語1〜2文。「記事を読んだ感想」として自然な、敬意＋ウィットの形。丁寧語ベース。営業・自分の記事への誘導はゼロ。

【noteタイトル3案（titleJa + titleAltJa）】
noteでは読まれるかどうかの大半がタイトルで決まる。note公式の有料記事500件分析によれば、読まれるタイトルの共通点は「具体性」「読者にとっての価値の明確さ」「トレンド性」。風刺コラムでは次のように翻訳する:
- 具体性: 固有名詞・数字をタイトルにそのまま見せ、その違和感で引く（「最近思ったこと」型の曖昧タイトルは禁止）
- 価値: 読むと何が分かるか・どんな気分になれるかが一目で伝わる
- トレンド性: その日の話題語（人名・企業名・事件）を自然に入れる
本命titleJaに加え、titleAltJaに別角度の2案を作り、型を必ず変えること:
- 1案は「固有名詞・数字の違和感」型（例: 具体的な数字がそのままオチになっている）
- 1案は「これで語れる」型（読み終えたら何をどう語れるようになるかが伝わる。例:「『5000億ドルのAI投資』の話題が出たら、この一言を返せばいい」）
- 1案は「ぼやき・本音がこぼれた」型
- 「〜話」で終える型も有効（例:「オンタリオ湖を『レイク・アメリカ』に改名して、湖は一ミリも動かなかった話」）。事実+結果のズレを一文に収めて「話」で締めると、note読者の既読感覚に馴染む
titleEn / subtitleEn も同じ思想で: 英語の副題は「この記事を読むと何が語れるようになるか」を軽く匂わせる（説教くさくせず、ウィットで）。
釣らない。本文が答えられない約束をタイトルでしない。「〜がヤバい」「衝撃の」等の摩耗した釣り語は使わない。

【出力形式（厳守）】
- 有効なJSONのみを出力する。前置き・後書き・コードフェンスは一切付けない。

JSONスキーマ:
{
  "thread": "<日本語1〜2文。今日の5本を貫く『一本の糸』（人間の編集者向けメモ）>",
  "titleEn": "<英語タイトル。ニュースレターの件名になる。punchyに、60文字以内>",
  "subtitleEn": "<英語の副題(dek)。1文・15語以内。タイトルの下に表示され、開封を後押しするフック>",
  "titleJa": "<日本語タイトル本命案。note記事の見出しになる>",
  "titleAltJa": ["<日本語タイトル別案（本命と型を変える）>", "<日本語タイトル別案（さらに別の型）>"],
  "leadJa": "<日本語。note記事の最上部(タイトル直下)に置く掴み2〜3文。タイトルの約束をすぐ回収しつつ、続きを読ませる。ペルソナの声で>",
  "introEn": "<英語80〜130語。Today's Forecast(導入)。掴み→5本の予告→ニュース連動の前向き占い。段落は\\n\\nで区切る>",
  "introJa": "<日本語250〜400字。今日を占うよ〜(導入)。掴み→5本の予告→ニュース連動の前向き占い。段落は\\n\\nで区切る>",
  "quipEn": "<英語1〜2文。Today's Punchline(締めの総括の一撃)>",
  "quipJa": "<日本語1〜2文。今日のパンチライン。口に出して自然な形>",
  "notesEn": ["<英語>", "...", "...", "...", "...", "...", "...", "..."],
  "xJa": ["<日本語>", "...", "...", "...", "...", "...", "...", "..."],
  "raidEn": ["<英語・ニュース1対応>", "<ニュース2対応>", "<ニュース3対応>", "<ニュース4対応>", "<ニュース5対応>"],
  "raidJa": ["<日本語・ニュース1対応>", "<ニュース2対応>", "<ニュース3対応>", "<ニュース4対応>", "<ニュース5対応>"]
}"""


def _recent_openings(limit: int = 3) -> list[str]:
    """直近のアーカイブから日本語の書き出しを集める（マンネリ防止用）。"""
    snippets = []
    try:
        files = sorted(ARCHIVE_DIR.glob("*.json"))[-limit:]
        for f in files:
            try:
                ed = json.loads(f.read_text(encoding="utf-8")).get("editorial") or {}
                for key in ("leadJa", "introJa"):
                    t = (ed.get(key) or "").strip()
                    if t:
                        first = t.split("\n", 1)[0][:60]
                        if first:
                            snippets.append(first)
            except Exception:
                continue
    except Exception:
        pass
    return snippets


def build_editorial_prompt(candidates: list[dict], today: str) -> str:
    """5候補を圧縮した素材リストにして渡す。"""
    lines = [f"本日 {today} の確定済み候補5本です。この素材だけを使って、"
             "指定スキーマのJSONだけを出力してください。\n"]
    recent = _recent_openings()
    if recent:
        lines.append("【昨日までの書き出し（重要：以下と似た書き出し・言い回しを今日使うのは禁止。"
                     "特に同じ決まり文句の再利用は読者に「またこれか」と思われる）】")
        for s in recent:
            lines.append(f"  ×「{s}」")
        lines.append("")
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


def _strip_corner_label(text: str, labels: tuple) -> str:
    """本文1行目がコーナー名の重複だったら取り除く。"""
    first, _, rest = text.partition("\n")
    key = first.strip().strip("#*:： 　〜~")
    for lb in labels:
        if key.lower() == lb.strip("〜~").lower() and rest.strip():
            return rest.strip()
    return text


def validate_editorial(data: dict, candidates: list[dict]) -> dict:
    """編集室データの検証・正規化。壊れた候補はここで弾く。"""
    thread = _req_str(data.get("thread"), "editorial: thread", 8)
    title_en = _req_str(data.get("titleEn"), "editorial: titleEn", 8)
    title_ja = _req_str(data.get("titleJa"), "editorial: titleJa", 5)
    intro_en = _req_str(data.get("introEn"), "editorial: introEn", 120)
    intro_ja = _req_str(data.get("introJa"), "editorial: introJa", 100)
    # 本文冒頭にコーナー名が重複していたら除去（見出しは組版で自動付与されるため）
    intro_en = _strip_corner_label(intro_en, ("today's forecast",))
    intro_ja = _strip_corner_label(intro_ja, ("今日を占うよ〜", "今日を占うよ", "今日を占う"))

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

    # コメント弾（任意項目・無くても失敗にしない）
    raid_en = data.get("raidEn")
    raid_en = ([r.strip() for r in raid_en
                if isinstance(r, str) and len(r.strip()) >= 15][:5]
               if isinstance(raid_en, list) else [])
    raid_ja = data.get("raidJa")
    raid_ja = ([r.strip() for r in raid_ja
                if isinstance(r, str) and len(r.strip()) >= 10][:5]
               if isinstance(raid_ja, list) else [])

    return {
        "thread": thread,
        "titleEn": title_en,
        "subtitleEn": subtitle_en,
        "titleJa": title_ja,
        "titleAltJa": title_alt,
        "leadJa": lead_ja,
        "introEn": intro_en,
        "introJa": intro_ja,
        "quipEn": quip_en,
        "quipJa": quip_ja,
        "notesEn": notes[:NUM_NOTES],
        "xJa": x_posts[:NUM_X_POSTS],
        "raidEn": raid_en,
        "raidJa": raid_ja,
    }


def _strip_b(s: str) -> str:
    """commentary内の<b>タグを除去してプレーン化。"""
    return re.sub(r"</?b>", "", str(s)).strip()


def assemble_full_text(editorial: dict, candidates: list[dict]) -> None:
    """モジュール構成のMarkdown下書きを組み立てる。
    構成: 今日を占うよ〜(導入) → ニュース5ブロック(概要→どこが笑える？→ジョーク) → 今日のまとめジョーク/パンチライン"""

    # --- 英語版 ---
    parts_en = [f"# {editorial['titleEn']}", "",
                "## Today's Forecast", "",
                editorial["introEn"], "", "---", ""]
    for i, c in enumerate(candidates, start=1):
        n = c["news"]
        irony = (c.get("ironyEn") or [{}])[0]
        parts_en += [f"## {i}. {n['headline']}", "",
                     f"*{n['source']} — [source]({n['url']})*", "",
                     c.get("newsEn", ""), "",
                     "**Why It's Funny**", ""]
        for label, key in (("Contradiction", "contradiction"),
                           ("Absurdity", "absurdity"),
                           ("View from Tokyo", "outside")):
            v = irony.get(key)
            if v:
                parts_en += [f"- **{label}:** {v}"]
        cap = (c.get("captions") or [""])[0]
        parts_en += ["", "**Say It Out Loud**", "", f"> {cap}", "", "---", ""]
    parts_en += ["## Today's Punchline", "", f"> {editorial['quipEn']}", ""] \
        if editorial.get("quipEn") else []
    editorial["fullEn"] = "\n".join(parts_en).strip() + "\n"

    # --- 日本語版 ---
    parts_ja = [f"# {editorial['titleJa']}", "",
                "## 今日を占うよ〜", "",
                editorial["introJa"], "", "---", ""]
    for i, c in enumerate(candidates, start=1):
        n = c["news"]
        parts_ja += [f"## {i}. {n['headline']}", "",
                     f"*{n['source']}（[記事]({n['url']})）*", "",
                     n.get("summary", ""), "",
                     "**どこが笑える？**", ""]
        for point in (c.get("commentary") or []):
            parts_ja += [f"- {_strip_b(point)}"]
        cap_ja = (c.get("captionsJa") or [""])[0]
        parts_ja += ["", "**このニュースをジョークにするなら...**", "", f"> {cap_ja}", "", "---", ""]
    parts_ja += ["## 今日のまとめジョーク/パンチライン", "", f"> {editorial['quipJa']}", ""] \
        if editorial.get("quipJa") else []
    parts_ja += ["今日も読んでくれてありがとうございます。また明日の朝、ここで。", ""]
    editorial["fullJa"] = "\n".join(parts_ja).strip() + "\n"


def generate_editorial(client: Anthropic, candidates: list[dict],
                       today: str) -> dict | None:
    """編集室データを生成する。失敗したら None を返す（本体の生成は止めない）。"""
    if not EDITORIAL_ENABLED:
        print("[info] EDITORIAL_ENABLED=0 — skipping editorial generation")
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
7. 日本語の話し言葉: introJa・leadJa・quipJa等が「書き言葉」になっていたら書き直す。短い段落（1〜3文で改行、空行区切り）、会話の接続（「で、」「いや、」）、言い直し、自問自答、文末の散らし。同じ文末が3回続いたら不合格。
8. 導入「今日を占うよ〜」(introJa/introEn)の検品: 掴み→5本の予告→ニュース連動の前向き占い、の3要素が揃い、読者(報われないと感じている人)を元気づけて本編へ送り出せているか。
9. SNS単体成立の検品（notesEn・xJa・quipEn・quipJa）: これらは記事の外で、前提ゼロのままスクロール中に読まれる。3基準で検品する——①面白いか（読む手が止まるか。説明で終わっていたらオチを足す）②事実が元データと一致するか（数字の盛り・言い過ぎ・出典にない断定は即修正）③単体で文章として成立するか（ニュースを知らない読者が一読で意味を取れるか。主語の欠落・指示語の宙吊り・不自然な文法を直す）。3つのどれかを欠く投稿は書き直す。

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
    "quipEn": "<同>", "quipJa": "<同>",
    "notesEn": ["<変更する場合のみ・8本すべて>"], "xJa": ["<同・8本すべて>"],
    "introEn": "<変更する場合のみ・導入の全文>",
    "introJa": "<変更する場合のみ・導入の全文>"
  }
}
- candidatePatches: 直す候補だけを入れる。フィールドも直すものだけ（captionsだけ、imagePromptsだけ、でよい）。
- introEn/introJaを直す場合は導入の全文を出す。
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
                       "leadJa", "introEn", "introJa",
                       "quipEn", "quipJa", "notesEn", "xJa")},
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
                   "leadJa", "introEn", "introJa", "quipEn", "quipJa",
                   "notesEn", "xJa")
    for k in simple_keys:
        if k in patch and patch[k] is not None:
            merged[k] = patch[k]
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
# 3.4. 日本語文体パス（AI感ハンター）
#   - 完成した日本語稿だけを読み、「AIが書いたと感じさせる文」を特定して
#     その文だけをラフな話し言葉に差し替える専門パス
#   - 差分パッチ方式（_merge_editorial_patch を再利用）。失敗しても本体は止めない
# ----------------------------------------------------------------

POLISH_JA_SYSTEM_PROMPT = """あなたは「AI感ハンター」。日本語の完成原稿を一文ずつ声に出して読み、「AIが書いた」と感じさせる文だけを見つけ出し、その文だけをラフな話し言葉に書き直す専門職です。全体を書き直してはいけない。すでに人間らしい文は1文字も触らない。

【AI感の兆候（これを探す）】
1. 翻訳調:「〜することができます」「〜と言えるでしょう」「〜に他なりません」「〜が求められています」
2. 説明口調・プレゼン口調: 綺麗に要約した文、「つまり」「そして」「しかし」で几帳面に接続された文
3. 整いすぎ: 対句、三段並列、同じ長さの文の連続、同じ文末の連続（です。です。です。）
4. 漢語密度が高い: 名詞が連続する硬い文（「保育費補助制度の待機児童問題の深刻化」）
5. 主語と理屈が几帳面すぎる: 話し言葉なら省略するはずの主語・接続が全部書いてある
6. 「うまくまとめた感」のある締め: 気の利いた総括で綺麗に着地しようとする文
7. 過剰な丁寧さ: 距離を感じる敬語、営業スマイルのような文
8. 使い古しの決まり文句:「声が出ました」「今日も読みました」「信じられますか」。見つけたら別の入り方に書き直す
9. 読者への指示形:「〜してください」「〜でいてください」「〜しましょう」→「〜でいいんだよ」「〜だと思うよ」など、隣に座って話す形に直す

【直し方（見つけた文だけに適用）】
- 声に出して、友達に話すならどう言うかに置き換える
- 途中で切る。「制度は存在するが需要に追いついていない」→「制度はある。満員なだけ。」
- 理屈をツッコミに変える。「この矛盾について考える必要があります」→「何なんですかね、これ。」
- 漢語をひらく。「深刻化している」→「どんどんひどくなってる」
- 完璧な締めを、本音がこぼれた形に崩す
- 段落は1〜3文+空行（\\n\\n）のリズムを守る

【出力形式（差分方式・厳守）】
直した箇所だけをJSONで出力。全て合格なら patch は {} とする。有効なJSONのみ、前置き禁止。
{
  "notes": "<日本語1〜2文。何箇所直したか、代表的な修正例を1つ>",
  "patch": {
    "titleJa": "<直す場合のみ>", "titleAltJa": ["<直す場合のみ・全案>"],
    "leadJa": "<直す場合のみ>", "quipJa": "<同>", "introJa": "<直す場合のみ・導入の全文>",
    "xJa": ["<直す場合のみ・8本すべて>"]
  }
}
※introJaを直す場合は導入の全文を出す。事実・ジョークの内容は変えない。文体だけを直す。"""


def polish_japanese(client: Anthropic, candidates: list[dict],
                    editorial: dict | None) -> dict | None:
    """第三パス: 日本語のAI感を除去。失敗したらそのまま返す。"""
    if not POLISH_JA_ENABLED or editorial is None:
        return editorial
    try:
        material = {k: editorial.get(k) for k in
                    ("titleJa", "titleAltJa", "leadJa", "quipJa",
                     "introJa", "xJa")}
        print(f"[info] Japanese style pass (AI-scent hunter, model={REVIEW_MODEL})")
        message = client.messages.create(
            model=REVIEW_MODEL,
            max_tokens=POLISH_JA_MAX_TOKENS,
            system=POLISH_JA_SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": "本日の日本語完成稿です。AI感の残る文を特定し、"
                                  "指定スキーマのJSONだけを出力してください。\n\n"
                                  + json.dumps(material, ensure_ascii=False, indent=1)}],
        )
        text = "".join(b.text for b in message.content if b.type == "text")
        usage = getattr(message, "usage", None)
        if usage:
            print(f"[info] polish tokens: in={usage.input_tokens} out={usage.output_tokens}")
        data = extract_json(text)
        allowed = {"titleJa", "titleAltJa", "leadJa", "quipJa",
                   "introJa", "xJa"}
        patch = {k: v for k, v in (data.get("patch") or {}).items() if k in allowed}
        new_editorial = editorial
        if patch:
            try:
                saved_notes = editorial.get("reviewNotes", "")
                new_editorial = _merge_editorial_patch(editorial, patch, candidates)
                assemble_full_text(new_editorial, candidates)
                if saved_notes:
                    new_editorial["reviewNotes"] = saved_notes
                print(f"[info] polish: patched {', '.join(sorted(patch.keys()))}")
            except Exception as e:
                print(f"[warn] polish patch rejected (keeping previous): {e}", file=sys.stderr)
                new_editorial = editorial
        notes = data.get("notes")
        if isinstance(notes, str) and notes.strip():
            prev = new_editorial.get("reviewNotes", "")
            new_editorial["reviewNotes"] = (prev + " ／ 文体パス: " + notes.strip()) if prev \
                else "文体パス: " + notes.strip()
        print("[ok] Japanese style pass done")
        return new_editorial
    except Exception as e:
        print(f"[warn] Japanese style pass failed (keeping previous): {e}", file=sys.stderr)
        return editorial


# ----------------------------------------------------------------
# 3.6. 記事用画像へのPunchy合成
#   - 風刺画の原画にPunchyのリアクションをステッカー風に合成し、
#     note/Substack記事の埋め込み用画像(candidate-N-punchy.jpg)を作る
# ----------------------------------------------------------------

def generate_article_images(candidates: list[dict], today: str) -> None:
    """各候補の画像にPunchyを合成した記事用バージョンを生成。失敗しても本体は止めない。"""
    if not HAS_PIL:
        return
    from PIL import Image
    try:
        reactions = []
        for i in range(1, 6):
            p = ROOT / "images" / f"reaction-{i}.png"
            if p.exists():
                try:
                    reactions.append(Image.open(p).convert("RGBA"))
                except Exception:
                    pass
        if not reactions:
            print("[info] no reaction sprites — article images keep raw art")
            return
        try:
            off = int(today[8:10]) % len(reactions)
        except Exception:
            off = 0
        angles = (-11, 9, -9, 12, -10)
        done = 0
        for idx, c in enumerate(candidates):
            if not c.get("image"):
                continue
            src = ROOT / c["image"]
            if not src.exists():
                continue
            art = Image.open(src).convert("RGBA")
            r = reactions[(idx + off) % len(reactions)]
            size = max(220, art.width // 5)
            s = r.resize((size, size), Image.LANCZOS).rotate(
                angles[idx % 5], expand=True, resample=Image.BICUBIC)
            art.paste(s, (art.width - s.width + size // 6, 20), s)
            out = IMAGES_DIR / today / f"candidate-{idx + 1}-punchy.jpg"
            art.convert("RGB").save(out, "JPEG", quality=88)
            c["imagePunchy"] = f"images/{today}/candidate-{idx + 1}-punchy.jpg"
            done += 1
        print(f"[ok] article images with Punchy: {done}/{len(candidates)}")
    except Exception as e:
        print(f"[warn] article image compositing failed (raw art will be used): {e}",
              file=sys.stderr)


# ----------------------------------------------------------------
# 3.65. 記事の見出し画像（note用アイキャッチ / Substackカバー）
#   - 5枚の風刺画をコラージュし、その日のタイトルを帯に載せる
#   - note: 1280x670(日本語) / Substack: 1200x630(英語)
# ----------------------------------------------------------------

def _cover_crop(img, w: int, h: int):
    """中央クロップでw×hにフィットさせる。"""
    from PIL import Image
    ratio = max(w / img.width, h / img.height)
    r = img.resize((int(img.width * ratio) + 1, int(img.height * ratio) + 1),
                   Image.LANCZOS)
    x = (r.width - w) // 2
    y = (r.height - h) // 2
    return r.crop((x, y, x + w, y + h))


def _collage_base(candidates, W: int, H: int):
    """5枚モザイク背景（左に大1枚+右に2x2）。欠けはクリームで埋める。"""
    from PIL import Image
    base = Image.new("RGB", (W, H), (243, 237, 224))
    arts = []
    for c in candidates[:5]:
        p = ROOT / c["image"] if c.get("image") else None
        if p and p.exists():
            try:
                arts.append(Image.open(p).convert("RGB"))
                continue
            except Exception:
                pass
        arts.append(None)
    lw = W // 2
    cw, ch = W - lw, H // 2
    slots = [(0, 0, lw, H), (lw, 0, cw // 2, ch), (lw + cw // 2, 0, cw - cw // 2, ch),
             (lw, ch, cw // 2, H - ch), (lw + cw // 2, ch, cw - cw // 2, H - ch)]
    for art, (x, y, w, h) in zip(arts, slots):
        if art:
            base.paste(_cover_crop(art, w - 2, h - 2), (x + 1, y + 1))
    return base


def _wrap_cjk(draw, text: str, font, max_width: int) -> list[str]:
    """CJK対応の文字単位折り返し。"""
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_width:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def generate_article_headers(candidates: list[dict], editorial: dict | None,
                             today: str) -> dict:
    """note用アイキャッチとSubstackカバーを生成。失敗時は空dict。"""
    if not HAS_PIL or not editorial:
        return {}
    from PIL import Image, ImageDraw, ImageFont
    out: dict = {}
    try:
        mascot = None
        mp = ROOT / "images" / "mascot.png"
        if mp.exists():
            try:
                mascot = Image.open(mp).convert("RGBA")
            except Exception:
                mascot = None

        def compose(W, H, title, brand, jp: bool, fname: str):
            img = _collage_base(candidates, W, H)
            d = ImageDraw.Draw(img, "RGBA")
            text_w = W - 120 - (240 if mascot else 0)

            # タイトルの長さに応じてフォントサイズを自動調整（2行以内を優先、最大3行）
            def load(size):
                return (ImageFont.truetype(FONT_DIR_CJK, size, index=0) if jp
                        else ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", size))
            f_title, lines = None, []
            for size in ((72, 60, 50) if jp else (62, 52, 44)):
                f_title = load(size)
                lines = (_wrap_cjk(d, title, f_title, text_w) if jp
                         else _wrap_text(d, title, f_title, text_w))
                if len(lines) <= (2 if size > 55 else 3):
                    break
            lines = lines[:3]
            line_h = f_title.size + 12

            # 帯の高さを行数から逆算（タイトル+ブランド行が必ず収まる）
            band_top = H - (34 + len(lines) * line_h + 18 + 46 + 24)
            band_top = max(int(H * 0.34), band_top)
            d.rectangle([0, band_top, W, H], fill=(246, 241, 231, 240))
            d.rectangle([0, band_top, W, band_top + 8], fill=(205, 107, 87, 255))
            y = band_top + 34
            for ln in lines:
                d.text((60, y), ln, font=f_title, fill=(69, 63, 54))
                y += line_h
            f_small = (ImageFont.truetype(FONT_DIR_CJK, 28, index=0) if jp
                       else ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 26))
            d.text((60, H - 48), brand, font=f_small, fill=(95, 114, 145))
            if mascot:
                mh = min(230, H - band_top - 20)
                s = mascot.resize((mh, mh), Image.LANCZOS).rotate(
                    -8, expand=True, resample=Image.BICUBIC)
                img.paste(s, (W - s.width - 18, H - s.height - 14), s)
            path = IMAGES_DIR / today / fname
            img.save(path, "JPEG", quality=90)
            return f"images/{today}/{fname}"

        title_ja = editorial.get("titleJa") or ""
        title_en = editorial.get("titleEn") or ""
        if title_ja and not os.path.exists(FONT_DIR_CJK):
            print("[warn] CJK font missing — skipping note header "
                  "(add fonts-noto-cjk to the workflow)", file=sys.stderr)
            title_ja = ""
        if title_ja:
            out["note"] = compose(1280, 670, title_ja,
                                  "明日使えるアメリカンジョーク", True, "note-header.jpg")
        if title_en:
            out["substack"] = compose(1200, 630, title_en,
                                      "JOKES YOU CAN USE — the view from Tokyo",
                                      False, "substack-cover.jpg")
        print(f"[ok] article header images: {', '.join(out.keys()) or 'none'}")
    except Exception as e:
        print(f"[warn] header image generation failed: {e}", file=sys.stderr)
    return out


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
FONT_DIR_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"  # 日本語(要fonts-noto-cjk)
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

        def _paste_sprite(target, sprite, size, angle, x, y):
            """Punchyをステッカー風に配置(拡大+回転+端からのはみ出しOK)。"""
            s = sprite.resize((size, size), Image.LANCZOS)
            s = s.rotate(angle, expand=True, resample=Image.BICUBIC)
            target.paste(s, (x, y), s)
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
        _slide_footer(d, f"1/{total}", today)
        if mascot:
            # 表紙: 大きく傾けて右下の端からはみ出させる(フッターの上まで)
            _paste_sprite(img, mascot, 430, -12, CAROUSEL_W - 390, CAROUSEL_H - 610)
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
            # Punchyのリアクション(記事番号+日付で表情ローテーション、左右交互に傾く)
            if reactions:
                rr = reactions[(i - 1 + reaction_offset) % len(reactions)]
                angle = (-11, 9, -9, 12, -10)[(i - 1) % 5]
                _paste_sprite(img, rr, 270, angle, CAROUSEL_W - 260, 96)
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
        _slide_footer(d, f"{total}/{total}", today)
        if mascot_cta:
            # CTA: コーヒー版を大きく傾けて右下に(カップが見えるよう内側に寄せる)
            _paste_sprite(img, mascot_cta, 400, 8, CAROUSEL_W - 440, CAROUSEL_H - 580)
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
                  carousel: list[str] | None = None,
                  headers: dict | None = None) -> None:
    daily = {
        "version": 1,
        "date": today,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "auto (rss + claude)",
        "candidates": candidates,
        "editorial": editorial,  # モノローグ下書き+SNS候補（生成失敗時は null）
        "carousel": carousel or [],  # TikTok/Instagram用カルーセル画像の相対パス
        "headers": headers or {},  # 見出し画像 {note: path, substack: path}
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
            # 三段階目: 日本語文体パス（AI感ハンター）
            editorial = polish_japanese(client, candidates, editorial)
            # 画像生成は「おまけ」扱い: 全滅しても daily.json は出す
            try:
                generate_images(candidates, today)
            except Exception as e:
                print(f"[warn] image stage failed entirely (placeholders will be shown): {e}",
                      file=sys.stderr)
            # 記事用画像にPunchyを合成（note/Substack埋め込み用）
            generate_article_images(candidates, today)
            # 見出し画像（noteアイキャッチ / Substackカバー）
            headers = generate_article_headers(candidates, editorial, today)
            # カルーセル組版（風刺画の後。失敗しても本体は止めない）
            carousel = generate_carousel(candidates, editorial, today)
            write_outputs(candidates, today, editorial, carousel, headers)
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
