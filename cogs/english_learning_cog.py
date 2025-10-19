import os
import json
import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
from openai import AsyncOpenAI
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import io
import re

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
            # openai v1.0以降の書き方に修正
            response = await self.openai_client.audio.speech.create(
                model="tts-1", voice="alloy", input=phrase_to_speak, response_format="mp3"
            )
            audio_bytes = response.content # .content でバイト列を取得
            # --- ここまで ---

            # --- Discordに音声ファイルを送信 ---
            audio_buffer = io.BytesIO(audio_bytes)
            audio_file = discord.File(fp=audio_buffer, filename=f"phrase_{phrase_index}.mp3")
            await interaction.followup.send(f"🔊 \"{phrase_to_speak}\"", file=audio_file, ephemeral=True)
            # --- ここまで ---

        except ValueError:
            logging.error(f"custom_idからインデックスの解析に失敗: {custom_id}")
            await interaction.followup.send("ボタン処理エラー。", ephemeral=True)
        # openai v1.0以降のエラーハンドリングに修正
        except openai.APIError as e:
             logging.error(f"OpenAI APIエラー (TTS生成中): {e}", exc_info=True)
             await interaction.followup.send(f"音声生成中にOpenAI APIエラーが発生しました: {e}", ephemeral=True)
        except Exception as e:
            logging.error(f"tts_button_callback内でエラー: {e}", exc_info=True)
            await interaction.followup.send(f"音声の生成・送信中にエラーが発生しました: {e}", ephemeral=True)

class EnglishLearning(commands.Cog):
    def __init__(self, bot, openai_api_key, gemini_api_key, dropbox_token): # dropbox_token を受け取るように修正
        self.bot = bot
        self.openai_client = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None # キーがない場合はNone
        genai.configure(api_key=gemini_api_key)
        self.model = genai.GenerativeModel("gemini-2.5-pro")
        self.dbx = dropbox.Dropbox(dropbox_token) if dropbox_token else None # トークンがない場合はNone
        self.session_dir = "/english_sessions" # Dropbox内のパス
        # is_ready フラグを追加
        self.is_ready = bool(self.openai_client and self.dbx) # OpenAIとDropboxが初期化できたらTrue
        if not self.openai_client:
            logging.warning("OpenAI API Key not found. TTS functionality will be disabled.")
        if not self.dbx:
            logging.warning("Dropbox Token not found. Session saving/loading will be disabled.")
        logging.info("EnglishLearning Cog initialized.")


    def _get_session_path(self, user_id: int) -> str:
        # vault_path を考慮しない（session_dir がルートからのパス）
        return f"{self.session_dir}/{user_id}.json"

    @app_commands.command(name="english_chat", description="AIと英会話を始めます")
    async def english_chat(self, interaction: discord.Interaction):
        if not self.is_ready: # is_ready をチェック
             await interaction.response.send_message("English Learning機能は現在利用できません（設定不足）。", ephemeral=True)
             return

        await interaction.response.defer()
        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)

        session = await self._load_session_from_dropbox(user_id)

        # Geminiのシステムインストラクションを追加
        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        model_with_instruction = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction) # モデル名修正

        if session:
            logging.info(f"セッション再開: {session_path}")
            # start_chat に履歴を渡す
            chat = model_with_instruction.start_chat(history=session)
            # 最初のメッセージを修正
            response = await asyncio.wait_for(chat.send_message_async("Welcome back! Let's continue our English conversation. How have you been?"), timeout=60)
            response_text = response.text if response and hasattr(response, "text") else "Hi again! Let's chat."
            await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client) if self.openai_client else None) # TTSViewにクライアントを渡す
        else:
            logging.info(f"新規セッション開始: {session_path}")
            # start_chat は空の履歴で開始
            chat = model_with_instruction.start_chat(history=[])
            initial_prompt = "Hi! I'm your AI English partner. Let's chat! How's it going?"

            try:
                response = await asyncio.wait_for(chat.send_message_async(initial_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi! Let's chat."
                await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client) if self.openai_client else None) # TTSViewにクライアントを渡す
            except asyncio.TimeoutError:
                logging.error("初回応答タイムアウト")
                response_text = "Sorry, the initial response timed out. Let's start anyway. How are you?"
                await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client) if self.openai_client else None) # TTSViewにクライアントを渡す
            except Exception as e_init:
                logging.error(f"初回応答生成失敗: {e_init}", exc_info=True)
                response_text = "Sorry, an error occurred while starting the chat. Let's try starting simply. How are you?"
                await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client) if self.openai_client else None) # TTSViewにクライアントを渡す

        # chatオブジェクトを保存（修正：セッション管理が必要なら追加）
        # self.chat_sessions[user_id] = chat # chat_sessions 属性が必要

        try:
            await interaction.followup.send("会話を続けるには、このメッセージに返信してください。終了は `/end`", ephemeral=True, delete_after=60)
        except discord.HTTPException:
            pass # メッセージが既に削除されている場合など

    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx:
            logging.warning("Dropbox client is not initialized. Cannot load session.")
            return None

        session_path = self._get_session_path(user_id)
        try:
            logging.info(f"Loading session from: {session_path}")
            # Dropbox API呼び出しを非同期に実行
            metadata, res = await asyncio.to_thread(self.dbx.files_download, session_path)

            try:
                # Geminiライブラリが要求する形式に変換
                loaded_data = json.loads(res.content)
                history = []
                for item in loaded_data:
                    # 'parts' が文字列のリストであることを確認し、テキストとして結合
                    parts_text = "".join(item.get("parts", []))
                    history.append({"role": item.get("role"), "parts": [{"text": parts_text}]}) # parts を辞書のリストに
                return history
            except json.JSONDecodeError as json_e:
                logging.error(f"JSON解析失敗 ({session_path}): {json_e}")
                return None
            except Exception as format_e: # 形式変換エラー
                logging.error(f"Session data format error ({session_path}): {format_e}")
                return None


        except ApiError as e:
            if (
                isinstance(e.error, DownloadError)
                and e.error.is_path()
                and e.error.get_path().is_not_found()
            ):
                logging.info(f"Session file not found for {user_id} at {session_path}")
                return None
            logging.error(f"Dropbox APIエラー ({session_path}): {e}")
            return None

        except Exception as e:
            logging.error(f"セッション読込エラー ({session_path}): {e}", exc_info=True)
            return None

    async def _save_session_to_dropbox(self, user_id: int, history: list):
        if not self.dbx:
            logging.warning("Dropbox client is not initialized. Cannot save session.")
            return

        session_path = self._get_session_path(user_id)
        try:
            # GeminiのhistoryオブジェクトをJSONシリアライズ可能な形式に変換
            serializable_history = []
            for turn in history:
                # Content オブジェクトから role と parts を取得
                role = getattr(turn, "role", None)
                parts = getattr(turn, "parts", [])
                if role and parts:
                    # Part オブジェクトから text を取得し、リストとして格納
                    part_texts = [getattr(p, "text", str(p)) for p in parts]
                    serializable_history.append({"role": role, "parts": part_texts})

            if not serializable_history:
                 logging.warning(f"History for user {user_id} is empty or not serializable. Skipping save.")
                 return

            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode("utf-8")
            # Dropbox API呼び出しを非同期に実行
            await asyncio.to_thread(
                self.dbx.files_upload,
                content,
                session_path,
                mode=WriteMode("overwrite"),
            )
            logging.info(f"Saved session to: {session_path}")

        except Exception as e:
            logging.error(f"セッション保存失敗 ({session_path}): {e}", exc_info=True)

    @app_commands.command(name="end", description="英会話を終了します")
    async def end_chat(self, interaction: discord.Interaction):
        if not self.is_ready: # is_ready をチェック
             await interaction.response.send_message("English Learning機能は現在利用できません（設定不足）。", ephemeral=True)
             return

        await interaction.response.defer()
        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)

        try:
            # Dropbox API呼び出しを非同期に実行
            await asyncio.to_thread(self.dbx.files_delete_v2, session_path)
            await interaction.followup.send("セッションファイルを削除しました。お疲れさまでした！") # メッセージを修正
        except ApiError as e:
             # is_not_found エラーは無視しても良い場合がある
            if isinstance(e.error, dropbox.exceptions.PathLookupError) and e.error.is_not_found():
                 logging.warning(f"Session file not found during deletion, might have been already deleted: {session_path}")
                 await interaction.followup.send("セッションファイルが見つかりませんでした（既に削除済みかもしれません）。")
            else:
                logging.error(f"セッション削除失敗 ({session_path}): {e}")
                await interaction.followup.send("セッションファイルの削除に失敗しました。")
        except Exception as e:
            logging.error(f"英会話終了エラー: {e}", exc_info=True)
            await interaction.followup.send("セッション終了処理中にエラーが発生しました。")

    # --- on_message リスナーを追加 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ボット自身のメッセージ、他のチャンネル、コマンド呼び出しは無視
        if message.author.bot or str(message.channel.id) != os.getenv("ENGLISH_LEARNING_CHANNEL_ID") or message.content.startswith('/'):
             return

        user_id = message.author.id

        # chat = self.chat_sessions[user_id]
        async with message.channel.typing():
             try:

                # ダミー応答 (上記TODO実装までの仮)
                await asyncio.sleep(1) # AIが考えているように見せる
                await message.reply(f"Received: {message.content} (Chat handling not fully implemented yet)",
                                    view=TTSView(f"Received: {message.content}", self.openai_client) if self.openai_client else None)

             except Exception as e:
                 logging.error(f"英会話中のメッセージ処理エラー: {e}", exc_info=True)
                 await message.reply("Sorry, an error occurred while processing your message.")


async def setup(bot):
    # 環境変数を取得
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    # dropbox_token は OAuth 2 refresh token を使うように修正
    dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
    dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")

    # 必須のキーを確認
    if not gemini_key or not dropbox_refresh_token or not dropbox_app_key or not dropbox_app_secret:
        logging.error("EnglishLearningCog: 必須の環境変数 (GEMINI_API_KEY, DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET) が不足しているため、Cogをロードしません。")
        return

    # Dropboxクライアントを作成（refresh token を使う）
    try:
        dbx = dropbox.Dropbox(
            app_key=dropbox_app_key,
            app_secret=dropbox_app_secret,
            oauth2_refresh_token=dropbox_refresh_token
        )
        # 接続テスト（任意）
        dbx.users_get_current_account()
        logging.info("Dropbox connection successful using refresh token.")
    except Exception as e:
        logging.error(f"Failed to initialize Dropbox client for EnglishLearningCog: {e}", exc_info=True)
        return # Dropboxクライアントが初期化できなければCogをロードしない

    # Cogをインスタンス化して追加
    # __init__ に渡す引数を修正 (Dropboxクライアントを直接渡すのではなく、トークン情報を渡す)
    await bot.add_cog(
        EnglishLearning(
            bot,
            openai_key,
            gemini_key,
            dropbox_refresh_token
        )
    )