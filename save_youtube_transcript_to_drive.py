#!/usr/bin/env python3
import io
import json
import os
import re
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

# 完全自動化のため空欄でOK
TARGET_VIDEO_URLS = []
# 待機枠や配信中を飛ばしてアーカイブを探すため、3件チェックに設定
MAX_RECENT_VIDEOS_TO_CHECK = 3


def get_drive_service():
    """環境変数からサービスアカウント認証情報を読み込み、Drive APIを初期化"""
    sa_json = os.environ.get("GCP_CREDENTIALS") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise ValueError("環境変数 GCP_CREDENTIALS が設定されていません。")

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
    """Googleドライブ上に既にファイルが存在するか確認"""
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
            print(f"[SKIP] {filename} はすでに保存済みです (サイズ: {file_size:,} bytes)")
            return True
    return False


def get_recent_videos() -> List[Dict[str, str]]:
    """対象動画および直近の配信メタデータを取得（Bot検知完全回避版）"""
    videos: List[Dict[str, str]] = []
    seen_ids = set()

    # 個別動画抽出用のydl設定（モバイル偽装でBot判定を回避）
    ydl_opts_single = {
        "quiet": True,
        "skip_download": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
    }

    # 1. チャンネルURLから標準HTTP通信で動画IDリストを直接スクレイピング
    try:
        req = urllib.request.Request(
            CHANNEL_STREAMS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        html = urllib.request.urlopen(req).read().decode("utf-8")
        
        # HTML内から動画IDを抽出（順番を保持して重複排除）
        found_vids = []
        for match in re.finditer(r'"videoId":"([a-zA-Z0-9_-]{11})"', html):
            vid = match.group(1)
            if vid not in found_vids:
                found_vids.append(vid)

        print(f"[INFO] チャンネルページから {len(found_vids)} 件の動画IDを検出しました。")

        # 上から順番に最大数まで詳細をチェック
        valid_count = 0
        with yt_dlp.YoutubeDL(ydl_opts_single) as ydl:
            for vid in found_vids[:10]: # 最新10件から探す
                if valid_count >= MAX_RECENT_VIDEOS_TO_CHECK:
                    break
                if vid in seen_ids:
                    continue
                
                try:
                    info = ydl.extract_info(vid, download=False)
                    live_status = info.get("live_status")
                    
                    if live_status in ["is_live", "is_upcoming"]:
                        print(f"[SKIP] 配信中または待機枠のためスキップします: {info.get('title')}")
                        continue
                    
                    seen_ids.add(vid)
                    videos.append({
                        "id": vid,
                        "title": info.get("title", ""),
                        "upload_date": info.get("upload_date") or datetime.now().strftime("%Y%m%d"),
                    })
                    valid_count += 1
                except Exception as e:
                    print(f"[WARN] 動画詳細の取得スキップ ({vid}): {e}")

    except Exception as e:
        print(f"[ERROR] チャンネル一覧のHTML取得に失敗: {e}")

    # 2. 直接指定分（もしあれば追加）
    with yt_dlp.YoutubeDL(ydl_opts_single) as ydl:
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
                pass

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
    """faster-whisperを使用して文字起こしを実行"""
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
    """Googleドライブへアップロード保存"""
    v3_query = f"'{DRIVE_FOLDER_ID}' in parents and name contains '{filename.replace('.txt', '')}' and trashed = false"

    res = drive_service.files().list(
        q=v3_query,
        fields="files(id, name, mimeType, size)",
        pageSize=10,
    ).execute()
    existing_files = res.get("files", [])

    content_bytes = content.encode("utf-8")

    for f in existing_files:
        try:
            drive_service.files().delete(fileId=f["id"]).execute()
        except Exception as e:
            pass

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

        if is_already_saved(drive_service, filename):
            success_count += 1
            continue

        audio_file = None
        try:
            audio_file = download_audio(vid)
            transcript_text = transcribe_audio(audio_file)
            
            if not transcript_text:
                print(f"[WARN] 有効な文字起こしテキストが生成されませんでした。")
                error_count += 1
                continue

            save_or_update_drive_file(drive_service, filename, transcript_text)
            success_count += 1

        except Exception as e:
            print(f"[ERROR] 動画 {vid} の処理中にエラーが発生しました: {e}")
            error_count += 1

        finally:
            if audio_file and os.path.exists(audio_file):
                os.remove(audio_file)

    print("\n[INFO] --- 実行結果 ---")
    print(f"成功/スキップ: {success_count}件, エラー: {error_count}件")

    if success_count == 0 and error_count > 0:
        exit(1)


if __name__ == "__main__":
    main()
