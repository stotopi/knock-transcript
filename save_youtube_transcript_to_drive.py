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

# 優先取得する動画URL（個別指定がない場合は空でOK）
TARGET_VIDEO_URLS = []
# チェックする最新配信の件数（無料枠節約のため最新1件に設定）
MAX_RECENT_VIDEOS_TO_CHECK = 1


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
        "extract_flat": False, # 詳細なメタデータ（live_status等）を取得するため False のまま
        "skip_download": True,
        "playlistend": MAX_RECENT_VIDEOS_TO_CHECK, # 先ほど 3 に変更した箇所
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
    }
    videos: List[Dict[str, str]] = []
    seen_ids = set()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # (TARGET_VIDEO_URLSの処理はそのまま)
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

        # チャンネル一覧からの取得処理
        try:
            playlist_info = ydl.extract_info(CHANNEL_STREAMS_URL, download=False)
            entries = playlist_info.get("entries", []) if playlist_info else []
            for entry in entries:
                if not entry:
                    continue
                
                # ▼ ここを追加: 待機枠や配信中の動画を除外する ▼
                live_status = entry.get("live_status")
                if live_status in ["is_live", "is_upcoming"]:
                    print(f"[SKIP] 配信中または待機枠のためスキップします: {entry.get('title')}")
                    continue
                # ▲ ここまで ▲

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
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios']
            }
        },
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


def save_or_update_drive_file(drive_service, filename: str, content: str) -> str:
    """Googleドライブへ文字起こしテキストをアップロード保存"""
    v3_query = f"'{DRIVE_FOLDER_ID}' in parents and name contains '{filename.replace('.txt', '')}' and trashed = false"

    res = drive_service.files().list(
        q=v3_query,
        fields="files(id, name, mimeType, size)",
        pageSize=10,
    ).execute()
    existing_files = res.get("files", [])

    content_bytes = content.encode("utf-8")

    # 古いファイルや省略版が存在する場合は上書きのために一度クリーンアップ
    for f in existing_files:
        try:
            drive_service.files().delete(fileId=f["id"]).execute()
            print(f"[CLEANUP] 既存のファイル ({f['name']}) を整理しました。")
        except Exception as e:
            print(f"[WARN] 既存ファイル削除エラー: {e}")

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

    print(f"[SUCCESS] {filename} をドライブに保存しました！ ({len(content_bytes):,} bytes)")
    print(f"          ファイルリンク: {created.get('webViewLink')}")
    return created["id"]


def main():
    print("=== ノックチャンネル 文字起こし全文 自動保存スクリプト開始 ===")
    drive_service = get_drive_service()
    videos = get_recent_videos()

    if not videos:
        print("対象の動画が見つかりませんでした。")
        return

    success_count = 0
    error_count = 0

    for v in videos:
        vid = v["id"]
        raw_date = v["upload_date"]
        formatted_date = f"{raw_date[:4]}_{raw_date[4:6]}_{raw_date[6:8]}"
        filename = f"{formatted_date}_文字起こし.txt"

        print(f"\n--- 確認中: {v['title']} ({formatted_date}, ID: {vid}) ---")

        # 1. 既に保存済みなら重い処理（ダウンロード・AI認識）をスキップ
        if is_already_saved(drive_service, filename):
            success_count += 1
            continue

        audio_file = None
        try:
            # 2. 音声のみ抽出ダウンロード
            audio_file = download_audio(vid)

            # 3. faster-whisperで文字起こし
            transcript_text = transcribe_audio(audio_file)
            if not transcript_text:
                print(f"[WARN] 有効な文字起こしテキストが生成されませんでした。")
                error_count += 1
                continue

            # 4. Googleドライブへ保存
            save_or_update_drive_file(drive_service, filename, transcript_text)
            success_count += 1

        except Exception as e:
            print(f"[ERROR] 動画 {vid} の処理中にエラーが発生しました: {e}")
            error_count += 1

        finally:
            # 5. 一時保存した音声ファイルを削除して容量を解放
            if audio_file and os.path.exists(audio_file):
                os.remove(audio_file)
                print(f"[INFO] 一時音声ファイル ({audio_file}) を削除しました。")

    print("\n[INFO] --- 実行結果 ---")
    print(f"成功/スキップ: {success_count}件, エラー: {error_count}件")

    if success_count == 0 and error_count > 0:
        exit(1)


if __name__ == "__main__":
    main()
