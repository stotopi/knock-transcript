#!/usr/bin/env python3
"""
【Google Colab ワンクリック実行対応版】
このファイルの中身（コード全体）をコピーして、Google Colabの1つのセルに貼り付けて実行してください。
（パッケージのインストールも自動で実行されるため、事前の !pip install セルも不要です）
"""

import io
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Optional, List, Dict

# 必要なライブラリを自動インストール（Google Colab等で1セルだけで動くように対応）
subprocess.check_call(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "--upgrade",
        "youtube-transcript-api",
        "yt-dlp[default]",
        "google-api-python-client",
        "google-auth",
    ]
)

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ==========================================
# 設定項目
# ==========================================
CHANNEL_STREAMS_URL = "https://www.youtube.com/@knock2525/streams"
DRIVE_FOLDER_ID = "1v0p6mO7kROq_zGAI-wArsiTBNpbvr12v"

# 優先取得する動画URL（2026/10/14配信分をデフォルト設定済み）
TARGET_VIDEO_URLS = [
    "https://www.youtube.com/live/7mrFHdSr0O8",
]
MAX_RECENT_VIDEOS_TO_CHECK = 3


def get_drive_service():
    """Google Colab認証 または サービスアカウント認証でDrive APIクライアントを取得"""
    try:
        from google.colab import auth  # type: ignore
        auth.authenticate_user()
        return build("drive", "v3")
    except ImportError:
        pass

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        from google.oauth2 import service_account
        creds_info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)

    import google.auth
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)


def format_timestamp(seconds: float) -> str:
    """秒数を既存ファイルと同じタイムスタンプ形式（例: 1:361 分 36 秒）に整形"""
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


def get_recent_videos() -> List[Dict[str, str]]:
    """対象動画リスト、およびノックチャンネルの直近の配信メタデータを取得"""
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


def fetch_full_transcript(video_id: str) -> Optional[str]:
    """YouTube動画の日本語文字起こし全文を取得（v0.6系・v1.0系両対応、3時間超でも省略なし）"""
    try:
        entries = None
        # youtube-transcript-api v1.0以降の新APIに対応
        if hasattr(YouTubeTranscriptApi, "fetch"):
            try:
                ytt_api = YouTubeTranscriptApi()
                entries = ytt_api.fetch(video_id, languages=["ja"])
            except TypeError:
                pass

        # v0.6系の従来APIへのフォールバック
        if entries is None and hasattr(YouTubeTranscriptApi, "list_transcripts"):
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            try:
                transcript = transcript_list.find_transcript(["ja"])
            except Exception:
                transcript = transcript_list.find_generated_transcript(["ja"])
            entries = transcript.fetch()
        elif entries is None and hasattr(YouTubeTranscriptApi, "get_transcript"):
            entries = YouTubeTranscriptApi.get_transcript(video_id, languages=["ja"])

        if not entries:
            return None

        lines = []
        for item in entries:
            start = item["start"] if isinstance(item, dict) else getattr(item, "start", 0.0)
            raw_text = item["text"] if isinstance(item, dict) else getattr(item, "text", "")
            text = raw_text.replace("\n", " ").strip()
            ts_label = format_timestamp(start)
            lines.append(f"{ts_label}{text}  ")

        full_text = "\n".join(lines)
        return full_text if len(full_text) > 100 else None
    except Exception as e:
        print(f"[INFO] 動画 {video_id} の文字起こしはまだ取得できません: {e}")
        return None


def save_or_update_drive_file(drive_service, filename: str, content: str) -> str:
    """指定フォルダ内の同名ファイルをチェックし、重複を作らずに全文.txtを保存"""
    v3_query = f"'{DRIVE_FOLDER_ID}' in parents and name contains '{filename.replace('.txt', '')}' and trashed = false"

    res = drive_service.files().list(
        q=v3_query,
        fields="files(id, name, mimeType, size)",
        pageSize=10,
    ).execute()
    existing_files = res.get("files", [])

    content_bytes = content.encode("utf-8")

    for f in existing_files:
        file_size = int(f.get("size", 0))
        mime_type = f.get("mimeType", "")
        if mime_type == "text/plain" and file_size >= 30000:
            print(f"[SKIP] {filename} はすでに全文保存済みです (サイズ: {file_size:,} bytes, ID: {f['id']})")
            return f["id"]
        else:
            print(f"[CLEANUP] 省略版の既存ファイル ({f['name']}, ID: {f['id']}) を削除して全文版に更新します...")
            try:
                drive_service.files().delete(fileId=f["id"]).execute()
            except Exception as e:
                print(f"[WARN] 既存ファイルの削除をスキップしました: {e}")

    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype="text/plain", resumable=True)
    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID],
        "mimeType": "text/plain",
    }
    created = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink",
    ).execute()

    print(f"[SUCCESS] {filename} を全文保存しました！ ({len(content_bytes):,} bytes)")
    print(f"          リンク: {created.get('webViewLink')}")
    return created["id"]


def main():
    print("=== ノックチャンネル 文字起こし全文 自動保存スクリプト開始 ===")
    drive_service = get_drive_service()
    videos = get_recent_videos()

    if not videos:
        print("[ERROR] 対象の動画が見つかりませんでした。動画情報の取得に失敗した可能性があります。")
        raise SystemExit(1)

    saved_count = 0
    transcript_failure_count = 0

    for v in videos:
        vid = v["id"]
        raw_date = v["upload_date"]
        formatted_date = f"{raw_date[:4]}_{raw_date[4:6]}_{raw_date[6:8]}"
        filename = f"{formatted_date}_文字起こし.txt"

        print(f"\n--- 確認中: {v['title']} ({formatted_date}, ID: {vid}) ---")
        transcript_text = fetch_full_transcript(vid)
        if not transcript_text:
            transcript_failure_count += 1
            print(f"[WARN] 文字起こしを取得できなかったため保存をスキップしました: {vid}")
            continue

        save_or_update_drive_file(drive_service, filename, transcript_text)
        saved_count += 1

    if saved_count == 0:
        print(f"[ERROR] 文字起こしを1件も保存できませんでした（取得失敗: {transcript_failure_count}件）。")
        raise SystemExit(1)

    print(f"\n=== 処理完了：{saved_count}件保存 ===")


if __name__ == "__main__":
    main()
