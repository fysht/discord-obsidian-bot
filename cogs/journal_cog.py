import os
import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, time, timedelta
import zoneinfo
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import aiohttp
import openai
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import asyncio

# --- 共通関数をインポート ---
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HIGHLIGHT_PROMPT_TIME = time(hour=7, minute=30, tzinfo=JST)
JOURNAL_PROMPT_TIME = time(hour=21, minute=30, tzinfo=JST)
HIGHLIGHT_EMOJI = "✨"
SUPPORTED_AUDIO_TYPES = ['audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm']

# --- UIコンポーネント ---

# --- 朝のハイライト選択用 View ---
class AIHighlightSelectionView(discord.ui.View):
    def __init__(self, cog, candidates: list):
        super().__init__(timeout=None)
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
        for child in self.children:
            if isinstance(child, discord.ui.Button): child.disabled = True
            if child.custom_id == interaction.data['custom_id']: child.style = discord.ButtonStyle.success
        await interaction.edit_original_response(view=self)
        await self.cog.set_highlight_on_calendar(selected_highlight, interaction)

    async def propose_other_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_embed = interaction.message.embeds[0]
        new_embed.description = "✅ AIの提案以外のハイライトを設定しますね。\n\n今日のハイライト候補を、このメッセージに**返信する形**で教えてください（音声入力も可能です）。"
        new_embed.color = discord.Color.blurple()
        await interaction.edit_original_response(embed=new_embed, view=None)

class HighlightSelectionView(discord.ui.View):
    def __init__(self, candidates: list, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.cog = bot.get_cog("JournalCog")
        for candidate in candidates:
            button = discord.ui.Button(label=candidate[:80], style=discord.ButtonStyle.secondary, custom_id=f"highlight_{candidate[:90]}")
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_highlight_text = interaction.data['custom_id'].replace("highlight_", "", 1)
        for child in self.children:
            if isinstance(child, discord.ui.Button): child.disabled = True
            if child.custom_id == interaction.data['custom_id']: child.style = discord.ButtonStyle.success
        await interaction.edit_original_response(view=self)
        await self.cog.set_highlight_on_calendar(selected_highlight_text, interaction)

# --- 夜のジャーナル用 Modal/View ---
class JournalModal(discord.ui.Modal, title="今日一日の振り返り (1/2)"):
    def __init__(self, cog_instance):
        super().__init__(timeout=None)
        self.cog = cog_instance
    location_main = discord.ui.TextInput(label="1. 主な訪問先", placeholder="今日、最も長く滞在した、あるいは重要だった場所", required=False, style=discord.TextStyle.short)
    location_other = discord.ui.TextInput(label="2. その他の訪問先", placeholder="その他に記録しておきたい場所", required=False, style=discord.TextStyle.short)
    meal_breakfast = discord.ui.TextInput(label="3. 朝食", placeholder="朝に何を食べましたか？", required=False, style=discord.TextStyle.short)
    meal_lunch = discord.ui.TextInput(label="4. 昼食", placeholder="昼に何を食べましたか？", required=False, style=discord.TextStyle.short)
    meal_dinner = discord.ui.TextInput(label="5. 夕食", placeholder="夜に何を食べましたか？", required=False, style=discord.TextStyle.short, row=4)

class JournalModalP2(discord.ui.Modal, title="今日一日の振り返り (2/2)"):
    def __init__(self, cog_instance, part1_data: dict):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.part1_data = part1_data
    highlight = discord.ui.TextInput(label="6. 今日のハイライト", placeholder="最も良かった出来事や、充実感を得られた瞬間", required=False, style=discord.TextStyle.short)
    grateful = discord.ui.TextInput(label="7. 感謝したこと", placeholder="今日感謝したいと感じた出来事", required=False, style=discord.TextStyle.short)
    thoughts = discord.ui.TextInput(label="8. 頭に浮かんだこと", placeholder="考えたこと、気づき、学び、疑問などを自由に記述してください。", required=False, style=discord.TextStyle.paragraph)
    action_for_tomorrow = discord.ui.TextInput(label="9. 明日へのアクション", placeholder="今日の振り返りを踏まえ、明日試したいことや意識したいこと", required=False, style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        all_data = {
            "main_location": self.part1_data['location_main'].value, "other_location": self.part1_data['location_other'].value,
            "breakfast": self.part1_data['meal_breakfast'].value, "lunch": self.part1_data['meal_lunch'].value, "dinner": self.part1_data['meal_dinner'].value,
            "condition": self.part1_data['condition'], "highlight": self.highlight.value, "grateful_for": self.grateful.value,
            "thoughts": self.thoughts.value, "action_for_tomorrow": self.action_for_tomorrow.value
        }
        await self.cog.process_journal_entry(interaction, all_data)

class JournalView(discord.ui.View):
    def __init__(self, cog_instance):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.condition = None
        self.add_item(discord.ui.Select(
            placeholder="今日のコンディションを選択 (1:最悪 ~ 10:最高)",
            options=[discord.SelectOption(label=str(i), value=str(i)) for i in range(1, 11)],
            custom_id="condition_select"
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.data.get("custom_id") == "condition_select":
            self.condition = interaction.data["values"][0]
            await interaction.response.defer()
        return True

    @discord.ui.button(label="振り返りを入力する", style=discord.ButtonStyle.primary, row=1)
    async def open_journal_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.condition:
            await interaction.response.send_message("今日のコンディションを選択してください。", ephemeral=True, delete_after=10)
            return

        part1_modal = JournalModal(self.cog)
        await interaction.response.send_modal(part1_modal)
        
        timed_out = await part1_modal.wait()

        if not timed_out:
            part1_data = {
                'location_main': part1_modal.location_main, 'location_other': part1_modal.location_other,
                'meal_breakfast': part1_modal.meal_breakfast, 'meal_lunch': part1_modal.meal_lunch, 'meal_dinner': part1_modal.meal_dinner,
                'condition': self.condition
            }
            part2_modal = JournalModalP2(self.cog, part1_data)
            await interaction.followup.send_modal(part2_modal)


# --- Cog本体 ---
class JournalCog(commands.Cog):
    """朝のハイライト設定と夜のジャーナリングを支援するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()
        
        if not self._validate_env_vars():
            logging.error("JournalCog: 必須の環境変数が不足。Cogを無効化します。")
            return
        try:
            self.session = aiohttp.ClientSession()
            self.creds = self._get_google_credentials()
            self.gemini_model = self._initialize_ai_model()
            self.dbx = self._initialize_dropbox_client()
            if self.openai_api_key:
                self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            self.is_ready = True
            logging.info("✅ JournalCogが正常に初期化されました。")
        except Exception as e:
            logging.error(f"❌ JournalCogの初期化中にエラー: {e}", exc_info=True)
    
    def _load_env_vars(self):
        self.channel_id = int(os.getenv("JOURNAL_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.google_token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")

    def _validate_env_vars(self) -> bool:
        return all([self.channel_id, self.gemini_api_key, self.openai_api_key, self.google_token_path, self.dropbox_refresh_token, self.dropbox_vault_path])

    def _get_google_credentials(self):
        token_path = self.google_token_path
        if os.getenv("RENDER"):
             token_path = f"/etc/secrets/{os.path.basename(token_path)}"
        if os.path.exists(token_path):
            try:
                creds = Credentials.from_authorized_user_file(token_path, ['https://www.googleapis.com/auth/calendar'])
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                return creds
            except Exception as e:
                logging.error(f"❌ Google APIトークンのリフレッシュに失敗: {e}")
                return None
        logging.warning(f"Google Calendarの認証情報ファイルが見つかりません: {token_path}")
        return None

    def _initialize_ai_model(self):
        genai.configure(api_key=self.gemini_api_key)
        return genai.GenerativeModel("gemini-2.5-pro")

    def _initialize_dropbox_client(self):
        return dropbox.Dropbox(
            app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret,
            oauth2_refresh_token=self.dropbox_refresh_token
        )

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.prompt_daily_highlight.is_running(): self.prompt_daily_highlight.start()
            if not self.prompt_daily_journal.is_running(): self.prompt_daily_journal.start()

    async def cog_unload(self):
        await self.session.close()
        self.prompt_daily_highlight.cancel()
        self.prompt_daily_journal.cancel()

    # --- 朝のハイライト機能 ---
    async def _get_todays_events(self) -> list:
        if not self.creds: return []
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            today = datetime.now(JST).date()
            time_min = datetime.combine(today, time.min, tzinfo=JST).isoformat()
            time_max = datetime.combine(today, time.max, tzinfo=JST).isoformat()
            events_result = service.events().list(
                calendarId='primary', timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime'
            ).execute()
            return events_result.get('items', [])
        except HttpError as e:
            logging.error(f"Googleカレンダーからの予定取得中にエラー: {e}")
            return []

    async def set_highlight_on_calendar(self, highlight_text: str, interaction: discord.Interaction):
        if not self.creds:
            await interaction.followup.send("❌ Google Calendarの認証情報がありません。", ephemeral=True)
            return
        event_summary = f"{HIGHLIGHT_EMOJI} ハイライト: {highlight_text}"
        today_str = datetime.now(JST).date().isoformat()
        event = {'summary': event_summary, 'start': {'date': today_str}, 'end': {'date': (datetime.fromisoformat(today_str).date() + timedelta(days=1)).isoformat()}}
        try:
            service = build('calendar', 'v3', credentials=self.creds)
            service.events().insert(calendarId='primary', body=event).execute()
            await interaction.followup.send(f"✅ 今日のハイライト「**{highlight_text}**」をカレンダーに登録しました！", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ カレンダーへの登録中にエラーが発生しました: {e}", ephemeral=True)
            logging.error(f"ハイライトのカレンダー登録中にエラー: {e}", exc_info=True)

    @tasks.loop(time=HIGHLIGHT_PROMPT_TIME)
    async def prompt_daily_highlight(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        events = await self._get_todays_events()
        if events:
            event_list_str = "\n".join([f"- {e.get('summary', '名称未設定')}" for e in events if 'date' not in e.get('start', {})])
            if event_list_str:
                prompt = f"あなたは優秀なアシスタントです。以下の今日のカレンダーの予定リストから、最も重要だと思われる「ハイライト」の候補を3つまで提案してください。提案は箇条書きのリスト形式で、提案のテキストのみを出力してください。前置きや結論は不要です。\n\n# 今日の予定\n{event_list_str}"
                response = await self.gemini_model.generate_content_async(prompt)
                ai_candidates = [line.strip().lstrip("-* ").strip() for line in response.text.split('\n') if line.strip()]
                if ai_candidates:
                    embed = discord.Embed(title=f"{HIGHLIGHT_EMOJI} 今日のハイライトを決めましょう", description="🤖 今日のご予定から、AIがハイライト候補を提案します。以下から選ぶか、自分で提案してください。", color=discord.Color.gold())
                    view = AIHighlightSelectionView(self, ai_candidates)
                    await channel.send(embed=embed, view=view)
                    return

        advice_text = "おはようございます！今日という一日を最高のものにするため、**今日のハイライト**を決めましょう。\n\n" \
                      "今日のハイライト候補を、このメッセージに**返信する形**で教えてください（音声入力も可能です）。"
        embed = discord.Embed(title=f"{HIGHLIGHT_EMOJI} 今日のハイライトを決めましょう", description=advice_text, color=discord.Color.gold())
        await channel.send(embed=embed)

    async def handle_highlight_candidates(self, message: discord.Message, original_msg):
        await original_msg.add_reaction("🤔")
        formatting_prompt = f"以下のテキストは、今日やりたいことのリストです。内容を解釈し、箇条書きのリスト形式で出力してください。箇条書きのテキストのみを生成し、前置きや説明は一切含めないでください。\n---\n{message.content}\n---"
        response = await self.gemini_model.generate_content_async(formatting_prompt)
        candidates = [line.strip().lstrip("-* ").strip() for line in response.text.strip().split('\n') if line.strip()]
        
        view = HighlightSelectionView(candidates, self.bot)
        embed = discord.Embed(title="候補リスト", description="以下から今日のハイライトを選択してください。", color=discord.Color.blue())
        await message.reply(embed=embed, view=view)
        await original_msg.delete()

    # --- 夜のジャーナル機能 ---
    @tasks.loop(time=JOURNAL_PROMPT_TIME)
    async def prompt_daily_journal(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return
        embed = discord.Embed(title="📝 一日の振り返り (ジャーナル)", description="お疲れ様でした。今日一日を振り返り、明日のための準備をしましょう。", color=discord.Color.from_rgb(100, 150, 200))
        view = JournalView(self)
        await channel.send(embed=embed, view=view)

    async def process_journal_entry(self, interaction: discord.Interaction, initial_data: dict):
        final_data = initial_data.copy()
        if initial_data.get("thoughts"):
            prompt = f"以下のジャーナル内容を分析し、ユーザーが思考をさらに深めるための、最も効果的な深掘りの質問を1つだけ生成してください。\n質問は、ユーザーが内省を促されるような、具体的でオープンな問いかけにしてください。\n質問文のみを生成し、前置きや挨拶は一切含めないでください。\n\n# ジャーナル内容\n{initial_data['thoughts']}"
            try:
                response = await self.gemini_model.generate_content_async(prompt)
                ai_question = response.text.strip()
                
                # `interaction.followup.send` は `WebhookMessage` を返す
                followup_message = await interaction.followup.send(f"✅ 振り返りを承りました。ありがとうございます。\n\n追加で一つだけ質問させてください。\n\n**🤔 {ai_question}**\n\nこのメッセージに返信する形で、考えをお聞かせください。", ephemeral=True, wait=True)
                
                def check(m):
                    return m.author == interaction.user and m.channel == interaction.channel and m.reference and m.reference.message_id == followup_message.id

                try:
                    follow_up_message_response = await self.bot.wait_for('message', timeout=600.0, check=check)
                    final_data["ai_question"] = ai_question
                    final_data["ai_answer"] = follow_up_message_response.content
                    await follow_up_message_response.add_reaction("✅")
                except asyncio.TimeoutError:
                    await interaction.followup.send("タイムアウトしました。最初の入力内容のみで保存します。", ephemeral=True)
            except Exception as e:
                logging.error(f"AIによる深掘り質問の生成に失敗: {e}")
        
        await self.save_journal_to_obsidian(final_data)
        await interaction.followup.send("✅ すべての振り返りをObsidianに記録しました！お疲れ様でした。", ephemeral=True)


    async def save_journal_to_obsidian(self, data: dict):
        date_str = datetime.now(JST).strftime('%Y-%m-%d')
        content = f"# {date_str} のジャーナル\n\n## 📖 ライフログ\n"
        if data.get('main_location'): content += f"- **主な訪問先**: {data['main_location']}\n"
        if data.get('other_location'): content += f"- **その他の訪問先**: {data['other_location']}\n"
        if data.get('breakfast'): content += f"- **朝食**: {data['breakfast']}\n"
        if data.get('lunch'): content += f"- **昼食**: {data['lunch']}\n"
        if data.get('dinner'): content += f"- **夕食**: {data['dinner']}\n"
        if data.get('condition'): content += f"- **今日のコンディション**: {data['condition']}/10\n"
        content += "\n## 🧠 ジャーナリング\n"
        if data.get('highlight'): content += f"### ✨ ハイライト\n{data['highlight']}\n\n"
        if data.get('grateful_for'): content += f"### 🙏 感謝したこと\n{data['grateful_for']}\n\n"
        if data.get('thoughts'): content += f"### 🤔 頭に浮かんだこと\n{data['thoughts']}\n\n"
        if data.get('ai_question'):
            content += f"### 🤖 AIによる深掘り\n**Q:** {data['ai_question']}\n**A:** {data.get('ai_answer', '(回答なし)')}\n\n"
        if data.get('action_for_tomorrow'): content += f"### 🚀 明日へのアクション\n{data['action_for_tomorrow']}\n"

        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            _, res = self.dbx.files_download(daily_note_path)
            current_content = res.content.decode('utf-8')
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): current_content = f"# {date_str}\n"
            else: raise
        
        new_content = update_section(current_content, content, "## Journal")
        self.dbx.files_upload(new_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
        logging.info(f"Obsidianのデイリーノートにジャーナルを保存しました: {daily_note_path}")

    # --- 共通メッセージリスナー ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id: return
        if not message.reference or not message.reference.message_id: return

        try:
            original_msg = await message.channel.fetch_message(message.reference.message_id)
        except discord.NotFound:
            return

        if original_msg.author.id != self.bot.user.id or not original_msg.embeds: return
        
        embed_title = original_msg.embeds[0].title
        if "今日のハイライトを決めましょう" in embed_title:
            user_input = message.content
            if message.attachments and any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
                await message.add_reaction("⏳")
                temp_audio_path = Path(f"./temp_{message.attachments[0].filename}")
                try:
                    async with self.session.get(message.attachments[0].url) as resp:
                        if resp.status == 200:
                            with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                    with open(temp_audio_path, "rb") as audio_file:
                        transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                    user_input = transcription.text
                    await message.remove_reaction("⏳", self.bot.user)
                    await message.add_reaction("✅")
                except Exception as e:
                    logging.error(f"音声認識エラー: {e}", exc_info=True)
                finally:
                    if os.path.exists(temp_audio_path): os.remove(temp_audio_path)
            
            if user_input:
                message.content = user_input
                await self.handle_highlight_candidates(message, original_msg)

async def setup(bot: commands.Bot):
    await bot.add_cog(JournalCog(bot))