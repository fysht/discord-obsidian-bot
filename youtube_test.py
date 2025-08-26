# -*- coding: utf-8 -*-
import os
import re
import discord
from dotenv import load_dotenv
import requests
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- 環境変数 ---
load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
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

# --- YouTube 字幕抽出の汎用ヘルパー ---
def extract_transcript_text(fetched):
    """
    fetched は
      - 古いバージョン: list[dict] のこともある
      - 新しい v1 系: FetchedTranscript（イテラブル）を返し、要素は FetchedTranscriptSnippet オブジェクト（.text属性）になる
    この関数は両者を吸収して文字列を返す。
    """
    texts = []
    # 1) もし list の生データで来たら
    if isinstance(fetched, list):
        for it in fetched:
            if isinstance(it, dict):
                texts.append(it.get('text', ''))
            elif hasattr(it, 'text'):
                texts.append(getattr(it, 'text', ''))
            else:
                # 最低限文字列化
                texts.append(str(it))
        return " ".join(t.strip() for t in texts if t)

    # 2) FetchedTranscript などイテラブルなオブジェクト
    try:
        for snippet in fetched:
            if isinstance(snippet, dict):
                texts.append(snippet.get('text', ''))
            elif hasattr(snippet, 'text'):
                texts.append(getattr(snippet, 'text', ''))
            else:
                try:
                    texts.append(snippet['text'])
                except Exception:
                    texts.append(str(snippet))
        return " ".join(t.strip() for t in texts if t)
    except TypeError:
        # 3) to_raw_data() を試す（README で推奨されている方法）
        if hasattr(fetched, 'to_raw_data'):
            raw = fetched.to_raw_data()
            return " ".join(item.get('text', '').strip() for item in raw if item.get('text'))
        # 4) 最終的フォールバック
        return str(fetched)

# --- YouTube 要約保存処理 ---
async def save_youtube_summary_to_obsidian(message):
    url = message.content.strip()
    # 動画ID抽出（youtu.be / watch?v= の両方に対応）
    video_id_match = re.search(r"(?:v=|/)([0-9A-Za-z_-]{11})(?:[&?]|$)", url)
    if not video_id_match:
        return
    video_id = video_id_match.group(1)
    try:
        await message.add_reaction('🔄')  # 処理中
        # 字幕取得
        try:
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=['ja', 'en'])
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            print(f"字幕が見つかりません: {url} / {e}")
            try:
                await message.clear_reactions()
            except:
                pass
            await message.add_reaction('🔇')
            return
        except Exception as e:
            print(f"字幕取得で例外: {e}")
            try:
                await message.clear_reactions()
            except:
                pass
            await message.add_reaction('❌')
            return

        # 安全にテキスト化
        transcript_text = extract_transcript_text(fetched)
        if not transcript_text:
            print("取得した字幕テキストが空です")
            try:
                await message.clear_reactions()
            except:
                pass
            await message.add_reaction('🔇')
            return

        # Gemini 要約
        if not GEMINI_API_KEY:
            print("エラー: YouTube要約にGemini APIキーが必要です。")
            try:
                await message.clear_reactions()
            except:
                pass
            await message.add_reaction('⚠️')
            return
        model = genai.GenerativeModel('gemini-2.5-pro')
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
        final_content = f"# {title}\n\n**Source:** <{url}>\n\n---\n\n## 要約\n{summary}\n\n---\n\n## 字幕（抜粋）\n{transcript_text[:4000]}"
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        try:
            await message.clear_reactions()
        except:
            pass
        await message.add_reaction('✅')
        print(f"YouTube動画の要約を保存しました: {title}")
    except Exception as e:
        print(f"YouTube要約処理中にエラー: {url}, エラー: {e}")
        try:
            await message.clear_reactions()
        except:
            pass
        await message.add_reaction('❌')

# --- on_ready ---
@client.event
async def on_ready():
    print(f'{client.user} としてログインしました')
    if not OBSIDIAN_VAULT_PATH:
        print("エラー: OBSIDIAN_VAULT_PATHが未設定")
        await client.close()
        return

    channel_name = 'youtube-summaries'
    print(f'--- チャンネル \"{channel_name}\" 同期開始 ---')
    target_channel = discord.utils.get(client.get_all_channels(), name=channel_name)
    if not target_channel:
        print(f'エラー: チャンネル \"{channel_name}\" が見つかりません')
    else:
        messages_to_process = []
        async for message in target_channel.history(limit=200):
            if not message.author.bot and not any(r.emoji == '✅' for r in message.reactions):
                messages_to_process.append(message)
        messages_to_process.reverse()
        for message in messages_to_process:
            await save_youtube_summary_to_obsidian(message)
        print(f'--- \"{channel_name}\" 同期完了 ---')

    await client.close()

if TOKEN:
    client.run(TOKEN)
else:
    print("【エラー】Discordトークンが見つかりません")