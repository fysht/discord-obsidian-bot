import discord
from discord.ext import commands
import os
import aiohttp
from datetime import datetime
import google.generativeai as genai

# Gemini APIの設定（手書き文字認識用）
# 環境変数または直接APIキーを設定してください
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
genai.configure(api_key=GEMINI_API_KEY)

class HandwrittenMemo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # --- 設定エリア ---
        self.OBSIDIAN_VAULT_PATH = r"C:\Path\To\Your\Obsidian\Vault"
        
        # 保存先フォルダ設定
        self.ZT_FOLDER = "00_ZeroSecondThinking"    # ZTあり：ゼロ秒思考用
        self.INBOX_FOLDER = "99_Inbox"              # ZTなし：通常メモ用
        
        # 判定用マーカー
        self.MARKER_KEYWORD = "ZT"

    async def check_for_zt_marker(self, image_bytes):
        """
        Gemini APIを使用して、画像内に「ZT」という手書き文字があるか判定する
        """
        try:
            model = genai.GenerativeModel('gemini-2.5-pro')
            
            # 画像データをAPIに渡せる形式に変換
            image_parts = [
                {
                    "mime_type": "image/jpeg", # またはimage/pngなど
                    "data": image_bytes
                }
            ]
            
            prompt = (
                f"この画像の手書きメモの中に「{self.MARKER_KEYWORD}」または「zt」という"
                "アルファベットの識別子が書かれていますか？"
                "書かれている場合は 'YES'、書かれていない場合は 'NO' とだけ答えてください。"
            )

            response = await model.generate_content_async([prompt, image_parts[0]])
            result_text = response.text.strip().upper()
            
            print(f"AI Recognition Result: {result_text}") # デバッグ用ログ
            
            return "YES" in result_text

        except Exception as e:
            print(f"OCR Error: {e}")
            # エラー時は安全側に倒してFalse（通常メモ扱い）にするか、通知するか選択
            return False

    def get_save_path(self, folder_name, filename):
        """保存先のフルパス生成＆フォルダ作成"""
        folder_path = os.path.join(self.OBSIDIAN_VAULT_PATH, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        return os.path.join(folder_path, filename)

    async def save_to_obsidian_daily(self, filename, mode, folder_name):
        """Obsidianの日次ノート（Daily Note）にリンクを追記する"""
        today_str = datetime.now().strftime('%Y-%m-%d')
        daily_note_path = os.path.join(self.OBSIDIAN_VAULT_PATH, "01_Daily", f"{today_str}.md") # 日次ノートのパスは環境に合わせて修正してください
        
        # 日次ノートフォルダがない場合は作成（念のため）
        os.makedirs(os.path.dirname(daily_note_path), exist_ok=True)

        timestamp = datetime.now().strftime('%H:%M')
        # Obsidianのリンク形式 ![[filename]]
        link_text = f"\n\n## {timestamp} スキャンメモ ({mode})\n![[{folder_name}/{filename}]]\n"

        with open(daily_note_path, 'a', encoding='utf-8') as f:
            f.write(link_text)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        # 画像が添付されているか確認
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image'):
                    await self.process_scanned_image(message, attachment)

    async def process_scanned_image(self, message, attachment):
        """スキャン画像をダウンロードし、振り分け処理を行う"""
        
        # 1. 画像をメモリ上にダウンロード
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as resp:
                if resp.status != 200:
                    return
                image_bytes = await resp.read()

        # 2. AIによる「ZT」判定
        # ユーザーへのフィードバック（処理中であることを伝える）
        processing_msg = await message.channel.send("🔍 スキャン画像を解析中...")
        
        is_zt = await self.check_for_zt_marker(image_bytes)
        
        # 3. ファイル名と保存先の決定
        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        original_name, ext = os.path.splitext(attachment.filename)
        
        if is_zt:
            # ゼロ秒思考メモの場合
            folder = self.ZT_FOLDER
            mode_label = "ゼロ秒思考"
            filename = f"ZT_{timestamp_str}{ext}"
            response_text = f"✅ **ゼロ秒思考(ZT)** として認識しました。\n保存先: `{folder}`"
        else:
            # 通常メモの場合
            folder = self.INBOX_FOLDER
            mode_label = "手書きメモ"
            filename = f"Memo_{timestamp_str}{ext}"
            response_text = f"📁 **通常メモ** として保存しました。\n保存先: `{folder}`"

        # 4. ファイル保存
        save_path = self.get_save_path(folder, filename)
        with open(save_path, 'wb') as f:
            f.write(image_bytes)

        # 5. Obsidianの日次ノートへリンク追記
        await self.save_to_obsidian_daily(filename, mode_label, folder)

        # 6. 完了通知
        await processing_msg.delete()
        await message.channel.send(response_text)

async def setup(bot):
    await bot.add_cog(HandwrittenMemo(bot))