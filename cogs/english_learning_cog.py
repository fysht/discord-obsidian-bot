import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import random
import asyncio
from datetime import time, datetime
import zoneinfo
import aiohttp
import google.generativeai as genai
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import json
import io
import tempfile
import openai

# --- Common function import ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。")
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
        # Dummy function
        if section_header in current_content:
            lines = current_content.split('\n'); try: header_index = lines.index(section_header); insert_index = header_index + 1
            while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '): insert_index += 1
            lines.insert(insert_index, link_to_add); return "\n".join(lines)
            except ValueError: return f"{current_content}\n\n{section_header}\n{link_to_add}\n"
        else: return f"{current_content}\n\n{section_header}\n{link_to_add}\n"

# --- Constants ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SAKUBUN_NOTE_PATH = "/Study/瞬間英作文リスト.md"
ENGLISH_LOG_PATH = "/English Learning/Chat Logs" # 英会話ログ保存先
SAKUBUN_LOG_PATH = "/Study/Sakubun Log" # 瞬間英作文ログ保存先
DAILY_NOTE_ENGLISH_LOG_HEADER = "## English Learning Logs" # デイリーノートの見出し名 (英会話)
DAILY_NOTE_SAKUBUN_LOG_HEADER = "## Sakubun Logs" # デイリーノートの見出し名 (瞬間英作文)


# --- Helper Function ---
def extract_phrases_from_markdown_list(text: str, heading: str) -> list[str]:
    """特定のMarkdown見出しの下にある箇条書き項目を抽出する"""
    phrases = []
    try:
        # 見出しの下のセクションを見つける正規表現
        pattern = rf"^\#{{2,}}\s*{re.escape(heading)}.*?\n((?:^\s*[-*+]\s+.*?\n?)+)"
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)

        if match:
            list_section = match.group(1)
            # 個々のリスト項目（箇条書き記号の後のテキスト）を抽出
            raw_phrases = re.findall(r"^\s*[-*+]\s+(.+)", list_section, re.MULTILINE)
            # フレーズ内のMarkdown記号を除去し、前後の空白を削除
            phrases = [re.sub(r'[*_`~]', '', p.strip()) for p in raw_phrases if p.strip()] # 空の項目は除外
            logging.info(f"見出し '{heading}' の下からフレーズを抽出しました: {phrases}")
        else:
            logging.warning(f"指定された見出し '{heading}' またはその下の箇条書きが見つかりませんでした。")
    except Exception as e:
        logging.error(f"見出し '{heading}' の下のフレーズ抽出中にエラー: {e}", exc_info=True)
    return phrases

# --- UI Component: TTSView ---
class TTSView(discord.ui.View):
    MAX_BUTTONS = 5 # 表示するボタンの最大数

    def __init__(self, phrases_or_text: list[str] | str, openai_client):
        """
        初期化時に文字列または文字列のリストを受け取る。
        文字列の場合は全体を発音するボタンを1つ生成。
        リストの場合は各要素を発音するボタンを複数生成（上限あり）。
        """
        super().__init__(timeout=3600) # タイムアウトを1時間に設定
        self.openai_client = openai_client
        self.phrases = [] # 発音対象のフレーズを格納するリスト

        if isinstance(phrases_or_text, str):
            # --- 単一の文字列が渡された場合 ---
            clean_text = re.sub(r'<@!?\d+>', '', phrases_or_text)
            clean_text = re.sub(r'[*_`~#]', '', clean_text)
            full_text = clean_text.strip()[:2000]

            if full_text:
                self.phrases.append(full_text)
                label = (full_text[:25] + '...') if len(full_text) > 28 else full_text
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary, custom_id="tts_phrase_0"
                )
                button.callback = self.tts_button_callback
                self.add_item(button)

        elif isinstance(phrases_or_text, list):
            # --- 文字列のリストが渡された場合 ---
            self.phrases = phrases_or_text[:self.MAX_BUTTONS]
            for index, phrase in enumerate(self.phrases):
                clean_phrase = re.sub(r'[*_`~#]', '', phrase.strip())[:2000]
                if not clean_phrase: continue

                label = (clean_phrase[:25] + '...') if len(clean_phrase) > 28 else clean_phrase
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary,
                    custom_id=f"tts_phrase_{index}", row=index // 5
                )
                button.callback = self.tts_button_callback
                self.add_item(button)

    async def tts_button_callback(self, interaction: discord.Interaction):
        """動的に生成されたすべてのTTSボタンの共通コールバック"""
        custom_id = interaction.data.get("custom_id")
        logging.info(f"TTSボタンクリック: {custom_id} by {interaction.user}")

        if not custom_id or not custom_id.startswith("tts_phrase_"):
            await interaction.response.send_message("無効なボタンIDです。", ephemeral=True, delete_after=10)
            return

        try:
            phrase_index = int(custom_id.split("_")[-1])
            if not (0 <= phrase_index < len(self.phrases)):
                await interaction.response.send_message("無効なフレーズインデックスです。", ephemeral=True, delete_after=10)
                return

            phrase_to_speak = self.phrases[phrase_index]

            if not phrase_to_speak:
                await interaction.response.send_message("空のフレーズは読み上げできません。", ephemeral=True, delete_after=10)
                return
            if not self.openai_client:
                await interaction.response.send_message("TTS機能が設定されていません (OpenAI APIキー未設定)。", ephemeral=True, delete_after=10)
                return

            await interaction.response.defer(ephemeral=True, thinking=True)

            # --- OpenAI TTS API 呼び出し ---
            response = await self.openai_client.audio.speech.create(
                model="tts-1", voice="alloy", input=phrase_to_speak, response_format="mp3"
            )
            audio_bytes = response.content
            # --- ここまで ---

            # --- Discordに音声ファイルを送信 ---
            audio_buffer = io.BytesIO(audio_bytes)
            audio_file = discord.File(fp=audio_buffer, filename=f"phrase_{phrase_index}.mp3")
            await interaction.followup.send(f"🔊 \"{phrase_to_speak}\"", file=audio_file, ephemeral=True)
            # --- ここまで ---

        except ValueError:
            logging.error(f"custom_idからインデックスの解析に失敗: {custom_id}")
            await interaction.followup.send("ボタン処理エラー。", ephemeral=True)
        except openai.APIError as e:
             logging.error(f"OpenAI APIエラー (TTS生成中): {e}", exc_info=True)
             await interaction.followup.send(f"音声生成中にOpenAI APIエラーが発生しました: {e}", ephemeral=True)
        except Exception as e:
            logging.error(f"tts_button_callback内でエラー: {e}", exc_info=True)
            await interaction.followup.send(f"音声の生成・送信中にエラーが発生しました: {e}", ephemeral=True)


# --- Cog: EnglishLearningCog ---
class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    """瞬間英作文とAI壁打ちチャットによる英語学習を支援するCog"""

    # --- __init__ ---
    def __init__(self, bot: commands.Bot):
        self.bot = bot; self.is_ready = False; self._load_env_vars()
        if not self._validate_env_vars(): logging.error("EnglishLearningCog: 必須環境変数不足。無効化。"); return
        try:
            self.session = aiohttp.ClientSession(); genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") 
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            if self.openai_api_key: self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            else: self.openai_client = None; logging.warning("OpenAI APIキー未設定。TTS機能無効。")
            self.chat_sessions = {}; self.sakubun_questions = []; self.is_ready = True
            logging.info("✅ EnglishLearningCog初期化成功。")
        except Exception as e: logging.error(f"❌ EnglishLearningCog初期化エラー: {e}", exc_info=True)

    # --- _load_env_vars ---
    def _load_env_vars(self):
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY"); self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET"); self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault"); self.openai_api_key = os.getenv("OPENAI_API_KEY")

    # --- _validate_env_vars ---
    def _validate_env_vars(self) -> bool:
        if not self.openai_api_key: logging.warning("OpenAI APIキー未設定。TTS利用不可。")
        return all([self.channel_id != 0, self.gemini_api_key, self.dropbox_refresh_token])

    # --- _get_session_path ---
    def _get_session_path(self, user_id: int) -> str: return f"{self.dropbox_vault_path}/.bot/english_session_{user_id}.json"

    # --- on_ready ---
    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return; await self._load_sakubun_questions()
        if not self.morning_sakubun_task.is_running(): self.morning_sakubun_task.start()
        if not self.evening_sakubun_task.is_running(): self.evening_sakubun_task.start()

    # --- cog_unload ---
    async def cog_unload(self):
        if self.is_ready: await self.session.close(); self.morning_sakubun_task.cancel(); self.evening_sakubun_task.cancel()

    # --- _load_sakubun_questions ---
    async def _load_sakubun_questions(self):
        if not self.is_ready: return
        try:
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"; _, res = self.dbx.files_download(path)
            content = res.content.decode('utf-8'); questions = re.findall(r'^\s*-\s*(.+?)(?:\s*::\s*.*)?$', content, re.MULTILINE)
            if questions: self.sakubun_questions = [q.strip() for q in questions]; logging.info(f"Obsidianから{len(self.sakubun_questions)}問の瞬間英作文の問題を読み込みました。")
            else: logging.warning(f"Obsidianのファイル ({SAKUBUN_NOTE_PATH}) に問題が見つかりませんでした (形式: '- 日本語文')。")
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): logging.warning(f"瞬間英作文ファイルが見つかりません: {path}")
            else: logging.error(f"Dropbox APIエラー (瞬間英作文読み込み): {e}")
        except Exception as e: logging.error(f"Obsidianからの問題読み込み中に予期せぬエラーが発生しました: {e}", exc_info=True)

    # --- morning_sakubun_task, evening_sakubun_task, _run_sakubun_session ---
    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self): channel = self.bot.get_channel(self.channel_id); await self._run_sakubun_session(channel, 1, "朝")
    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self): channel = self.bot.get_channel(self.channel_id); await self._run_sakubun_session(channel, 2, "夜")
    async def _run_sakubun_session(self, channel: discord.TextChannel, num_questions: int, session_name: str):
        if not self.sakubun_questions: await channel.send("⚠️ 瞬間英作文の問題リストが空のため、出題できません。"); return
        questions = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions)))
        embed = discord.Embed(title=f"✍️ 今日の{session_name}・瞬間英作文", description=f"これから{len(questions)}問出題します。", color=discord.Color.purple()).set_footer(text="20秒後に問題が出題されます。"); await channel.send(embed=embed); await asyncio.sleep(20)
        for i, q_text in enumerate(questions):
            q_embed = discord.Embed(title=f"第 {i+1} 問", description=f"**{q_text}**", color=discord.Color.blue()).set_footer(text="このメッセージに返信する形で英訳を投稿してください。"); await channel.send(embed=q_embed); await asyncio.sleep(20)

    # --- /english command ---
    @app_commands.command(name="english", description="AIとの英会話チャットを開始または再開します。")
    async def english(self, interaction: discord.Interaction):
        if interaction.channel.id != self.channel_id: await interaction.response.send_message(f"<#{self.channel_id}>でのみ利用可。", ephemeral=True); return
        if interaction.user.id in self.chat_sessions: await interaction.response.send_message("既にセッション中。終了は `/end`。", ephemeral=True); return
        await interaction.response.defer()
        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        model = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction) # モデル名修正
        history_json = await self._load_session_from_dropbox(interaction.user.id)
        history = [{'role': item['role'], 'parts': item['parts']} for item in history_json] if history_json else []
        chat = model.start_chat(history=history); self.chat_sessions[interaction.user.id] = chat
        async with interaction.channel.typing():
            response_text = ""
            if history: prompt = "Hi there! Let's continue our conversation. How are you doing?"; response_text = prompt
            else:
                initial_prompt = "Hi! I'm your AI English conversation partner. Let's have a chat! How's your day going so far?"
                try: response = await chat.send_message_async(initial_prompt); response_text = response.text
                except Exception as e_init: logging.error(f"英会話初回応答生成失敗: {e_init}"); response_text = "Hi! Let's chat. How are you?"
            # TTSViewにチャット応答(文字列)を渡す
            await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))

    # --- _load_session_from_dropbox ---
    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        try: _, res = self.dbx.files_download(self._get_session_path(user_id)); return json.loads(res.content)
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): return None
            logging.error(f"英語セッション読込失敗: {e}"); return None
        except json.JSONDecodeError: logging.error(f"英語セッションJSON解析失敗: {self._get_session_path(user_id)}"); return None

    # --- _save_session_to_dropbox ---
    async def _save_session_to_dropbox(self, user_id: int, history: list):
        try:
            path = self._get_session_path(user_id)
            serializable_history = [{"role": t.role, "parts": [p.text for p in t.parts if hasattr(p, 'text')]} for t in history if hasattr(t, 'role') and hasattr(t, 'parts')]
            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode('utf-8'); self.dbx.files_upload(content, path, mode=WriteMode('overwrite'))
        except Exception as e: logging.error(f"英語セッション保存失敗: {e}")

    # --- _extract_english_for_tts ---
    async def _extract_english_for_tts(self, review_text: str) -> str:
        # この関数は現在 /end でレビュー全体から英語を抜き出すために使われているが、
        # extract_phrases_from_markdown_list で重要フレーズのみを対象にするため、
        # 将来的に不要になる可能性あり。
        try:
            prompt = f"以下の英会話レビューから英語のフレーズや例文だけ抜き出しスペース区切り一行で出力(日本語/記号除外):\n\n# 元のレビュー\n{review_text}"
            response = await self.gemini_model.generate_content_async(prompt)
            if response and hasattr(response, 'text'): return response.text.strip()
            else: logging.warning(f"TTS英語抽出API応答不正: {response}"); return ""
        except Exception as e: logging.error(f"TTS英語抽出失敗: {e}", exc_info=True); return ""

    # --- _generate_chat_review ---
    async def _generate_chat_review(self, history: list) -> str:
        conversation_log = "\n".join([f"**{'You' if t.role == 'user' else 'AI'}:** {t.parts[0].text}" for t in history if hasattr(t, 'role') and t.role in ['user', 'model'] and hasattr(t, 'parts') and t.parts and hasattr(t.parts[0], 'text')])
        if not conversation_log: return "十分な対話がありませんでした。"
        # プロンプト内の指示を修正
        prompt = f"""あなたはプロの英語教師です。ログを分析しレビューを作成してください。
# 指示
1. **会話の要約**: 1〜2文で。
2. **重要フレーズ**: 3〜5個。**必ず `### 重要フレーズ` 見出しの下にフレーズのみ箇条書き (`- Phrase/Word`) で記述し、解説はその後で。**
3. **改善点**: 1〜2点、改善案と共に。
4. Markdown形式、ポジティブなトーンで。
# 会話ログ
{conversation_log}"""
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            if response and hasattr(response, 'text'): return response.text
            else: logging.warning(f"レビュー生成API応答不正: {response}"); return "レビュー生成失敗。"
        except Exception as e: logging.error(f"レビュー生成エラー: {e}", exc_info=True); return "レビュー生成エラー。"

    # --- _save_chat_log_to_obsidian (デイリーノートリンク含む) ---
    async def _save_chat_log_to_obsidian(self, user: discord.User, history: list, review: str):
        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        title = f"英会話ログ {user.display_name} {date_str}"; safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', f"{user.display_name} {date_str}")
        filename = f"{timestamp}-英会話ログ {safe_title_part}.md"
        conversation_log = "\n".join([f"- **{'You' if t.role == 'user' else 'AI'}:** {t.parts[0].text}" for t in history if hasattr(t, 'role') and t.role in ['user', 'model'] and hasattr(t, 'parts') and t.parts and hasattr(t.parts[0], 'text')])
        note_content = f"# {title}\n\n- Date: {date_str}\n- Participant: {user.display_name}\n\n[[{date_str}]]\n\n---\n\n## 💬 Session Review\n{review}\n\n---\n\n## 📜 Full Transcript\n{conversation_log}\n"
        note_path = f"{self.dropbox_vault_path}{ENGLISH_LOG_PATH}/{filename}"
        try:
            self.dbx.files_upload(note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"英会話ログ保存成功: {note_path}")
            # --- Daily note link addition ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            try: _, res = self.dbx.files_download(daily_note_path); daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): daily_note_content = f"# {date_str}\n"
                else: raise
            note_filename_for_link = filename.replace('.md', ''); link_path_part = ENGLISH_LOG_PATH.lstrip('/')
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}]]"
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_ENGLISH_LOG_HEADER)
            self.dbx.files_upload(new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート ({daily_note_path}) に英会話ログリンク追記成功。")
            # --- End daily note link ---
        except ApiError as e: logging.error(f"英会話ログ保存/デイリーノート更新 Dropboxエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"英会話ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)

    # --- _save_sakubun_log_to_obsidian (デイリーノートリンク含む) ---
    async def _save_sakubun_log_to_obsidian(self, japanese_question: str, user_answer: str, feedback_text: str):
        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', japanese_question[:20]); filename = f"{timestamp}-Sakubun_{safe_title_part}.md"
        model_answers = ""; model_answers_match = re.search(r"### Model Answer(?:s)?\n(.+?)(?:\n###|$)", feedback_text, re.DOTALL | re.IGNORECASE)
        if model_answers_match: model_answers = "\n".join([f"- {line.strip()}" for line in model_answers_match.group(1).splitlines() if line.strip()])
        note_content = f"# {date_str} 瞬間英作文\n\n- Date: [[{date_str}]]\n---\n\n## 問題\n{japanese_question}\n\n## あなたの回答\n{user_answer}\n\n## AIによるフィードバック\n{feedback_text}\n"
        if model_answers: note_content += f"---\n\n## モデルアンサー\n{model_answers}\n"
        note_path = f"{self.dropbox_vault_path}{SAKUBUN_LOG_PATH}/{filename}"
        try:
            self.dbx.files_upload(note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"瞬間英作文ログ保存成功: {note_path}")
            # --- Daily note link addition ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            try: _, res = self.dbx.files_download(daily_note_path); daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): daily_note_content = f"# {date_str}\n"
                else: raise
            note_filename_for_link = filename.replace('.md', ''); link_path_part = SAKUBUN_LOG_PATH.lstrip('/')
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{japanese_question[:30]}...]]"
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_SAKUBUN_LOG_HEADER)
            self.dbx.files_upload(new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート ({daily_note_path}) に瞬間英作文ログリンク追記成功。")
            # --- End daily note link ---
        except ApiError as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 Dropboxエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)

    # --- on_message ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id: return

        # /end command
        if message.content.strip().lower() == "/end":
            session = self.chat_sessions.pop(message.author.id, None)
            if session:
                await message.channel.send("Ending session...")
                async with message.channel.typing():
                    history = session.history if hasattr(session, 'history') else []
                    review_text = await self._generate_chat_review(history)
                    # ★重要フレーズを抽出
                    important_phrases = extract_phrases_from_markdown_list(review_text, "重要フレーズ")
                    review_embed = discord.Embed(title="💬 Review", description=review_text, color=discord.Color.gold())
                    review_embed.timestamp = datetime.now(JST)
                    review_embed.set_footer(text=f"{message.author.display_name}'s session")
                    # ★抽出したフレーズリストをTTSViewに渡す
                    view = TTSView(important_phrases, self.openai_client) if important_phrases else None
                    await message.channel.send(embed=review_embed, view=view)
                    await self._save_session_to_dropbox(message.author.id, history)
                    await self._save_chat_log_to_obsidian(message.author, history, review_text)
            else: await message.reply("No active session.", delete_after=10)
            return

        # Sakubun answer
        if message.reference and message.reference.message_id:
            try:
                original_msg = await message.channel.fetch_message(message.reference.message_id)
                if original_msg.author.id == self.bot.user.id and original_msg.embeds and "第" in original_msg.embeds[0].title:
                    await self.handle_sakubun_answer(message, message.content.strip(), original_msg)
                    return
            except discord.NotFound: pass
            except Exception as e_ref: logging.error(f"参照メッセージ処理エラー: {e_ref}")

        # Regular chat message
        if message.author.id in self.chat_sessions:
            await self.handle_chat_message(message)

    # --- handle_sakubun_answer ---
    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        if not user_answer: await message.add_reaction("❓"); await asyncio.sleep(5); await message.remove_reaction("❓", self.bot.user); return
        await message.add_reaction("🤔")
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")
        prompt = f"""あなたはプロ英語教師です。添削と解説をしてください。
# 指示
- 評価してください。
- `### Model Answer` 見出しの下に**モデルアンサー英文のみを箇条書き (`- Answer Sentence`)** で2〜3個提示。
- 文法ポイント解説。
- Markdown形式で。
# 日本語の原文
{japanese_question}
# 学習者の英訳
{user_answer}"""
        feedback_text = "フィードバック生成失敗。"
        view = None
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            if response and hasattr(response, 'text'): feedback_text = response.text
            feedback_embed = discord.Embed(title=f"添削結果: 「{japanese_question}」", description=feedback_text, color=discord.Color.green())

            # ★モデルアンサーを抽出
            model_answers = extract_phrases_from_markdown_list(feedback_text, "Model Answer")

            # ★抽出したモデルアンサーリストをTTSViewに渡す
            if model_answers: view = TTSView(model_answers, self.openai_client)

            await message.reply(embed=feedback_embed, view=view)
            await self._save_sakubun_log_to_obsidian(japanese_question, user_answer, feedback_text) # ログ保存

        except Exception as e_fb: logging.error(f"瞬間英作文フィードバック/保存エラー: {e_fb}", exc_info=True); await message.reply("エラー発生。")
        finally: try: await message.remove_reaction("🤔", self.bot.user) except discord.HTTPException: pass

    # --- handle_chat_message ---
    async def handle_chat_message(self, message: discord.Message):
        session = self.chat_sessions.get(message.author.id);
        if not session or not message.content: return
        async with message.channel.typing():
            try:
                response = await session.send_message_async(message.content)
                response_text = response.text if response and hasattr(response, 'text') else "Sorry..."
                # TTSViewにチャット応答(文字列)を渡す
                await message.reply(response_text, view=TTSView(response_text, self.openai_client))
            except Exception as e: logging.error(f"英会話応答エラー: {e}"); await message.reply("Sorry, error occurred.")

# --- setup ---
async def setup(bot: commands.Bot):
    if int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) != 0: await bot.add_cog(EnglishLearningCog(bot))
    else: logging.warning("ENGLISH_LEARNING_CHANNEL_ID未設定のためEnglishLearningCog未ロード。")