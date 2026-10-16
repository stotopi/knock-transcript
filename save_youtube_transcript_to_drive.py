import io
import json
import os
import sys
from datetime import datetime
from typing import Optional, List, Dict

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ==========================================
# 設定項目
# ==========================================
CHANNEL_ID = "UCOyqJqA00RMBp04E3fGkKwg" # ノックチャンネルのチャンネルID（要確認・修正）
DRIVE_FOLDER_ID = "1v0p6mO7kROq_zGAI-wArsiTBNpbvr12v"
TARGET_VIDEO_URLS = ["https://www.youtube.com/live/7mrFHdSr0O8"]

# 実行状態の集計用
stats = {"success": 0, "already_exists": 0, "error": 0}

def get_drive_service():
    """Google Colab認証 または サービスアカウント認証でDrive APIクライアントを取得"""
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
    """秒数を既存ファイルと同じタイムスタンプ形式に整形"""
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

def get_recent_videos(api_key: str, channel_id: str, max_results: int = 3) -> List[Dict[str, str]]:
    """YouTube Data API v3を使用して最新の配信情報を取得"""
    videos = []
    seen_ids = set()

    try:
        youtube = build('youtube', 'v3', developerKey=api_key)
        
        # 指定URLの取得
        for url in TARGET_VIDEO_URLS:
            video_id = url.split('/')[-1]
            if video_id not in seen_ids:
                request = youtube.videos().list(part="snippet", id=video_id)
                response = request.execute()
                for item in response.get('items', []):
                    seen_ids.add(item['id'])
                    videos.append({
                        "id": item['id'],
                        "title": item['snippet']['title'],
                        "upload_date": item['snippet']['publishedAt'][:10].replace('-', '')
                    })

        # チャンネルの最新動画取得
        request = youtube.search().list(
            part="id,snippet",
            channelId=channel_id,
            maxResults=max_results,
            order="date",
            type="video"
        )
        response = request.execute()
        for item in response.get('items', []):
            vid = item['id'].get('videoId')
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                videos.append({
                    "id": vid,
                    "title": item['snippet']['title'],
                    "upload_date": item['snippet']['publishedAt'][:10].replace('-', '')
                })
    except Exception as e:
        print(f"[ERROR] 動画一覧の取得に失敗しました: {e}")
        stats["error"] += 1

    return videos

def fetch_full_transcript(video_id: str) -> Optional[str]:
    """YouTube動画の日本語文字起こし全文を取得"""
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ja'])
        lines = []
        for item in transcript_list:
            start = item["start"]
            text = item["text"].replace("\n", " ").strip()
            ts_label = format_timestamp(start)
            lines.append(f"{ts_label}{text}  ")
        return "\n".join(lines)
    except (TranscriptsDisabled, NoTranscriptFound):
        print(f"[WARN] 動画 {video_id} は字幕が無効、または日本語字幕がありません。")
    except Exception as e:
        print(f"[ERROR] 動画 {video_id} の字幕取得中にエラー: {e}")
        stats["error"] += 1
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
            print(f"[SKIP] {filename} はすでに全文保存済みです")
            stats["already_exists"] += 1
            return f["id"]
        else:
            print(f"[CLEANUP] 省略版の既存ファイルを削除して更新します...")
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

    print(f"[SUCCESS] {filename} を全文保存しました！")
    stats["success"] += 1
    return created["id"]

def main():
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("[ERROR] YOUTUBE_API_KEY が設定されていません。")
        sys.exit(1)

    print("=== ノックチャンネル 文字起こし全文 自動保存スクリプト開始 ===")
    drive_service = get_drive_service()
    videos = get_recent_videos(api_key, CHANNEL_ID)

    if not videos and stats["error"] == 0:
        print("[WARN] 対象の動画が見つかりませんでした。")
        
    for v in videos:
        vid = v["id"]
        filename = f"{v['upload_date'][:4]}_{v['upload_date'][4:6]}_{v['upload_date'][6:8]}_文字起こし.txt"

        print(f"\n--- 確認中: {v['title']} (ID: {vid}) ---")
        transcript_text = fetch_full_transcript(vid)
        if not transcript_text:
            continue

        save_or_update_drive_file(drive_service, filename, transcript_text)

    print("\n[INFO] --- 実行結果 ---")
    print(f"成功: {stats['success']}件, 重複スキップ: {stats['already_exists']}件, エラー: {stats['error']}件")

    if stats["success"] == 0 and stats["already_exists"] == 0:
        if stats["error"] > 0 or not videos:
            print("[ERROR] 有効な文字起こしを1件も処理できませんでした。ジョブを失敗扱いにします。")
            sys.exit(1)
        
    if stats["error"] > 0:
        print("[ERROR] 一部の処理でエラーが発生したため、ジョブを失敗扱いにします。")
        sys.exit(1)
        
    print("[INFO] 正常終了しました。")

if __name__ == "__main__":
    main()
