import os
import re
import io
import discord
from dotenv import load_dotenv
import pytz
import requests
from readability import Document
from markdownify import markdownify as md
import pdfplumber
import chardet
import google.generativeai as genai

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- 環境変数 ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
OBSIDIAN_VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# --- Discord Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def get_unique_filepath(folder: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    counter = 2
    candidate = os.path.join(folder, filename)
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base} ({counter}){ext}")
        counter += 1
    return candidate

# --- YouTube 要約保存処理 ---
async def save_youtube_summary_to_obsidian(message):
    url = message.content.strip()
    video_id_match = re.search(r"(?:v=|/)([0-9A-Za-z_-]{11})(?:[&?]|$)", url)
    if not video_id_match:
        return
    video_id = video_id_match.group(1)

    try:
        await message.add_reaction('🔄')  # 処理中マーク

        # 字幕取得
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ja', 'en'])
            transcript_text = " ".join([
                getattr(item, "text", item.get("text", "")) for item in transcript_list
            ])
        except (TranscriptsDisabled, NoTranscriptFound):
            print(f"字幕が見つかりません: {url}")
            await message.clear_reactions()
            await message.add_reaction('🔇')
            return
        except Exception as e:
            print(f"字幕取得で例外: {e}")
            await message.clear_reactions()
            await message.add_reaction('❌')
            return

        if not transcript_text:
            print("取得した字幕テキストが空です")
            await message.clear_reactions()
            await message.add_reaction('🔇')
            return

        # Gemini で要約生成
        if not GEMINI_API_KEY:
            print("Gemini APIキーが必要です")
            await message.clear_reactions()
            await message.add_reaction('⚠️')
            return

        model = genai.GenerativeModel("gemini-2.5-pro")
        prompt = f"以下のYouTube動画の文字起こしを、重要なポイントを3〜5点で簡潔にまとめてください。\n\n{transcript_text}"
        response = model.generate_content(prompt)
        summary = getattr(response, "text", str(response))

        # 動画タイトル取得
        try:
            video_page = requests.get(f"https://www.youtube.com/watch?v={video_id}", timeout=10)
            title_match = re.search(r"<title>(.*?)</title>", video_page.text, flags=re.S | re.I)
            title = title_match.group(1).replace(" - YouTube", "").strip() if title_match else f"YouTube {video_id}"
        except Exception:
            title = f"YouTube {video_id}"

        safe_title = sanitize_filename(title) or f"YouTube-{video_id}"
        summaries_folder = os.path.join(OBSIDIAN_VAULT_PATH, "YouTube Summaries")
        os.makedirs(summaries_folder, exist_ok=True)
        save_path = get_unique_filepath(summaries_folder, f"{safe_title}.md")

        final_content = (
            f"# {title}\n\n"
            f"**Source:** <{url}>\n\n"
            f"---\n\n"
            f"## 要約\n{summary}\n\n"
            f"---\n\n"
            f"## 字幕（抜粋）\n{transcript_text[:4000]}"
        )

        with open(save_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        await message.clear_reactions()
        await message.add_reaction('✅')
        print(f"YouTube動画の要約を保存しました: {title}")

    except Exception as e:
        print(f"YouTube要約処理中にエラー: {url}, エラー: {e}")
        await message.clear_reactions()
        await message.add_reaction('❌')

# --- Discord イベント ---
@client.event
async def on_ready():
    print(f'ログイン完了: {client.user}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # YouTubeリンクを検出したら要約保存
    if "youtube.com/watch" in message.content or "youtu.be/" in message.content:
        await save_youtube_summary_to_obsidian(message)

# --- Bot 起動 ---
if __name__ == "__main__":
    if TOKEN:
        client.run(TOKEN)
    else:
        print("Discord TOKEN が設定されていません。")