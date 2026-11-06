import os
import json
import urllib.request
import xml.etree.ElementTree as ET
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

# 2. 対象チャンネルと保存先フォルダの設定
CHANNEL_ID = 'UCTGk0UuXAgJczNXDJb3EVaQ' # ノックチャンネル
DRIVE_FOLDER_ID = '1kexclZcDIdcE97zNsgmEM3IXtK6vb9IE'

# 3. RSSフィードから最新動画のIDを自動取得
print(f"チャンネルID: {CHANNEL_ID} の最新動画を検索中...")
rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
response = urllib.request.urlopen(rss_url)
tree = ET.parse(response)
root = tree.getroot()

namespace = {'yt': 'http://www.youtube.com/xml/schemas/2015'}
latest_entry = root.find('{http://www.w3.org/2005/Atom}entry')
if latest_entry is None:
    raise ValueError("動画が見つかりませんでした。")
VIDEO_ID = latest_entry.find('yt:videoId', namespace).text
print(f"最新の動画ID [{VIDEO_ID}] を取得しました！")

# 4. RapidAPIへリクエストを送信して文字起こしを取得
url = "https://youtube-transcript3.p.rapidapi.com/api/transcript-with-videoid"
querystring = {"videoId": VIDEO_ID}
headers = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": "youtube-transcript3.p.rapidapi.com"
}

print(f"動画ID: {VIDEO_ID} の文字起こしを取得中...")
api_response = requests.get(url, headers=headers, params=querystring)
api_response.raise_for_status() # エラーがあれば処理を止める
transcript_data = api_response.text # 取得したデータをテキスト化

# 5. Google Driveへ保存するための準備
credentials_dict = json.loads(GCP_CREDENTIALS)
credentials = service_account.Credentials.from_service_account_info(credentials_dict)
drive_service = build('drive', 'v3', credentials=credentials)

# 6. Driveへファイルをアップロード
file_metadata = {
    'name': f'transcript_{VIDEO_ID}.txt', # 保存されるファイル名
    'parents': [DRIVE_FOLDER_ID]
}
media = MediaIoBaseUpload(io.BytesIO(transcript_data.encode('utf-8')), mimetype='text/plain')

print("Google Driveへ保存中...")
drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
print("完了しました！指定のフォルダに保存されました。")
