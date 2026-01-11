import os
import discord
from discord.ext import commands
import logging
from datetime import datetime
import zoneinfo
import google.generativeai as genai
import aiohttp
import dropbox
from dropbox.files import WriteMode
import re
import asyncio
import json

# --- 共通関数をインポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class JournalCog(commands.Cog):
    """
    手書き振り返りに対するAIアドバイザー機能を提供するCog。
    HandwrittenMemoCogから呼び出され、ライフログと手書き内容を統合して分析・保存します。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()

        if not self._validate_env_vars():
            return

        try:
            self.session = aiohttp.ClientSession()
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") 
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token, 
                app_key=self.dropbox_app_key, 
                app_secret=self.dropbox_app_secret
            )
            self.is_ready = True
            logging.info("✅ JournalCog (Advisor Mode) initialized.")
        except Exception as e:
            logging.error(f"JournalCog init failed: {e}")

    def _load_env_vars(self):
        # 呼び出し元が機能していればここも問題ないはずですが、念のため独立して設定を持ちます
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

    def _validate_env_vars(self) -> bool:
        required = ["GEMINI_API_KEY", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"]
        return all(getattr(self, name.lower(), None) for name in required)

    async def cog_unload(self):
        if self.session: await self.session.close()

    # --- Helper Methods ---

    async def _get_life_logs_content(self, date_str: str) -> str:
        """指定された日付のLifeLogsセクション（時間記録）を取得する"""
        if not self.dbx: return ""
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
            content = res.content.decode('utf-8')
            # "## Life Logs" から次の見出しまでを抽出
            match = re.search(r'##\s*Life\s*Logs\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
            return ""
        except: return ""

    # --- Core Logic: Called by HandwrittenMemoCog ---

    async def process_handwritten_journal(self, handwritten_content: str, date_str: str) -> discord.Embed:
        """
        手書きメモの内容（OCR結果）を受け取り、ライフログと統合してジャーナルとアドバイスを生成する。
        
        Args:
            handwritten_content (str): OCRで読み取った振り返りの内容（Markdownテキスト）
            date_str (str): 対象の日付 "YYYY-MM-DD"
            
        Returns:
            discord.Embed: AIからのアドバイスを含むEmbed
        """
        if not self.is_ready:
            return discord.Embed(title="Error", description="JournalCog is not ready.", color=discord.Color.red())

        # 1. その日のライフログ (時間記録) を取得
        life_logs = await self._get_life_logs_content(date_str)
        
        # 2. AIによる分析とアドバイス生成
        try:
            prompt = f"""
            あなたはユーザーの専属コーチです。
            ユーザーが書いた「手書きの振り返り（OCR）」と、システムが記録した「ライフログ（時間記録）」を統合し、
            **今日一日の分析と、明日への具体的なアドバイス**を行ってください。

            # 情報ソース
            ## 【A】ライフログ（客観的な時間の使い方）
            {life_logs if life_logs else "(記録なし)"}
            
            ## 【B】手書きの振り返り（ユーザーの主観・思考）
            {handwritten_content}

            # 指示
            以下のフォーマットでMarkdownテキストを出力してください。
            ユーザーへの語りかけ口調（「〜ですね」「〜しましょう」）で書いてください。

            ### 1. 🤖 AI Analysis & Advice
            - **時間の使い方**: ライフログと振り返りを照らし合わせ、時間の使い方の傾向や、集中できていた点、改善できる点を指摘してください。
            - **メンタルケア**: ユーザーの感情に寄り添い、ポジティブなフィードバックや励ましを行ってください。
            - **明日への提案**: 明日具体的に意識すべきアクションを1〜2点提案してください。

            ### 2. 📝 Daily Summary
            - 今日の出来事を簡潔に（3〜5行程度で）要約してください。これは後で見返すための記録です。
            """
            
            response = await self.gemini_model.generate_content_async(prompt)
            ai_output = response.text.strip()
        
        except Exception as e:
            logging.error(f"AI Journal Generation Error: {e}")
            ai_output = f"⚠️ AI分析に失敗しました。\n\nAdvice generation failed: {e}"

        # 3. Obsidianへの保存データ作成
        # 手書き内容（オリジナル） + AIのアドバイス + サマリー をまとめて保存
        full_content_to_save = f"""
{ai_output}

### Source (Handwritten OCR)
{handwritten_content}
"""
        
        # 4. Obsidianに保存 (セクション: ## Journal)
        save_success = await self._save_to_obsidian(date_str, full_content_to_save, "## Journal")
        
        # 5. Discordへの返信Embed作成
        # Embedには「AIのアドバイス」部分をメインに表示する
        
        # 出力から "### 1. 🤖 AI Analysis & Advice" の部分だけを抽出して表示（簡易的なパース）
        advice_part = ai_output
        if "### 1. 🤖 AI Analysis & Advice" in ai_output:
            parts = ai_output.split("### 2. 📝 Daily Summary")
            advice_part = parts[0].replace("### 1. 🤖 AI Analysis & Advice", "").strip()

        embed = discord.Embed(
            title=f"🤖 AI Advice for {date_str}",
            description=advice_part[:4000], # 文字数制限対策
            color=discord.Color.gold()
        )
        
        footer_text = "Obsidian: Saved ✅" if save_success else "Obsidian: Save Failed ❌"
        embed.set_footer(text=f"{footer_text} | Based on handwritten log")

        return embed

    async def _save_to_obsidian(self, date_str: str, content_to_add: str, section: str) -> bool:
        path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, path)
                current = res.content.decode('utf-8')
            except: 
                # ファイルがない場合は本来ありえない（HandwrittenMemoCogで作られているはず）が、念のため空で作成
                current = f"# Daily Note {date_str}\n"
            
            new_content = update_section(current, content_to_add, section)
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), path, mode=WriteMode('overwrite'))
            return True
        except Exception as e:
            logging.error(f"Obsidian save error: {e}")
            return False

async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))