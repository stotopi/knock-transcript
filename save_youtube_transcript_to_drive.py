import os
import yt_dlp
from faster_whisper import WhisperModel

# ==========================================
# 既存のGoogle Drive認証・アップロード用関数群は
# そのままここに残す（以下は想定の関数名）
# from my_drive_module import upload_text_to_drive
# ==========================================

def download_audio(video_id, output_dir="./audio_temp"):
    """yt-dlpを使用して動画から音声(MP3)のみを抽出して保存する"""
    os.makedirs(output_dir, exist_ok=True)
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    
    # 映像を無視して最低限の通信量で最高音質を取得する設定
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, f'{video_id}.%(ext)s'),
        'quiet': False, # GitHub Actionsのログに進捗を出す場合はFalse
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    
    print(f"[INFO] {video_id} の音声をダウンロードしています...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])
        
    return os.path.join(output_dir, f"{video_id}.mp3")


def transcribe_audio(audio_path):
    """faster-whisperを使用してローカル(CPU)で文字起こしを実行する"""
    print(f"[INFO] {audio_path} の文字起こしを開始します。数十分かかる場合があります...")
    
    # CPU環境向けに int8 量子化でロードし、速度とメモリを最適化
    model = WhisperModel("base", device="cpu", compute_type="int8")
    
    # 音声を解析
    segments, info = model.transcribe(audio_path, beam_size=5, language="ja")
    print(f"[INFO] 検出言語: {info.language} (確率: {info.language_probability:.2f})")
    
    transcript_text = ""
    # segmentsはジェネレーターになっており、ループで回すたびに順次解析が進む
    for segment in segments:
        line = f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}\n"
        transcript_text += line
        
    return transcript_text


def main():
    # 処理対象の動画IDリスト（ログにあった3件を例として記載）
    video_ids = ['7mrFHdSr0O8', 'aNF9k1IxXUs', 'LSfUG9FIxpI']
    
    success_count = 0
    error_count = 0

    for video_id in video_ids:
        print(f"\n--- 処理中: {video_id} ---")
        audio_file = None
        
        try:
            # 1. YouTubeから音声のみを抽出ダウンロード
            audio_file = download_audio(video_id)
            
            # 2. ダウンロードした音声をAIでテキスト化
            full_text = transcribe_audio(audio_file)
            
            # 3. Googleドライブへ保存（既存の関数を呼び出す）
            # file_name = f"{video_id}_transcript.txt"
            # upload_text_to_drive(file_name, full_text)
            print(f"[SUCCESS] 動画 {video_id} の文字起こしと保存が完了しました。")
            success_count += 1
            
        except Exception as e:
            print(f"[ERROR] 動画 {video_id} の処理中にエラーが発生しました: {e}")
            error_count += 1
            
        finally:
            # 4. 次の動画の処理やランナーの容量制限を考慮し、処理済みの音声ファイルは削除する
            if audio_file and os.path.exists(audio_file):
                os.remove(audio_file)
                print(f"[INFO] 一時ファイル {audio_file} を削除しました。")

    print("\n[INFO] --- 実行結果 ---")
    print(f"成功: {success_count}件, エラー: {error_count}件")
    
    if success_count == 0:
        exit(1)

if __name__ == "__main__":
    main()
