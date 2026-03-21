import os
import discord
from discord.ext import commands
import datetime
import asyncio
import logging
from google.genai import types # ★ツール呼び出し用に追加

from config import JST
from utils.obsidian_utils import update_section
from prompts import PROMPT_STUDY_CHAT

class StudyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # ★ メモチャンネルではなく、学習専用チャンネルのIDを読み込む
        self.study_channel_id = int(os.getenv("STUDY_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # ==========================================
        # 1. 学習チャンネル（親）での発言からスレッドを自動作成
        # ==========================================
        if message.channel.id == self.study_channel_id and not isinstance(message.channel, discord.Thread):
            text = message.content.strip()

            # AIに持たせる「科目名を抽出してスレッドを作る」ツール
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

                    # ツールが呼び出された（科目名が分かった）場合
                    if response.function_calls:
                        for fc in response.function_calls:
                            if fc.name == "create_study_thread":
                                subject_name = fc.args.get("subject_name", "名称未設定")
                                await self._create_study_thread(message, subject_name)
                                return
                    
                    # ツールが呼ばれなかった場合（「がんばるぞー」等のただのつぶやき）
                    if response.text:
                        await message.reply(response.text.strip())

                except Exception as e:
                    logging.error(f"Study Thread Creation Error: {e}")
                    await message.reply("ごめんね、AIの処理中にエラーが発生しちゃった💦")
            return


        # ==========================================
        # 2. 学習スレッド内でのチャット処理（変更なし）
        # ==========================================
        if isinstance(message.channel, discord.Thread) and message.channel.name.startswith("✍️ "):
            text = message.content.strip()
            subject_name = message.channel.name[2:].strip()

            study_data = await self._read_study_data(subject_name)
            if not study_data:
                await message.reply(f"ごめんね、Obsidianの `StudyData` フォルダの中に「{subject_name}」のテキストや過去問のデータが見つからなかったよ💦")
                return

            prompt = f"{PROMPT_STUDY_CHAT}\n\n{study_data}\n\n【私の発言・回答】\n{text}"

            async with message.channel.typing():
                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro", contents=prompt
                    )
                    ai_reply = response.text.strip()
                    await message.reply(ai_reply)
                    
                    await self._append_qa_to_study_log(subject_name, text, ai_reply)
                    
                except Exception as e:
                    logging.error(f"Study Chat Error: {e}")
                    await message.reply("ごめんね、AIの処理中にエラーが発生しちゃった💦")

    # ==========================================
    # 内部処理メソッド
    # ==========================================
    async def _create_study_thread(self, message: discord.Message, subject_name: str):
        """スレッドを実際に作成する処理"""
        service = self.drive_service.get_service()
        if service:
            study_data_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "StudyData")
            if not study_data_folder_id:
                await self.drive_service.create_folder(service, self.drive_folder_id, "StudyData")
        
        msg = await message.reply(f"✍️ 『{subject_name}』の学習を開始するよ！\nObsidianの `StudyData` フォルダに `{subject_name}_テキスト.md` と `{subject_name}_過去問.md` を入れておいてね。")
        thread = await msg.create_thread(name=f"✍️ {subject_name}", auto_archive_duration=10080)
        await thread.send("ここが学習ルームだよ！\n「この過去問解説して！」「応用問題出して！」「先生役やるから採点して！」みたいに自由に話しかけてね。")

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
                
        return combined_data if combined_data else None

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
            content = f"---\ntitle: {subject_name} 学習ログ\ndate: {now_str}\ntags: [study_log]\n---\n\n# {subject_name} 学習ログ\n\n## 📝 学習タイムライン\n\n"
            f_id = await self.drive_service.upload_text(service, logs_folder_id, file_name, content)
            content_to_update = content
        else:
            content_to_update = await self.drive_service.read_text_file(service, f_id)
            
        if not content_to_update: return
        
        user_formatted = user_text.replace('\n', '\n    ')
        ai_formatted = ai_text.replace('\n', '\n        ')
        
        log_entry = f"- **{time_str}** 👤 {user_formatted}\n    - 🤖 {ai_formatted}"
        
        new_content = update_section(content_to_update, log_entry, "## 📝 学習タイムライン")
        await self.drive_service.update_text(service, f_id, new_content)

async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))