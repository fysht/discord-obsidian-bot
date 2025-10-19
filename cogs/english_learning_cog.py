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
                      if line.strip() == section_header:
                           header_index = i
                           break
                 if header_index == -1: raise ValueError # 見つからなければValueError
                 insert_index = header_index + 1
                 while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '): insert_index += 1
                 # 挿入前に空行を追加
                 if insert_index > 0 and lines[insert_index-1].strip() != "":
                     lines.insert(insert_index, "")
                 lines.insert(insert_index, link_to_add)
                 return "\n".join(lines)
            except ValueError: # <-- ValueError をキャッチする except を追加
                 # ヘッダーが見つからなかった場合、末尾に追加
                 return f"{current_content.strip()}\n\n{section_header}\n{link_to_add}\n"
        else: # セクション自体がない場合も末尾に追加
             return f"{current_content.strip()}\n\n{section_header}\n{link_to_add}\n"
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
        # 見出しの下のセクションを見つける正規表現 (ヘッダーレベル不問、大文字小文字無視)
        pattern = rf"^\#+\s*{re.escape(heading)}.*?\n((?:^\s*[-*+]\s+.*?\n?)+)"
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)

        if match:
            list_section = match.group(1)
            # 個々のリスト項目（箇条書き記号の後のテキスト）を抽出
            raw_phrases = re.findall(r"^\s*[-*+]\s+(.+)", list_section, re.MULTILINE)
            # フレーズ内のMarkdown記号を除去し、前後の空白を削除
            phrases = [re.sub(r'[*_`~]', '', p.strip()) for p in raw_phrases if p.strip()] # 空の項目は除外
            logging.info(f"見出し '{heading}' の下からフレーズを抽出しました: {len(phrases)}件")
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
        """
        super().__init__(timeout=3600) # タイムアウトを1時間に設定
        self.openai_client = openai_client
        self.phrases = [] # 発音対象のフレーズを格納するリスト

        if isinstance(phrases_or_text, str):
            # --- 単一の文字列が渡された場合 ---
            # メンションとMarkdown記号を除去
            clean_text = re.sub(r'<@!?\d+>', '', phrases_or_text)
            clean_text = re.sub(r'[*_`~#]', '', clean_text)
            full_text = clean_text.strip()[:2000] # 長さ制限

            if full_text:
                self.phrases.append(full_text)
                # ラベルが長すぎる場合の切り詰め処理
                label = (full_text[:25] + '...') if len(full_text) > 28 else full_text
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary, custom_id="tts_phrase_0"
                )
                button.callback = self.tts_button_callback
                self.add_item(button)

        elif isinstance(phrases_or_text, list):
            # --- 文字列のリストが渡された場合 ---
            added_count = 0
            for index, phrase in enumerate(phrases_or_text):
                if added_count >= self.MAX_BUTTONS: break # 上限に達したら終了
                # Markdown記号を除去し、空白削除、長さ制限
                clean_phrase = re.sub(r'[*_`~#]', '', phrase.strip())[:2000]
                if not clean_phrase: continue # 空のフレーズはスキップ

                self.phrases.append(clean_phrase) # phrasesリストにはclean_phraseを追加
                label = (clean_phrase[:25] + '...') if len(clean_phrase) > 28 else clean_phrase
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary,
                    custom_id=f"tts_phrase_{added_count}", # indexではなく追加したボタンの番号を使う
                    row = added_count // 5 # 5個ごとに改行
                )
                button.callback = self.tts_button_callback
                self.add_item(button)
                added_count += 1

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
            # BytesIOを使ってメモリ上でファイルを扱う
            audio_buffer = io.BytesIO(audio_bytes)
            # ファイル名を一意にする (例: phrase_インデックス.mp3)
            audio_file = discord.File(fp=audio_buffer, filename=f"phrase_{phrase_index}.mp3")
            # ephemeral=True で本人にのみ送信
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
            # followup.send が使えるか確認
            if interaction.response.is_done():
                 await interaction.followup.send(f"音声の生成・送信中にエラーが発生しました: {e}", ephemeral=True)
            else:
                 # まだ応答していない場合 (通常はdeferされているはずだが念のため)
                 await interaction.response.send_message(f"音声の生成・送信中にエラーが発生しました: {e}", ephemeral=True)


# --- Cog: EnglishLearningCog ---
class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    """瞬間英作文とAI壁打ちチャットによる英語学習を支援するCog"""

    # --- __init__ ---
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()
        if not self._validate_env_vars():
             logging.error("EnglishLearningCog: 必須環境変数不足。無効化。")
             return
        try:
            self.session = aiohttp.ClientSession()
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-pro") # モデル確認
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            # OpenAIクライアント初期化 (APIキーがある場合のみ)
            if self.openai_api_key:
                 self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
                 logging.info("EnglishLearningCog: OpenAI client initialized for TTS.")
            else:
                 self.openai_client = None
                 logging.warning("EnglishLearningCog: OpenAI APIキー未設定。TTS機能無効。")
            self.chat_sessions = {}
            self.sakubun_questions = []
            self.is_ready = True
            logging.info("✅ EnglishLearningCog初期化成功。")
        except Exception as e:
             logging.error(f"❌ EnglishLearningCog初期化エラー: {e}", exc_info=True)
             self.is_ready = False # 初期化失敗時はFalseに


    # --- _load_env_vars ---
    def _load_env_vars(self):
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")

    # --- _validate_env_vars ---
    def _validate_env_vars(self) -> bool:
        # OpenAI APIキーはTTSにのみ必要なので、必須ではない
        if not self.openai_api_key:
             logging.warning("EnglishLearningCog: OpenAI APIキー未設定。TTS利用不可。")
        # 必須項目をチェック
        required = [
             self.channel_id != 0,
             self.gemini_api_key,
             self.dropbox_refresh_token,
             self.dropbox_app_key, # Dropbox関連も必須
             self.dropbox_app_secret,
             self.dropbox_vault_path
        ]
        if not all(required):
             missing = []
             if self.channel_id == 0: missing.append("ENGLISH_LEARNING_CHANNEL_ID")
             if not self.gemini_api_key: missing.append("GEMINI_API_KEY")
             if not self.dropbox_refresh_token: missing.append("DROPBOX_REFRESH_TOKEN")
             if not self.dropbox_app_key: missing.append("DROPBOX_APP_KEY")
             if not self.dropbox_app_secret: missing.append("DROPBOX_APP_SECRET")
             if not self.dropbox_vault_path: missing.append("DROPBOX_VAULT_PATH")
             logging.error(f"EnglishLearningCog: 不足している必須環境変数: {', '.join(missing)}")
             return False
        return True

    # --- _get_session_path ---
    def _get_session_path(self, user_id: int) -> str:
        # .bot ディレクトリが存在しない場合に備える
        bot_dir = f"{self.dropbox_vault_path}/.bot"
        # パス結合を安全に行う
        return os.path.join(bot_dir, f"english_session_{user_id}.json").replace("\\", "/")


    # --- on_ready ---
    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return
        await self._load_sakubun_questions()
        if not self.morning_sakubun_task.is_running():
             self.morning_sakubun_task.start()
             logging.info(f"Morning Sakubun task scheduled for {MORNING_SAKUBUN_TIME}.")
        if not self.evening_sakubun_task.is_running():
             self.evening_sakubun_task.start()
             logging.info(f"Evening Sakubun task scheduled for {EVENING_SAKUBUN_TIME}.")

    # --- cog_unload ---
    async def cog_unload(self):
        if self.is_ready:
            if self.session and not self.session.closed:
                 await self.session.close()
            if hasattr(self, 'morning_sakubun_task'): self.morning_sakubun_task.cancel()
            if hasattr(self, 'evening_sakubun_task'): self.evening_sakubun_task.cancel()
            logging.info("EnglishLearningCog unloaded.")


    # --- _load_sakubun_questions ---
    async def _load_sakubun_questions(self):
        if not self.is_ready: return
        # Dropboxクライアントの存在確認
        if not self.dbx: logging.error("Cannot load Sakubun questions: Dropbox client not available."); return

        try:
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"
            logging.info(f"Loading Sakubun questions from Dropbox: {path}")
            # files_downloadを非同期実行
            metadata, res = await asyncio.to_thread(self.dbx.files_download, path)
            content = res.content.decode('utf-8')
            # 正規表現を修正: 行頭の空白、ハイフン/アスタリスク/プラス、空白、本体、(任意)::英語訳
            questions = re.findall(r'^\s*[-*+]\s+(.+?)(?:\s*::\s*.*)?$', content, re.MULTILINE)
            if questions:
                 # 前後の空白を除去
                 self.sakubun_questions = [q.strip() for q in questions if q.strip()]
                 logging.info(f"Obsidianから{len(self.sakubun_questions)}問の瞬間英作文の問題を読み込みました。")
            else:
                 logging.warning(f"Obsidianのファイル ({path}) に問題が見つかりませんでした (形式: '- 日本語文')。")
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                 logging.warning(f"瞬間英作文ファイルが見つかりません: {path}")
            else:
                 logging.error(f"Dropbox APIエラー (瞬間英作文読み込み): {e}")
            self.sakubun_questions = [] # エラー時は空にする
        except Exception as e:
             logging.error(f"Obsidianからの問題読み込み中に予期せぬエラーが発生しました: {e}", exc_info=True)
             self.sakubun_questions = [] # エラー時は空にする


    # --- morning_sakubun_task, evening_sakubun_task, _run_sakubun_session ---
    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel: await self._run_sakubun_session(channel, 1, "朝")
        else: logging.error(f"Morning Sakubun: Channel {self.channel_id} not found.")

    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel: await self._run_sakubun_session(channel, 2, "夜")
        else: logging.error(f"Evening Sakubun: Channel {self.channel_id} not found.")

    async def _run_sakubun_session(self, channel: discord.TextChannel, num_questions: int, session_name: str):
        if not self.is_ready: return
        if not self.sakubun_questions:
             await channel.send("⚠️ 瞬間英作文の問題リストが空のため、出題できません。Obsidianのファイルを確認してください。")
             return

        try:
            # ランダムに問題を選択
            questions_to_ask = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions)))
            logging.info(f"Starting {session_name} Sakubun session with {len(questions_to_ask)} questions.")

            embed = discord.Embed(
                title=f"✍️ 今日の{session_name}・瞬間英作文",
                description=f"これから{len(questions_to_ask)}問出題します。",
                color=discord.Color.purple()
            ).set_footer(text="約20秒後に最初の問題が出題されます。")
            await channel.send(embed=embed)
            await asyncio.sleep(20) # 最初の待機

            for i, q_text in enumerate(questions_to_ask):
                q_embed = discord.Embed(
                    title=f"第 {i+1} 問 / {len(questions_to_ask)}",
                    description=f"**{q_text}**", # 問題文を太字に
                    color=discord.Color.blue()
                ).set_footer(text="このメッセージに **返信 (Reply)** する形で英訳を投稿してください。")
                await channel.send(embed=q_embed)
                # 次の問題までの間隔 (必要なら)
                if i < len(questions_to_ask) - 1:
                     await asyncio.sleep(20) # 例: 20秒待機

        except Exception as e:
             logging.error(f"{session_name} Sakubun session中にエラー: {e}", exc_info=True)
             await channel.send(f"⚠️ {session_name}の瞬間英作文セッション中にエラーが発生しました。")

    # --- /english command ---
    @app_commands.command(name="english", description="AIとの英会話チャットを開始または再開します。")
    async def english(self, interaction: discord.Interaction):
        if not self.is_ready: await interaction.response.send_message("⚠️ Cogが初期化されていません。", ephemeral=True); return
        if interaction.channel_id != self.channel_id: await interaction.response.send_message(f"<#{self.channel_id}>でのみ利用可。", ephemeral=True); return
        if interaction.user.id in self.chat_sessions: await interaction.response.send_message("既にセッション中。終了は `/end`。", ephemeral=True); return

        await interaction.response.defer(thinking=True) # thinking=True に変更

        # Dropboxクライアントの存在確認
        if not self.dbx: await interaction.followup.send("⚠️ Dropbox接続エラーのため、セッションを開始できません。", ephemeral=True); return

        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        try:
            model = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction) 
        except Exception as e_model:
            logging.error(f"Geminiモデルの初期化に失敗: {e_model}")
            await interaction.followup.send("⚠️ AIモデルの初期化に失敗しました。", ephemeral=True); return


        # --- セッション履歴のロード ---
        history_data = await self._load_session_from_dropbox(interaction.user.id)
        # Content オブジェクトに変換する (Geminiライブラリの仕様に合わせる)
        history_for_chat = []
        if history_data:
             for item in history_data:
                  # 'parts' がリストであることを確認
                  parts_list = item.get('parts', [])
                  if isinstance(parts_list, list) and parts_list:
                       # parts の中身が文字列であることを想定
                       history_for_chat.append({'role': item.get('role'), 'parts': [str(p) for p in parts_list]})
                  elif isinstance(parts_list, str): # 古い形式かもしれない場合
                       history_for_chat.append({'role': item.get('role'), 'parts': [parts_list]})
        # --- ここまで ---


        try:
            chat = model.start_chat(history=history_for_chat) # 変換後の履歴を使用
            self.chat_sessions[interaction.user.id] = chat
            logging.info(f"Started English chat session for user {interaction.user.id}")
        except Exception as e_start_chat:
             logging.error(f"チャットセッションの開始に失敗: {e_start_chat}", exc_info=True)
             await interaction.followup.send("⚠️ チャットセッションの開始に失敗しました。", ephemeral=True); return

        async with interaction.channel.typing():
            response_text = ""
            if history_for_chat:
                prompt = "Hi there! Let's continue our conversation. How are you doing?"
                response_text = prompt
                # TTSViewに初期応答(文字列)を渡す
                await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
            else:
                initial_prompt = "Hi! I'm your AI English conversation partner. Let's have a chat! How's your day going so far?"
                try:
                    # 初回メッセージを非同期で送信
                    response = await asyncio.wait_for(chat.send_message_async(initial_prompt), timeout=60)
                    response_text = response.text if response and hasattr(response, 'text') else "Hi! Let's chat. How are you?"
                    # TTSViewにチャット応答(文字列)を渡す
                    await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
                except asyncio.TimeoutError:
                     logging.error("英会話初回応答生成タイムアウト")
                     response_text = "Sorry, I took too long to respond. Let's try again. How are you?"
                     await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
                except Exception as e_init:
                     logging.error(f"英会話初回応答生成失敗: {e_init}", exc_info=True)
                     response_text = "Sorry, I couldn't start our chat properly. Let's try again. How are you?"
                     await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))

            # 終了コマンドの案内を追加 (followupが完了した後)
            try:
                await interaction.followup.send("終了したいときは `/end` と入力してください。", ephemeral=True, delete_after=60)
            except discord.HTTPException: pass # フォローアップ送信失敗は無視


    # --- _load_session_from_dropbox ---
    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx: logging.error("Cannot load session: Dropbox client not available."); return None
        session_path = self._get_session_path(user_id)
        try:
            logging.info(f"Loading English session from Dropbox: {session_path}")
            # files_downloadを非同期実行
            metadata, res = await asyncio.to_thread(self.dbx.files_download, session_path)
            # JSONデコードのエラーハンドリングを追加
            try:
                return json.loads(res.content)
            except json.JSONDecodeError as json_e:
                logging.error(f"英語セッションJSON解析失敗 ({session_path}): {json_e}")
                return None # 不正なJSONの場合はNoneを返す
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.info(f"English session file not found for user {user_id}. Starting new session.")
                return None # ファイルがない場合はNone
            logging.error(f"英語セッション読込Dropbox APIエラー ({session_path}): {e}")
            return None
        except Exception as e:
             logging.error(f"英語セッション読込中に予期せぬエラー ({session_path}): {e}", exc_info=True)
             return None

    # --- _save_session_to_dropbox ---
    async def _save_session_to_dropbox(self, user_id: int, history: list):
        if not self.dbx: logging.error("Cannot save session: Dropbox client not available."); return
        session_path = self._get_session_path(user_id)
        try:
            # chat.history からシリアライズ可能な形式に変換
            serializable_history = []
            for turn in history:
                 # role と parts が存在するか確認
                 role = getattr(turn, 'role', None)
                 parts = getattr(turn, 'parts', [])
                 if role and parts:
                      # parts内の各要素にtext属性があるか確認
                      part_texts = [getattr(p, 'text', str(p)) for p in parts]
                      serializable_history.append({"role": role, "parts": part_texts})

            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode('utf-8')
            # files_uploadを非同期実行
            await asyncio.to_thread(
                 self.dbx.files_upload,
                 content, session_path, mode=WriteMode('overwrite')
            )
            logging.info(f"Saved English session to Dropbox: {session_path}")
        except Exception as e:
             logging.error(f"英語セッション保存失敗 ({session_path}): {e}", exc_info=True)

    # --- _generate_chat_review ---
    async def _generate_chat_review(self, history: list) -> str:
        if not self.gemini_model: return "レビュー生成機能が無効です。"

        # chat.history から会話ログを構築 (エラーハンドリング強化)
        conversation_log_parts = []
        for turn in history:
             role = getattr(turn, 'role', None)
             parts = getattr(turn, 'parts', [])
             if role in ['user', 'model'] and parts:
                  # 最初のパートのテキストを取得 (存在すれば)
                  text = getattr(parts[0], 'text', None)
                  if text:
                       prefix = 'You' if role == 'user' else 'AI'
                       conversation_log_parts.append(f"**{prefix}:** {text}")

        conversation_log = "\n".join(conversation_log_parts)

        if not conversation_log: return "今回のセッションでは、レビューを作成するのに十分な対話がありませんでした。"

        prompt = f"""あなたはプロの英語教師です。以下の生徒との英会話ログを分析し、学習内容をまとめたレビューを作成してください。
# 指示
1.  **会話の要約**: このセッションでどのような話題について話したか、1〜2文で簡潔にまとめてください。
2.  **重要フレーズ**: 生徒が学んだり使ったりした特に重要な単語やフレーズを3〜5個選んでください。**必ず `### 重要フレーズ` という見出しの下に、フレーズのみを箇条書き (`- Phrase/Word`) で記述してください。** 各フレーズの説明や日本語訳はその後に記述してください。
3.  **改善点とアドバイス**: 生徒の英語で文法的な誤りや不自然だった点を1〜2点指摘し、正しい表現やより自然な言い方を具体的に提案してください。
4.  全体をMarkdown形式で、生徒を励ますようなポジティブなトーンで記述してください。
# 英会話ログ
{conversation_log}"""
        try:
            # 応答生成 (タイムアウト設定)
            response = await asyncio.wait_for(self.gemini_model.generate_content_async(prompt), timeout=120)
            if response and hasattr(response, 'text'):
                 return response.text.strip()
            else:
                 logging.warning(f"レビュー生成API応答不正: {response}")
                 return "レビューの生成中に問題が発生しました（応答不正）。"
        except asyncio.TimeoutError:
             logging.error("レビュー生成タイムアウト")
             return "レビューの生成がタイムアウトしました。"
        except Exception as e:
             logging.error(f"レビュー生成エラー: {e}", exc_info=True)
             return f"レビューの生成中にエラーが発生しました: {type(e).__name__}"


    # --- _save_chat_log_to_obsidian ---
    async def _save_chat_log_to_obsidian(self, user: discord.User, history: list, review: str):
        if not self.dbx: logging.error("Cannot save chat log: Dropbox client not available."); return

        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        # ファイル名に使えない文字を置換、長さを制限
        safe_user_name = re.sub(r'[\\/*?:"<>|]', '_', user.display_name)[:50]
        title = f"英会話ログ {safe_user_name} {date_str}"
        filename = f"{timestamp}-英会話ログ {safe_user_name}.md"

        # chat.history から会話ログを構築
        conversation_log_parts = []
        for turn in history:
             role = getattr(turn, 'role', None)
             parts = getattr(turn, 'parts', [])
             if role in ['user', 'model'] and parts:
                  text = getattr(parts[0], 'text', None)
                  if text:
                       prefix = 'You' if role == 'user' else 'AI'
                       conversation_log_parts.append(f"- **{prefix}:** {text}")
        conversation_log = "\n".join(conversation_log_parts)

        # ノート内容
        note_content = f"# {title}\n\n- Date: [[{date_str}]]\n- Participant: {user.display_name} ({user.id})\n\n---\n\n## 💬 Session Review\n{review}\n\n---\n\n## 📜 Full Transcript\n{conversation_log}\n"
        note_path = f"{self.dropbox_vault_path}{ENGLISH_LOG_PATH}/{filename}".replace("\\", "/") # パス区切り文字を統一

        try:
            # files_uploadを非同期実行
            await asyncio.to_thread(
                 self.dbx.files_upload,
                 note_content.encode('utf-8'), note_path, mode=WriteMode('add')
            )
            logging.info(f"英会話ログ保存成功 (Obsidian): {note_path}")

            # --- Daily note link addition ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md".replace("\\", "/")
            daily_note_content = f"# {date_str}\n\n" # デフォルト内容
            try:
                 metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                 daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if not (isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found()):
                    logging.error(f"デイリーノートのダウンロードエラー ({daily_note_path}): {e}")
                    # デイリーノート更新は失敗するが、ログ保存は完了している

            # パスから '.md' を削除し、先頭のスラッシュを除去
            note_filename_for_link = filename[:-3]
            link_path_part = ENGLISH_LOG_PATH.strip('/')
            # 正しいリンク形式 [[Folder/Subfolder/Note Name]]
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}]]"

            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_ENGLISH_LOG_HEADER)
            # files_uploadを非同期実行
            await asyncio.to_thread(
                 self.dbx.files_upload,
                 new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite')
            )
            logging.info(f"デイリーノート ({daily_note_path}) に英会話ログリンク追記成功。")
            # --- End daily note link ---
        except ApiError as e:
             logging.error(f"英会話ログ保存/デイリーノート更新 Dropboxエラー: {e}", exc_info=True)
        except Exception as e:
             logging.error(f"英会話ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)


    # --- _save_sakubun_log_to_obsidian ---
    async def _save_sakubun_log_to_obsidian(self, japanese_question: str, user_answer: str, feedback_text: str):
        if not self.dbx: logging.error("Cannot save Sakubun log: Dropbox client not available."); return

        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        # ファイル名に使えない文字を置換し、日本語部分を短くする
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', japanese_question)[:20] # 20文字に制限
        filename = f"{timestamp}-Sakubun_{safe_title_part}.md"

        # モデルアンサーを抽出 (エラーハンドリング強化)
        model_answers = ""
        try:
             # 大文字小文字無視、改行含む複数行マッチ
             model_answers_match = re.search(r"^\#\#\#\s*Model Answer(?:s)?\s*\n(.+?)(?=\n^\#\#\#|\Z)", feedback_text, re.DOTALL | re.MULTILINE | re.IGNORECASE)
             if model_answers_match:
                  # 箇条書き部分のみを抽出してフォーマット
                  answers_block = model_answers_match.group(1).strip()
                  raw_answers = re.findall(r"^\s*[-*+]\s+(.+)", answers_block, re.MULTILINE)
                  model_answers = "\n".join([f"- {ans.strip()}" for ans in raw_answers if ans.strip()])
        except Exception as e_re:
             logging.error(f"モデルアンサーの抽出中にエラー: {e_re}")

        # ノート内容
        note_content = f"# {date_str} 瞬間英作文: {japanese_question}\n\n- Date: [[{date_str}]]\n---\n\n## 問題\n{japanese_question}\n\n## あなたの回答\n{user_answer}\n\n## AIによるフィードバック\n{feedback_text}\n"
        if model_answers: note_content += f"\n---\n\n## モデルアンサー\n{model_answers}\n" # モデルアンサーがあれば追記
        note_path = f"{self.dropbox_vault_path}{SAKUBUN_LOG_PATH}/{filename}".replace("\\", "/")

        try:
            # files_uploadを非同期実行
            await asyncio.to_thread(
                 self.dbx.files_upload,
                 note_content.encode('utf-8'), note_path, mode=WriteMode('add')
            )
            logging.info(f"瞬間英作文ログ保存成功 (Obsidian): {note_path}")

            # --- Daily note link addition ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md".replace("\\", "/")
            daily_note_content = f"# {date_str}\n\n"
            try:
                 metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                 daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if not (isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found()):
                    logging.error(f"デイリーノートダウンロードエラー ({daily_note_path}): {e}")

            note_filename_for_link = filename[:-3]
            link_path_part = SAKUBUN_LOG_PATH.strip('/')
            # リンクテキストを日本語の問題文にする (短縮)
            link_text = japanese_question[:30] + "..." if len(japanese_question) > 33 else japanese_question
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{link_text}]]"

            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_SAKUBUN_LOG_HEADER)
            # files_uploadを非同期実行
            await asyncio.to_thread(
                 self.dbx.files_upload,
                 new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite')
            )
            logging.info(f"デイリーノート ({daily_note_path}) に瞬間英作文ログリンク追記成功。")
            # --- End daily note link ---
        except ApiError as e:
             logging.error(f"瞬間英作文ログ保存/デイリーノート更新 Dropboxエラー: {e}", exc_info=True)
        except Exception as e:
             logging.error(f"瞬間英作文ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)


    # --- on_message ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel_id != self.channel_id: return

        # /end command
        if message.content.strip().lower() == "/end":
            session = self.chat_sessions.pop(message.author.id, None)
            if session:
                await message.channel.send(f"{message.author.mention} の英会話セッションを終了します。レビューを作成中...")
                async with message.channel.typing():
                    # セッション履歴を取得 (存在確認)
                    history = getattr(session, 'history', [])
                    review_text = await self._generate_chat_review(history)

                    # --- レビューから重要フレーズを抽出 ---
                    important_phrases = extract_phrases_from_markdown_list(review_text, "重要フレーズ")
                    # --- ここまで ---

                    review_embed = discord.Embed(
                        title="💬 Session Review",
                        description=review_text,
                        color=discord.Color.gold(),
                        timestamp=datetime.now(JST) # タイムスタンプ
                    ).set_footer(text=f"{message.author.display_name}'s session")

                    # TTS View (重要フレーズがある場合のみ)
                    tts_view = TTSView(important_phrases, self.openai_client) if important_phrases and self.openai_client else None

                    await message.channel.send(embed=review_embed, view=tts_view) # viewを適用

                    # セッション履歴とログを保存
                    await self._save_session_to_dropbox(message.author.id, history)
                    await self._save_chat_log_to_obsidian(message.author, history, review_text)
            else:
                 await message.reply("現在開始されている英会話セッションはありません。", delete_after=10)
            return

        # Sakubun answer (返信の場合)
        if message.reference and message.reference.message_id:
            try:
                # 参照先のメッセージを取得
                original_msg = await message.channel.fetch_message(message.reference.message_id)
                # それがボットのメッセージで、かつEmbedがあり、タイトルに「第」が含まれるか？
                if (original_msg.author.id == self.bot.user.id and
                    original_msg.embeds and
                    original_msg.embeds[0].title and # タイトル存在確認
                    "第" in original_msg.embeds[0].title):

                    user_answer = message.content.strip()
                    if user_answer: # 回答が空でない場合のみ処理
                         await self.handle_sakubun_answer(message, user_answer, original_msg)
                    else: # 回答が空の場合
                         await message.add_reaction("❓")
                         await asyncio.sleep(5)
                         await message.remove_reaction("❓", self.bot.user)
                    return # 返信処理が終わったら以降は実行しない
            except discord.NotFound:
                 pass # 参照先が見つからない場合は通常のチャットとして処理
            except Exception as e_ref:
                 logging.error(f"瞬間英作文の返信処理中にエラー: {e_ref}")

        # Regular chat message
        if message.author.id in self.chat_sessions:
            await self.handle_chat_message(message)


    # --- handle_sakubun_answer ---
    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        if not self.gemini_model: await message.reply("⚠️ AIフィードバック機能が無効です。"); return
        if not original_msg.embeds or not original_msg.embeds[0].description: return # 問題文がない

        await message.add_reaction("🤔") # 処理中を示す
        japanese_question = original_msg.embeds[0].description.strip().replace("**","") # Markdown除去

        prompt = f"""あなたはプロの英語教師です。以下の日本語の文に対する学習者の英訳を添削し、フィードバックとモデルアンサーを提供してください。
# 指示
- 学習者の英訳が良い点、改善できる点を具体的に評価してください。
- 文法的な誤りや不自然な点があれば、根拠と共に分かりやすく解説してください。
- **必ず `### Model Answer` という見出しの下に、自然で正確なモデルアンサー（英文のみ）を箇条書き (`- Answer Sentence`) で2〜3個提示してください。**
- フィードバック全体をMarkdown形式で記述してください。
# 日本語の原文
{japanese_question}
# 学習者の英訳
{user_answer}"""

        feedback_text = "フィードバックの生成中にエラーが発生しました。"
        tts_view = None # TTS Viewの初期化

        try:
            # 応答生成 (タイムアウト設定)
            response = await asyncio.wait_for(self.gemini_model.generate_content_async(prompt), timeout=90)
            if response and hasattr(response, 'text'):
                 feedback_text = response.text.strip()
            else:
                 logging.warning(f"瞬間英作文フィードバック応答不正: {response}")

            feedback_embed = discord.Embed(
                title=f"添削結果: 「{japanese_question}」",
                description=feedback_text,
                color=discord.Color.green()
            )

            # --- モデルアンサーを抽出してTTS Viewを作成 ---
            model_answers = extract_phrases_from_markdown_list(feedback_text, "Model Answer")
            if model_answers and self.openai_client:
                 tts_view = TTSView(model_answers, self.openai_client)
            # --- ここまで ---

            await message.reply(embed=feedback_embed, view=tts_view, mention_author=False) # viewを適用, メンション抑制

            # Obsidianにログを保存
            await self._save_sakubun_log_to_obsidian(japanese_question, user_answer, feedback_text)

        except asyncio.TimeoutError:
             logging.error("瞬間英作文フィードバック生成タイムアウト")
             await message.reply("フィードバックの生成がタイムアウトしました。", mention_author=False)
        except Exception as e_fb:
             logging.error(f"瞬間英作文フィードバック/保存エラー: {e_fb}", exc_info=True)
             await message.reply(f"フィードバック処理中にエラーが発生しました: {type(e_fb).__name__}", mention_author=False)
        finally:
             try: await message.remove_reaction("🤔", self.bot.user)
             except discord.HTTPException: pass


    # --- handle_chat_message ---
    async def handle_chat_message(self, message: discord.Message):
        session = self.chat_sessions.get(message.author.id)
        if not session or not message.content: return # セッションがないかメッセージが空

        # メッセージ内容をログ出力（デバッグ用）
        logging.info(f"Handling chat message from {message.author.id}: {message.content[:50]}...")

        async with message.channel.typing(): # タイピング表示
            try:
                # Geminiに応答を非同期でリクエスト (タイムアウト設定)
                response = await asyncio.wait_for(session.send_message_async(message.content), timeout=60)
                response_text = response.text if response and hasattr(response, 'text') else "Sorry, I couldn't generate a response."

                # --- TTS View を作成 ---
                tts_view = TTSView(response_text, self.openai_client) if self.openai_client else None
                # --- ここまで ---

                # 返信 (メンション抑制)
                await message.reply(response_text, view=tts_view, mention_author=False)

            except asyncio.TimeoutError:
                 logging.error(f"英会話応答タイムアウト (User: {message.author.id})")
                 await message.reply("Sorry, I took too long to think. Could you say that again?", mention_author=False)
            except Exception as e:
                 logging.error(f"英会話応答エラー (User: {message.author.id}): {e}", exc_info=True)
                 await message.reply(f"Sorry, an error occurred while processing your message: {type(e).__name__}", mention_author=False)

# --- setup ---
async def setup(bot: commands.Bot):
    # チャンネルIDが0でないか、環境変数が設定されているかなどを確認
    if int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) != 0:
        await bot.add_cog(EnglishLearningCog(bot))
    else:
        logging.warning("ENGLISH_LEARNING_CHANNEL_ID未設定のためEnglishLearningCog未ロード。")