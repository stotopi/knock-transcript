#!/usr/bin/env python3
import io
import json
import os
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

# 優先取得する動画URL（必要に応じて追加可能）
TARGET_VIDEO_URLS = [
    "https://www.youtube.com/live/7mrFHdSr0O8",
]
MAX_RECENT_VIDEOS_TO_CHECK = 3


def get_drive_service():
    """環境変数（GitHub Secrets）からサービスアカウント認証情報を読み込み、Drive APIクライアントを初期化"""
    sa_json = os.environ.get("GCP_CREDENTIALS") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise ValueError("環境変数 GCP_CREDENTIALS（または GOOGLE_SERVICE_ACCOUNT_JSON）が設定されていません。")

    creds_info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def format_timestamp(seconds: float) -> str:
    """秒数をタイムスタンプ形式に整形"""
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
    """Googleドライブ上に既に十分なサイズの全文ファイルが存在するか確認（無駄な文字起こしを回避）"""
    v3_query = f"'{DRIVE_FOLDER_ID}' in parents and name contains '{filename.replace('.txt', '')}' and trashed = false"
    res = drive_service.files().list(
        q=v3_query,
        fields="files(id, name, mimeType, size)",
        pageSize=5,
    ).execute()
    files = res.get("files", [])

    for f in files:
        file_size = int(f.get("size", 0))
        mime_type = f.get("mimeType", "")
        if mime_type == "text/plain" and file_size >= 30000:
            print(f"[SKIP] {filename} はすでに保存済みです (サイズ: {file_size:,} bytes, ID: {f['id']})")
            return True
    return False


def get_recent_videos() -> List[Dict[str, str]]:
    """対象動画および直近の配信メタデータを取得"""
    ydl_opts = {
        "quiet": True,
        "extract_flat": False,
        "skip_download": True,
        "playlistend": MAX_RECENT_VIDEOS_TO_CHECK,
    }
    videos: List[Dict[str, str]] = []
    seen_ids = set()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for url in TARGET_VIDEO_URLS:
            try:
                info = ydl.extract_info(url, download=False)
                vid = info.get("id")
                if vid and vid not in seen_ids:
                    seen_ids.add(vid)
                    videos.append({
                        "id": vid,
                        "title": info.get("title", ""),
                        "upload_date": info.get("upload_date") or datetime.now().strftime("%Y%m%d"),
                    })
            except Exception as e:
                print(f"[WARN] 個別動画情報の取得スキップ ({url}): {e}")

        try:
            playlist_info = ydl.extract_info(CHANNEL_STREAMS_URL, download=False)
            entries = playlist_info.get("entries", []) if playlist_info else []
            for entry in entries:
                if not entry:
                    continue
                vid = entry.get("id")
                if vid and vid not in seen_ids:
                    seen_ids.add(vid)
                    videos.append({
                        "id": vid,
                        "title": entry.get("title", ""),
                        "upload_date": entry.get("upload_date") or datetime.now().strftime("%Y%m%d"),
                    })
        except Exception as e:
            print(f"[WARN] チャンネル一覧の取得スキップ: {e}")

    return videos


def download_audio(video_id: str, output_dir: str = "./audio_temp") -> str:
    """yt-dlpを使って動画から音声(MP3)のみを抽出ダウンロード"""
    os.makedirs(output_dir, exist_ok=True)
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    out_path = os.path.join(output_dir, f"{video_id}.mp3")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, f"{video_id}.%(ext)s"),
        'quiet': False,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }

    print(f"[INFO] 音声データをダウンロード中... ({video_id})")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    return out_path


def transcribe_audio(audio_path: str) -> str:
    """faster-whisperを使用してローカルCPU環境で高速文字起こしを実行"""
    print(f"[INFO] AI文字起こしを開始します (faster-whisper: baseモデル)...")
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, beam_size=5, language="ja")

    print(f"[INFO] 検出言語: {info.language} (精度: {info.language_probability:.2f})")

    lines = []
    for segment in segments:
        ts_label = format_timestamp(segment.start)
        text = segment.text.replace("\n", " ").strip()
        lines.append(f"{ts_label}{text}  ")

    full_text = "\n".join(lines)
    return full_text if len(full_text) > 100 else ""


def save_or_update_drive_file(drive_service, filename: str, content: str)
