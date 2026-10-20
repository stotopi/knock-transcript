#!/usr/bin/env python3
import io
import json
import os
import re
import tempfile
import urllib.request
from datetime import datetime
from typing import List, Dict

import yt_dlp
from faster_whisper import WhisperModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

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
        if f.get("mimeType", "") == "text/plain" and int(f.get("size", 0)) >= 30000:
            print(f"[SKIP] {filename} はすでに保存済みです (サイズ: {f.get('size')} bytes)")
            return True
    return False

def setup_cookie_file():
    cookie_data = os.environ.get("YOUTUBE_COOKIES")
    if not cookie_data or not cookie_data.strip():
        print("[WARN] YOUTUBE_COOKIES が設定されていません。ボット検知される可能性があります。")
        return None
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    with os.fdopen(fd, 'w') as f:
        f.write(cookie_data)
    return path

def get_recent_videos(cookie_file: str) -> List[Dict[str, str]]:
    videos: List[Dict[str, str]] = []
    seen_ids = set()
    
    ydl_opts_single = {
        "quiet": True, 
        "skip_download": True,
        "username": "oauth2",
        "password": ""
    }
    
    # Cookieがある場合は通常クライアント、ない場合はモバイル偽装を使用
    if cookie_file:
        ydl_opts_single["cookiefile"] = cookie_file
    else:
        ydl_opts_single["extractor_args"] = {"youtube": {"player_client": ["android", "ios"]}}

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

        valid_count = 0
        with yt_dlp.YoutubeDL(ydl_opts_single) as ydl:
            for vid in found_vids[:15]:
                if valid_count >= MAX_RECENT_VIDEOS_TO_CHECK: break
                if vid in seen_ids: continue
                try:
                    info = ydl.extract_info(vid, download=False)
                    if info.get("live_status") in ["is_live", "is_upcoming"]:
                        print(f"[SKIP] 配信中または待機枠のためスキップ: {info.get('title')}")
                        continue
                    seen_ids.add(vid)
                    videos.append({
                        "id": vid, "title": info.get("title", ""),
                        "upload_date": info.get("upload_date") or datetime.now().strftime("%Y%m%d")
                    })
                    valid_count += 1
                except Exception as e:
                    print(f"[WARN] 動画詳細取得スキップ ({vid}): {e}")
    except Exception as e:
        print(f"[ERROR] HTML取得失敗: {e}")

    return videos

def download_audio(video_id: str, cookie_file: str, output_dir: str = "./audio_temp") -> str:
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{video_id}.mp3")
    
    ydl_opts = {
        'format': 'bestaudio/best', 
        'outtmpl': os.path.join(output_dir, f"{video_id}.%(ext)s"),
        'quiet': False,
        'username': "oauth2",
        'password': "",
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
    }
    
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    else:
        ydl_opts["extractor_args"] = {'youtube': {'player_client': ['android', 'ios']}}

    print(f"[INFO] 音声ダウンロード中... ({video_id})")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    return out_path

def transcribe_audio(audio_path: str) -> str:
    print(f"[INFO] AI文字起こし開始...")
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, beam_size=5, language="ja")
    lines = [f"{format_timestamp(s.start)}{s.text.replace(chr(10), ' ').strip()}  " for s in segments]
    full_text = "\n".join(lines)
    return full_text if len(full_text) > 100 else ""

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
    print("=== 自動保存スクリプト開始 ===")
    cookie_file = setup_cookie_file()
    drive_service = get_drive_service()
    videos = get_recent_videos(cookie_file)
    
    if not videos:
        print("対象動画なし。")
        if cookie_file and os.path.exists(cookie_file): os.remove(cookie_file)
        return
        
    success_count, error_count = 0, 0
    for v in videos:
        vid, raw_date = v["id"], v["upload_date"]
        filename = f"{raw_date[:4]}_{raw_date[4:6]}_{raw_date[6:8]}_文字起こし.txt"
        print(f"\n--- 確認中: {v['title']} ---")
        
        if is_already_saved(drive_service, filename):
            success_count += 1
            continue
            
        audio_file = None
        try:
            audio_file = download_audio(vid, cookie_file)
            text = transcribe_audio(audio_file)
            if not text:
                error_count += 1
                continue
            save_or_update_drive_file(drive_service, filename, text)
            success_count += 1
        except Exception as e:
            print(f"[ERROR] {vid}: {e}")
            error_count += 1
        finally:
            if audio_file and os.path.exists(audio_file): os.remove(audio_file)
            
    print(f"\n[INFO] 成功/スキップ: {success_count}, エラー: {error_count}")
    
    if cookie_file and os.path.exists(cookie_file):
        os.remove(cookie_file)
        
    if success_count == 0 and error_count > 0: exit(1)

if __name__ == "__main__":
    main()
