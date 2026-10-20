#!/usr/bin/env python3
import io
import json
import os
import re
import urllib.request
from datetime import datetime
from typing import List, Dict

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from youtube_transcript_api import YouTubeTranscriptApi

# ==========================================
# 設定項目
# ==========================================
CHANNEL_STREAMS_URL = "https://www.youtube.com/@knock2525/streams"
DRIVE_FOLDER_ID = "1v0p6mO7kROq_zGAI-wArsiTBNpbvr12v"
MAX_RECENT_VIDEOS_TO_CHECK = 3

def get_drive_service():
    sa_json = os.environ.get("GCP_CREDENTIALS") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise ValueError("環境変数 GCP_CREDENTIALS が設定されていません。")
    creds_info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def format_timestamp(seconds: float) -> str:
    total_sec = int(seconds)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        short_ts = f"{hours}:{minutes:02d}:{secs:02d}"
        jp_ts = f"{hours} 時間 {minutes} 分 {secs} 秒"
    else:
        short_ts = f"{minutes}:{secs:02d}"
        jp_ts = f"{minutes} 分 {secs} 秒" if minutes > 0 else f"{secs} 秒"
    return f"{short_ts}{jp_ts}"

def is_already_saved(drive_service, filename: str) -> bool:
    v3_query = f"'{DRIVE_FOLDER_ID}' in parents and name contains '{filename.replace('.txt', '')}' and trashed = false"
    res = drive_service.files().list(q=v3_query, fields="files(id, name, mimeType, size)", pageSize=5).execute()
    for f in res.get("files", []):
        if f.get("mimeType", "") == "text/plain" and int(f.get("size", 0)) >= 10000:
            print(f"[SKIP] {filename} はすでに保存済みです (サイズ: {f.get('size')} bytes)")
            return True
    return False

def get_video_metadata(video_id: str) -> Dict[str, str]:
    """タイトルと配信日を軽量に取得"""
    title = f"YouTube_{video_id}"
    upload_date = datetime.now().strftime("%Y%m%d")
    
    # タイトル取得 (oEmbed API: 制限なし・認証不要)
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode("utf-8"))
            title = data.get("title", title)
    except Exception:
        pass

    # 配信日取得 (動画ページのmetaタグから抽出)
    try:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        req = urllib.request.Request(watch_url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req).read().decode("utf-8")
        match = re.search(r'"datePublished":"(\d{4})-(\d{2})-(\d{2})"', html)
        if match:
            upload_date = f"{match.group(1)}{match.group(2)}{match.group(3)}"
    except Exception:
        pass

    return {"id": video_id, "title": title, "upload_date": upload_date}

def get_recent_videos() -> List[Dict[str, str]]:
    """チャンネルページから直近の動画IDを抽出"""
    videos: List[Dict[str, str]] = []
    try:
        req = urllib.request.Request(
            CHANNEL_STREAMS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
        )
        html = urllib.request.urlopen(req).read().decode("utf-8")
        found_vids = []
        for match in re.finditer(r'"videoId":"([a-zA-Z0-9_-]{11})"', html):
            vid = match.group(1)
            if vid not in found_vids:
                found_vids.append(vid)

        print(f"[INFO] チャンネルから {len(found_vids)} 件の動画IDを検出")
        for vid in found_vids[:MAX_RECENT_VIDEOS_TO_CHECK]:
            meta = get_video_metadata(vid)
            videos.append(meta)
    except Exception as e:
        print(f"[ERROR] チャンネル一覧の取得失敗: {e}")

    return videos

def fetch_transcript(video_id: str) -> str:
    """公式自動字幕を取得して整形"""
    print(f"[INFO] 字幕データを取得中... ({video_id})")
    try:
        # 日本語字幕（自動生成含む）を優先取得
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ja'])
        lines = [
            f"{format_timestamp(item['start'])}{item['text'].replace(chr(10), ' ').strip()}  "
            for item in transcript_list
        ]
        return "\n".join(lines)
    except Exception as e:
        print(f"[WARN] 字幕が取得できませんでした (配信直後/待機枠/字幕無効): {e}")
        return ""

def save_or_update_drive_file(drive_service, filename: str, content: str) -> str:
    v3_query = f"'{DRIVE_FOLDER_ID}' in parents and name contains '{filename.replace('.txt', '')}' and trashed = false"
    res = drive_service.files().list(q=v3_query, fields="files(id)", pageSize=10).execute()
    for f in res.get("files", []):
        try: drive_service.files().delete(fileId=f["id"]).execute()
        except: pass
    media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/plain", resumable=True)
    created = drive_service.files().create(
        body={"name": filename, "parents": [DRIVE_FOLDER_ID], "mimeType": "text/plain"},
        media_body=media, fields="id"
    ).execute()
    print(f"[SUCCESS] {filename} 保存完了")
    return created["id"]

def main():
    print("=== YouTube公式字幕 自動保存スクリプト開始 ===")
    drive_service = get_drive_service()
    videos = get_recent_videos()

    if not videos:
        print("対象動画なし。")
        return

    success_count, error_count = 0, 0
    for v in videos:
        vid, raw_date = v["id"], v["upload_date"]
        filename = f"{raw_date[:4]}_{raw_date[4:6]}_{raw_date[6:8]}_文字起こし.txt"
        print(f"\n--- 確認中: {v['title']} ({vid}) ---")

        if is_already_saved(drive_service, filename):
            success_count += 1
            continue

        text = fetch_transcript(vid)
        if not text:
            print(f"[SKIP] 字幕データが存在しないため処理をスキップします。")
            continue

        try:
            save_or_update_drive_file(drive_service, filename, text)
            success_count += 1
        except Exception as e:
            print(f"[ERROR] Drive保存失敗 ({vid}): {e}")
            error_count += 1

    print(f"\n[INFO] 処理完了 - 成功/スキップ: {success_count}, エラー: {error_count}")

if __name__ == "__main__":
    main()
