import os
import json
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# 1. GitHubのSecrets（金庫）から鍵を読み込む
RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY')
GCP_CREDENTIALS = os.environ.get('GCP_CREDENTIALS')

if not RAPIDAPI_KEY or not GCP_CREDENTIALS:
    raise ValueError("APIキーまたは認証情報が設定されていません。GitHubのSecretsを確認してください。")

# 2. 対象の動画IDと保存先の設定
VIDEO_ID = 'ここにノックチャンネル等の対象動画IDを入れる' # 例: HymDukNiXdU
DRIVE_FOLDER_ID = 'ここにGoogle Driveの保存先フォルダIDを入れる'

# 3. RapidAPIへリクエストを送信して文字起こしを取得
# ※注意: 以下の url と x-rapidapi-host は、RapidAPI画面の「Code Snippets (Python > Requests)」を参考に正しいものに書き換えてください。
url = "https://YOUR_RAPIDAPI_HOST_HERE/..." 
querystring = {"videoId": VIDEO_ID}
headers = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": "YOUR_RAPIDAPI_HOST_HERE"
}

print(f"動画ID: {VIDEO_ID} の文字起こしを取得中...")
response = requests.get(url, headers=headers, params=querystring)
response.raise_for_status() # エラーがあればここで処理を止める
transcript_data = response.text # 取得したデータをテキスト化

# 4. Google Driveへ保存するための準備
credentials_dict = json.loads(GCP_CREDENTIALS)
credentials = service_account.Credentials.from_service_account_info(credentials_dict)
drive_service = build('drive', 'v3', credentials=credentials)

# 5. Driveへファイルをアップロード
file_metadata = {
    'name': f'transcript_{VIDEO_ID}.txt', # 保存されるファイル名
    'parents': [DRIVE_FOLDER_ID]
}
media = MediaIoBaseUpload(io.BytesIO(transcript_data.encode('utf-8')), mimetype='text/plain')

print("Google Driveへ保存中...")
file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
print(f"完了しました！ ファイルID: {file.get('id')} に保存されました。")
