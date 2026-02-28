import os
import discord
from discord.ext import commands
from google.genai import types
import logging
import datetime
import asyncio
import aiohttp
import json

from config import JST

class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.user_name = "あなた"
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        self.drive_service = bot.drive_service
        self.calendar_service = bot.calendar_service
        self.tasks_service = getattr(bot, 'tasks_service', None)
        self.gemini_client = bot.gemini_client
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.pdf_cache = {}

    async def _append_raw_message_to_obsidian(self, text: str, folder_name: str = "DailyNotes", file_name: str = None, target_heading: str = "## 💬 Timeline"):
        if not text: return
        service = self.drive_service.get_service()
        if not service: return

        folder_id = await self.drive_service.find_file(service, self.drive_folder_id, folder_name)
        if not folder_id: folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, folder_name)

        now = datetime.datetime.now(JST)
        time_str = now.strftime('%H:%M')
        if not file_name: file_name = f"{now.strftime('%Y-%m-%d')}.md"

        f_id = await self.drive_service.find_file(service, folder_id, file_name)
        formatted_text = text.replace('\n', '\n  ')
        append_text = f"- {time_str} {formatted_text}\n"

        content = ""
        if f_id:
            try: content = await self.drive_service.read_text_file(service, f_id)
            except: pass

        if target_heading not in content:
            if content and not content.endswith('\n'): content += '\n\n'
            content += f"{target_heading}\n{append_text}"
        else:
            parts = content.split(target_heading)
            sub_parts = parts[1].split("\n## ")
            if not sub_parts[0].endswith('\n'): sub_parts[0] += '\n'
            sub_parts[0] += append_text
            if len(sub_parts) > 1: parts[1] = "\n## ".join(sub_parts)
            else: parts[1] = sub_parts[0]
            content = target_heading.join(parts)

        if f_id: await self.drive_service.update_text(service, f_id, content)
        else: await self.drive_service.upload_text(service, folder_id, file_name, content)

    async def _append_english_log_to_obsidian(self, text: str):
        if not text: return
        
        prompt = f"""以下のテキストが日本語であれば自然な英語に翻訳し、英語であればより自然なネイティブ表現に修正してください。
出力は英語のテキストのみとし、解説や挨拶は一切含めないでください。
【テキスト】
{text}"""
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            english_text = response.text.strip()
        except Exception as e:
            logging.error(f"PartnerCog 英訳エラー: {e}")
            return
            
        service = self.drive_service.get_service()
        if not service: return

        base_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "EnglishLearning")
        if not base_folder_id: base_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "EnglishLearning")
        
        logs_folder_id = await self.drive_service.find_file(service, base_folder_id, "Logs")
        if not logs_folder_id: logs_folder_id = await self.drive_service.create_folder(service, base_folder_id, "Logs")

        now = datetime.datetime.now(JST)
        time_str = now.strftime('%H:%M')
        file_name = f"{now.strftime('%Y-%m-%d')}_EN.md"

        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)
        formatted_en = english_text.replace('\n', '\n  ')
        formatted_ja = text.replace('\n', '\n  ')
        
        append_text = f"- {time_str} [EN] {formatted_en}\n  - [JA] {formatted_ja}\n"

        content = ""
        if f_id:
            try: content = await self.drive_service.read_text_file(service, f_id)
            except: pass

        target_heading = "## 💬 English Log"
        if target_heading not in content:
            if content and not content.endswith('\n'): content += '\n\n'
            content += f"{target_heading}\n{append_text}"
        else:
            parts = content.split(target_heading)
            sub_parts = parts[1].split("\n## ")
            if not sub_parts[0].endswith('\n'): sub_parts[0] += '\n'
            sub_parts[0] += append_text
            if len(sub_parts) > 1: parts[1] = "\n## ".join(sub_parts)
            else: parts[1] = sub_parts[0]
            content = target_heading.join(parts)

        if f_id: await self.drive_service.update_text(service, f_id, content)
        else: await self.drive_service.upload_text(service, logs_folder_id, file_name, content)

    async def _search_drive_notes(self, keywords: str):
        return await self.drive_service.search_markdown_files(keywords)

    async def generate_and_send_routine_message(self, context_data: str, instruction: str):
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel: return
        system_prompt = "あなたは私を日々サポートする親密なパートナーの女性です。LINEでのやり取りを想定し、短いやり取りを複数回続けるイメージで温かみのあるタメ口で話してください。長々とした返信は不要です。"
        prompt = f"{system_prompt}\n以下のデータを元にDiscordで話しかけて。\n【データ】\n{context_data}\n【指示】\n{instruction}\n- 事務的にならず自然な会話で、前置きは不要。長文は絶対に避け、1〜2文程度の短いメッセージにすること。"
        try:
            response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
            await channel.send(response.text.strip())
        except Exception as e: logging.error(f"PartnerCog 定期メッセージ生成エラー: {e}")

    async def fetch_todays_chat_log(self, channel):
        today_start = datetime.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        logs = []
        async for msg in channel.history(after=today_start, limit=None, oldest_first=True):
            if msg.content.startswith("/"): continue
            role = "AI" if msg.author.id == self.bot.user.id else "User"
            logs.append(f"{role}: {msg.content}")
        return "\n".join(logs)

    # ★ 修正: SDK用の履歴取得関数（ロールは "model"）
    async def _build_conversation_context_sdk(self, channel, current_msg_id: int, limit=30):
        messages = []
        async for msg in channel.history(limit=limit + 1, oldest_first=False):
            if msg.id == current_msg_id: continue
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            if msg.content.startswith("📚 "): continue
            
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments: text += " [メディア送信]"
            messages.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
        return list(reversed(messages))

    # ★ 修正: REST API用の履歴取得関数（ロールは "assistant"）
    async def _build_conversation_context_rest(self, channel, current_msg_id: int, limit=30):
        messages = []
        async for msg in channel.history(limit=limit + 1, oldest_first=False):
            if msg.id == current_msg_id: continue
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            if msg.content.startswith("📚 "): continue
            
            role = "assistant" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments: text += " [メディア送信]"
            messages.append({"role": role, "parts": [{"text": text}]})
        return list(reversed(messages))

    async def _show_interim_summary(self, message: discord.Message):
        async with message.channel.typing():
            logs = await self.fetch_todays_chat_log(message.channel)
            if not logs:
                await message.reply("今日はまだ何も話してないね！")
                return
            prompt = f"""あなたは私の優秀なパートナーです。今日のここまでの会話ログを整理して、箇条書きのメモを作成して。
【指示】
1. メモの文末はすべて「である調（〜である、〜だ）」で統一すること。
2. ログの中から「User（私）」の投稿内容のみを抽出し、AIの発言内容は一切メモに含めないでください。
3. 私自身が書いたメモとして整理すること。
4. 情報の整理はするが、要約や大幅な削除はしないこと。

【出力構成】
・📝 Events & Actions
・💡 Insights & Thoughts
・➡️ Next Actions
最後に一言、親密なタメ口でポジティブな言葉を添えて。
{logs}"""
            try:
                response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
                await message.reply(f"今のところこんな感じ！👇\n\n{response.text.strip()}")
            except Exception as e: await message.reply(f"ごめんね、エラーが出ちゃった💦 ({e})")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        
        is_book_thread = isinstance(message.channel, discord.Thread) and message.channel.name.startswith("📖 ")
        if message.channel.id != self.memo_channel_id and not is_book_thread: return

        self.user_name = message.author.display_name
        text = message.content.strip()
        is_short_message = len(text) < 30

        if text and not text.startswith('/'):
            if is_book_thread:
                book_title = message.channel.name[2:].strip()
                file_name = f"{book_title}.md"
                asyncio.create_task(self._append_raw_message_to_obsidian(text, folder_name="BookNotes", file_name=file_name, target_heading="## 📖 Reading Log"))
            else:
                asyncio.create_task(self._append_raw_message_to_obsidian(text))
                asyncio.create_task(self._append_english_log_to_obsidian(text))

        if is_short_message and text in ["まとめ", "途中経過", "整理して", "今の状態"]:
            await self._show_interim_summary(message)
            return

        input_parts = []
        if text: input_parts.append({"text": text})
        for att in message.attachments:
            if att.content_type and att.content_type.startswith(('image/', 'audio/')):
                pass
                
        if not input_parts: return

        async with message.channel.typing():
            now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')

            gemini_file = None
            if is_book_thread:
                book_title = message.channel.name[2:].strip()
                gemini_file = self.pdf_cache.get(book_title)
                
                if not gemini_file:
                    service = self.drive_service.get_service()
                    if service:
                        pdf_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "BookPDFs")
                        if not pdf_folder_id:
                            pdf_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "BookPDFs")
                            
                        pdf_file_name = f"{book_title}.pdf"
                        pdf_file_id = await self.drive_service.find_file(service, pdf_folder_id, pdf_file_name)
                        
                        if pdf_file_id:
                            status_msg = await message.channel.send("📚 Google Driveに本のPDFデータを発見したよ！今から内容をAIの頭脳に読み込むから、少し待ってね...")
                            try:
                                local_pdf_path = f"temp_{pdf_file_id}.pdf"
                                success = await self.drive_service.download_file(service, pdf_file_id, local_pdf_path)
                                if success:
                                    uploaded_file = await asyncio.to_thread(
                                        self.gemini_client.files.upload, file=local_pdf_path
                                    )
                                    
                                    await status_msg.edit(content="📚 PDFをAIに送信中... 脳内で解析しているからちょっと待ってね！(数秒〜数十秒かかります)")
                                    
                                    while True:
                                        file_info = await asyncio.to_thread(self.gemini_client.files.get, name=uploaded_file.name)
                                        if file_info.state.name == "ACTIVE":
                                            break
                                        elif file_info.state.name == "FAILED":
                                            raise Exception("Gemini APIでのPDF解析に失敗しました。")
                                        await asyncio.sleep(2)

                                    self.pdf_cache[book_title] = uploaded_file
                                    gemini_file = uploaded_file
                                    if os.path.exists(local_pdf_path):
                                        os.remove(local_pdf_path)
                                    await status_msg.edit(content="📚 読み込み完了！この本の内容を踏まえてなんでも聞いてね！")
                                else:
                                    await status_msg.edit(content="💦 PDFのダウンロードに失敗しちゃったみたい。")
                            except Exception as e:
                                logging.error(f"PDF Upload Error: {e}")
                                await status_msg.edit(content="💦 PDFの読み込み中にエラーが起きちゃった。")

            if gemini_file:
                # 読書スレッド (REST API)
                system_prompt = f"""あなたはユーザー（{self.user_name}）の専属読書メンターです。提供されたPDFデータに基づき、ユーザーの質問や壁打ちに対して示唆に富む回答を提供してください。
現在時刻: {now_str} (JST)
1. 専門的でありながら親しみやすいトーンで話してください。
2. ユーザーの仕事や日常生活にどう活かせるか、具体例を交えてアドバイスしてください。"""

                # ★ REST専用の履歴取得関数を使用（ロールは assistant）
                history = await self._build_conversation_context_rest(message.channel, message.id, limit=10)
                
                input_parts.insert(0, {"fileData": {"mimeType": gemini_file.mime_type, "fileUri": gemini_file.uri}})
                history.append({"role": "user", "parts": input_parts})
                
                payload = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": history
                }
                
                logging.info(f"REST API Payload: {json.dumps(payload, ensure_ascii=False)}")

                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key={self.gemini_api_key}"
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                reply_text = data['candidates'][0]['content']['parts'][0]['text']
                                await message.channel.send(reply_text.strip())
                            else:
                                error_data = await resp.text()
                                logging.error(f"REST API Error Details: {error_data}")
                                await message.channel.send("ごめんね、本の読み込みでちょっとエラーが起きちゃったみたい💦")
                except Exception as e:
                    logging.error(f"PartnerCog REST API 通信エラー: {e}")
                    await message.channel.send("ごめんね、ちょっと今考え込んでて…もう一回お願いできる？💦")

            else:
                # 日常スレッド (SDK)
                system_prompt = f"""あなたはユーザー（{self.user_name}）の親密なパートナー（女性）であり、頼れる英会話の先生です。LINEなどのチャットでのやり取りを想定し、親しみやすいトーンで話してください。
現在時刻: {now_str} (JST)
1. ユーザーが日本語で話しかけた場合は日本語のみで返信し、英語で話しかけた場合は完全に英語のみで返信してください。
2. 英語で話しかけた際、不自然な点があれば最後に英語で優しくワンポイントアドバイスを添えてください。
3. LINEのような歯切れの良い短文（1〜2文程度）で返信すること。長文や語りすぎは絶対に避けてください。
4. 過去の記録を知りたい時は `search_memory` を使う。
5. 予定とタスクの使い分け: カレンダーは日時が決まっているもの、Google Tasksは日時が決まっていないToDo。"""

                function_tools = [
                    types.Tool(function_declarations=[
                        types.FunctionDeclaration(name="search_memory", description="Obsidianを検索する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"keywords": types.Schema(type=types.Type.STRING)}, required=["keywords"])),
                        types.FunctionDeclaration(name="check_schedule", description="カレンダーを確認する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING)}, required=["date"])),
                        types.FunctionDeclaration(name="create_calendar_event", description="カレンダーに追加する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"summary": types.Schema(type=types.Type.STRING), "start_time": types.Schema(type=types.Type.STRING), "end_time": types.Schema(type=types.Type.STRING)}, required=["summary", "start_time", "end_time"])),
                        types.FunctionDeclaration(name="delete_calendar_event", description="カレンダーから削除する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING), "keyword": types.Schema(type=types.Type.STRING)}, required=["date", "keyword"])),
                        types.FunctionDeclaration(name="check_tasks", description="Google Tasksを確認する。", parameters=types.Schema(type=types.Type.OBJECT, properties={})),
                        types.FunctionDeclaration(name="add_task", description="Google Tasksに追加する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"title": types.Schema(type=types.Type.STRING)}, required=["title"])),
                        types.FunctionDeclaration(name="complete_task", description="Google Tasksを完了する。", parameters=types.Schema(type=types.Type.OBJECT, properties={"keyword": types.Schema(type=types.Type.STRING)}, required=["keyword"]))
                    ])
                ]

                # ★ SDK専用の履歴取得関数を使用（ロールは model）
                sdk_contents = await self._build_conversation_context_sdk(message.channel, message.id, limit=10)
                
                # 今回の入力（テキスト）をPart形式で追加
                input_parts_sdk = [types.Part.from_text(text=text)]
                sdk_contents.append(types.Content(role="user", parts=input_parts_sdk))

                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=sdk_contents,
                        config=types.GenerateContentConfig(system_instruction=system_prompt, tools=function_tools)
                    )

                    if response.function_calls:
                        sdk_contents.append(response.candidates[0].content)
                        function_responses = []
                        
                        for function_call in response.function_calls:
                            tool_result = ""
                            if function_call.name == "search_memory": tool_result = await self._search_drive_notes(function_call.args["keywords"])
                            elif function_call.name == "check_schedule": tool_result = await self.calendar_service.list_events_for_date(function_call.args["date"]) if self.calendar_service else "エラー"
                            elif function_call.name == "create_calendar_event": tool_result = await self.calendar_service.create_event(function_call.args["summary"], function_call.args["start_time"], function_call.args["end_time"], "") if self.calendar_service else "エラー"
                            elif function_call.name == "delete_calendar_event": tool_result = await self.calendar_service.delete_event_by_keyword(function_call.args["date"], function_call.args["keyword"]) if self.calendar_service else "エラー"
                            elif function_call.name == "check_tasks": tool_result = await self.tasks_service.get_uncompleted_tasks() if self.tasks_service else "エラー"
                            elif function_call.name == "add_task": tool_result = await self.tasks_service.add_task(function_call.args["title"]) if self.tasks_service else "エラー"
                            elif function_call.name == "complete_task": tool_result = await self.tasks_service.complete_task_by_keyword(function_call.args["keyword"]) if self.tasks_service else "エラー"

                            function_responses.append(types.Part.from_function_response(name=function_call.name, response={"result": str(tool_result)}))

                        sdk_contents.append(types.Content(role="user", parts=function_responses))
                        response_final = await self.gemini_client.aio.models.generate_content(
                            model="gemini-2.5-flash", contents=sdk_contents, config=types.GenerateContentConfig(system_instruction=system_prompt)
                        )
                        if response_final.text: await message.channel.send(response_final.text.strip())
                    else:
                        if response.text: await message.channel.send(response.text.strip())

                except Exception as e:
                    logging.error(f"PartnerCog SDK 会話生成エラー: {e}")
                    await message.channel.send("ごめんね、ちょっと今考え込んでて…もう一回お願いできる？💦")

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))