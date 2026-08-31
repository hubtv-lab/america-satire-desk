#!/usr/bin/env python3
"""毎日の縦動画をYouTube(Hub-TV)へ自動アップロードする。既定は英語版。

- 認証: OAuthリフレッシュトークン方式（GitHub Secretsに保存）
  必要なSecrets: YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN
- Secrets未設定なら静かにスキップ（手動アップロード運用のまま）
- 注意: Googleの仕様で、API監査(無料・フォーム申請)が通るまで、
  APIからのアップロードは自動的に「非公開」になる。
  それまでは YT_PRIVACY=private で届いた動画を、YouTube Studioで
  「公開」に切り替える(1タップ)。監査通過後は vars.YT_PRIVACY=public に。
"""

from __future__ import annotations
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / "daily.json"


def main() -> None:
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not (cid and csec and rtok):
        print("[skip] YouTube secrets not set — manual upload mode")
        return

    d = json.loads(DAILY.read_text(encoding="utf-8"))
    key = os.environ.get("YT_VIDEO_KEY", "video")  # 既定=英語版。日本語版は videoJa
    rel = d.get(key)
    if not rel:
        print(f"[skip] {key} not found in daily.json — no video to upload")
        return
    path = ROOT / rel
    if not path.exists():
        print(f"[skip] video file missing: {rel}")
        return

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=rtok,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cid,
        client_secret=csec,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)

    ed = d.get("editorial") or {}
    is_ja = key == "videoJa"
    if is_ja:
        title = (ed.get("titleJa") or "今日のアメリカ").strip()
    else:
        title = (ed.get("titleEn") or "Today in America").strip()
    if len(title) > 88:
        title = title[:88] + "…"
    title += " #Shorts"

    desc_parts = []
    if is_ja:
        if ed.get("leadJa"):
            desc_parts.append(ed["leadJa"].strip())
        desc_parts.append(
            "毎朝、アメリカのニュース5本を「明日そのまま使えるジョーク」に変換してお届けする教養メディアです。\n"
            "📖 note（日本語・全文）: https://note.com/jokes_youcanuse1\n"
            "📰 Substack（English）: https://jokesyoucanuse.substack.com\n\n"
            "制作: 東京の人間1人+AI編集部（ナレーションはAI音声です。事実は出典記事のみをソースにしています）"
        )
        tags = ["アメリカ", "ニュース", "時事", "ジョーク", "アメリカンジョーク", "shorts"]
        lang = "ja"
    else:
        if ed.get("subtitleEn"):
            desc_parts.append(ed["subtitleEn"].strip())
        desc_parts.append(
            "Five American news stories, turned into jokes you can actually use — every morning.\n"
            "Full written breakdown (free): https://jokesyoucanuse.substack.com\n\n"
            "Made in Tokyo by one human and his AI newsroom. AI narration; facts come only from the linked sources."
        )
        tags = ["news", "satire", "comedy", "usa", "american politics", "jokes", "shorts"]
        lang = "en"
    body = {
        "snippet": {
            "title": title,
            "description": "\n\n".join(desc_parts),
            "tags": tags,
            "categoryId": "23",  # Comedy
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": os.environ.get("YT_PRIVACY", "private"),
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(path), mimetype="video/mp4",
                            chunksize=4 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk()
    print(f"[ok] uploaded to YouTube: https://youtu.be/{resp['id']} "
          f"(privacy={body['status']['privacyStatus']})")


if __name__ == "__main__":
    main()
