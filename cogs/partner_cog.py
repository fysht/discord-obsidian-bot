import discord
from discord.ext import commands
from discord import app_commands
from google import genai
from google.genai import types
import os
import datetime
import asyncio
import logging
import re
import zoneinfo
import io

# Google Drive API
# 修正: サービスアカウントではなく、ユーザー認証(token.json)用のライブラリを使用
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# 既存のユーティリティ
from web_parser import parse_url_with_readability
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SCOPES = ['https://www.googleapis.com/auth/drive']
# 修正: service_account.json ではなく token.json を使用
TOKEN_FILE = 'token.json'

class PartnerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.gemini_client = None
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        
        self.last_interaction = None

    # --- Google Drive Helper Methods ---
    def _get_drive_service(self):
        """トークンファイルを使用してDriveサービスを取得（修正済み）"""
        creds = None
        # token.json が存在すれば読み込む
        if os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except Exception as e:
                logging.error(f"PartnerCog: Token read error: {e}")

        # トークンが無効または期限切れの場合のリフレッシュ処理
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    # リフレッシュしたトークンを保存
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                except Exception as e:
                    logging.error(f"PartnerCog: Token refresh error: {e}")
                    return None
            else:
                logging.error("PartnerCog: Valid token not found.")
                return None

        try:
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            logging.error(f"PartnerCog: Drive Init Error: {e}")
            return None

    async def _find_file(self, service, parent_id, name):
        """指定フォルダ内のファイル/フォルダIDを検索"""
        query = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        results = await asyncio.to_thread(
            lambda: service.files().list(q=query, fields="files(id)").execute()
        )
        files = results.get('files', [])
        return files[0]['id'] if files else None

    async def _create_folder(self, service, parent_id, name):
        """フォルダ作成"""
        file_metadata = {
            'name': name,
            'parents': [parent_id],
            'mimeType': 'application/vnd.google-apps.folder'
        }
        file = await asyncio.to_thread(
            lambda: service.files().create(body=file_metadata, fields='id').execute()
        )
        return file.get('id')

    async def _upload_text(self, service, parent_id, name, content):
        """テキストファイルをアップロード"""
        file_metadata = {'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        file = await asyncio.to_thread(
            lambda: service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        )
        return file.get('id')

    async def _read_text_file(self, service, file_id):
        """テキストファイルの中身を読み込む"""
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = await asyncio.to_thread(downloader.next_chunk)
            return fh.getvalue().decode('utf-8')
        except Exception as e:
            logging.error(f"Read File Error: {e}")
            return ""

    async def _update_daily_note_link(self, service, date_str, link_text, section_header):
        """デイリーノートにリンクを追記する"""
        loop = asyncio.get_running_loop()
        
        daily_folder = await self._find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder:
            daily_folder = await self._create_folder(service, self.drive_folder_id, "DailyNotes")

        filename = f"{date_str}.md"
        f_id = await self._find_file(service, daily_folder, filename)
        
        content = ""
        if f_id:
            content = await self._read_text_file(service, f_id)
        else:
            content = f"# Daily Note {date_str}\n\n"

        new_content = update_section(content, link_text, section_header)

        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        if f_id:
            await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else:
            await loop.run_in_executor(None, lambda: service.files().create(body={'name': filename, 'parents': [daily_folder]}, media_body=media).execute())

    # --- Core Logic ---

    async def _save_data_to_drive(self):
        """(既存の処理) 会話ログの一時保存など"""
        pass 

    async def _fetch_yesterdays_journal(self):
        """(既存の処理) 前日の日記取得"""
        return ""

    async def _build_conversation_context(self, channel, limit=50, ignore_msg_id=None):
        """会話履歴を取得"""
        messages = []
        async for msg in channel.history(limit=limit, oldest_first=False):
            if ignore_msg_id and msg.id == ignore_msg_id:
                continue
            
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments: text += " [メディア送信]"
            messages.append({'role': role, 'text': text})
        
        return list(reversed(messages))

    async def _process_and_save_content(self, message, url, content_type, title, raw_text):
        """記事・動画の保存処理"""
        date_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:30]
        
        folder_name = "YouTube" if content_type == "YouTube" else "WebClips"
        section_header = "## YouTube" if content_type == "YouTube" else "## WebClips"
        
        file_basename = f"{date_str}-{safe_title}"
        filename = f"{file_basename}.md"
        
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service:
            await message.add_reaction('❌')
            return

        final_content = ""
        
        if content_type == "YouTube":
            user_comment = message.content.replace(url, "").strip()
            final_content = (
                f"# {title}\n\n"
                f"- **URL:** {url}\n"
                f"- **Saved at:** {datetime.datetime.now(JST)}\n\n"
                f"## Note\n{user_comment}\n\n"
                f"---\n"
            )
        else:
            if len(raw_text) < 50: return 
            prompt = f"以下のWeb記事をObsidian保存用にMarkdownで整理。\nタイトル: {title}\nURL: {url}\n\n{raw_text}"
            try:
                response = await self.gemini_client.aio.models.generate_content(
                    model='gemini-2.5-pro', 
                    contents=prompt
                )
                final_content = f"{response.text}\n\n---\n**Saved at:** {datetime.datetime.now(JST)}"
            except Exception as e:
                logging.error(f"Summary Gen Error: {e}")
                return

        try:
            folder_id = await self._find_file(service, self.drive_folder_id, folder_name)
            if not folder_id: 
                folder_id = await self._create_folder(service, self.drive_folder_id, folder_name)
            
            await self._upload_text(service, folder_id, filename, final_content)

            link_str = f"- [[{folder_name}/{file_basename}|{title}]]"
            await self._update_daily_note_link(service, date_str, link_str, section_header)

            await message.reply(f"✅ {content_type}情報を保存し、日記にリンクしました！\n📂 `{folder_name}/{filename}`")
        
        except Exception as e:
            logging.error(f"Save Process Error: {e}")
            await message.add_reaction('❌')

    async def _generate_reply(self, channel, inputs: list, trigger_type="reply", extra_context="", ignore_msg_id=None):
        if not self.gemini_client: return None
        
        weather_info = "天気情報取得不可"
        stock_info = "株価情報取得不可" 
        
        yesterday_memory = await self._fetch_yesterdays_journal()
        
        system_prompt = (
            f"あなたはユーザーの知的パートナーAIです。\n"
            f"現在日時: {datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')}\n"
            f"天気: {weather_info}\n"
            f"株価: {stock_info}\n"
            f"昨日の記憶: {yesterday_memory}\n"
            f"ユーザーの文脈: {extra_context}\n"
            "返答は簡潔に、親しみを込めて。"
        )

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        
        recent_msgs = await self._build_conversation_context(channel, limit=30, ignore_msg_id=ignore_msg_id)
        
        for msg in recent_msgs:
            contents.append(types.Content(role=msg['role'], parts=[types.Part.from_text(text=msg['text'])]))
        
        user_parts = []
        for inp in inputs:
            if isinstance(inp, str): user_parts.append(types.Part.from_text(text=inp))
            else: user_parts.append(inp)
        
        if user_parts:
            contents.append(types.Content(role="user", parts=user_parts))
        else:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text="(きっかけ)")]))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.5-pro', 
                contents=contents, 
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
                )
            )
            return response.text
        except Exception as e:
            logging.error(f"GenAI Error: {e}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.channel_id: return

        self.last_interaction = datetime.datetime.now(JST)
        await self._save_data_to_drive()

        url_match = re.search(r'https?://\S+', message.content)
        input_parts = [message.content]
        extra_ctx = ""

        if url_match:
            url = url_match.group()
            is_youtube = "youtube.com" in url or "youtu.be" in url
            
            async with message.channel.typing():
                try:
                    title, text_content = await asyncio.to_thread(parse_url_with_readability, url)
                    if is_youtube:
                        await self._process_and_save_content(message, url, "YouTube", title, text_content)
                        extra_ctx = f"ユーザーがYouTube動画を共有しました: {title}"
                    else:
                        await self._process_and_save_content(message, url, "WebClip", title, text_content)
                        extra_ctx = f"ユーザーがWeb記事を共有しました: {title}\n内容要約: {text_content[:200]}..."
                except Exception as e:
                    logging.error(f"URL Parse Error: {e}")
                    await message.add_reaction('⚠️')

        async with message.channel.typing():
            reply = await self._generate_reply(
                message.channel, 
                input_parts, 
                trigger_type="reply", 
                extra_context=extra_ctx, 
                ignore_msg_id=message.id
            )
            if reply:
                await message.reply(reply)

async def setup(bot):
    await bot.add_cog(PartnerCog(bot))