import discord
from discord.ext import commands
import os
from datetime import datetime
import google.generativeai as genai
import asyncio

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# --- 定数定義 ---
ZT_FOLDER_NAME = "00_ZeroSecondThinking"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class ZeroSecondThinking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)

    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request()); open(TOKEN_FILE,'w').write(creds.to_json())
                except: return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    def _find_file(self, service, parent_id, name):
        res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _create_folder(self, service, parent_id, name):
        f = service.files().create(body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id').execute()
        return f.get('id')

    async def _save_to_drive(self, filename, content):
        if not self.drive_folder_id: return False
        
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False

        zt_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, ZT_FOLDER_NAME)
        if not zt_folder: zt_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, ZT_FOLDER_NAME)
        
        # 既存ファイルがあれば追記、なければ作成
        file_id = await loop.run_in_executor(None, self._find_file, service, zt_folder, filename)
        
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        
        if file_id:
            # 追記（Google Drive APIで直接追記はできないため、一度読み込んで結合して更新するか、別ファイルにするが、
            # ここではシンプルに「既存取得→結合→更新」を行う）
            # ※ 頻繁な追記には向かないが、この用途なら許容範囲
            pass 
            # 実装簡略化のため、今回は「ファイル名に時刻を含めて毎回新規作成」または「ダウンロードして追記アップロード」
            # ここでは「ダウンロードして追記」を実装
            
            # ダウンロード
            fh = io.BytesIO()
            from googleapiclient.http import MediaIoBaseDownload
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
            done=False
            while not done: _, done = downloader.next_chunk()
            current_content = fh.getvalue().decode('utf-8')
            
            new_content = current_content + content
            media_update = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown')
            await loop.run_in_executor(None, lambda: service.files().update(fileId=file_id, media_body=media_update).execute())
            
        else:
            await loop.run_in_executor(None, lambda: service.files().create(body={'name': filename, 'parents': [zt_folder], 'mimeType': 'text/markdown'}, media_body=media).execute())
            
        return True

    async def generate_zt_themes(self, keyword=None):
        try:
            model = genai.GenerativeModel('gemini-2.5-pro')
            user_intent = f"「{keyword}」というキーワードに関連して" if keyword else "今、何を書くべきか迷っている状態に対して、頭の中を整理するために"
            prompt = (
                f"あなたは『ゼロ秒思考（赤羽雄二氏提唱）』のメモ書きファシリテーターです。\n"
                f"ユーザーは{user_intent}、1分間で書き出すためのメモのタイトル（テーマ）を求めています。\n"
                "ユーザーの思考を深掘りし、感情や課題を吐き出させるような、具体的で刺激的なタイトルを5つ提案してください。\n\n"
                "**条件:**\n"
                "1. タイトルは疑問形（～はなぜか？、～をどうするか？など）を中心にする。\n"
                "2. 抽象的な言葉だけでなく、具体的で少しドキッとするような切り口も含める。\n"
                "3. 箇条書きで出力する。\n"
                "4. 余計な挨拶は省略し、テーマ案だけを出力する。"
            )
            response = await model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            print(f"Gemini API Error: {e}")
            return "（AI生成エラー）申し訳ありません。現在テーマを生成できません。"

    @commands.command(name='zt_theme', aliases=['theme'])
    async def suggest_theme(self, ctx, *, text=None):
        async with ctx.typing():
            suggestions = await self.generate_zt_themes(text)
        header = f"💡 **「{text if text else 'おまかせ'}」に関するゼロ秒思考テーマ案**"
        message = f"{header}\n\n{suggestions}\n\n*気になったものを1つ選んで、1分間で書き殴ってみましょう！*"
        await ctx.send(message)

    @commands.command(name='zt')
    async def digital_zt(self, ctx, *, content):
        date_str = datetime.now().strftime('%Y-%m-%d')
        filename = f"{date_str}_ZeroSecondThinking.md"
        
        entry = f"\n\n## {datetime.now().strftime('%H:%M')} (Digital)\n{content}\n"
        
        success = await self._save_to_drive(filename, entry)
        if success: await ctx.message.add_reaction('✅')
        else: await ctx.send("❌ Google Driveへの保存に失敗しました。")

async def setup(bot):
    await bot.add_cog(ZeroSecondThinking(bot))