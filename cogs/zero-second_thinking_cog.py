import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import aiohttp
import openai
import google.generativeai as genai
from datetime import datetime, time
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import json
import asyncio
from PIL import Image
import io

# 共通関数をインポート
from utils.obsidian_utils import update_section
# Google Docs Handlerをインポート (エラーハンドリング付き)
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("Google Docs連携が有効です (ZeroSecondThinkingCog)。")
except ImportError:
    logging.warning("google_docs_handler.pyが見つからないため、Google Docs連携は無効です (ZeroSecondThinkingCog)。")
    google_docs_enabled = False
    # ダミー関数を定義
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]
SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp'] # HEIC対応を追加する場合はここに'image/heic', 'image/heif'を追加
THINKING_TIMES = [
    time(hour=9, minute=0, tzinfo=JST),
    time(hour=12, minute=0, tzinfo=JST),
    time(hour=15, minute=0, tzinfo=JST),
    time(hour=18, minute=0, tzinfo=JST),
    time(hour=21, minute=0, tzinfo=JST),
]

# --- HEIC support (Optional: If pillow-heif is installed) ---
try:
    # from PIL import Image # Already imported above
    import pillow_heif
    pillow_heif.register_heif_opener()
    # HEICのMIMEタイプをサポートリストに追加
    SUPPORTED_IMAGE_TYPES.append('image/heic')
    SUPPORTED_IMAGE_TYPES.append('image/heif')
    logging.info("HEIC/HEIF image support enabled.")
except ImportError:
    logging.warning("pillow_heif not installed. HEIC/HEIF support is disabled.")
# --- End HEIC support ---


class ZeroSecondThinkingCog(commands.Cog):
    """
    Discord上でゼロ秒思考を支援するためのCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.channel_id = int(os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID", "0"))
        self.openai_api_key = os.getenv("OPENAI_API_KEY") # 音声入力用
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") # テキスト生成・画像認識用

        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.history_path = f"{self.dropbox_vault_path}/.bot/zero_second_thinking_history.json"

        # --- 初期チェックとAPIクライアント初期化 ---
        if not all([self.channel_id, self.openai_api_key, self.gemini_api_key, self.dropbox_refresh_token]):
            logging.warning("ZeroSecondThinkingCog: 必要な環境変数が不足しています。")
            self.is_ready = False
        else:
            try:
                self.session = aiohttp.ClientSession()
            except Exception as e:
                 logging.error(f"aiohttp ClientSessionの初期化に失敗: {e}")
                 self.is_ready = False
                 return

            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") # メインのテキスト生成モデル
            self.gemini_vision_model = genai.GenerativeModel("gemini-2.5-pro") # 画像認識用モデル (handwritten_memo_cogに合わせる)
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            self.is_ready = True
            self.last_question_answered = True # 起動時はリセット状態とみなす
            self.latest_question_message_id = None # 最新の質問メッセージIDを保持

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            self.thinking_prompt_loop.start()
            logging.info(f"ゼロ秒思考の定時通知タスクを開始しました。")

    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        if self.is_ready:
            if hasattr(self, 'session') and self.session and not self.session.closed:
                await self.session.close()
            self.thinking_prompt_loop.cancel()

    async def _get_thinking_history(self) -> list:
        """過去の思考履歴をDropboxから読み込む"""
        try:
            _, res = self.dbx.files_download(self.history_path)
            return json.loads(res.content.decode('utf-8'))
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return []
            logging.error(f"思考履歴の読み込みに失敗: {e}")
            return []
        except json.JSONDecodeError:
            logging.error(f"思考履歴ファイル ({self.history_path}) のJSON形式が不正です。空のリストを返します。")
            return []

    async def _save_thinking_history(self, history: list):
        """思考履歴をDropboxに保存（最新10件まで）"""
        try:
            limited_history = history[-10:]
            self.dbx.files_upload(
                json.dumps(limited_history, ensure_ascii=False, indent=2).encode('utf-8'),
                self.history_path,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"思考履歴の保存に失敗: {e}")

    @tasks.loop(time=THINKING_TIMES)
    async def thinking_prompt_loop(self):
        """定時にお題を投稿するループ"""
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        try:
            # --- 未回答の質問があれば削除 ---
            if not self.last_question_answered and self.latest_question_message_id:
                try:
                    old_question_msg = await channel.fetch_message(self.latest_question_message_id)
                    await old_question_msg.delete()
                    logging.info(f"未回答の質問 (ID: {self.latest_question_message_id}) を削除しました。")
                    self.latest_question_message_id = None
                except discord.NotFound:
                    logging.warning(f"削除対象の未回答質問が見つかりませんでした (ID: {self.latest_question_message_id})。")
                except discord.Forbidden:
                    logging.error("未回答質問の削除権限がありません。")
                except Exception as e_del:
                    logging.error(f"未回答質問の削除中にエラー: {e_del}", exc_info=True)
            # --- ここまで ---

            history = await self._get_thinking_history()
            history_context = ""
            if self.last_question_answered and history:
                history_context = "\n".join([f"- {item['question']}: {item['answer'][:100]}..." for item in history])
            else:
                 if not self.last_question_answered:
                    await self._save_thinking_history([])
                    history_context = "履歴はありません。"


            prompt = f"""
            あなたは思考を深めるための問いを投げかけるコーチです。
            私が「ゼロ秒思考」を行うのを支援するため、質の高いお題を1つだけ生成してください。

            # 指示
            - ユーザーの過去の思考履歴を参考に、より深い洞察を促す問いを生成してください。
            - 過去の回答内容を掘り下げるような質問や、関連するが異なる視点からの質問が望ましいです。
            - 過去数回の質問と重複しないようにしてください。
            - お題はビジネス、自己啓発、人間関係、創造性など、多岐にわたるテーマから選んでください。
            - 前置きや挨拶は一切含めず、お題のテキストのみを生成してください。

            # 過去の思考履歴（質問と回答の要約）
            {history_context if history_context else "履歴はありません。"}
            ---
            お題:
            """
            response = await self.gemini_model.generate_content_async(prompt)
            question = "デフォルトのお題: 今、一番気になっていることは何ですか？"
            if response and hasattr(response, 'text') and response.text.strip():
                 question = response.text.strip().replace("*", "")
            else:
                 logging.warning(f"Geminiからの質問生成に失敗、または空の応答: {response}")


            embed = discord.Embed(title="🤔 ゼロ秒思考の時間です", description=f"お題: **{question}**", color=discord.Color.teal())
            embed.set_footer(text="このメッセージに返信する形で、思考を書き出してください（音声・手書きメモ画像も可）。`/zst_end`で終了。")

            sent_message = await channel.send(embed=embed)
            self.latest_question_message_id = sent_message.id

            self.last_question_answered = False

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] 定時お題生成エラー: {e}", exc_info=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、Zero-Second Thinkingのフローを処理する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return

        if message.content.strip().lower() == "/zst_end":
            await self.end_thinking_session(message)
            return

        if not message.reference or not message.reference.message_id:
            return

        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        try:
            original_msg = await channel.fetch_message(message.reference.message_id)
        except discord.NotFound:
            return

        if original_msg.author.id != self.bot.user.id or not original_msg.embeds:
            return

        embed_title = original_msg.embeds[0].title
        if "ゼロ秒思考の時間です" not in embed_title and "さらに深掘りしましょう" not in embed_title:
            return

        if original_msg.id != self.latest_question_message_id:
             try:
                await message.reply("これは古い質問への回答のようです。最新の質問に回答するか、`/zst_end`で終了してください。", delete_after=15)
                # 古い質問への回答メッセージは削除しない
                # await message.delete(delay=15)
             except discord.HTTPException: pass
             return

        self.last_question_answered = True
        self.latest_question_message_id = None

        last_question_match = re.search(r'お題: \*\*(.+?)\*\*', original_msg.embeds[0].description)
        last_question = "不明なお題"
        if last_question_match:
            last_question = last_question_match.group(1)
        else:
            logging.warning("質問メッセージからお題の抽出に失敗しました。")
            last_question = original_msg.embeds[0].title # フォールバック

        input_type = "text"
        attachment_to_process = None
        if message.attachments:
            img_attachment = next((att for att in message.attachments if att.content_type in SUPPORTED_IMAGE_TYPES), None)
            audio_attachment = next((att for att in message.attachments if att.content_type in SUPPORTED_AUDIO_TYPES), None)
            if img_attachment:
                input_type = "image"
                attachment_to_process = img_attachment
            elif audio_attachment:
                input_type = "audio"
                attachment_to_process = audio_attachment

        if input_type == "text" and not message.content.strip():
             logging.info("テキストメッセージが空のため処理をスキップします。")
             self.last_question_answered = False
             self.latest_question_message_id = original_msg.id
             try:
                 await message.add_reaction("❓")
                 # 空の回答メッセージは削除しない
                 # await message.delete(delay=10)
             except discord.HTTPException: pass
             return

        await self._process_thinking_memo(message, last_question, original_msg, input_type, attachment_to_process)

    async def _process_thinking_memo(self, message: discord.Message, last_question: str, original_msg: discord.Message, input_type: str, attachment: discord.Attachment = None):
        """思考メモを処理し、Obsidianに記録し、掘り下げ質問を生成する"""
        temp_audio_path = None
        formatted_answer = "回答の処理に失敗しました。"
        try:
            await original_msg.edit(delete_after=None) # Keep original question visible longer
            await message.add_reaction("⏳")

            # --- 入力タイプに応じた処理 ---
            if input_type == "audio" and attachment:
                temp_audio_path = Path(f"./temp_{attachment.filename}")
                async with self.session.get(attachment.url) as resp:
                    if resp.status == 200:
                        with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                    else: raise Exception(f"音声ファイルのダウンロード失敗: Status {resp.status}")

                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                transcribed_text = transcription.text

                formatting_prompt = (
                    "以下の音声メモの文字起こしを、構造化された箇条書きのMarkdown形式でまとめてください。\n"
                    "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                    f"---\n\n{transcribed_text}"
                )
                response = await self.gemini_model.generate_content_async(formatting_prompt)
                formatted_answer = response.text.strip() if response and hasattr(response, 'text') else transcribed_text

            # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
            elif input_type == "image" and attachment:
                # handwritten_memo_cogと同様の方法で画像データを取得・処理
                async with self.session.get(attachment.url) as resp:
                    if resp.status != 200:
                        raise Exception(f"画像ファイルのダウンロードに失敗: Status {resp.status}")
                    image_bytes = await resp.read()

                img = Image.open(io.BytesIO(image_bytes))

                vision_prompt = [
                    "この画像は手書きのメモです。内容を読み取り、構造化された箇条書きのMarkdown形式でテキスト化してください。返答には前置きや説明は含めず、箇条書きのテキスト本体のみを生成してください。",
                    img,
                ]
                # handwritten_memo_cog と同じモデルを使用
                response = await self.gemini_vision_model.generate_content_async(vision_prompt)
                formatted_answer = response.text.strip() if response and hasattr(response, 'text') else "手書きメモの読み取りに失敗しました。"
            # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<

            else: # テキスト入力の場合 (デフォルト)
                formatted_answer = message.content.strip()
            # --- ここまで ---

            # 思考履歴を更新
            history = await self._get_thinking_history()
            history.append({"question": last_question, "answer": formatted_answer})
            await self._save_thinking_history(history)

            # --- Obsidianへの保存処理 ---
            now = datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            safe_title = re.sub(r'[\\/*?:"<>|]', "", last_question)[:50]
            if not safe_title: safe_title = "Untitled"
            timestamp = now.strftime('%Y%m%d%H%M%S')
            note_filename = f"{timestamp}-{safe_title}.md"
            note_path = f"{self.dropbox_vault_path}/Zero-Second Thinking/{note_filename}"

            new_note_content = (
                f"# {last_question}\n\n"
                f"- **Source:** Discord ({input_type.capitalize()})\n"
                f"- **作成日:** {daily_note_date}\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"---\n\n"
                f"## 回答\n{formatted_answer}"
            )
            self.dbx.files_upload(new_note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"[Zero-Second Thinking] 新規ノートを作成: {note_path}")

            # --- デイリーノートへのリンク追記 ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
            daily_note_content = ""
            try:
                _, res = self.dbx.files_download(daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# {daily_note_date}\n"
                    logging.info(f"デイリーノートが見つからなかったため新規作成: {daily_note_path}")
                else: raise

            note_filename_for_link = note_filename.replace('.md', '')
            link_to_add = f"- [[Zero-Second Thinking/{note_filename_for_link}|{last_question}]]"
            section_header = "## Zero-Second Thinking"
            new_daily_content = update_section(daily_note_content, link_to_add, section_header)
            self.dbx.files_upload(new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノートにリンクを追記: {daily_note_path}")

            # --- Google Docs への保存 ---
            if google_docs_enabled:
                gdoc_content = f"## 質問\n{last_question}\n\n## 回答\n{formatted_answer}"
                gdoc_title = f"ゼロ秒思考 - {daily_note_date} - {last_question[:30]}"
                try:
                    await append_text_to_doc_async(
                        text_to_append=gdoc_content,
                        source_type="Zero-Second Thinking",
                        title=gdoc_title
                    )
                    logging.info("Google Docsにゼロ秒思考ログを保存しました。")
                except Exception as e_gdoc:
                    logging.error(f"Google Docsへのゼロ秒思考ログ保存中にエラー: {e_gdoc}", exc_info=True)
            # --- ここまで ---

            # ★ 「記録しました」メッセージを送信せず、ユーザー回答も削除しない
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
            # logging.info(f"ユーザーの回答 (ID: {message.id}) は削除されません。") # ログは不要かも


            # --- 掘り下げ質問の生成 ---
            digging_prompt = f"""
            ユーザーは「ゼロ秒思考」を行っています。以下の「元の質問」と「ユーザーの回答」を踏まえて、思考をさらに深めるための鋭い掘り下げ質問を1つだけ生成してください。
            # 元の質問
            {last_question}
            # ユーザーの回答
            {formatted_answer}
            ---
            掘り下げ質問:
            """
            response = await self.gemini_model.generate_content_async(digging_prompt)
            new_question = "追加の質問: さらに詳しく教えてください。"
            if response and hasattr(response, 'text') and response.text.strip():
                 new_question = response.text.strip().replace("*", "")
            else:
                 logging.warning(f"Geminiからの深掘り質問生成に失敗、または空の応答: {response}")

            embed = discord.Embed(title="🤔 さらに深掘りしましょう", description=f"お題: **{new_question}**", color=discord.Color.blue())
            embed.set_footer(text="このメッセージに返信する形で、思考を書き出してください。`/zst_end`で終了。")

            sent_message = await message.channel.send(embed=embed)
            self.latest_question_message_id = sent_message.id
            self.last_question_answered = False

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] 処理中にエラー: {e}", exc_info=True)
            self.last_question_answered = True # エラー時は一旦リセット
            self.latest_question_message_id = None
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
                # エラー時もユーザーメッセージは削除しない
                # await message.delete(delay=ANSWER_DELETE_DELAY)
            except discord.HTTPException: pass
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.remove(temp_audio_path)
                except OSError as e_rm:
                     logging.error(f"一時音声ファイル削除失敗: {e_rm}")

    # --- /zst_end コマンド処理用メソッド ---
    async def end_thinking_session(self, message: discord.Message):
        """ゼロ秒思考セッションを終了する"""
        channel = message.channel
        if not self.last_question_answered and self.latest_question_message_id:
            try:
                last_question_msg = await channel.fetch_message(self.latest_question_message_id)
                await last_question_msg.delete()
                logging.info(f"ユーザーのリクエストにより未回答の質問 (ID: {self.latest_question_message_id}) を削除しました。")
                await message.reply("未回答の質問を削除し、ゼロ秒思考セッションを終了しました。", delete_after=10)
            except discord.NotFound:
                logging.warning(f"終了リクエスト時、削除対象の質問が見つかりませんでした (ID: {self.latest_question_message_id})。")
                await message.reply("終了対象の質問が見つかりませんでした。", delete_after=10)
            except discord.Forbidden:
                logging.error("終了リクエスト時、質問の削除権限がありません。")
                await message.reply("質問の削除権限がありません。", delete_after=10)
            except Exception as e_del:
                logging.error(f"終了リクエスト時の質問削除中にエラー: {e_del}", exc_info=True)
                await message.reply("質問の削除中にエラーが発生しました。", delete_after=10)
            finally:
                self.last_question_answered = True
                self.latest_question_message_id = None
        else:
            await message.reply("現在、未回答の質問はありません。新しい質問をお待ちください。", delete_after=10)

        try:
            await message.delete(delay=10)
        except discord.HTTPException: pass

async def setup(bot: commands.Bot):
    """CogをBotに追加する"""
    if not all([os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID"),
                os.getenv("OPENAI_API_KEY"),
                os.getenv("GEMINI_API_KEY"),
                os.getenv("DROPBOX_REFRESH_TOKEN"),
                os.getenv("DROPBOX_APP_KEY"),
                os.getenv("DROPBOX_APP_SECRET")]):
        logging.error("ZeroSecondThinkingCog: 必要な環境変数が不足しているため、Cogをロードしません。")
        return
    try:
         from PIL import Image # Pillow の存在確認
    except ImportError:
         logging.error("ZeroSecondThinkingCog: Pillowライブラリが見つかりません。手書きメモ機能を使用するには `pip install Pillow` を実行してください。Cogをロードしません。")
         return

    await bot.add_cog(ZeroSecondThinkingCog(bot))