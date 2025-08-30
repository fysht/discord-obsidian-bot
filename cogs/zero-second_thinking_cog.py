import os
import discord
from discord.ext import commands
import logging
import aiohttp
import openai
import google.generativeai as genai
from datetime import datetime
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
import re

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]

class ZeroSecondThinkingCog(commands.Cog):
    """
    Discord上でゼロ秒思考を支援するためのCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.zero_second_thinking_channel_id = int(os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID", 0))
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        # --- ユーザーの状態を管理 ---
        self.user_states = {}

        # --- 初期チェック ---
        if not self.zero_second_thinking_channel_id:
            logging.warning("ZeroSecondThinkingCog: ZERO_SECOND_THINKING_CHANNEL_IDが設定されていません。")
        if not self.openai_api_key:
            logging.warning("ZeroSecondThinkingCog: OPENAI_API_KEYが設定されていません。")
        if not self.gemini_api_key:
            logging.warning("ZeroSecondThinkingCog: GEMINI_API_KEYが設定されていません。")
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("ZeroSecondThinkingCog: Dropboxの認証情報が不足しています。")

        # --- APIクライアントの初期化 ---
        self.session = aiohttp.ClientSession()
        if self.openai_api_key:
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)

    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        await self.session.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、Zero-Second Thinkingのフローを処理する"""
        # --- 処理実行の条件判定 ---
        if message.author.bot:
            return
        if message.channel.id != self.zero_second_thinking_channel_id:
            return

        user_id = message.author.id

        # --- テキストメッセージの処理 ---
        if message.content.lower() == 's':
            await self._start_new_session(message)
            # メッセージを削除
            try:
                await message.delete()
            except discord.Forbidden:
                logging.warning(f"ZeroSecondThinkingCog: Botにメッセージ削除の権限がありません。(User: {message.author})")
            except discord.NotFound:
                pass # すでに削除されている場合は何もしない
            return
        
        if message.content.lower() == 'd':
            await self._dig_deeper(message)
            # メッセージを削除
            try:
                await message.delete()
            except discord.Forbidden:
                logging.warning(f"ZeroSecodThinkingCog: Botにメッセージ削除の権限がありません。(User: {message.author})")
            except discord.NotFound:
                pass # すでに削除されている場合は何もしない
            return

        # --- 音声メッセージ（添付ファイル）の処理 ---
        if message.attachments and any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
            if user_id in self.user_states and 'last_question' in self.user_states[user_id]:
                await self._process_thinking_memo(message, message.attachments[0])
            else:
                # ユーザーが思考セッションを開始していない場合は何もしない
                return

    async def _start_new_session(self, message: discord.Message):
        """新しいZero-Second Thinkingセッションを開始する"""
        user_id = message.author.id
        try:
            async with message.channel.typing():
                # 1. Gemini APIでお題を生成
                model = genai.GenerativeModel("gemini-2.5-pro")
                prompt = (
                    "あなたはこれから、私が「ゼロ秒思考」を行うのを支援します。\n"
                    "ゼロ秒思考とは、赤羽雄二氏が提唱する、A4用紙に1件1ページのメモを1分以内に書き、深く考える習慣です。\n"
                    "これから私がこのゼロ秒思考を行いますので、ゼロ秒思考を行うのに適したお題を1つ、前置きや返答を一切含めずに生成してください。\n"
                    "テーマはビジネス、自己啓発、プライベートなど多岐にわたりますが、深い洞察を促すような問いをお願いします。"
                )
                response = await model.generate_content_async(prompt)
                question = response.text.strip()

                # 2. 状態をリセットし、新しいお題を保存
                self.user_states[user_id] = {'last_question': question}
                
                # 3. お題をチャンネルに投稿
                await message.channel.send(f"お題: **{question}**")
                logging.info(f"[Zero-Second Thinking] New session for {message.author}: {question}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Failed to start new session: {e}", exc_info=True)
            await message.channel.send(f"エラーが発生しました: {e}")

    async def _dig_deeper(self, message: discord.Message):
        """既存の思考を深掘りする"""
        user_id = message.author.id
        state = self.user_states.get(user_id)

        # 1. 状態のチェック
        if not state or 'last_question' not in state or 'last_answer' not in state:
            await message.channel.send("先に `s` で新しい思考を開始し、一度音声で回答してください。")
            return

        try:
            async with message.channel.typing():
                # 2. Gemini APIで深掘りの質問を生成
                model = genai.GenerativeModel("gemini-2.5-pro")
                prompt = (
                    "以下の『これまでの文脈』を踏まえて、思考をさらに深掘りするための、鋭い問いを1つだけ、前置きや返答を一切含めずに生成してください。\n\n"
                    "# これまでの文脈\n"
                    f"## 問い\n{state['last_question']}\n\n"
                    f"## 回答\n{state['last_answer']}"
                )
                response = await model.generate_content_async(prompt)
                new_question = response.text.strip()

                # 3. 状態を更新
                self.user_states[user_id]['last_question'] = new_question
                
                # 4. 新しいお題をチャンネルに投稿
                await message.channel.send(f"次の問い: **{new_question}**")
                logging.info(f"[Zero-Second Thinking] Digging deeper for {message.author}: {new_question}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Failed to dig deeper: {e}", exc_info=True)
            await message.channel.send(f"エラーが発生しました: {e}")


    async def _process_thinking_memo(self, message: discord.Message, attachment: discord.Attachment):
        """音声メモを処理し、思考を記録するコアロジック"""
        temp_audio_path = None
        user_id = message.author.id
        state = self.user_states.get(user_id, {})
        last_question = state.get('last_question', '不明なお題')

        try:
            # 1. 進捗表示と音声処理
            await message.add_reaction("⏳")
            temp_audio_path = Path(f"./temp_{attachment.filename}")

            # 音声ファイルをダウンロード
            async with self.session.get(attachment.url) as resp:
                if resp.status == 200:
                    with open(temp_audio_path, 'wb') as f:
                        f.write(await resp.read())
                else:
                    raise Exception(f"音声ファイルのダウンロードに失敗: Status {resp.status}")

            # Whisper APIで文字起こし
            with open(temp_audio_path, "rb") as audio_file:
                transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            transcribed_text = transcription.text

            # 2. 回答の整形
            model = genai.GenerativeModel("gemini-2.5-pro")
            formatting_prompt = (
                "以下の音声メモの文字起こしを、構造化された箇条書きのMarkdown形式でまとめてください。\n"
                "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"---\n\n{transcribed_text}"
            )
            response = await model.generate_content_async(formatting_prompt)
            formatted_answer = response.text.strip()

            # 3. Obsidianへの保存
            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                now = datetime.now(JST)
                daily_note_date = now.strftime('%Y-%m-%d')
                
                # 既存ノートへの追記か、新規作成かを判断
                if 'note_path' in state:
                    # 既存ノートに追記
                    note_path = state['note_path']
                    
                    # 既存ノートをダウンロード
                    _, res = dbx.files_download(note_path)
                    current_content = res.content.decode('utf-8')

                    # 追記する内容を作成
                    content_to_append = (
                        f"\n\n---\n\n"
                        f"### ▼ {last_question}\n"
                        f"{formatted_answer}"
                    )
                    new_content = current_content + content_to_append
                    
                    # ノートを上書きアップロード
                    dbx.files_upload(
                        new_content.encode('utf-8'),
                        note_path,
                        mode=WriteMode('overwrite')
                    )
                    logging.info(f"[Zero-Second Thinking] Appended to note for {message.author}: {note_path}")

                else:
                    # 新規ノートを作成
                    safe_title = re.sub(r'[\\/*?:"<>|]', "", last_question)
                    if not safe_title: safe_title = "Untitled"
                    timestamp = now.strftime('%Y%m%d%H%M%S')
                    
                    note_filename = f"{timestamp}-{safe_title}.md"
                    note_path = f"{self.dropbox_vault_path}/Zero-Second Thinking/{note_filename}" # フォルダ名を変更

                    # ノート内容を作成
                    new_note_content = (
                        f"# {last_question}\n\n"
                        f"- **Source:** Discord Voice Memo\n"
                        f"- **作成日:** {daily_note_date}\n\n"
                        f"[[{daily_note_date}]]\n\n"
                        f"---\n\n"
                        f"## 回答\n"
                        f"{formatted_answer}"
                    )

                    # ノートを新規アップロード
                    dbx.files_upload(
                        new_note_content.encode('utf-8'),
                        note_path,
                        mode=WriteMode('add')
                    )
                    logging.info(f"[Zero-Second Thinking] Created new note for {message.author}: {note_path}")
                    
                    # デイリーノートを更新
                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                    try:
                        _, res = dbx.files_download(daily_note_path)
                        daily_note_content = res.content.decode('utf-8')
                    except dropbox.exceptions.ApiError as e:
                        if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                            daily_note_content = "" # ファイルがなければ新規作成
                        else:
                            raise
                    
                    # リンクを作成
                    note_filename_for_link = note_filename.replace('.md', '')
                    link_to_add = f"- [[Zero-Second Thinking/{note_filename_for_link}]]" # リンクパスも変更
                    
                    # 挿入位置を調整
                    lines = daily_note_content.split('\n')
                    youtube_summaries_heading = "## YouTube Summaries"
                    memo_heading = "## Memo"
                    zero_second_thinking_heading = "## Zero-Second Thinking"

                    insert_index = -1 # デフォルトは末尾

                    # YouTube SummariesとMemoの間に挿入
                    youtube_index = -1
                    memo_index = -1
                    for i, line in enumerate(lines):
                        if line.strip() == youtube_summaries_heading:
                            youtube_index = i
                        if line.strip() == memo_heading:
                            memo_index = i
                        
                        # YouTube Summariesの直後にZero-Second Thinkingが存在する場合、その次
                        if line.strip() == zero_second_thinking_heading and youtube_index != -1 and i > youtube_index:
                            insert_index = i + 1
                            while insert_index < len(lines) and lines[insert_index].strip().startswith('-'):
                                insert_index += 1
                            break

                    if youtube_index != -1 and memo_index != -1 and youtube_index < memo_index:
                        # YouTube SummariesとMemoの間に挿入
                        # Zero-Second Thinkingセクションが既にあるか確認し、その下に追記
                        found_zero_second_thinking_section = False
                        for i in range(youtube_index + 1, memo_index):
                            if lines[i].strip() == zero_second_thinking_heading:
                                found_zero_second_thinking_section = True
                                insert_index = i + 1
                                while insert_index < len(lines) and lines[insert_index].strip().startswith('-'):
                                    insert_index += 1
                                break
                        if not found_zero_second_thinking_section:
                            # 間にセクションがなければ、新規でセクションとリンクを作成
                            insert_index = memo_index # Memoの直前に挿入
                            lines.insert(insert_index, zero_second_thinking_heading)
                            lines.insert(insert_index + 1, link_to_add)
                    elif youtube_index != -1 and memo_index == -1:
                        # YouTube SummariesはあるがMemoがない場合、YouTube Summariesの後に挿入
                        found_zero_second_thinking_section = False
                        for i in range(youtube_index + 1, len(lines)):
                            if lines[i].strip() == zero_second_thinking_heading:
                                found_zero_second_thinking_section = True
                                insert_index = i + 1
                                while insert_index < len(lines) and lines[insert_index].strip().startswith('-'):
                                    insert_index += 1
                                break
                        if not found_zero_second_thinking_section:
                            # セクションがなければ、新規でセクションとリンクを作成
                            insert_index = youtube_index + 1
                            lines.insert(insert_index, zero_second_thinking_heading)
                            lines.insert(insert_index + 1, link_to_add)
                    elif youtube_index == -1 and memo_index != -1:
                        # YouTube SummariesはないがMemoがある場合、Memoの前に挿入
                        found_zero_second_thinking_section = False
                        for i in range(0, memo_index):
                            if lines[i].strip() == zero_second_thinking_heading:
                                found_zero_second_thinking_section = True
                                insert_index = i + 1
                                while insert_index < len(lines) and lines[insert_index].strip().startswith('-'):
                                    insert_index += 1
                                break
                        if not found_zero_second_thinking_section:
                            # セクションがなければ、新規でセクションとリンクを作成
                            insert_index = memo_index # Memoの直前に挿入
                            lines.insert(insert_index, zero_second_thinking_heading)
                            lines.insert(insert_index + 1, link_to_add)
                    else: # どちらのセクションも見つからない場合、ファイルの末尾に追加
                        if not any(line.strip() == zero_second_thinking_heading for line in lines):
                            if lines and lines[-1].strip(): # ファイルが空でなければ改行を追加
                                lines.append("")
                            lines.append(zero_second_thinking_heading)
                            lines.append(link_to_add)
                        else:
                            # Zero-Second Thinkingセクションが既にある場合、その下に追記
                            for i, line in enumerate(lines):
                                if line.strip() == zero_second_thinking_heading:
                                    insert_index = i + 1
                                    while insert_index < len(lines) and lines[insert_index].strip().startswith('-'):
                                        insert_index += 1
                                    lines.insert(insert_index, link_to_add)
                                    break
                    
                    if insert_index != -1 and not (youtube_index != -1 and memo_index != -1 and youtube_index < memo_index and not found_zero_second_thinking_section):
                        # 特定のケースを除き、insert_indexが設定されていれば挿入
                        # (すでにセクションとリンクを同時に挿入している場合は二重挿入を避ける)
                        if not any(line.strip() == link_to_add for line in lines): # 重複防止
                            lines.insert(insert_index, link_to_add)


                    # 最終的なリストを結合
                    new_daily_content = "\n".join(lines)
                    dbx.files_upload(
                        new_daily_content.encode('utf-8'),
                        daily_note_path,
                        mode=WriteMode('overwrite')
                    )

                    # 状態にnote_pathを保存
                    self.user_states[user_id]['note_path'] = note_path

            # 4. 完了報告
            await message.channel.send(f"**思考が記録されました**\n{formatted_answer}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
            
            # 状態を更新
            self.user_states[user_id]['last_answer'] = formatted_answer
            logging.info(f"[Zero-Second Thinking] Successfully processed for {message.author}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Error during processing: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass
        finally:
            # 一時ファイルを削除
            if temp_audio_path and os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

async def setup(bot: commands.Bot):
    """CogをBotに追加する"""
    # 必要な環境変数がすべて設定されているか確認
    if not all([os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID"),
                os.getenv("OPENAI_API_KEY"), 
                os.getenv("GEMINI_API_KEY"), 
                os.getenv("DROPBOX_REFRESH_TOKEN")]):
        logging.error("ZeroSecondThinkingCog: 必要な環境変数が不足しているため、Cogをロードしません。")
        return
    await bot.add_cog(ZeroSecondThinkingCog(bot))