import os
import discord
from discord.ext import commands
from google import genai
from google.genai import types
import logging
import datetime
import zoneinfo
import json
import io
import asyncio

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar']
BOT_FOLDER = ".bot"
DATA_FILE_NAME = "partner_data.json"

class PartnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.user_name = "あなた"
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        
        self.reminders = []
        self.current_task = None
        self.last_interaction = datetime.datetime.now(JST)
        
        self.gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    async def cog_load(self):
        await self.load_data_from_drive()

    async def cog_unload(self):
        await self.save_data_to_drive()

    def get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE): creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request()); open(TOKEN_FILE,'w').write(creds.to_json())
                except: return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    def get_calendar_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE): creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        return build('calendar', 'v3', credentials=creds) if creds else None

    async def _find_file(self, service, parent_id, name, mime_type=None):
        loop = asyncio.get_running_loop()
        query = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        if mime_type:
            query += f" and mimeType = '{mime_type}'"
        try:
            res = await loop.run_in_executor(None, lambda: service.files().list(q=query, fields="files(id)").execute())
            files = res.get('files', [])
            return files[0]['id'] if files else None
        except: return None

    async def load_data_from_drive(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_drive_service)
        if not service: return
        b_folder = await self._find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return
        f_id = await self._find_file(service, b_folder, DATA_FILE_NAME)
        if f_id:
            try:
                request = service.files().get_media(fileId=f_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                data = json.loads(fh.getvalue().decode('utf-8'))
                
                self.reminders = data.get('reminders', [])
                ct = data.get('current_task')
                if ct: self.current_task = {'name': ct['name'], 'start': datetime.datetime.fromisoformat(ct['start'])}
                li = data.get('last_interaction')
                if li: self.last_interaction = datetime.datetime.fromisoformat(li)
            except: pass

    async def save_data_to_drive(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_drive_service)
        if not service: return
        
        ct_save = None
        if self.current_task: ct_save = {'name': self.current_task['name'], 'start': self.current_task['start'].isoformat()}
            
        data = {'reminders': self.reminders, 'current_task': ct_save, 'last_interaction': self.last_interaction.isoformat()}
        
        b_folder = await self._find_file(service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder:
            meta = {'name': BOT_FOLDER, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}
            b_folder_obj = await loop.run_in_executor(None, lambda: service.files().create(body=meta, fields='id').execute())
            b_folder = b_folder_obj.get('id')
            
        f_id = await self._find_file(service, b_folder, DATA_FILE_NAME)
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')), mimetype='application/json')
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': DATA_FILE_NAME, 'parents': [b_folder]}, media_body=media).execute())

    # --- 変更：引数を増やし、保存先のフォルダ名・ファイル名・見出しを指定できるように汎用化 ---
    async def _append_raw_message_to_obsidian(self, text: str, folder_name: str = "DailyNotes", file_name: str = None, target_heading: str = "## 💬 タイムライン"):
        if not text: return
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_drive_service)
        if not service: return

        # ターゲットのフォルダを探す
        folder_id = await self._find_file(service, self.drive_folder_id, folder_name, "application/vnd.google-apps.folder")
        if not folder_id:
            meta = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}
            folder_obj = await loop.run_in_executor(None, lambda: service.files().create(body=meta, fields='id').execute())
            folder_id = folder_obj.get('id')

        now = datetime.datetime.now(JST)
        time_str = now.strftime('%H:%M')
        
        # ファイル名が指定されていない場合はデイリーノートとする
        if not file_name:
            file_name = f"{now.strftime('%Y-%m-%d')}.md"
        
        f_id = await self._find_file(service, folder_id, file_name)
        
        formatted_text = text.replace('\n', '\n  ')
        append_text = f"- {time_str} {formatted_text}\n"
        
        content = ""
        if f_id:
            try:
                request = service.files().get_media(fileId=f_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                content = fh.getvalue().decode('utf-8')
                if content and not content.endswith('\n'):
                    content += '\n'
            except Exception as e:
                logging.error(f"Note読み込みエラー: {e}")
        
        if target_heading not in content:
            if content and not content.endswith('\n'):
                content += '\n\n'
            content += f"{target_heading}\n{append_text}"
        else:
            parts = content.split(target_heading)
            sub_parts = parts[1].split("\n## ")
            if not sub_parts[0].endswith('\n'):
                sub_parts[0] += '\n'
            sub_parts[0] += append_text
            
            if len(sub_parts) > 1:
                parts[1] = "\n## ".join(sub_parts)
            else:
                parts[1] = sub_parts[0]
                
            content = target_heading.join(parts)
        
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        if f_id:
            await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else:
            await loop.run_in_executor(None, lambda: service.files().create(body={'name': file_name, 'parents': [folder_id]}, media_body=media).execute())

    async def _search_drive_notes(self, keywords: str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_drive_service)
        if not service: return "検索エラー"
        query = f"fullText contains '{keywords}' and mimeType = 'text/markdown' and trashed = false"
        try:
            results = await loop.run_in_executor(None, lambda: service.files().list(q=query, pageSize=3, fields="files(id, name)").execute())
            files = results.get('files', [])
            if not files: return f"「{keywords}」に関するメモは見つからなかったよ。"
            search_results = []
            for file in files:
                try:
                    request = service.files().get_media(fileId=file['id'])
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: _, done = downloader.next_chunk()
                    snippet = fh.getvalue().decode('utf-8')[:800] 
                    search_results.append(f"【{file['name']}】\n{snippet}\n")
                except: continue
            return f"検索結果:\n" + "\n---\n".join(search_results)
        except Exception as e: return f"検索エラー: {e}"

    async def _check_schedule(self, date_str: str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_calendar_service)
        if not service: return "エラー"
        try:
            # === 【修正箇所】JSTタイムゾーンを付与し、UTC化を防ぐ ===
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=JST)
            time_min = dt.replace(hour=0, minute=0, second=0).isoformat()
            time_max = dt.replace(hour=23, minute=59, second=59).isoformat()
            # ========================================================
            
            events_result = await loop.run_in_executor(None, lambda: service.events().list(calendarId=self.calendar_id, timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy='startTime').execute())
            events = events_result.get('items', [])
            if not events: return f"{date_str} の予定は特にないみたいだよ。"
            result_text = f"【{date_str} の予定】\n"
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                summary = event.get('summary', '(タイトルなし)')
                if 'T' in start: result_text += f"- {datetime.datetime.fromisoformat(start).strftime('%H:%M')} : {summary}\n"
                else: result_text += f"- 終日 : {summary}\n"
            return result_text
        except Exception as e: return f"エラー: {e}"

    async def _create_calendar_event(self, summary: str, start_time: str, end_time: str, location: str = "", description: str = ""):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_calendar_service)
        if not service: return "エラー"
        event_body = {'summary': summary, 'location': location, 'description': description, 'start': {'dateTime': start_time, 'timeZone': 'Asia/Tokyo'}, 'end': {'dateTime': end_time, 'timeZone': 'Asia/Tokyo'}}
        try:
            event = await loop.run_in_executor(None, lambda: service.events().insert(calendarId=self.calendar_id, body=event_body).execute())
            return f"予定を作成したよ！: {event.get('htmlLink')}"
        except Exception as e: return f"エラー: {e}"

    async def _set_reminder(self, target_time: str, content: str, user_id: int):
        self.reminders.append({'time': target_time, 'content': content, 'user_id': user_id})
        await self.save_data_to_drive()
        dt = datetime.datetime.fromisoformat(target_time)
        return f"了解！ {dt.strftime('%m月%d日 %H:%M')} に「{content}」でお知らせするね。"

    async def generate_and_send_routine_message(self, context_data: str, instruction: str):
        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel: return
        system_prompt = "あなたは私を日々サポートする、20代女性の親密なAIパートナーです。LINEのような短く温かみのあるタメ口で話してください。"
        prompt = f"{system_prompt}\n以下のデータを元にDiscordで話しかけて。\n【データ】\n{context_data}\n【指示】\n{instruction}\n- 事務的にならず自然な会話で、前置きは不要。長々とした返信はせず、短いメッセージにすること。"
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

    async def _build_conversation_context(self, channel, limit=30):
        messages = []
        async for msg in channel.history(limit=limit, oldest_first=False):
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments: text += " [メディア送信]"
            messages.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
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
2. 【最重要】ログの中から「User（私）」の投稿内容のみを抽出し、AIの発言内容は一切メモに含めないでください。
3. 【重要】私自身が書いたメモとして整理すること。「AIに話した」「AIが〜と言った」などの表現は完全に排除し、一人称視点（「〇〇をした」「〇〇について考えた」など）の事実や思考として記述してください。
4. 可能な限り私の投稿内容をすべて拾うこと。
5. 情報の整理はするが、要約や大幅な削除はしないこと。

【出力構成】
後で見返しやすいよう、必ず以下の順番と見出しで整理してください。該当内容がない項目は省略可能です。
・📝 出来事・行動記録
・💡 考えたこと・気づき
・➡️ ネクストアクション

最後に一言、親密なタメ口でポジティブな言葉を添えて。
{logs}"""
            try:
                response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
                await message.reply(f"今のところこんな感じ！👇\n\n{response.text.strip()}")
            except Exception as e: await message.reply(f"ごめんね、エラーが出ちゃった💦 ({e})")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        
        # --- 追加：発言場所が「通常チャンネル」か「本のスレッド」かを判定 ---
        is_book_thread = isinstance(message.channel, discord.Thread) and message.channel.name.startswith("📖 ")
        
        # メモチャンネル、または本のスレッド以外での発言は無視
        if message.channel.id != self.memo_channel_id and not is_book_thread: 
            return

        self.user_name = message.author.display_name
        text = message.content.strip()
        self.last_interaction = datetime.datetime.now(JST)

        is_short_message = len(text) < 30

        # --- 変更：保存先のルーティング ---
        if text and not text.startswith('/'):
            if is_book_thread:
                # 本のスレッドの場合は、BookNotesフォルダの該当ファイルへ保存
                book_title = message.channel.name[2:].strip() # "📖 "を除外してタイトルを取得
                file_name = f"{book_title}.md"
                asyncio.create_task(self._append_raw_message_to_obsidian(text, folder_name="BookNotes", file_name=file_name, target_heading="## 💬 読書ログ"))
            else:
                # 通常のタイムラインの場合は今まで通りDailyNotesへ保存
                asyncio.create_task(self._append_raw_message_to_obsidian(text))
        # -----------------------------------------------------

        if is_short_message and text in ["まとめ", "途中経過", "整理して", "今の状態"]:
            await self._show_interim_summary(message)
            await self.save_data_to_drive()
            return

        task_updated = False
        if is_short_message and any(w in text for w in ["開始", "やる", "読む", "作業"]):
            if not self.current_task: 
                self.current_task = {'name': text, 'start': datetime.datetime.now(JST)}
                task_updated = True
        elif is_short_message and any(w in text for w in ["終了", "終わった", "完了"]):
            if self.current_task:
                self.current_task = None
                task_updated = True
        if task_updated: await self.save_data_to_drive()

        input_parts = []
        if text: input_parts.append(types.Part.from_text(text=text))
        for att in message.attachments:
            if att.content_type and att.content_type.startswith(('image/', 'audio/')):
                input_parts.append(types.Part.from_bytes(data=await att.read(), mime_type=att.content_type))
        if not input_parts: 
            await self.save_data_to_drive()
            return

        async with message.channel.typing():
            now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')
            task_info = "現在実行中のタスクは特になし。"
            if self.current_task:
                elapsed = int((datetime.datetime.now(JST) - self.current_task['start']).total_seconds() / 60)
                task_info = f"現在「{self.current_task['name']}」というタスクを実行中（{elapsed}分経過）。"

            system_prompt = f"""
            あなたはユーザー（{self.user_name}）の親密なパートナー（20代女性）です。LINEのようなチャットでのやり取りを想定し、温かみのあるタメ口で話してください。
            **現在時刻:** {now_str} (JST)
            **ユーザーの状態:** {task_info}
            **会話の目的:** 日々の他愛ない会話を楽しみつつ、自然な形でユーザーに寄り添うこと。
            **指針:**
            1. 【長さの制限】LINEのような歯切れの良い短文（1〜2文程度）で返信すること。長文や語りすぎは絶対に避けてください。
            2. 【質問の制限】共感や相槌（リアクション）をメインとし、毎回の返信で質問を投げかけるのは避けること（質問攻め厳禁）。
            3. 【引き際】会話がひと段落したと感じた時や、ユーザーが単に報告をしてくれただけの時は、無理に質問で深掘りせず「そっか！」「お疲れ様！」「いいね！」などの共感のみで会話を自然に区切ってください。
            4. 求められない限り「アドバイス」はせず、聞き上手・壁打ち相手に徹すること。
            5. 過去の記録が知りたい時は `search_memory` を使う。
            6. スケジュールの確認や作成は `check_schedule` や `Calendar` を使う。
            7. ユーザーが「〇時に教えて」「〇分後にリマインドして」などと【未来の通知を依頼】した時のみ `set_reminder` を使う。
            """

            function_tools = [
                types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name="set_reminder", description="未来の通知をセットする。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"target_time": types.Schema(type=types.Type.STRING, description="ISO 8601形式の時刻"), "content": types.Schema(type=types.Type.STRING, description="通知内容")}, required=["target_time", "content"])
                    ),
                    types.FunctionDeclaration(
                        name="search_memory", description="Obsidianをキーワード検索する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"keywords": types.Schema(type=types.Type.STRING)}, required=["keywords"])
                    ),
                    types.FunctionDeclaration(
                        name="check_schedule", description="カレンダーを確認する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"date": types.Schema(type=types.Type.STRING, description="YYYY-MM-DD")}, required=["date"])
                    ),
                    types.FunctionDeclaration(
                        name="create_calendar_event", description="カレンダーに予定を追加する。",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={"summary": types.Schema(type=types.Type.STRING), "start_time": types.Schema(type=types.Type.STRING), "end_time": types.Schema(type=types.Type.STRING), "location": types.Schema(type=types.Type.STRING), "description": types.Schema(type=types.Type.STRING)}, required=["summary", "start_time", "end_time"])
                    )
                ])
            ]

            contents = await self._build_conversation_context(message.channel, limit=10)
            contents.append(types.Content(role="user", parts=input_parts))

            try:
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_prompt, tools=function_tools)
                )

                if response.function_calls:
                    function_call = response.function_calls[0]
                    tool_result = ""
                    if function_call.name == "set_reminder": tool_result = await self._set_reminder(function_call.args["target_time"], function_call.args["content"], message.author.id)
                    elif function_call.name == "search_memory": tool_result = await self._search_drive_notes(function_call.args["keywords"])
                    elif function_call.name == "check_schedule": tool_result = await self._check_schedule(function_call.args["date"])
                    elif function_call.name == "create_calendar_event": tool_result = await self._create_calendar_event(function_call.args["summary"], function_call.args["start_time"], function_call.args["end_time"], function_call.args.get("location",""), function_call.args.get("description",""))

                    contents.append(response.candidates[0].content)
                    contents.append(types.Content(role="user", parts=[types.Part.from_function_response(name=function_call.name, response={"result": tool_result})]))
                    
                    response_final = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro",
                        contents=contents,
                        config=types.GenerateContentConfig(system_instruction=system_prompt)
                    )
                    if response_final.text: await message.channel.send(response_final.text.strip())
                else:
                    if response.text: await message.channel.send(response.text.strip())

            except Exception as e:
                logging.error(f"PartnerCog 会話生成エラー: {e}")
                await message.channel.send("ごめんね、ちょっと今考え込んでて…もう一回お願いできる？💦")
        
        await self.save_data_to_drive()

async def setup(bot: commands.Bot):
    await bot.add_cog(PartnerCog(bot))