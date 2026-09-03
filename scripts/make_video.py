#!/usr/bin/env python3
"""毎日の縦動画(v4)を自動生成する。
構成: Punchyのぴょこん登場+短い宣言 → ニュース5本(概要→ハンコ「バンッ」+真顔+キラリ→ジョーク) → 締め+CTA

- 音声: edge-tts(無料)。テンポ高め(EN +22% / JA +14%)、イントロはさらに高速。
  TTSの前後の無音は自動カットしてテンポを詰める
- ジョークは「ハンコ風スタンプ」(朱肉レッド・二重枠・インクのかすれ)で風刺画の上に押される
- ハンコが押れた瞬間、右上のキャラが真顔(mascot.png)に変わり、キラリと光る
- 効果音: ffmpegで自前合成(登場ポップ音・区切りのチーン・締めのタダー)。著作権フリー
- BGM: リポジトリに assets/bgm.mp3 があれば小音量(10%)で自動ミックス(任意)
- FAKE_TTS=1 で無音ダミー(テスト用)
"""

from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / "daily.json"
BGM = ROOT / "assets" / "bgm.mp3"

LANG = os.environ.get("VIDEO_LANG", "en")   # "en" or "ja"
# en-US-GuyNeural: TikTok系ミーム動画で定番の、明るくエネルギッシュな「友達の声」
_DEFAULT_VOICES = {"en": "en-US-GuyNeural", "ja": "ja-JP-KeitaNeural"}
VOICE = os.environ.get("VIDEO_VOICE") or _DEFAULT_VOICES.get(LANG, _DEFAULT_VOICES["en"])
RATE = os.environ.get("VIDEO_RATE", "+22%" if LANG == "en" else "+14%")
PITCH = os.environ.get("VIDEO_PITCH", "+2Hz" if LANG == "en" else "+0Hz")
INTRO_RATE = os.environ.get("VIDEO_INTRO_RATE", "+28%" if LANG == "en" else "+20%")
FAKE_TTS = os.environ.get("FAKE_TTS") == "1"
# 動画に入れる本数(既定3本)。残りは記事へ誘導する「読みたい」ギャップにする
STORIES = max(1, min(5, int(os.environ.get("VIDEO_STORIES", "3"))))
MIN_TOTAL_SEC = 63.0
MAX_SEG_PAD = 1.6
KEEP_DAYS = 3
BG_COLOR = "0x33302B"
CREAM = (247, 243, 234)
TEAL = (84, 167, 156)
INK = (51, 48, 43)
CORAL = (224, 122, 95)
FPS = 30

# ハンコ(スタンプ)の色: 朱肉レッド + 薄い紙下地(絵の上でも読めるように)
STAMP_INK = (176, 42, 46)
STAMP_PAPER = (250, 246, 236)
# カルーセルのスライド寸法と、リアクション画像の配置(generate_daily.pyと同じ値)
SLIDE_W, SLIDE_H = 1080, 1350
REACT_POS, REACT_SIZE = (820, 96), 270
REACT_ANGLES = (-11, 9, -9, 12, -10)
STAMP_ANGLES = (-7, 6, -8, 7, -6)

# アサイドが生成に無い日のフォールバック(日替わりでズレる)
FALLBACK_ASIDES_EN = ["Incredible.", "And nobody blinked.",
                      "This is fine. Totally fine.",
                      "You can't write this stuff.",
                      "Democracy, ladies and gentlemen."]
FALLBACK_ASIDES_JA = ["いやあ、皮肉だね。", "笑うしかないでしょ、これ。",
                      "誰か止めなかったの？", "脚本なしでこれだよ。",
                      "アメリカ、今日も平常運転。"]

# ジョーク直前の「助走」(今から笑いどころだよ、の合図。日替わり+記事ごとにローテ)
JOKE_CUES_EN = ["Now, here's the joke.", "Say it with me:",
                "And the punchline?", "Here's your line for tomorrow:",
                "All together now:"]
JOKE_CUES_JA = ["で、ここからが笑いどころ。", "はい、ジョークいくよ。",
                "明日使うならこれ。", "ここ、笑うとこね。",
                "じゃあ、一言でまとめるよ。"]


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd[:8])}...\n{p.stderr[-800:]}")


def probe_duration(path: Path) -> float:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    return float(p.stdout.strip())


def clean_for_speech(s: str) -> str:
    s = re.sub(r"[\"“”‘’]", "", str(s))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def first_sentences(s: str, max_sentences: int, max_chars: int) -> str:
    """ニュース文を先頭N文・上限文字数に圧縮(ショート3分制限対策)。"""
    s = clean_for_speech(s)
    parts = re.split(r"(?<=[.!?。！？])\s+", s)
    out = " ".join(parts[:max_sentences]).strip()
    if len(out) > max_chars:
        cut = out[:max_chars]
        # 文の途中で切らない(最後の句点まで)
        m = re.search(r"^(.+[.!?。！？])", cut)
        out = m.group(1) if m else cut
    return out


async def tts_to_file(text: str, out: Path, rate: str) -> None:
    if FAKE_TTS:
        dur = max(1.5, min(14.0, len(text) * 0.05))
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", f"{dur:.2f}", "-c:a", "libmp3lame", "-q:a", "9", str(out)])
        return
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, VOICE, rate=rate, pitch=PITCH)
    except TypeError:  # 古いedge-ttsはpitch未対応
        comm = edge_tts.Communicate(text, VOICE, rate=rate)
    await comm.save(str(out))


def trim_silence(src: Path) -> Path:
    """TTSの前後の無音をカットしてテンポを詰める(失敗したら元のまま)。"""
    if FAKE_TTS:
        return src  # ダミー音声は全部無音なので触らない
    out = src.with_name(src.stem + "-trim.mp3")
    try:
        run(["ffmpeg", "-y", "-i", str(src), "-af",
             ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.04,"
              "areverse,"
              "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.10,"
              "areverse"),
             "-c:a", "libmp3lame", "-q:a", "4", str(out)])
        if probe_duration(out) >= 0.3:
            return out
    except Exception as e:
        print(f"[warn] silence trim failed: {e}", file=sys.stderr)
    return src


# ---------- 効果音の自前合成(著作権フリー) ----------

def synth_sfx(tmp: Path) -> dict[str, Path]:
    """登場ポップ・区切りチーン・締めタダーを合成する。"""
    sfx = {}
    specs = {
        # ぴょこん(上昇スイープ)
        "pop": "aevalsrc=0.55*sin(2*PI*(350+1900*t)*t)*exp(-9*t):d=0.42:s=44100",
        # チーン(2音の鐘)
        "ding": ("aevalsrc=0.4*sin(2*PI*1318*t)*exp(-8*t)"
                 "+0.25*sin(2*PI*1976*t)*exp(-10*t):d=0.4:s=44100"),
        # タダー(2音上がり)
        "tada": ("aevalsrc=0.45*sin(2*PI*784*t)*exp(-5*t)"
                 "+0.45*sin(2*PI*1175*max(t-0.14\\,0))*exp(-5*max(t-0.14\\,0)):d=0.6:s=44100"),
        # バンッ(スタンプを押す衝撃音: 低音の打撃+短いクリック)
        "stamp": ("aevalsrc=0.95*sin(2*PI*82*t)*exp(-14*t)"
                  "+0.35*sin(2*PI*240*t)*exp(-30*t):d=0.4:s=44100"),
    }
    for name, expr in specs.items():
        out = tmp / f"sfx_{name}.wav"
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", expr, str(out)])
        sfx[name] = out
    return sfx


def concat_audio(parts: list[Path], out: Path) -> None:
    """SFX+ナレーションを1本の音声に連結(サンプルレート統一)。"""
    inputs = []
    for p in parts:
        inputs += ["-i", str(p)]
    labels = "".join(f"[a{i}]" for i in range(len(parts)))
    fmt = ";".join(
        f"[{i}:a]aformat=sample_rates=44100:channel_layouts=mono[a{i}]"
        for i in range(len(parts)))
    run(["ffmpeg", "-y", *inputs, "-filter_complex",
         f"{fmt};{labels}concat=n={len(parts)}:v=0:a=1[out]",
         "-map", "[out]", str(out)])


# ---------- ハンコ風スタンプ + 真顔すり替え + キラリ(PIL) ----------

def _pil_font(size: int, ja: bool):
    from PIL import ImageFont
    cands = (["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"] if ja
             else ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"])
    for p in cands:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    """空白区切りで折り返し。空白が無い日本語は文字単位で折り返す。"""
    words = text.split(" ")
    joiner = " "
    if len(words) <= 1:
        words = list(text)
        joiner = ""
    lines, cur = [], ""
    for w in words:
        t = (cur + joiner + w) if cur else w
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_stamp(caption: str, angle: float, tmp: Path, idx: int) -> Path:
    """ジョークを「ハンコ」として描く: 朱肉レッドの二重枠 + インクのかすれ + 薄い紙下地。"""
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    ja = LANG == "ja"
    W, pad_x = 920, 60
    f = _pil_font(46 if ja else 50, ja)
    probe = ImageDraw.Draw(Image.new("RGBA", (W, 10)))
    lines = _wrap(probe, caption, f, W - pad_x * 2)
    lh = f.size + 14
    H = 128 + len(lines) * lh

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 紙の下地(半透明) — 風刺画の上でも文字が読めるように
    d.rounded_rectangle([4, 4, W - 5, H - 5], radius=20,
                        fill=STAMP_PAPER + (215,))
    # ハンコの二重枠: 外は太く、内は細く
    d.rounded_rectangle([10, 10, W - 11, H - 11], radius=16,
                        outline=STAMP_INK + (255,), width=13)
    d.rounded_rectangle([34, 34, W - 35, H - 35], radius=8,
                        outline=STAMP_INK + (255,), width=5)
    y = (H - len(lines) * lh) // 2 + 2
    for ln in lines:
        w = d.textlength(ln, font=f)
        d.text(((W - w) / 2, y), ln, font=f, fill=STAMP_INK + (255,))
        y += lh
    # インクのかすれ: 細かい斑点 + 大きめのムラ(押しの強弱)
    fine = Image.effect_noise((W, H), 62).point(lambda v: 0 if v > 193 else 255)
    blotch = (Image.effect_noise((max(2, W // 7), max(2, H // 7)), 70)
              .resize((W, H), Image.BILINEAR)
              .point(lambda v: 150 if v > 176 else 255))
    a = img.getchannel("A")
    a = ImageChops.multiply(a, fine.convert("L"))
    a = ImageChops.multiply(a, blotch.convert("L"))
    a = a.filter(ImageFilter.GaussianBlur(0.6))
    img.putalpha(a)
    img = img.rotate(angle, expand=True, resample=Image.BICUBIC)
    out = tmp / f"stamp{idx}.png"
    img.save(out)
    return out


def build_slam_frame(plain_path: Path, caption: str, story_i: int,
                     tmp: Path) -> tuple[Path, tuple[int, int]]:
    """バンッの画を作る: ①右上のキャラを真顔(mascot.png)に交換 ②ハンコを押す。
    返り値: (フレーム画像, キラリの表示位置(1080x1920の動画座標系の中心))"""
    import math
    from PIL import Image
    img = Image.open(plain_path).convert("RGB")
    angle = REACT_ANGLES[story_i % 5]
    # 元のリアクション(270px・回転あり)の中心を計算し、
    # ひと回り大きい真顔を同じ角度で上から重ねて完全に覆う
    rad = math.radians(abs(angle))
    w1 = REACT_SIZE * (math.cos(rad) + math.sin(rad))
    cx, cy = REACT_POS[0] + w1 / 2, REACT_POS[1] + w1 / 2
    mp = ROOT / "images" / "mascot.png"
    m = Image.open(mp).convert("RGBA").resize((352, 352), Image.LANCZOS)
    m = m.rotate(angle, expand=True, resample=Image.BICUBIC)
    img.paste(m, (int(cx - m.width / 2), int(cy - m.height / 2)), m)
    # ハンコを風刺画のど真ん中へ
    stamp = Image.open(build_stamp(caption, STAMP_ANGLES[story_i % 5],
                                   tmp, story_i))
    img.paste(stamp, (SLIDE_W // 2 - stamp.width // 2,
                      460 - stamp.height // 2), stamp)
    out = tmp / f"slam{story_i}.png"
    img.save(out)
    # キラリの位置: 真顔の左上あたり。スライド座標→動画(1080x1920)座標へ
    # (上下レターボックス+285、スタンプ直後の平均ズーム1.08を補正)
    spx, spy = cx - 200, cy - 95
    vx = int(spx * 1.08 - 43)
    vy = int((spy + 285) * 1.08 - 77)
    return out, (vx, vy)


def build_sparkle(tmp: Path) -> Path:
    """キラリ(白い四芒星)を描く。動画側で回転+フェードさせる。"""
    from PIL import Image, ImageDraw, ImageFilter
    S = 220
    c = S // 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(c, c - 96), (c + 16, c), (c, c + 96), (c - 16, c)],
              fill=(255, 255, 255, 235))
    d.polygon([(c - 96, c), (c, c + 16), (c + 96, c), (c, c - 16)],
              fill=(255, 255, 255, 235))
    d.ellipse([c - 14, c - 14, c + 14, c + 14], fill=(255, 255, 240, 255))
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    out = tmp / "sparkle.png"
    img.save(out)
    return out


# ---------- イントロカード(PIL) ----------

def build_intro_card(tmp: Path) -> Path:
    """ブランドカード(1080x1920)を描画。文言は言語別。"""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 26], fill=TEAL)
    d.rectangle([0, H - 26, W, H], fill=TEAL)

    def font(path_candidates, size):
        for p in path_candidates:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        return ImageFont.load_default()

    cjk = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]
    latin = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]

    if LANG == "ja":
        f_small = font(cjk, 54)
        f_big = font(cjk, 120)
        lines = [("ニュースは、", f_small, INK, 560),
                 ("笑おう。", f_big, CORAL, 640),
                 ("今日の5本、いくよ！", f_small, INK, 820)]
    else:
        f_small = font(latin, 58)
        f_big = font(latin, 108)
        lines = [("DON'T JUST READ", f_small, INK, 520),
                 ("THE NEWS.", f_small, INK, 600),
                 ("LAUGH AT IT.", f_big, CORAL, 700),
                 ("5 stories. Go.", f_small, INK, 880)]
    for text, f, color, y in lines:
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, y), text, font=f, fill=color)
    out = tmp / "intro_card.png"
    img.save(out)
    return out


def build_cta_card(tmp: Path, remaining: int) -> Path:
    """締めのお誘いカード(1080x1920)。Punchyがバウンドで乗る。"""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 26], fill=TEAL)
    d.rectangle([0, H - 26, W, H], fill=TEAL)

    def font(paths, size):
        for p in paths:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        return ImageFont.load_default()

    cjk = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]
    latin = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]

    if LANG == "ja":
        lines = [(f"残りの{remaining}本は", font(cjk, 64), INK, 480),
                 ("note で", font(cjk, 140), CORAL, 580),
                 ("待ってるよ", font(cjk, 64), INK, 780),
                 ("毎朝5本、ぜんぶ無料", font(cjk, 44), TEAL, 900)]
    else:
        lines = [(f"{remaining} MORE STORIES", font(latin, 76), INK, 460),
                 ("ON SUBSTACK", font(latin, 96), CORAL, 570),
                 ("— FREE —", font(latin, 60), TEAL, 710),
                 ("jokesyoucanuse.substack.com", font(latin, 40), INK, 820)]
    for text, f, color, y in lines:
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, y), text, font=f, fill=color)
    out = tmp / "cta_card.png"
    img.save(out)
    return out


def pick_punchy_cta() -> Path:
    for name in ("mascot-cta.png", "mascot.png", "reaction-5.png"):
        p = ROOT / "images" / name
        if p.exists():
            return p
    return pick_punchy()


def render_intro(card: Path, punchy: Path, audio: Path, out: Path, dur: float) -> None:
    """Punchyがバウンドしながら登場するイントロを描画。"""
    vf = (
        f"[0:v]scale=1080:1920,setsar=1[bg];"
        f"[1:v]scale=640:-1[p0];"
        f"[p0]rotate=0.12*sin(9*t):c=none:ow=rotw(0.15):oh=roth(0.15)[p];"
        f"[bg][p]overlay="
        f"x='(W-w)/2 + 40*sin(3*t)':"
        f"y='H-h-180 - 1500*exp(-3.2*t)*abs(cos(5.5*t))',"
        f"fps={FPS},format=yuv420p"
    )
    run(["ffmpeg", "-y",
         "-loop", "1", "-t", f"{dur:.2f}", "-i", str(card),
         "-loop", "1", "-t", f"{dur:.2f}", "-i", str(punchy),
         "-i", str(audio),
         "-filter_complex", vf,
         "-map", "2:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "1",
         "-shortest", "-t", f"{dur:.2f}", str(out)])


def render_segment(img: Path, audio: Path, out: Path, dur: float,
                   slam: bool = False, sparkle: Path | None = None,
                   spos: tuple[int, int] | None = None) -> None:
    frames = max(1, int(dur * FPS))
    if slam:
        # スタンプの衝撃: 大きめから0.25秒で沈み込み→ゆっくりズーム
        zexpr = f"'if(lte(on,7),1.16-0.023*on,1.0+0.05*(on-7)/{frames})'"
    else:
        zexpr = f"'1+0.07*on/{frames}'"
    base = (
        f"[0:v]scale=1080:1350,setsar=1[fg];"
        f"color=c={BG_COLOR}:s=1080x1920:r={FPS}:d={dur:.2f}[bg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"zoompan=z={zexpr}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps={FPS}"
    )
    inputs = ["-loop", "1", "-t", f"{dur:.2f}", "-i", str(img),
              "-i", str(audio)]
    if slam and sparkle and spos:
        # キラリ: バンッと同時に現れ、回転しながらすっと消える
        box = 312  # hypot(220,220)を丸めた出力枠。中心合わせに使う
        x, y = spos[0] - box // 2, spos[1] - box // 2
        vf = (
            base + "[zp];"
            f"[2:v]format=rgba,"
            f"rotate='1.2*t':c=none:ow={box}:oh={box},"
            f"fade=in:st=0.03:d=0.09:alpha=1,"
            f"fade=out:st=0.42:d=0.34:alpha=1[sp];"
            f"[zp][sp]overlay=x={x}:y={y}:enable='lte(t,0.85)',format=yuv420p"
        )
        inputs += ["-loop", "1", "-t", f"{dur:.2f}", "-i", str(sparkle)]
    else:
        vf = base
    run(["ffmpeg", "-y", *inputs,
         "-filter_complex", vf,
         "-map", "1:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "1",
         "-shortest", "-t", f"{dur:.2f}", str(out)])


# ---------- 台本 ----------

def _slides(d: dict) -> list[str]:
    car = d.get("carousel") or []
    slides = car if isinstance(car, list) else (car.get("slides") or [])
    if len(slides) < 3 or not (d.get("candidates") or []):
        raise SystemExit("[skip] carousel/candidates not ready; no video today")
    return slides


def build_segments(d: dict, tmp: Path) -> list[dict]:
    """{kind, img, text, rate, sfx} のリスト。"""
    ed = d.get("editorial") or {}
    cands = d.get("candidates") or []
    slides = _slides(d)
    ja = LANG == "ja"

    asides = (ed.get("asideJa") if ja else ed.get("asideEn")) or []
    fallback = FALLBACK_ASIDES_JA if ja else FALLBACK_ASIDES_EN

    segs = []
    # 1) イントロ(宣言) — 要点だけ短く、すぐSTORY 1へ
    if ja:
        intro_text = "ニュースは、笑おう！今日の5本、いくよ！"
    else:
        intro_text = "Don't just read the news — laugh at it! Five stories, go!"
    segs.append({"kind": "intro", "text": intro_text, "rate": INTRO_RATE, "sfx": "pop"})

    # 2) ニュース5本: ①ニュース読み上げ(素の画) → ②ジョークがスタンプで出現+読み上げ
    try:
        day_off = int((d.get("date") or "2026-01-01")[8:10])
    except Exception:
        day_off = 0
    cues = JOKE_CUES_JA if ja else JOKE_CUES_EN
    for i, c in enumerate(cands[:STORIES]):
        n = c.get("news") or {}
        idx = i + 1
        if idx >= len(slides):
            continue
        base_img = slides[idx]
        plain_img = base_img.replace(".jpg", "-plain.jpg")
        stamped_img = base_img.replace(".jpg", "-stamped.jpg")
        if not (ROOT / plain_img).exists():
            plain_img = base_img
        if not (ROOT / stamped_img).exists():
            stamped_img = base_img
        aside = clean_for_speech(asides[i] if i < len(asides) else fallback[i])
        cue = cues[(i + day_off) % len(cues)]
        if ja:
            news_text = f"{idx}本目。{first_sentences(n.get('summary') or '', 2, 130)}"
            joke = clean_for_speech((c.get("captionsJa") or [""])[0])
        else:
            news_text = (f"Story {idx}. "
                         f"{first_sentences(c.get('newsEn') or n.get('headline') or '', 2, 230)}")
            joke = clean_for_speech((c.get("captions") or [""])[0])
        # ①ニュースの内容をしっかり読む(ジョーク無しの画)
        segs.append({"kind": "story", "img": plain_img, "text": news_text,
                     "rate": RATE, "sfx": "ding"})
        # ②ハンコ「バンッ」+ 真顔 + キラリ → 助走 → ジョーク → 捨て台詞
        #   フレームはここで自前合成(失敗したら旧スタンプ画にフォールバック)
        slam_img, spos = stamped_img, None
        try:
            frame, spos = build_slam_frame(ROOT / plain_img, joke, i, tmp)
            slam_img = str(frame)
        except Exception as e:
            print(f"[warn] slam frame failed for story {idx}: {e}",
                  file=sys.stderr)
        joke_text = f"{cue} {joke} …{aside}" if ja else f"{cue} ... {joke} ... {aside}"
        segs.append({"kind": "slam", "img": slam_img, "text": joke_text,
                     "rate": RATE, "sfx": "stamp", "spos": spos})

    # 3) 締め(パンチライン)
    if ja:
        quip = clean_for_speech(ed.get("quipJa") or "今日もそういう日でした。")
        outro = f"今日のまとめ。{quip}"
    else:
        quip = clean_for_speech(ed.get("quipEn") or "That's the week. Somehow.")
        outro = f"Today's punchline. {quip}"
    segs.append({"kind": "story", "img": slides[-1], "text": outro,
                 "rate": RATE, "sfx": "tada"})

    # 4) キャラクターのお誘い(残りの本数を記事へ)。5本全部見せた日は従来の一言だけ
    remaining = max(0, len(cands[:5]) - STORIES)
    if remaining > 0:
        if ja:
            cta = (f"今日は{STORIES}本だけ。残りの{remaining}本は、noteで待ってるよ。"
                   "ジョークが言えるくらい実用的にニュースを読みたいなら、おいで！また明日！")
        else:
            cta = (f"That was {STORIES} of today's 5 stories. "
                   f"The other {remaining} are waiting on Substack — free. "
                   "If you want news you can actually joke about... come join me! "
                   "See you tomorrow!")
    else:
        cta = ("詳しくはnoteで待ってるよ。フォローよろしく！また明日！" if ja else
               "The full breakdown is waiting on Substack. "
               "Follow me — new stories every morning!")
    segs.append({"kind": "cta", "text": cta, "rate": RATE, "sfx": "pop",
                 "remaining": remaining})
    return segs


def pick_punchy() -> Path:
    for name in ("reaction-3.png", "mascot.png", "reaction-1.png"):
        p = ROOT / "images" / name
        if p.exists():
            return p
    raise SystemExit("[skip] no punchy image found in images/")


def main() -> None:
    d = json.loads(DAILY.read_text(encoding="utf-8"))
    date = d.get("date")
    if not date:
        raise SystemExit("[skip] daily.json has no date")

    out_dir = ROOT / "videos" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"_tmp-{LANG}"
    tmp.mkdir(exist_ok=True)

    segs = build_segments(d, tmp)
    sfx = synth_sfx(tmp)
    print(f"[video:{LANG}] {len(segs)} segments for {date} (v4)")

    # 1) TTS(前後の無音カット) + SFX結合
    for i, s in enumerate(segs):
        voice = tmp / f"voice{i}.mp3"
        asyncio.run(tts_to_file(s["text"], voice, s["rate"]))
        merged = tmp / f"seg{i}.wav"
        concat_audio([sfx[s["sfx"]], trim_silence(voice)], merged)
        s["audio"] = merged
        s["adur"] = probe_duration(merged)

    # 2) 尺: 各セグメント=音声+短い余韻(イントロはさらに短くしてすぐ本編へ)
    base_pad = 0.18
    intro_pad = 0.10
    total = sum(s["adur"] + base_pad for s in segs)
    extra = max(0.0, MIN_TOTAL_SEC - total) / len(segs)
    pad = min(MAX_SEG_PAD, base_pad + extra)

    # 3) 描画
    card = build_intro_card(tmp)
    punchy = pick_punchy()
    sparkle = None
    try:
        sparkle = build_sparkle(tmp)
    except Exception as e:
        print(f"[warn] sparkle build failed: {e}", file=sys.stderr)
    parts = []
    for i, s in enumerate(segs):
        seg_mp4 = tmp / f"seg{i}.mp4"
        if s["kind"] == "intro":
            render_intro(card, punchy, s["audio"], seg_mp4,
                         s["adur"] + intro_pad)
        elif s["kind"] == "cta":
            ccard = build_cta_card(tmp, max(1, s.get("remaining") or 2))
            render_intro(ccard, pick_punchy_cta(), s["audio"], seg_mp4,
                         s["adur"] + pad)
        else:
            p = Path(s["img"])
            img_path = p if p.is_absolute() else ROOT / p
            render_segment(img_path, s["audio"], seg_mp4, s["adur"] + pad,
                           slam=(s["kind"] == "slam"),
                           sparkle=sparkle, spos=s.get("spos"))
        parts.append(seg_mp4)

    # 4) 連結
    concat_list = tmp / "list.txt"
    concat_list.write_text("".join(f"file '{p.name}'\n" for p in parts),
                           encoding="utf-8")
    final = out_dir / ("daily-short-ja.mp4" if LANG == "ja" else "daily-short.mp4")
    joined = tmp / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(concat_list), "-c", "copy", str(joined)])

    # 5) BGM(任意): assets/bgm.mp3 があれば小音量で敷く
    if BGM.exists():
        vdur = probe_duration(joined)
        run(["ffmpeg", "-y", "-i", str(joined),
             "-stream_loop", "-1", "-i", str(BGM),
             "-filter_complex",
             f"[1:a]volume=0.10,afade=t=out:st={max(0.0, vdur-2):.2f}:d=2[m];"
             f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "128k", str(final)])
        print("[info] BGM mixed from assets/bgm.mp3")
    else:
        shutil.move(str(joined), str(final))

    shutil.rmtree(tmp, ignore_errors=True)
    vdur = probe_duration(final)
    size_mb = final.stat().st_size / 1e6
    print(f"[ok] {final.relative_to(ROOT)}  {vdur:.1f}s  {size_mb:.1f}MB")
    if vdur < 60 and LANG == "en":
        print("[warn] under 60s — TikTok報酬対象外の尺です")

    rel = str(final.relative_to(ROOT)).replace(os.sep, "/")
    if LANG == "ja":
        d["videoJa"] = rel
        d["videoJaDuration"] = round(vdur, 1)
    else:
        d["video"] = rel
        d["videoDuration"] = round(vdur, 1)
    DAILY.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    # 古い動画を削除
    videos_dir = ROOT / "videos"
    dirs = sorted([p for p in videos_dir.iterdir() if p.is_dir()])
    for p in dirs[:-KEEP_DAYS]:
        shutil.rmtree(p, ignore_errors=True)
        print(f"[prune] removed {p}")


if __name__ == "__main__":
    main()
