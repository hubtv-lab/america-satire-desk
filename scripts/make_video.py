#!/usr/bin/env python3
"""毎日の縦動画(v2)を自動生成する。
構成: Punchyのぴょこん登場+宣言(約4秒) → ニュース5本(SFX→概要→ジョーク→捨て台詞) → 締め+CTA

- 音声: edge-tts(無料)。テンポ高め(EN +15% / JA +10%)、イントロはさらに高速
- 効果音: ffmpegで自前合成(登場ポップ音・区切りのチーン・締めのタダー)。著作権フリー
- BGM: リポジトリに assets/bgm.mp3 があれば小音量(10%)で自動ミックス(任意)
- イントロ: PILでブランドカードを描画し、Punchyがバウンドしながら登場
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
RATE = os.environ.get("VIDEO_RATE", "+15%" if LANG == "en" else "+10%")
PITCH = os.environ.get("VIDEO_PITCH", "+2Hz" if LANG == "en" else "+0Hz")
INTRO_RATE = os.environ.get("VIDEO_INTRO_RATE", "+22%" if LANG == "en" else "+16%")
FAKE_TTS = os.environ.get("FAKE_TTS") == "1"
MIN_TOTAL_SEC = 63.0
MAX_SEG_PAD = 1.6
KEEP_DAYS = 3
BG_COLOR = "0x33302B"
CREAM = (247, 243, 234)
TEAL = (84, 167, 156)
INK = (51, 48, 43)
CORAL = (224, 122, 95)
FPS = 30

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
                   slam: bool = False) -> None:
    frames = max(1, int(dur * FPS))
    if slam:
        # スタンプの衝撃: 大きめから0.25秒で沈み込み→ゆっくりズーム
        zexpr = f"'if(lte(on,7),1.16-0.023*on,1.0+0.05*(on-7)/{frames})'"
    else:
        zexpr = f"'1+0.07*on/{frames}'"
    vf = (
        f"[0:v]scale=1080:1350,setsar=1[fg];"
        f"color=c={BG_COLOR}:s=1080x1920:r={FPS}:d={dur:.2f}[bg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"zoompan=z={zexpr}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps={FPS}"
    )
    run(["ffmpeg", "-y",
         "-loop", "1", "-t", f"{dur:.2f}", "-i", str(img),
         "-i", str(audio),
         "-filter_complex", vf,
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


def build_segments(d: dict) -> list[dict]:
    """{kind, img, text, rate, sfx} のリスト。"""
    ed = d.get("editorial") or {}
    cands = d.get("candidates") or []
    slides = _slides(d)
    ja = LANG == "ja"

    asides = (ed.get("asideJa") if ja else ed.get("asideEn")) or []
    fallback = FALLBACK_ASIDES_JA if ja else FALLBACK_ASIDES_EN

    segs = []
    # 1) イントロ(宣言)
    if ja:
        intro_text = "ニュースは、知るだけじゃない。斬るんでもない。笑うんだ！今日の5本、いくよ！"
    else:
        intro_text = ("Don't just read the news. Don't just fight about it. "
                      "Laugh at it! Five stories. Go!")
    segs.append({"kind": "intro", "text": intro_text, "rate": INTRO_RATE, "sfx": "pop"})

    # 2) ニュース5本: ①ニュース読み上げ(素の画) → ②ジョークがスタンプで出現+読み上げ
    try:
        day_off = int((d.get("date") or "2026-01-01")[8:10])
    except Exception:
        day_off = 0
    cues = JOKE_CUES_JA if ja else JOKE_CUES_EN
    for i, c in enumerate(cands[:5]):
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
        # ②スタンプ「バンッ」→ 助走 → ジョーク → 捨て台詞
        joke_text = f"{cue} {joke} …{aside}" if ja else f"{cue} ... {joke} ... {aside}"
        segs.append({"kind": "slam", "img": stamped_img, "text": joke_text,
                     "rate": RATE, "sfx": "stamp"})

    # 3) 締め(パンチライン + CTA)
    if ja:
        quip = clean_for_speech(ed.get("quipJa") or "今日もそういう日でした。")
        outro = (f"今日のまとめ。{quip} "
                 "詳しくはSubstackで待ってるよ。フォローよろしく！また明日！")
    else:
        quip = clean_for_speech(ed.get("quipEn") or "That's the week. Somehow.")
        outro = (f"Today's punchline. {quip} "
                 "The full breakdown is waiting for you on Substack. "
                 "Follow me — new stories every morning!")
    segs.append({"kind": "story", "img": slides[-1], "text": outro,
                 "rate": RATE, "sfx": "tada"})
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

    segs = build_segments(d)
    sfx = synth_sfx(tmp)
    print(f"[video:{LANG}] {len(segs)} segments for {date} (v2)")

    # 1) TTS + SFX結合
    for i, s in enumerate(segs):
        voice = tmp / f"voice{i}.mp3"
        asyncio.run(tts_to_file(s["text"], voice, s["rate"]))
        merged = tmp / f"seg{i}.wav"
        concat_audio([sfx[s["sfx"]], voice], merged)
        s["audio"] = merged
        s["adur"] = probe_duration(merged)

    # 2) 尺: 各セグメント=音声+短い余韻
    base_pad = 0.30
    total = sum(s["adur"] + base_pad for s in segs)
    extra = max(0.0, MIN_TOTAL_SEC - total) / len(segs)
    pad = min(MAX_SEG_PAD, base_pad + extra)

    # 3) 描画
    card = build_intro_card(tmp)
    punchy = pick_punchy()
    parts = []
    for i, s in enumerate(segs):
        dur = s["adur"] + pad
        seg_mp4 = tmp / f"seg{i}.mp4"
        if s["kind"] == "intro":
            render_intro(card, punchy, s["audio"], seg_mp4, dur)
        else:
            render_segment(ROOT / s["img"], s["audio"], seg_mp4, dur,
                           slam=(s["kind"] == "slam"))
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
