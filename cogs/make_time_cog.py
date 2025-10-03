import os
import discord
from discord.ext import commands, tasks
import logging
import datetime
import zoneinfo
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
import aiohttp
import openai
from pathlib import Path
import re
import textwrap

from utils.obsidian_utils import update_section
import dropbox

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HIGHLIGHT_PROMPT_TIME = datetime.time(hour=7, minute=30, tzinfo=JST)
TUNING_PROMPT_TIME = datetime.time(hour=21, minute=30, tzinfo=JST)
HIGHLIGHT_EMOJI = "✨"
SUPPORTED_AUDIO_TYPES = ['audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm']

# --- View / Modal ---

class AIHighlightSelectionView(discord.ui.View):
    """AIが提案したハイライト候補を選択または自分で提案するためのView"""
    def __init__(self, cog, candidates: list):
        super().__init__(timeout=None) # タイムアウトを無効化
        self.cog = cog
        
        for candidate in candidates:
            button = discord.ui.Button(label=candidate[:80], style=discord.ButtonStyle.secondary, custom_id=f"ai_highlight_{candidate[:90]}")
            button.callback = self.select_callback
            self.add_item(button)
        
        other_button = discord.ui.Button(label="自分で候補を提案する", style=discord.ButtonStyle.primary, custom_id="propose_other")
        other_button.callback = self.propose_other_callback
        self.add_item(other_button)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_highlight = interaction.data['custom_id'].replace("ai_highlight_", "")
        
        # 選択されたボタンを成功にし、他を無効化
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
                if child.custom_id == interaction.data['custom_id']:
                    child.style = discord.ButtonStyle.success

        await interaction.edit_original_response(view=self)
        await self.cog.set_highlight_on_calendar(selected_highlight, interaction)

    async def propose_other_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # 元のメッセージを編集して、ユーザーの入力を促す
        new_embed = interaction.message.embeds[0]
        new_embed.description = (
            "✅ AIの提案以外のハイライトを設定しますね。\n\n"
            "今日のハイライト候補をいくつか、このメッセージに**返信する形**で教えてください（音声入力も可能です）。"
        )
        new_embed.color = discord.Color.blurple()
        
        # ユーザーが返信できるように、元のメッセージのViewを削除
        await interaction.edit_original_response(embed=new_embed, view=None)

class TuningInputModal(discord.ui.Modal, title="1日の振り返り"):
    def __init__(self, cog, energy_level: str, concentration_level: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.energy_level = energy_level
        self.concentration_level = concentration_level

    highlight_review = discord.ui.TextInput(
        label="1. 今日のハイライトはどうでしたか？",
        style=discord.TextStyle.short,
        placeholder="達成できたか、できなかったか、など",
        required=True,
    )
    gratitude_moment = discord.ui.TextInput(
        label="4. 今日の感謝の瞬間は何ですか？",
        style=discord.TextStyle.paragraph,
        placeholder="小さなことでも構いません",
        required=True,
    )
    next_action = discord.ui.TextInput(
        label="5. 明日試したい戦術や改善点は？",
        style=discord.TextStyle.paragraph,
        placeholder="今日の振り返りを元に、明日試すことを一つだけ書きましょう",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        reflection_text = (
            f"- **ハイライト**: {self.highlight_review.value}\n"
            f"- **エネルギーレベル**: {self.energy_level}/10\n"
            f"- **集中度**: {self.concentration_level}/10\n"
            f"- **感謝の瞬間**: {self.gratitude_moment.value}\n"
            f"- **明日の改善点**: {self.next_action.value}\n"
        )
        
        await self.cog.save_tuning_to_obsidian(reflection_text)
        
        await interaction.followup.send("✅ 振り返りを記録しました！お疲れ様でした。", ephemeral=True)
        await interaction.message.delete()

class DailyTuningView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None) # タイムアウトを無効化
        self.cog = cog
        self.energy_level = None
        self.concentration_level = None

        self.add_item(discord.ui.Select(
            placeholder="2. エネルギーレベルを選択 (1-10)",
            options=[discord.SelectOption(label=str(i), value=str(i)) for i in range(1, 11)],
            custom_id="energy_select"
        ))
        self.add_item(discord.ui.Select(
            placeholder="3. 集中度を選択 (1-10)",
            options=[discord.SelectOption(label=str(i), value=str(i)) for i in range(1, 11)],
            custom_id="concentration_select"
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get("custom_id")
        if custom_id == "energy_select":
            self.energy_level = interaction.data["values"][0]
            await interaction.response.defer()
        elif custom_id == "concentration_select":
            self.concentration_level = interaction.data["values"][0]
            await interaction.response.defer()
        return True

    @discord.ui.button(label="残りを入力する", style=discord.ButtonStyle.primary, row=2)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.energy_level or not self.concentration_level:
            await interaction.response.send_message("エネルギーと集中度の両方を選択してください。", ephemeral=True, delete_after=10)
            return
            
        modal = TuningInputModal(self.cog, self.energy_level, self.concentration_level)
        await interaction.response.send_modal(modal)


class HighlightSelectionView(discord.ui.View):
    def __init__(self, candidates: list, bot: commands.Bot, creds):
        super().__init__(timeout=None) # タイムアウトを無効化
        self.bot = bot
        self.creds = creds
        self.cog = bot.get_cog("MakeTimeCog")
        
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
        await self.cog.set_highlight_on_calendar(selected_highlight_text, interaction)

# --- Cog本体 ---
class MakeTimeCog(commands.Cog):
    """書籍『時間術大全』の習慣を実践するためのCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_environment_variables()
        self.session = aiohttp.ClientSession()
        self.user_states = {}
        self.last_highlight_message_id = None
        self.last_tuning_message_id = None

        if not self._are_credentials_valid():
            logging.error("MakeTimeCog: 必須の環境変数が不足。Cogを無効化します。")
            return
        try:
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

    async def _get_todays_events(self) -> list:
        """今日のGoogleカレンダーの予定を取得する"""
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            today = datetime.datetime.now(JST).date()
            time_min = datetime.datetime.combine(today, datetime.time.min, tzinfo=JST).isoformat()
            time_max = datetime.datetime.combine(today, datetime.time.max, tzinfo=JST).isoformat()
            
            events_result = service.events().list(
                calendarId='primary', timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime'
            ).execute()
            return events_result.get('items', [])
        except HttpError as e:
            logging.error(f"Googleカレンダーからの予定取得中にエラー: {e}")
            return []

    async def set_highlight_on_calendar(self, highlight_text: str, interaction: discord.Interaction):
        """指定されたテキストをハイライトとしてカレンダーに登録する"""
        event_summary = f"{HIGHLIGHT_EMOJI} ハイライト: {highlight_text}"
        today_str = datetime.datetime.now(JST).date().isoformat()
        
        event = {
            'summary': event_summary,
            'start': {'date': today_str},
            'end': {'date': (datetime.date.fromisoformat(today_str) + datetime.timedelta(days=1)).isoformat()},
        }
        
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            await interaction.followup.send(f"✅ 今日のハイライト「**{highlight_text}**」をカレンダーに登録しました！", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ カレンダーへの登録中にエラーが発生しました: {e}", ephemeral=True)
            logging.error(f"ハイライトのカレンダー登録中にエラー: {e}", exc_info=True)


    @tasks.loop(time=HIGHLIGHT_PROMPT_TIME)
    async def prompt_daily_highlight(self):
        """AIによる候補提案から始めるハイライト設定フロー"""
        channel = self.bot.get_channel(self.maketime_channel_id)
        if not channel: return

        if self.last_highlight_message_id:
            try:
                msg = await channel.fetch_message(self.last_highlight_message_id)
                await msg.delete()
            except discord.NotFound:
                pass
            finally:
                self.last_highlight_message_id = None

        events = await self._get_todays_events()
        
        # 予定がある場合はAIに提案させる
        if events:
            event_list_str = "\n".join([f"- {e.get('summary', '名称未設定')}" for e in events if 'date' not in e.get('start', {})]) # 終日予定は除く
            
            if event_list_str:
                prompt = f"""
                あなたは優秀なアシスタントです。以下の今日のカレンダーの予定リストから、最も重要だと思われる「ハイライト」の候補を3つまで提案してください。
                提案は箇条書きのリスト形式で、提案のテキストのみを出力してください。前置きや結論は不要です。
                
                # 今日の予定
                {event_list_str}
                """
                response = await self.gemini_model.generate_content_async(prompt)
                ai_candidates = [line.strip().lstrip("-* ").strip() for line in response.text.split('\n') if line.strip()]

                if ai_candidates:
                    embed = discord.Embed(
                        title=f"{HIGHLIGHT_EMOJI} 今日のハイライトを決めましょう",
                        description="🤖 今日のご予定から、AIがハイライト候補を提案します。以下から選ぶか、自分で提案してください。",
                        color=discord.Color.gold()
                    )
                    view = AIHighlightSelectionView(self, ai_candidates)
                    msg = await channel.send(embed=embed, view=view)
                    self.last_highlight_message_id = msg.id
                    return

        # 予定がない、またはAIが候補を提案できなかった場合は通常フロー
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
        msg = await channel.send(embed=embed)
        self.last_highlight_message_id = msg.id


    @tasks.loop(time=TUNING_PROMPT_TIME)
    async def prompt_daily_tuning(self):
        channel = self.bot.get_channel(self.maketime_channel_id)
        if not channel: return

        if self.last_tuning_message_id:
            try:
                msg = await channel.fetch_message(self.last_tuning_message_id)
                await msg.delete()
            except discord.NotFound:
                pass
            finally:
                self.last_tuning_message_id = None
        
        embed = discord.Embed(
            title="📝 1日の振り返り (Make Time Note)",
            description="お疲れ様でした。今日一日を振り返り、明日のためのチューニングをしましょう。",
            color=discord.Color.from_rgb(175, 175, 200)
        )
        view = DailyTuningView(self)
        msg = await channel.send(embed=embed, view=view)
        self.last_tuning_message_id = msg.id

    async def save_tuning_to_obsidian(self, reflection_text: str):
        today_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{today_str}.md"
        
        content_to_add = f"\n{reflection_text.strip()}\n"
        section_header = "## Make Time Note"

        try:
            try:
                _, res = self.dbx.files_download(daily_note_path)
                current_content = res.content.decode('utf-8')
            except dropbox.exceptions.ApiError as e:
                if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path().is_not_found():
                    current_content = ""
                else: raise

            new_content = update_section(current_content, content_to_add, section_header)
            
            self.dbx.files_upload(
                new_content.encode('utf-8'),
                daily_note_path,
                mode=dropbox.files.WriteMode('overwrite')
            )
            logging.info(f"Obsidianのデイリーノートに振り返りを保存しました: {daily_note_path}")

        except Exception as e:
            logging.error(f"Obsidianへの振り返り保存中にエラー: {e}", exc_info=True)


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
        
        if "今日のハイライトを決めましょう" not in embed_title:
            return

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

        await self.handle_highlight_candidates(message, original_msg)


    async def handle_highlight_candidates(self, message: discord.Message, original_msg):
        await original_msg.add_reaction("🤔")
        
        formatting_prompt = f"""
        以下のテキストは、今日やりたいことのリストです。内容を解釈し、箇条書きのリスト形式で出力してください。
        箇条書きのテキストのみを生成し、前置きや説明は一切含めないでください。
        ---
        {message.content}
        ---
        """
        formatting_response = await self.gemini_model.generate_content_async(formatting_prompt)
        formatted_candidates_text = formatting_response.text.strip()

        candidates = [line.strip().lstrip("-* ").strip() for line in formatted_candidates_text.split('\n') if line.strip()]
        
        analysis_prompt = f"""
        ユーザーは一日の最も重要なタスクである「ハイライト」を決めようとしています。
        以下の3つの基準に基づき、ユーザーが提示した各候補を分析し、選択の手助けをしてください。
        - 緊急性: 今日中に対応が必要か
        - 満足感: 達成感や大きな成果に繋がりそうか
        - 喜び: やっていて楽しい、ワクワクするか

        ユーザーの候補リスト:
        ---
        {formatted_candidates_text}
        ---

        分析結果を簡潔な箇条書きで提示してください。どの基準に合致するかを明記してください。
        前置きや結論は不要で、分析本文のみを生成してください。
        """
        analysis_response = await self.gemini_model.generate_content_async(analysis_prompt)
        
        self.user_states[message.author.id] = { "highlight_candidates": candidates }

        view = HighlightSelectionView(candidates, self.bot, self.creds)
        
        analysis_embed = discord.Embed(
            title="🤖 AIによるハイライト候補の分析",
            description=analysis_response.text,
            color=discord.Color.blue()
        )
        analysis_embed.add_field(name="あなたの候補リスト", value=f"```{formatted_candidates_text}```", inline=False)
        analysis_embed.set_footer(text="分析を参考に、以下から今日のハイライトを選択してください。")

        await message.reply(embed=analysis_embed, view=view)
        await original_msg.delete()


async def setup(bot: commands.Bot):
    await bot.add_cog(MakeTimeCog(bot))