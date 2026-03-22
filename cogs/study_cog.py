import os
import discord
from discord.ext import commands
import datetime
import asyncio
import logging
from google.genai import types

from config import JST
from utils.obsidian_utils import update_section
from prompts import PROMPT_STUDY_CHAT

class StudyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.study_channel_id = int(os.getenv("STUDY_CHANNEL_ID", 0))
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client
        self.notified_today = set()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # ==========================================
        # 1. 学習チャンネル（親）での発言からスレッドを自動作成
        # ==========================================
        if message.channel.id == self.study_channel_id and not isinstance(message.channel, discord.Thread):
            text = message.content.strip()

            extract_tool = types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="create_study_thread",
                        description="ユーザーの発言から学習したい科目名を抽出し、学習用のスレッドを作成します。",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "subject_name": {
                                    "type": "STRING",
                                    "description": "学習を開始する科目名（例：不動産登記法、民法など）"
                                }
                            },
                            "required": ["subject_name"]
                        }
                    )
                ]
            )

            prompt = f"ユーザーが新しく学習を始めようとしています。発言から科目名を抽出してツールを呼び出してください。ただの雑談や意味不明な言葉の場合はツールを呼ばずに短く返信してください。\n\n【ユーザーの発言】\n{text}"

            async with message.channel.typing():
                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro",
                        contents=prompt,
                        config=types.GenerateContentConfig(tools=[extract_tool])
                    )

                    if response.function_calls:
                        for fc in response.function_calls:
                            if fc.name == "create_study_thread":
                                subject_name = fc.args.get("subject_name", "名称未設定")
                                await self._create_study_thread(message, subject_name)
                                return
                    
                    if response.text:
                        await message.reply(response.text.strip())

                except Exception as e:
                    logging.error(f"Study Thread Creation Error: {e}")
                    await message.reply("ごめんね、AIの処理中にエラーが発生しちゃった💦")
            return


        # ==========================================
        # 2. 学習スレッド内でのチャット処理
        # ==========================================
        if isinstance(message.channel, discord.Thread) and message.channel.name.startswith("✍️ "):
            text = message.content.strip()
            subject_name = message.channel.name[2:].strip()

            study_data = await self._read_study_data(subject_name)
            if not study_data:
                await message.reply(f"ごめんね、Obsidianの `StudyData` フォルダの中に「{subject_name}」のテキストや過去問のデータが見つからなかったよ💦")
                return

            system_prompt = f"【参照用学習データ】\n{study_data}\n\n================\n{PROMPT_STUDY_CHAT}"
            contents = await self._build_conversation_context(message.channel, message.id, limit=10)
            
            input_parts = []
            if text: input_parts.append(types.Part.from_text(text=text))
            for att in message.attachments:
                if att.content_type and att.content_type.startswith(('image/', 'audio/')):
                    input_parts.append(types.Part.from_bytes(data=await att.read(), mime_type=att.content_type))
            
            if not input_parts and not contents:
                return

            contents.append(types.Content(role="user", parts=input_parts))

            # ★修正: ツールを2つ（弱点保存、まとめ保存）定義する
            study_tools = types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="save_weakness_note",
                        description="ユーザーが自分の言葉で間違えた理由を説明した後、その内容を弱点ノートに保存します。",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "question_and_answer": {
                                    "type": "STRING",
                                    "description": "間違えた問題と正解の内容"
                                },
                                "user_reason": {
                                    "type": "STRING",
                                    "description": "ユーザーが自分の言葉で説明した間違えた理由や覚え方"
                                }
                            },
                            "required": ["question_and_answer", "user_reason"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="save_set_summary",
                        description="5問セット終了時に、そのセットで学習した重要ポイントのまとめを学習ログノートに保存します。",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "summary_text": {
                                    "type": "STRING",
                                    "description": "保存するまとめの内容（箇条書き）"
                                }
                            },
                            "required": ["summary_text"]
                        }
                    )
                ]
            )

            async with message.channel.typing():
                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro",
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            tools=[study_tools]
                        )
                    )
                    
                    ai_reply = response.text.strip() if response.text else ""
                    
                    # ツールが呼ばれた場合の処理
                    if response.function_calls:
                        for fc in response.function_calls:
                            if fc.name == "save_weakness_note":
                                q_and_a = fc.args.get("question_and_answer", "")
                                reason = fc.args.get("user_reason", "")
                                await self._append_weakness_note(subject_name, q_and_a, reason)
                            
                            elif fc.name == "save_set_summary":
                                summary = fc.args.get("summary_text", "")
                                await self._append_summary_to_log(subject_name, summary)
                                
                        if not ai_reply:
                            ai_reply = "📝 学習ログの更新と保存が完了したよ！"

                    if ai_reply:
                        await message.reply(ai_reply)
                        await self._append_qa_to_study_log(subject_name, text, ai_reply)
                    
                    now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
                    cache_key = f"{now_str}_{subject_name}"
                    if cache_key not in self.notified_today:
                        self.notified_today.add(cache_key)
                        if self.memo_channel_id != 0:
                            memo_channel = self.bot.get_channel(self.memo_channel_id)
                            if memo_channel:
                                await memo_channel.send(f"【自動記録】✍️ 本日、『{subject_name}』の学習・過去問演習を行いました。")
                    
                except Exception as e:
                    logging.error(f"Study Chat Error: {e}")
                    await message.reply("ごめんね、AIの処理中にエラーが発生しちゃった💦")

    # ==========================================
    # 内部処理メソッド
    # ==========================================
    async def _build_conversation_context(self, channel, current_msg_id: int, limit=10):
        messages = []
        async for msg in channel.history(limit=limit + 1, oldest_first=False):
            if msg.id == current_msg_id: continue
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text_content = msg.content
            if msg.attachments: text_content += " [メディア送信]"
            messages.append(types.Content(role=role, parts=[types.Part.from_text(text=text_content)]))
        return list(reversed(messages))

    async def _create_study_thread(self, message: discord.Message, subject_name: str):
        service = self.drive_service.get_service()
        if service:
            study_data_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "StudyData")
            if not study_data_folder_id:
                await self.drive_service.create_folder(service, self.drive_folder_id, "StudyData")
        
        msg = await message.reply(f"✍️ 『{subject_name}』の学習を開始するよ！\nObsidianの `StudyData` フォルダに `{subject_name}_テキスト.md` と `{subject_name}_過去問.md` を入れておいてね。")
        thread = await msg.create_thread(name=f"✍️ {subject_name}", auto_archive_duration=10080)
        await thread.send("ここが学習ルームだよ！\n「過去問から問題出して！」「応用問題出して！」みたいに自由に話しかけてね。")

    async def _read_study_data(self, subject_name: str) -> str:
        service = self.drive_service.get_service()
        if not service: return None
        
        study_data_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "StudyData")
        if not study_data_folder_id: return None
        
        text_file_name = f"{subject_name}_テキスト.md"
        exam_file_name = f"{subject_name}_過去問.md"
        
        text_f_id = await self.drive_service.find_file(service, study_data_folder_id, text_file_name)
        exam_f_id = await self.drive_service.find_file(service, study_data_folder_id, exam_file_name)
        
        combined_data = ""
        if text_f_id:
            text_content = await self.drive_service.read_text_file(service, text_f_id)
            if text_content: combined_data += f"【テキストデータ：{subject_name}】\n{text_content}\n\n"
                
        if exam_f_id:
            exam_content = await self.drive_service.read_text_file(service, exam_f_id)
            if exam_content: combined_data += f"【過去問データ：{subject_name}】\n{exam_content}\n\n"

        # 弱点ノートも読み込んで復習に活かす
        logs_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "StudyLogs")
        if logs_folder_id:
            weakness_file_name = f"{subject_name}_弱点ノート.md"
            weakness_f_id = await self.drive_service.find_file(service, logs_folder_id, weakness_file_name)
            if weakness_f_id:
                weakness_content = await self.drive_service.read_text_file(service, weakness_f_id)
                if weakness_content: combined_data += f"【弱点ノート（過去に間違えてメモした内容）：{subject_name}】\n{weakness_content}\n\n"
                
        return combined_data if combined_data else None

    # ★修正: 初期ファイル生成時に Set Summaries の見出しを追加
    async def _append_qa_to_study_log(self, subject_name: str, user_text: str, ai_text: str):
        service = self.drive_service.get_service()
        if not service: return
        
        logs_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "StudyLogs")
        if not logs_folder_id: 
            logs_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "StudyLogs")
        
        file_name = f"{subject_name}_学習ログ.md"
        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)
        
        now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        time_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')
        
        if not f_id:
            content = f"---\ntitle: {subject_name} 学習ログ\ndate: {now_str}\ntags: [study_log]\n---\n\n# {subject_name} 学習ログ\n\n## 💡 Set Summaries\n\n## 📝 Study Log\n\n"
            f_id = await self.drive_service.upload_text(service, logs_folder_id, file_name, content)
            content_to_update = content
        else:
            content_to_update = await self.drive_service.read_text_file(service, f_id)
            # 古いファイルの場合、Set Summaries 見出しがなければ追加する処理（簡易版）
            if "## 💡 Set Summaries" not in content_to_update:
                content_to_update = content_to_update.replace("## 📝 Study Log", "## 💡 Set Summaries\n\n## 📝 Study Log")
            
        if not content_to_update: return
        
        user_formatted = user_text.replace('\n', '\n    ')
        ai_formatted = ai_text.replace('\n', '\n        ')
        
        log_entry = f"- **{time_str}** 👤 {user_formatted}\n    - 🤖 {ai_formatted}"
        
        new_content = update_section(content_to_update, log_entry, "## 📝 Study Log")
        await self.drive_service.update_text(service, f_id, new_content)

    # ★新規追加: 5問セットのまとめを保存するメソッド
    async def _append_summary_to_log(self, subject_name: str, summary_text: str):
        service = self.drive_service.get_service()
        if not service: return
        
        logs_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "StudyLogs")
        if not logs_folder_id: return
        
        file_name = f"{subject_name}_学習ログ.md"
        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)
        if not f_id: return
        
        content_to_update = await self.drive_service.read_text_file(service, f_id)
        if not content_to_update: return
        
        now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')
        
        # summary_text が改行を含んでいる場合はインデントを揃える
        formatted_summary = summary_text.replace('\n', '\n    ')
        
        log_entry = f"- **{now_str} のまとめ**\n    {formatted_summary}"
        
        new_content = update_section(content_to_update, log_entry, "## 💡 Set Summaries")
        await self.drive_service.update_text(service, f_id, new_content)

    async def _append_weakness_note(self, subject_name: str, q_and_a: str, user_reason: str):
        service = self.drive_service.get_service()
        if not service: return
        
        logs_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "StudyLogs")
        if not logs_folder_id: 
            logs_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "StudyLogs")
        
        file_name = f"{subject_name}_弱点ノート.md"
        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)
        
        now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
        
        if not f_id:
            content = f"---\ntitle: {subject_name} 弱点ノート\ndate: {now_str}\ntags: [weakness_note]\n---\n\n# {subject_name} 弱点ノート\n\n## 🚨 Weakness Log\n\n"
            f_id = await self.drive_service.upload_text(service, logs_folder_id, file_name, content)
            content_to_update = content
        else:
            content_to_update = await self.drive_service.read_text_file(service, f_id)
            
        if not content_to_update: return
        
        log_entry = f"- **{now_str} 追加**\n    - ❓ **問題**: {q_and_a}\n    - 💡 **自分のメモ**: {user_reason}"
        
        new_content = update_section(content_to_update, log_entry, "## 🚨 Weakness Log")
        await self.drive_service.update_text(service, f_id, new_content)

async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))