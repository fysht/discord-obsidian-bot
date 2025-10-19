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
    logging.warning("utils/obsidian_utils.pyが見つかりません。ダミー関数を使用します。")
    # --- ダミー関数の修正 ---
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
        if section_header in current_content:
            lines = current_content.split('\n')
            try: # この try に対して except が必要
                 header_index = -1
                 for i, line in enumerate(lines):
                      # 完全一致でヘッダーを探す
                      if line.strip() == section_header:
                           header_index = i
                           break
                 if header_index == -1: raise ValueError # 見つからなければValueError
                 insert_index = header_index + 1
                 # 次の##ヘッダーまで進む
                 while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                      insert_index += 1
                 # 挿入前に空行を追加 (既に空行でなければ)
                 if insert_index > 0 and lines[insert_index-1].strip():
                     lines.insert(insert_index, "")
                 # 挿入位置の後ろにも空行を追加 (次に要素が続く場合で、かつ空行でなければ)
                 if insert_index < len(lines) and lines[insert_index].strip() and not lines[insert_index].strip().startswith('## '):
                      lines.insert(insert_index, "")

                 lines.insert(insert_index, link_to_add)
                 return "\n".join(lines)
            except ValueError: # <-- ValueError をキャッチする except を追加
                 # ヘッダーが見つからなかった場合、ノートの末尾に追加
                 # 末尾が空行でない場合は空行を2つ追加
                 content_strip = current_content.strip()
                 if content_strip and not content_strip.endswith("\n\n"):
                      if not content_strip.endswith("\n"):
                           content_strip += "\n\n"
                      else:
                           content_strip += "\n"

                 return f"{content_strip}{section_header}\n{link_to_add}\n"
        else: # セクション自体がない場合も末尾に追加
             content_strip = current_content.strip()
             if content_strip and not content_strip.endswith("\n\n"):
                  if not content_strip.endswith("\n"):
                       content_strip += "\n\n"
                  else:
                       content_strip += "\n"
             return f"{content_strip}{section_header}\n{link_to_add}\n"
    # --- ダミー関数の修正ここまで ---


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
        pattern = rf"^\#+\s*{re.escape(heading)}.*?\n((?:^\s*[-*+]\s+.*?\n?)+)"
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        if match:
            list_section = match.group(1)
            raw_phrases = re.findall(r"^\s*[-*+]\s+(.+)", list_section, re.MULTILINE)
            phrases = [re.sub(r'[*_`~]', '', p.strip()) for p in raw_phrases if p.strip()]
            logging.info(f"見出し '{heading}' の下からフレーズを抽出しました: {len(phrases)}件")
        else:
            logging.warning(f"指定された見出し '{heading}' またはその下の箇条書きが見つかりませんでした。")
    except Exception as e:
        logging.error(f"見出し '{heading}' の下のフレーズ抽出中にエラー: {e}", exc_info=True)
    return phrases

# --- UI Component: TTSView ---
class TTSView(discord.ui.View):
    MAX_BUTTONS = 5
    def __init__(self, phrases_or_text: list[str] | str, openai_client):
        super().__init__(timeout=3600)
        self.openai_client = openai_client
        self.phrases = []
        if isinstance(phrases_or_text, str):
            clean_text = re.sub(r'<@!?\d+>', '', phrases_or_text); clean_text = re.sub(r'[*_`~#]', '', clean_text); full_text = clean_text.strip()[:2000]
            if full_text:
                self.phrases.append(full_text); label = (full_text[:25] + '...') if len(full_text) > 28 else full_text
                button = discord.ui.Button(label=f"🔊 {label}", style=discord.ButtonStyle.secondary, custom_id="tts_phrase_0"); button.callback = self.tts_button_callback; self.add_item(button)
        elif isinstance(phrases_or_text, list):
            added_count = 0
            for index, phrase in enumerate(phrases_or_text):
                if added_count >= self.MAX_BUTTONS: break
                clean_phrase = re.sub(r'[*_`~#]', '', phrase.strip())[:2000]
                if not clean_phrase: continue
                self.phrases.append(clean_phrase); label = (clean_phrase[:25] + '...') if len(clean_phrase) > 28 else clean_phrase
                button = discord.ui.Button(label=f"🔊 {label}", style=discord.ButtonStyle.secondary, custom_id=f"tts_phrase_{added_count}", row = added_count // 5)
                button.callback = self.tts_button_callback; self.add_item(button); added_count += 1
    async def tts_button_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id"); logging.info(f"TTS button clicked: {custom_id} by {interaction.user}")
        if not custom_id or not custom_id.startswith("tts_phrase_"): await interaction.response.send_message("無効なボタンID", ephemeral=True, delete_after=10); return
        try:
            phrase_index = int(custom_id.split("_")[-1])
            if not (0 <= phrase_index < len(self.phrases)): await interaction.response.send_message("無効なフレーズインデックス", ephemeral=True, delete_after=10); return
            phrase_to_speak = self.phrases[phrase_index]
            if not phrase_to_speak: await interaction.response.send_message("空フレーズ読み上げ不可", ephemeral=True, delete_after=10); return
            if not self.openai_client: await interaction.response.send_message("TTS機能未設定(OpenAI APIキー)", ephemeral=True, delete_after=10); return
            await interaction.response.defer(ephemeral=True, thinking=True)
            response = await self.openai_client.audio.speech.create(model="tts-1", voice="alloy", input=phrase_to_speak, response_format="mp3")
            audio_bytes = response.content; audio_buffer = io.BytesIO(audio_bytes); audio_file = discord.File(fp=audio_buffer, filename=f"phrase_{phrase_index}.mp3")
            await interaction.followup.send(f"🔊 \"{phrase_to_speak}\"", file=audio_file, ephemeral=True)
        except ValueError: logging.error(f"インデックス解析失敗: {custom_id}"); await interaction.followup.send("ボタン処理エラー", ephemeral=True)
        except openai.APIError as e: logging.error(f"OpenAI APIエラー(TTS): {e}", exc_info=True); await interaction.followup.send(f"音声生成中OpenAI APIエラー: {e}", ephemeral=True)
        except Exception as e:
            logging.error(f"tts_button_callbackエラー: {e}", exc_info=True)
            if interaction.response.is_done(): await interaction.followup.send(f"音声生成/送信エラー: {e}", ephemeral=True)
            else: await interaction.response.send_message(f"音声生成/送信エラー: {e}", ephemeral=True)

# --- Cog: EnglishLearningCog ---
class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot; self.is_ready = False; self._load_env_vars()
        if not self._validate_env_vars(): logging.error("EnglishLearningCog: 環境変数不足"); return
        try:
            self.session = aiohttp.ClientSession(); genai.configure(api_key=self.gemini_api_key); self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            if self.openai_api_key: self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key); logging.info("EnglishLearningCog: OpenAI client initialized.")
            else: self.openai_client = None; logging.warning("EnglishLearningCog: OpenAI APIキー未設定. TTS無効.")
            self.chat_sessions = {}; self.sakubun_questions = []; self.is_ready = True; logging.info("✅ EnglishLearningCog初期化成功。")
        except Exception as e: logging.error(f"❌ EnglishLearningCog初期化エラー: {e}", exc_info=True); self.is_ready = False
    def _load_env_vars(self):
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)); self.gemini_api_key = os.getenv("GEMINI_API_KEY"); self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET"); self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN"); self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault"); self.openai_api_key = os.getenv("OPENAI_API_KEY")
    def _validate_env_vars(self) -> bool:
        if not self.openai_api_key: logging.warning("EnglishLearningCog: OpenAI APIキー未設定. TTS不可.")
        required = [self.channel_id != 0, self.gemini_api_key, self.dropbox_refresh_token, self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_vault_path]
        if not all(required):
             missing = [];
             if self.channel_id == 0: missing.append("ENGLISH_LEARNING_CHANNEL_ID")
             if not self.gemini_api_key: missing.append("GEMINI_API_KEY")
             if not self.dropbox_refresh_token: missing.append("DROPBOX_REFRESH_TOKEN")
             if not self.dropbox_app_key: missing.append("DROPBOX_APP_KEY")
             if not self.dropbox_app_secret: missing.append("DROPBOX_APP_SECRET")
             if not self.dropbox_vault_path: missing.append("DROPBOX_VAULT_PATH")
             logging.error(f"EnglishLearningCog: 不足環境変数: {', '.join(missing)}"); return False
        return True
    def _get_session_path(self, user_id: int) -> str: bot_dir = f"{self.dropbox_vault_path}/.bot"; return os.path.join(bot_dir, f"english_session_{user_id}.json").replace("\\", "/")
    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return; await self._load_sakubun_questions()
        if not self.morning_sakubun_task.is_running(): self.morning_sakubun_task.start(); logging.info(f"Morning Sakubun task scheduled.")
        if not self.evening_sakubun_task.is_running(): self.evening_sakubun_task.start(); logging.info(f"Evening Sakubun task scheduled.")
    async def cog_unload(self):
        if self.is_ready:
            if self.session and not self.session.closed: await self.session.close()
            if hasattr(self, 'morning_sakubun_task'): self.morning_sakubun_task.cancel()
            if hasattr(self, 'evening_sakubun_task'): self.evening_sakubun_task.cancel(); logging.info("EnglishLearningCog unloaded.")
    async def _load_sakubun_questions(self):
        if not self.is_ready or not self.dbx: logging.error("Cannot load Sakubun questions: Cog not ready or Dropbox unavailable."); return
        try:
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"; logging.info(f"Loading Sakubun questions from: {path}")
            _, res = await asyncio.to_thread(self.dbx.files_download, path); content = res.content.decode('utf-8')
            questions = re.findall(r'^\s*[-*+]\s+(.+?)(?:\s*::\s*.*)?$', content, re.MULTILINE)
            if questions: self.sakubun_questions = [q.strip() for q in questions if q.strip()]; logging.info(f"Obsidianから{len(self.sakubun_questions)}問読み込み")
            else: logging.warning(f"Obsidianファイル({path})に問題見つからず"); self.sakubun_questions = []
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): logging.warning(f"瞬間英作文ファイルが見つかりません: {path}")
            else: logging.error(f"Dropbox APIエラー(瞬間英作文読込): {e}"); self.sakubun_questions = []
        except Exception as e: logging.error(f"Obsidian問題読込エラー: {e}", exc_info=True); self.sakubun_questions = []
    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self): channel = self.bot.get_channel(self.channel_id);
                                         if channel: await self._run_sakubun_session(channel, 1, "朝")
                                         else: logging.error(f"Morning Sakubun: Channel {self.channel_id} not found.")
    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self): channel = self.bot.get_channel(self.channel_id);
                                         if channel: await self._run_sakubun_session(channel, 2, "夜")
                                         else: logging.error(f"Evening Sakubun: Channel {self.channel_id} not found.")
    async def _run_sakubun_session(self, channel: discord.TextChannel, num_questions: int, session_name: str):
        if not self.is_ready: return
        if not self.sakubun_questions: await channel.send("⚠️ 瞬間英作文問題リスト空"); return
        try:
            questions_to_ask = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions))); logging.info(f"Starting {session_name} Sakubun with {len(questions_to_ask)} questions.")
            embed = discord.Embed(title=f"✍️ 今日の{session_name}・瞬間英作文", description=f"{len(questions_to_ask)}問出題", color=discord.Color.purple()).set_footer(text="約20秒後に最初の問題"); await channel.send(embed=embed); await asyncio.sleep(20)
            for i, q_text in enumerate(questions_to_ask):
                q_embed = discord.Embed(title=f"第 {i+1} 問 / {len(questions_to_ask)}", description=f"**{q_text}**", color=discord.Color.blue()).set_footer(text="返信(Reply)で英訳投稿"); await channel.send(embed=q_embed)
                if i < len(questions_to_ask) - 1: await asyncio.sleep(20)
        except Exception as e: logging.error(f"{session_name} Sakubunエラー: {e}", exc_info=True); await channel.send(f"⚠️ {session_name}瞬間英作文エラー")
    @app_commands.command(name="english", description="AIとの英会話チャットを開始または再開します。")
    async def english(self, interaction: discord.Interaction):
        if not self.is_ready: await interaction.response.send_message("⚠️ Cog未初期化", ephemeral=True); return
        if interaction.channel_id != self.channel_id: await interaction.response.send_message(f"<#{self.channel_id}>でのみ利用可", ephemeral=True); return
        if interaction.user.id in self.chat_sessions: await interaction.response.send_message("既にセッション中. 終了は `/end`", ephemeral=True); return
        await interaction.response.defer(thinking=True)
        if not self.dbx: await interaction.followup.send("⚠️ Dropbox接続エラー", ephemeral=True); return
        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        try: model = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)
        except Exception as e_model: logging.error(f"Geminiモデル初期化失敗: {e_model}"); await interaction.followup.send("⚠️ AIモデル初期化失敗", ephemeral=True); return
        history_data = await self._load_session_from_dropbox(interaction.user.id); history_for_chat = []
        if history_data:
             for item in history_data:
                  parts_list = item.get('parts', []); role = item.get('role')
                  if role and isinstance(parts_list, list) and parts_list: history_for_chat.append({'role': role, 'parts': [str(p) for p in parts_list]})
                  elif role and isinstance(parts_list, str): history_for_chat.append({'role': role, 'parts': [parts_list]})
        try: chat = model.start_chat(history=history_for_chat); self.chat_sessions[interaction.user.id] = chat; logging.info(f"Started English session for {interaction.user.id}")
        except Exception as e_start_chat: logging.error(f"チャットセッション開始失敗: {e_start_chat}", exc_info=True); await interaction.followup.send("⚠️ チャットセッション開始失敗", ephemeral=True); return
        async with interaction.channel.typing():
            response_text = ""
            if history_for_chat: prompt = "Hi again! Let's pick up where we left off. How have you been?"; response_text = prompt; await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
            else:
                initial_prompt = "Hi! I'm your AI English partner. Let's chat! How's it going?"
                try: response = await asyncio.wait_for(chat.send_message_async(initial_prompt), timeout=60); response_text = response.text if response and hasattr(response, 'text') else "Hi! Let's chat."; await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
                except asyncio.TimeoutError: logging.error("初回応答タイムアウト"); response_text = "Sorry, response timed out. How are you?"; await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
                except Exception as e_init: logging.error(f"初回応答生成失敗: {e_init}", exc_info=True); response_text = "Sorry, error starting chat. How are you?"; await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
            try: await interaction.followup.send("終了は `/end`", ephemeral=True, delete_after=60)
            except discord.HTTPException: pass
    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx: return None; session_path = self._get_session_path(user_id)
        try: logging.info(f"Loading session from: {session_path}"); _, res = await asyncio.to_thread(self.dbx.files_download, session_path)
             try: return json.loads(res.content)
             except json.JSONDecodeError as json_e: logging.error(f"JSON解析失敗 ({session_path}): {json_e}"); return None
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): logging.info(f"Session file not found for {user_id}"); return None
            logging.error(f"Dropbox APIエラー ({session_path}): {e}"); return None
        except Exception as e: logging.error(f"セッション読込エラー ({session_path}): {e}", exc_info=True); return None
    async def _save_session_to_dropbox(self, user_id: int, history: list):
        if not self.dbx: return; session_path = self._get_session_path(user_id)
        try:
            serializable_history = []
            for turn in history: role = getattr(turn, 'role', None); parts = getattr(turn, 'parts', [])
                 if role and parts: part_texts = [getattr(p, 'text', str(p)) for p in parts]; serializable_history.append({"role": role, "parts": part_texts})
            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode('utf-8')
            await asyncio.to_thread(self.dbx.files_upload, content, session_path, mode=WriteMode('overwrite'))
            logging.info(f"Saved session to: {session_path}")
        except Exception as e: logging.error(f"セッション保存失敗 ({session_path}): {e}", exc_info=True)
    async def _generate_chat_review(self, history: list) -> str:
        if not self.gemini_model: return "レビュー生成機能無効"; conversation_log_parts = []
        for turn in history: role = getattr(turn, 'role', None); parts = getattr(turn, 'parts', [])
             if role in ['user', 'model'] and parts: text = getattr(parts[0], 'text', None)
                  if text: prefix = 'You' if role == 'user' else 'AI'; conversation_log_parts.append(f"**{prefix}:** {text}")
        conversation_log = "\n".join(conversation_log_parts)
        if not conversation_log: return "レビュー作成に十分な対話なし"; prompt = f"""あなたはプロ英語教師。ログ分析しレビュー作成。
# 指示
1. **会話要約**: 1〜2文。
2. **重要フレーズ**: 3〜5個。**必ず `### 重要フレーズ` 見出し下にフレーズのみ箇条書き (`- Phrase/Word`)。解説後述。**
3. **改善点**: 1〜2点、改善案と共に。
4. Markdown形式、ポジティブトーンで。
# 会話ログ
{conversation_log}"""
        try: response = await asyncio.wait_for(self.gemini_model.generate_content_async(prompt), timeout=120)
             if response and hasattr(response, 'text'): return response.text.strip()
             else: logging.warning(f"レビュー生成API応答不正: {response}"); return "レビュー生成問題発生"
        except asyncio.TimeoutError: logging.error("レビュー生成タイムアウト"); return "レビュー生成タイムアウト"
        except Exception as e: logging.error(f"レビュー生成エラー: {e}", exc_info=True); return f"レビュー生成エラー: {type(e).__name__}"
    async def _save_chat_log_to_obsidian(self, user: discord.User, history: list, review: str):
        if not self.dbx: return; now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        safe_user_name = re.sub(r'[\\/*?:"<>|]', '_', user.display_name)[:50]; title = f"英会話ログ {safe_user_name} {date_str}"; filename = f"{timestamp}-英会話ログ {safe_user_name}.md"
        conversation_log_parts = []
        for turn in history: role = getattr(turn, 'role', None); parts = getattr(turn, 'parts', [])
             if role in ['user', 'model'] and parts: text = getattr(parts[0], 'text', None)
                  if text: prefix = 'You' if role == 'user' else 'AI'; conversation_log_parts.append(f"- **{prefix}:** {text}")
        conversation_log = "\n".join(conversation_log_parts)
        note_content = f"# {title}\n\n- Date: [[{date_str}]]\n- Participant: {user.display_name} ({user.id})\n\n---\n\n## 💬 Session Review\n{review}\n\n---\n\n## 📜 Full Transcript\n{conversation_log}\n"
        note_path = f"{self.dropbox_vault_path}{ENGLISH_LOG_PATH}/{filename}".replace("\\", "/")
        try:
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add')); logging.info(f"英会話ログ保存成功(Obsidian): {note_path}")
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md".replace("\\", "/"); daily_note_content = f"# {date_str}\n\n"
            try: _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path); daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if not (isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found()): logging.error(f"デイリーノートDLエラー({daily_note_path}): {e}")
            note_filename_for_link = filename[:-3]; link_path_part = ENGLISH_LOG_PATH.strip('/'); link_to_add = f"- [[{link_path_part}/{note_filename_for_link}]]"
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_ENGLISH_LOG_HEADER)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート({daily_note_path})に英会話ログリンク追記成功")
        except ApiError as e: logging.error(f"英会話ログ保存/DN更新 Dropboxエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"英会話ログ保存/DN更新 予期せぬエラー: {e}", exc_info=True)
    async def _save_sakubun_log_to_obsidian(self, japanese_question: str, user_answer: str, feedback_text: str):
        if not self.dbx: return; now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', japanese_question)[:20]; filename = f"{timestamp}-Sakubun_{safe_title_part}.md"; model_answers = ""
        try: model_answers_match = re.search(r"^\#\#\#\s*Model Answer(?:s)?\s*\n(.+?)(?=\n^\#\#\#|\Z)", feedback_text, re.DOTALL | re.MULTILINE | re.IGNORECASE)
             if model_answers_match: answers_block = model_answers_match.group(1).strip(); raw_answers = re.findall(r"^\s*[-*+]\s+(.+)", answers_block, re.MULTILINE); model_answers = "\n".join([f"- {ans.strip()}" for ans in raw_answers if ans.strip()])
        except Exception as e_re: logging.error(f"モデルアンサー抽出エラー: {e_re}")
        note_content = f"# {date_str} 瞬間英作文: {japanese_question}\n\n- Date: [[{date_str}]]\n---\n\n## 問題\n{japanese_question}\n\n## あなたの回答\n{user_answer}\n\n## AIによるフィードバック\n{feedback_text}\n"
        if model_answers: note_content += f"\n---\n\n## モデルアンサー\n{model_answers}\n"
        note_path = f"{self.dropbox_vault_path}{SAKUBUN_LOG_PATH}/{filename}".replace("\\", "/")
        try:
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add')); logging.info(f"瞬間英作文ログ保存成功(Obsidian): {note_path}")
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md".replace("\\", "/"); daily_note_content = f"# {date_str}\n\n"
            try: _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path); daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if not (isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found()): logging.error(f"デイリーノートDLエラー({daily_note_path}): {e}")
            note_filename_for_link = filename[:-3]; link_path_part = SAKUBUN_LOG_PATH.strip('/')
            link_text = japanese_question[:30] + "..." if len(japanese_question) > 33 else japanese_question; link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{link_text}]]"
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_SAKUBUN_LOG_HEADER)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート({daily_note_path})に瞬間英作文ログリンク追記成功")
        except ApiError as e: logging.error(f"瞬間英作文ログ保存/DN更新 Dropboxエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"瞬間英作文ログ保存/DN更新 予期せぬエラー: {e}", exc_info=True)
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id: return
        if message.content.strip().lower() == "/end":
            session = self.chat_sessions.pop(message.author.id, None)
            if session:
                await message.channel.send(f"{message.author.mention} セッション終了. レビュー作成中..."); async with message.channel.typing():
                    history = getattr(session, 'history', []); review_text = await self._generate_chat_review(history)
                    important_phrases = extract_phrases_from_markdown_list(review_text, "重要フレーズ")
                    review_embed = discord.Embed(title="💬 Session Review", description=review_text, color=discord.Color.gold(), timestamp=datetime.now(JST)).set_footer(text=f"{message.author.display_name}'s session")
                    tts_view = TTSView(important_phrases, self.openai_client) if important_phrases and self.openai_client else None
                    await message.channel.send(embed=review_embed, view=tts_view)
                    await self._save_session_to_dropbox(message.author.id, history); await self._save_chat_log_to_obsidian(message.author, history, review_text)
            else: await message.reply("アクティブなセッションなし", delete_after=10); return
        if message.reference and message.reference.message_id:
            try: original_msg = await message.channel.fetch_message(message.reference.message_id)
                 if (original_msg.author.id == self.bot.user.id and original_msg.embeds and original_msg.embeds[0].title and "第" in original_msg.embeds[0].title):
                    user_answer = message.content.strip()
                    if user_answer: await self.handle_sakubun_answer(message, user_answer, original_msg)
                    else: await message.add_reaction("❓"); await asyncio.sleep(5); await message.remove_reaction("❓", self.bot.user)
                    return
            except discord.NotFound: pass
            except Exception as e_ref: logging.error(f"瞬間英作文返信処理エラー: {e_ref}")
        if message.author.id in self.chat_sessions: await self.handle_chat_message(message)
    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        if not self.gemini_model: await message.reply("⚠️ AIフィードバック機能無効"); return
        if not original_msg.embeds or not original_msg.embeds[0].description: return
        await message.add_reaction("🤔"); japanese_question = original_msg.embeds[0].description.strip().replace("**","")
        prompt = f"""あなたはプロ英語教師。添削と解説。
# 指示
- 評価。
- `### Model Answer` 見出し下にモデルアンサー英文のみ箇条書き (`- Answer Sentence`) 2〜3個提示。
- 文法ポイント解説。
- Markdown形式。
# 日本語原文
{japanese_question}
# 学習者の英訳
{user_answer}"""
        feedback_text = "フィードバック生成エラー"; tts_view = None
        try: response = await asyncio.wait_for(self.gemini_model.generate_content_async(prompt), timeout=90)
             if response and hasattr(response, 'text'): feedback_text = response.text.strip()
             else: logging.warning(f"瞬間英作文FB応答不正: {response}")
             feedback_embed = discord.Embed(title=f"添削結果: 「{japanese_question}」", description=feedback_text, color=discord.Color.green())
             model_answers = extract_phrases_from_markdown_list(feedback_text, "Model Answer")
             if model_answers and self.openai_client: tts_view = TTSView(model_answers, self.openai_client)
             await message.reply(embed=feedback_embed, view=tts_view, mention_author=False)
             await self._save_sakubun_log_to_obsidian(japanese_question, user_answer, feedback_text)
        except asyncio.TimeoutError: logging.error("瞬間英作文FB生成タイムアウト"); await message.reply("フィードバック生成タイムアウト", mention_author=False)
        except Exception as e_fb: logging.error(f"瞬間英作文FB/保存エラー: {e_fb}", exc_info=True); await message.reply(f"FB処理エラー: {type(e_fb).__name__}", mention_author=False)
        finally: try: await message.remove_reaction("🤔", self.bot.user)
                 except discord.HTTPException: pass
    async def handle_chat_message(self, message: discord.Message):
        session = self.chat_sessions.get(message.author.id);
        if not session or not message.content: return
        logging.info(f"Handling chat from {message.author.id}: {message.content[:50]}...")
        async with message.channel.typing():
            try: response = await asyncio.wait_for(session.send_message_async(message.content), timeout=60)
                 response_text = response.text if response and hasattr(response, 'text') else "Sorry..."
                 tts_view = TTSView(response_text, self.openai_client) if self.openai_client else None
                 await message.reply(response_text, view=tts_view, mention_author=False)
            except asyncio.TimeoutError: logging.error(f"英会話応答タイムアウト (User: {message.author.id})"); await message.reply("Sorry, response timed out. Repeat?", mention_author=False)
            except Exception as e: logging.error(f"英会話応答エラー (User: {message.author.id}): {e}", exc_info=True); await message.reply(f"Sorry, error: {type(e).__name__}", mention_author=False)

# --- setup ---
async def setup(bot: commands.Bot):
    if int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) != 0: await bot.add_cog(EnglishLearningCog(bot))
    else: logging.warning("ENGLISH_LEARNING_CHANNEL_ID未設定のためEnglishLearningCog未ロード。")