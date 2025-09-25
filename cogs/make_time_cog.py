import os
import discord
from discord.ext import commands, tasks
import logging
import datetime
import zoneinfo
import google.generativeai as genai
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import aiohttp
import openai
from pathlib import Path
import re # 正規表現ライブラリをインポート

from utils.obsidian_utils import update_section
import dropbox

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HIGHLIGHT_PROMPT_TIME = datetime.time(hour=7, minute=30, tzinfo=JST)
TUNING_PROMPT_TIME = datetime.time(hour=21, minute=30, tzinfo=JST)
HIGHLIGHT_EMOJI = "✨"
SUPPORTED_AUDIO_TYPES = ['audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm']


class MakeTimeCog(commands.Cog):
    """書籍『とっぱらう』の習慣を実践するためのCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.session = aiohttp.ClientSession()

        # ユーザーの状態を一時的に保存
        self.user_states = {}

        if not self._are_credentials_valid():
            logging.error("MakeTimeCog: 必須の環境変数が不足。Cogを無効化します。")
            return
        try:
            # 各APIクライアントを初期化
            self.creds = self._get_google_credentials()
            self.gemini_model = self._initialize_ai_model()
            self.dbx = self._initialize_dropbox_client()
            if self.openai_api_key:
                self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            self.is_ready = True
            logging.info("✅ MakeTimeCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ MakeTimeCogの初期化中にエラー: {e}", exc_info=True)

    def _load_environment_variables(self):
        self.maketime_channel_id = int(os.getenv("MAKETIME_CHANNEL_ID", 0))
        self.google_token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")

    def _are_credentials_valid(self) -> bool:
        return all([
            self.maketime_channel_id, self.google_token_path, self.gemini_api_key,
            self.openai_api_key, self.dropbox_refresh_token, self.dropbox_vault_path
        ])

    def _get_google_credentials(self):
        token_path = self.google_token_path
        if os.getenv("RENDER"):
             token_path = f"/etc/secrets/{os.path.basename(token_path)}"
        if os.path.exists(token_path):
            return Credentials.from_authorized_user_file(token_path, ['https://www.googleapis.com/auth/calendar'])
        return None
    
    def _initialize_ai_model(self):
        genai.configure(api_key=self.gemini_api_key)
        return genai.GenerativeModel("gemini-2.5-pro")

    def _initialize_dropbox_client(self):
        return dropbox.Dropbox(
            app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret,
            oauth2_refresh_token=self.dropbox_refresh_token
        )
    
    async def cog_unload(self):
        await self.session.close()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.prompt_daily_highlight.is_running(): self.prompt_daily_highlight.start()
            if not self.prompt_daily_tuning.is_running(): self.prompt_daily_tuning.start()

    def cog_unload(self):
        self.prompt_daily_highlight.cancel()
        self.prompt_daily_tuning.cancel()

    # --- ハイライト選択フロー ---
    @tasks.loop(time=HIGHLIGHT_PROMPT_TIME)
    async def prompt_daily_highlight(self):
        channel = self.bot.get_channel(self.maketime_channel_id)
        if not channel: return
        
        advice_text = (
            "おはようございます！今日という一日を最高のものにするため、**今日のハイライト**を決めましょう。\n\n"
            "ハイライトを選ぶための3つの基準を参考にしてください:\n"
            "1. **緊急性**: 今日やらなければならないことは何ですか？\n"
            "2. **満足感**: 一日の終わりに「これをやって良かった」と思えることは何ですか？\n"
            "3. **喜び**: 純粋に楽しいこと、ワクワクすることは何ですか？\n\n"
            "今日のハイライト候補をいくつか、このメッセージに**返信する形**で教えてください（音声入力も可能です）。"
        )
        embed = discord.Embed(
            title=f"{HIGHLIGHT_EMOJI} 今日のハイライトを決めましょう",
            description=advice_text,
            color=discord.Color.gold()
        )
        await channel.send(embed=embed)

    # --- 振り返りフロー ---
    @tasks.loop(time=TUNING_PROMPT_TIME)
    async def prompt_daily_tuning(self):
        channel = self.bot.get_channel(self.maketime_channel_id)
        if not channel: return
        
        questions = (
            "お疲れ様でした。今日一日を振り返り、明日のためのチューニングをしましょう。\n\n"
            "以下の質問に、このメッセージに**返信する形**で答えてください（音声入力も可能です）。\n\n"
            "1. 今日のハイライトは何でしたか？（達成できたかどうか）\n"
            "2. エネルギーレベルは10段階でいくつでしたか？\n"
            "3. 集中度は10段階でいくつでしたか？\n"
            "4. 今日の感謝の瞬間は何ですか？\n"
            "5. 明日試してみたい戦術や改善点はありますか？"
        )
        embed = discord.Embed(
            title="📝 1日の振り返り (Make Time Note)",
            description=questions,
            color=discord.Color.from_rgb(175, 175, 200)
        )
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.maketime_channel_id:
            return
        if not message.reference or not message.reference.message_id:
            return

        channel = self.bot.get_channel(self.maketime_channel_id)
        original_msg = await channel.fetch_message(message.reference.message_id)

        if original_msg.author.id != self.bot.user.id or not original_msg.embeds:
            return
        
        embed_title = original_msg.embeds[0].title
        
        # 音声入力の処理
        if message.attachments and any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
            await message.add_reaction("⏳")
            temp_audio_path = Path(f"./temp_{message.attachments[0].filename}")
            try:
                async with self.session.get(message.attachments[0].url) as resp:
                    if resp.status == 200:
                        with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                
                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                message.content = transcription.text
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("✅")
            except Exception as e:
                logging.error(f"音声認識エラー: {e}", exc_info=True)
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
                return
            finally:
                if os.path.exists(temp_audio_path):
                    os.remove(temp_audio_path)
        
        if not message.content: return

        # ハイライト候補への返信を処理
        if "今日のハイライトを決めましょう" in embed_title:
            await self.handle_highlight_candidates(message, original_msg)
        
        # 振り返りへの返信を処理
        elif "1日の振り返り" in embed_title:
            await self.handle_tuning_response(message, original_msg)

    async def handle_highlight_candidates(self, message: discord.Message, original_msg):
        """ユーザーから提示されたハイライト候補をAIで分析する"""
        await original_msg.add_reaction("🤔")
        
        prompt = f"""
        ユーザーは一日の最も重要なタスクである「ハイライト」を決めようとしています。
        以下の3つの基準に基づき、ユーザーが提示した各候補を分析し、選択の手助けをしてください。
        - 緊急性: 今日中に対応が必要か
        - 満足感: 達成感や大きな成果に繋がりそうか
        - 喜び: やっていて楽しい、ワクワクするか

        ユーザーの候補リスト:
        ---
        {message.content}
        ---

        分析結果を簡潔な箇条書きで提示してください。どの基準に合致するかを明記してください。
        前置きや結論は不要で、分析本文のみを生成してください。
        """
        response = await self.gemini_model.generate_content_async(prompt)
        
        raw_candidates = re.split(r'[\n、,]', message.content)
        candidates = []
        for cand in raw_candidates:
            cleaned_cand = re.sub(r'^\s*[\d\.\-\*・]\s*', '', cand).strip()
            if cleaned_cand:
                candidates.append(cleaned_cand)
        
        self.user_states[message.author.id] = { "highlight_candidates": candidates }

        view = HighlightSelectionView(candidates, self.bot, self.creds)
        
        analysis_embed = discord.Embed(
            title="🤖 AIによるハイライト候補の分析",
            description=response.text,
            color=discord.Color.blue()
        )
        analysis_embed.set_footer(text="分析を参考に、以下から今日のハイライトを選択してください。")

        await message.reply(embed=analysis_embed, view=view)
        await original_msg.delete()


    async def handle_tuning_response(self, message: discord.Message, original_msg):
        """ユーザーの振り返りをObsidianに保存する"""
        await original_msg.add_reaction("✍️")
        
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{today_str}.md"
        
        reflection_text = f"\n{message.content.strip()}\n"
        section_header = "## Make Time Note"

        try:
            try:
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
            except dropbox.exceptions.ApiError as e:
                if isinstance(e.error, dropbox.files.DownloadError) and e.error.get_path().is_not_found():
                    current_content = ""
                else: raise

            new_content = update_section(current_content, reflection_text, section_header)
            
            self.dbx.files_upload(
                new_content.encode('utf-8'),
                daily_note_path,
                mode=dropbox.files.WriteMode('overwrite')
            )
            await message.add_reaction("✅")
            logging.info(f"Obsidianのデイリーノートに振り返りを保存しました: {daily_note_path}")

        except Exception as e:
            logging.error(f"Obsidianへの振り返り保存中にエラー: {e}", exc_info=True)
            await message.add_reaction("❌")
        
        await original_msg.delete()


class HighlightSelectionView(discord.ui.View):
    """ハイライトを選択するためのボタンを持つView"""
    def __init__(self, candidates: list, bot: commands.Bot, creds):
        super().__init__(timeout=300)
        self.bot = bot
        self.creds = creds
        
        for candidate in candidates:
            button = discord.ui.Button(
                label=candidate[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"highlight_{candidate[:90]}"
            )
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        selected_highlight_text = interaction.data['custom_id'].replace("highlight_", "", 1)
        
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
                if child.custom_id == interaction.data['custom_id']:
                    child.style = discord.ButtonStyle.success
        
        await interaction.edit_original_response(view=self)

        event_summary = f"{HIGHLIGHT_EMOJI} ハイライト: {selected_highlight_text}"
        today_str = datetime.datetime.now(JST).date().isoformat()
        
        event = {
            'summary': event_summary,
            'start': {'date': today_str},
            'end': {'date': (datetime.date.fromisoformat(today_str) + datetime.timedelta(days=1)).isoformat()},
        }
        
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            await interaction.followup.send(f"✅ 今日のハイライト「**{selected_highlight_text}**」をカレンダーに登録しました！", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ カレンダーへの登録中にエラーが発生しました: {e}", ephemeral=True)
            logging.error(f"ハイライトのカレンダー登録中にエラー: {e}", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MakeTimeCog(bot))