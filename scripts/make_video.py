#!/usr/bin/env python3
"""毎日の縦動画(60〜90秒)を自動生成する。
カルーセル7枚 + AI読み上げ(edge-tts) → videos/<date>/daily-short.mp4

TikTok Creator Rewards(1分以上の動画のみ報酬対象)と
YouTube Shorts(3分以内)の両方に合う 63〜100秒 を狙う。

- 音声: edge-tts (無料・Microsoft neural voice)。FAKE_TTS=1 で無音ダミー(テスト用)
- 映像: スライドを1080x1920キャンバス中央に置き、ゆっくりズーム
- 古い動画フォルダは3日分だけ残して削除(リポジトリ肥大防止)
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

LANG = os.environ.get("VIDEO_LANG", "en")   # "en" or "ja"
_DEFAULT_VOICES = {"en": "en-US-ChristopherNeural", "ja": "ja-JP-KeitaNeural"}
VOICE = os.environ.get("VIDEO_VOICE", _DEFAULT_VOICES.get(LANG, _DEFAULT_VOICES["en"]))
RATE = os.environ.get("VIDEO_RATE", "+8%" if LANG == "en" else "+4%")
FAKE_TTS = os.environ.get("FAKE_TTS") == "1"
MIN_TOTAL_SEC = 63.0          # TikTok報酬ライン(60秒)+安全マージン
MAX_SEG_PAD = 2.5             # 音声後の余韻(秒)
KEEP_DAYS = 3                 # videos/ に残す日数
BG_COLOR = "0x33302B"         # ブランドのインク色
FPS = 30


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
    """読み上げ用にテキストを整える(記号除去・引用符除去)。"""
    s = re.sub(r"[\"“”‘’]", "", str(s))
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def tts_to_file(text: str, out: Path) -> None:
    if FAKE_TTS:
        dur = max(2.0, min(15.0, len(text) * 0.055))
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", f"{dur:.2f}", "-c:a", "libmp3lame", "-q:a", "9", str(out)])
        return
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=RATE)
    await comm.save(str(out))


def build_segments_ja(d: dict) -> list[dict]:
    """日本語版の台本。英語スライド+日本語ナレーション(教養/英語学習の趣)。"""
    ed = d.get("editorial") or {}
    cands = d.get("candidates") or []
    car = d.get("carousel") or []
    slides = car if isinstance(car, list) else (car.get("slides") or [])
    if len(slides) < 3 or not cands:
        raise SystemExit("[skip] carousel/candidates not ready; no video today")

    segs = []
    title = clean_for_speech(ed.get("titleJa") or "今日のアメリカ")
    segs.append({"img": slides[0],
                 "text": f"{title}。今日の5本、いきます。"})

    for i, c in enumerate(cands[:5]):
        n = c.get("news") or {}
        summary = clean_for_speech(n.get("summary") or "")
        joke = clean_for_speech((c.get("captionsJa") or [""])[0])
        idx = i + 1
        if idx < len(slides):
            segs.append({"img": slides[idx],
                         "text": f"{idx}本目。{summary} …{joke}"})

    quip = clean_for_speech(ed.get("quipJa") or "今日もそういう日でした。")
    segs.append({"img": slides[-1],
                 "text": f"今日のまとめ。{quip} "
                         "フォローしておくと、毎朝ここに届くよ。"})
    return segs


def build_segments(d: dict) -> list[dict]:
    """スライドごとの読み上げ台本。スライドの文字と同じ内容を声にする。"""
    ed = d.get("editorial") or {}
    cands = d.get("candidates") or []
    car = d.get("carousel") or []
    slides = car if isinstance(car, list) else (car.get("slides") or [])
    if len(slides) < 3 or not cands:
        raise SystemExit("[skip] carousel/candidates not ready; no video today")

    segs = []
    title = clean_for_speech(ed.get("titleEn") or "Today in America")
    sub = clean_for_speech(ed.get("subtitleEn") or "")
    segs.append({"img": slides[0],
                 "text": f"{title}. {sub} Five stories. Let's go."})

    for i, c in enumerate(cands[:5]):
        n = c.get("news") or {}
        headline = clean_for_speech(n.get("headline") or "")
        joke = clean_for_speech((c.get("captions") or [""])[0])
        idx = i + 1
        if idx < len(slides):
            segs.append({"img": slides[idx],
                         "text": f"Story {idx}. {headline}. ... {joke}"})

    quip = clean_for_speech(ed.get("quipEn") or "That's the week. Somehow.")
    segs.append({"img": slides[-1],
                 "text": f"Today's punchline. {quip} "
                         "Follow for tomorrow's stories. New every morning."})
    return segs


def render_segment(img: Path, audio: Path, out: Path, dur: float) -> None:
    frames = max(1, int(dur * FPS))
    vf = (
        f"[0:v]scale=1080:1350,setsar=1[fg];"
        f"color=c={BG_COLOR}:s=1080x1920:r={FPS}:d={dur:.2f}[bg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"zoompan=z='1+0.07*on/{frames}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps={FPS}"
    )
    run(["ffmpeg", "-y",
         "-loop", "1", "-t", f"{dur:.2f}", "-i", str(img),
         "-i", str(audio),
         "-filter_complex", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "1",
         "-shortest", "-t", f"{dur:.2f}",
         str(out)])


def prune_old_videos(videos_dir: Path, keep: int) -> None:
    if not videos_dir.exists():
        return
    dirs = sorted([p for p in videos_dir.iterdir() if p.is_dir()])
    for p in dirs[:-keep]:
        shutil.rmtree(p, ignore_errors=True)
        print(f"[prune] removed {p}")


def main() -> None:
    d = json.loads(DAILY.read_text(encoding="utf-8"))
    date = d.get("date")
    if not date:
        raise SystemExit("[skip] daily.json has no date")

    out_dir = ROOT / "videos" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"_tmp-{LANG}"
    tmp.mkdir(exist_ok=True)

    segs = build_segments_ja(d) if LANG == "ja" else build_segments(d)
    print(f"[video:{LANG}] {len(segs)} segments for {date}")

    # 1) TTS
    for i, s in enumerate(segs):
        mp3 = tmp / f"seg{i}.mp3"
        asyncio.run(tts_to_file(s["text"], mp3))
        s["audio"] = mp3
        s["adur"] = probe_duration(mp3)

    # 2) 尺の設計: 各セグメント=音声+余韻。合計が63秒に満たなければ余韻を伸ばす
    base_pad = 0.7
    total = sum(s["adur"] + base_pad for s in segs)
    extra = max(0.0, MIN_TOTAL_SEC - total) / len(segs)
    pad = min(MAX_SEG_PAD, base_pad + extra)

    # 3) 各セグメントを描画
    parts = []
    for i, s in enumerate(segs):
        dur = s["adur"] + pad
        seg_mp4 = tmp / f"seg{i}.mp4"
        img = ROOT / s["img"]
        render_segment(img, s["audio"], seg_mp4, dur)
        parts.append(seg_mp4)

    # 4) 連結
    concat_list = tmp / "list.txt"
    concat_list.write_text("".join(f"file '{p.name}'\n" for p in parts),
                           encoding="utf-8")
    final = out_dir / ("daily-short-ja.mp4" if LANG == "ja" else "daily-short.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(concat_list), "-c", "copy", str(final)])

    shutil.rmtree(tmp, ignore_errors=True)
    vdur = probe_duration(final)
    size_mb = final.stat().st_size / 1e6
    print(f"[ok] {final.relative_to(ROOT)}  {vdur:.1f}s  {size_mb:.1f}MB")
    if vdur < 60 and LANG == "en":
        print("[warn] under 60s — TikTok報酬対象外の尺です")

    # 5) daily.json に動画パスを記録（言語別キー）
    rel = str(final.relative_to(ROOT)).replace(os.sep, "/")
    if LANG == "ja":
        d["videoJa"] = rel
        d["videoJaDuration"] = round(vdur, 1)
    else:
        d["video"] = rel
        d["videoDuration"] = round(vdur, 1)
    DAILY.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    # 6) 古い動画を削除(3日分だけ残す)
    prune_old_videos(ROOT / "videos", KEEP_DAYS)


if __name__ == "__main__":
    main()
