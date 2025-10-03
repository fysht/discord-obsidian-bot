import os
import discord
from discord.ext import commands, tasks
import logging
import json
import random
from datetime import datetime, time, timedelta
import zoneinfo
import dropbox
from dropbox.files import FileMetadata, DownloadError
from dropbox.exceptions import ApiError
import asyncio
import re
import textwrap

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
STUDY_CHANNEL_ID = int(os.getenv("STUDY_CHANNEL_ID", 0))
PREPARE_QUIZ_TIME = time(hour=7, minute=0, tzinfo=JST)
VAULT_STUDY_PATH = "/Study"
QUESTIONS_PER_DAY = 10

class SingleQuizView(discord.ui.View):
    def __init__(self, cog_instance, question_data):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.question_data = question_data
        self.is_answered = False

        for key in sorted(question_data['Options'].keys()):
            button = discord.ui.Button(label=key, style=discord.ButtonStyle.secondary, custom_id=f"answer_{key}")
            button.callback = self.button_callback
            self.add_item(button)
    
    async def button_callback(self, interaction: discord.Interaction):
        if self.is_answered:
            await interaction.response.send_message("この問題には既に回答済みです。", ephemeral=True, delete_after=10)
            return
            
        await interaction.response.defer()
        selected_option_key = interaction.data['custom_id'].split('_')[1]
        is_correct = (selected_option_key.upper() == self.question_data['Answer'].upper())

        await self.cog.process_answer(self.question_data['ID'], is_correct)
        self.is_answered = True
        
        for item in self.children:
            item.disabled = True
            if item.custom_id == interaction.data['custom_id']:
                item.style = discord.ButtonStyle.success if is_correct else discord.ButtonStyle.danger
        
        result_embed = interaction.message.embeds[0]
        result_embed.color = discord.Color.green() if is_correct else discord.Color.red()
        result_embed.title = "✅ 正解！" if is_correct else "❌ 不正解..."
        
        footer_text = f"正解: {self.question_data['Answer']}\n"
        footer_text += textwrap.fill(f"解説: {self.question_data['Explanation']}", width=60)
        result_embed.set_footer(text=footer_text)
        
        await interaction.edit_original_response(embed=result_embed, view=self)
        self.stop()

class StudyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self.daily_question_pool = []
        self._load_env_vars()
        
        if not self._validate_env_vars():
            logging.error("StudyCog: 必須の環境変数が不足。Cogを無効化します。")
            return
        try:
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            self.is_ready = True
        except Exception as e:
            logging.error(f"StudyCogの初期化中にエラー: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready and STUDY_CHANNEL_ID != 0:
            now = datetime.now(JST)
            if now.time() >= PREPARE_QUIZ_TIME and not self.daily_question_pool:
                logging.info("起動時に問題プールを準備します...")
                await self._prepare_question_pool_logic()

            if not self.prepare_daily_questions.is_running():
                logging.info("学習クイズの準備タスクをスケジュールします。")
                self.prepare_daily_questions.start()
            if not self.ask_next_question.is_running():
                logging.info("学習クイズの出題タスクを開始します。")
                self.ask_next_question.start()

    def _load_env_vars(self):
        self.dropbox_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path=os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.dropbox_app_key=os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret=os.getenv("DROPBOX_APP_SECRET")

    def _validate_env_vars(self) -> bool:
        return all([self.dropbox_refresh_token, self.dropbox_vault_path])

    def _parse_study_materials(self, raw_content: str) -> list[dict]:
        questions = []
        if not raw_content:
            return questions
        content = raw_content.replace('\xa0', ' ').lstrip("\ufeff")
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(content):
            m = re.search(r'\{', content[idx:])
            if not m: break
            start = idx + m.start()
            try:
                obj, end = decoder.raw_decode(content, start)
                if isinstance(obj, dict) and 'ID' in obj:
                    questions.append(obj)
                idx = end
            except json.JSONDecodeError:
                idx = start + 1
        return questions

    async def get_all_questions_from_vault(self) -> list[dict]:
        all_questions = []
        try:
            folder_path = f"{self.dropbox_vault_path}{VAULT_STUDY_PATH}"
            res = self.dbx.files_list_folder(folder_path, recursive=True)
            entries = list(res.entries)
            while getattr(res, "has_more", False):
                res = self.dbx.files_list_folder_continue(res.cursor)
                entries.extend(res.entries)
            for entry in entries:
                if isinstance(entry, FileMetadata) and entry.name.endswith('.md'):
                    try:
                        _, content_res = self.dbx.files_download(entry.path_display)
                        raw_content = content_res.content.decode('utf-8')
                        qs = self._parse_study_materials(raw_content)
                        all_questions.extend(qs)
                    except Exception as e:
                        logging.error(f"ファイル処理中にエラー: {entry.path_display} -> {e}")
            logging.info(f"get_all_questions_from_vault: 合計読み込み問題数 = {len(all_questions)}")
            return all_questions
        except Exception as e:
            logging.error(f"教材フォルダの読み込みに失敗: {e}")
            return []

    async def get_user_progress(self) -> dict:
        path = f"{self.dropbox_vault_path}/.bot/study_progress.json"
        try:
            _, res = self.dbx.files_download(path)
            return json.loads(res.content)
        except (ApiError, json.JSONDecodeError):
            return {}

    async def save_user_progress(self, progress: dict):
        path = f"{self.dropbox_vault_path}/.bot/study_progress.json"
        self.dbx.files_upload(json.dumps(progress, indent=2, ensure_ascii=False).encode('utf-8'), path, mode=dropbox.files.WriteMode('overwrite'))

    def get_next_review_date(self, correct_streak: int) -> str:
        today = datetime.now(JST).date()
        intervals = [1, 1, 7, 16, 35]
        days = intervals[min(correct_streak, len(intervals)-1)] if correct_streak > 0 else 1
        return (today + timedelta(days=days)).isoformat()

    async def process_answer(self, q_id: str, is_correct: bool):
        progress = await self.get_user_progress()
        streak = (progress.get(q_id, {}).get("correct_streak", 0) + 1) if is_correct else 0
        progress[q_id] = {
            "last_answered": datetime.now(JST).date().isoformat(),
            "correct_streak": streak,
            "next_review_date": self.get_next_review_date(streak)
        }
        await self.save_user_progress(progress)
        logging.info(f"回答を記録しました: ID={q_id}, 正解={is_correct}, 連続正解={streak}")

    async def _prepare_question_pool_logic(self):
        logging.info("本日の問題プールの作成を開始します...")
        all_questions = await self.get_all_questions_from_vault()
        user_progress = await self.get_user_progress()
        
        if not all_questions:
            logging.warning("教材から問題を1問も読み込めませんでした。")
            self.daily_question_pool = []
            return

        all_q_ids = {q['ID'] for q in all_questions}
        today_str = datetime.now(JST).date().isoformat()
        answered_ids = set(user_progress.keys())

        review_ids = {qid for qid in answered_ids if user_progress[qid].get("next_review_date", "1970-01-01") <= today_str}
        new_ids = all_q_ids - answered_ids
        
        # 優先度順にプール候補を追加
        pool_ids = list(review_ids)
        pool_ids.extend(list(new_ids))
        
        # 補充枠
        if len(pool_ids) < QUESTIONS_PER_DAY:
            answered_but_not_due = sorted(
                [qid for qid in answered_ids if qid not in review_ids],
                key=lambda qid: user_progress[qid].get("last_answered", "2000-01-01")
            )
            pool_ids.extend(answered_but_not_due)
        
        # 重複を除き、上限数まで選択
        final_pool_ids = list(dict.fromkeys(pool_ids))[:QUESTIONS_PER_DAY]
        
        id_to_question = {q['ID']: q for q in all_questions}
        self.daily_question_pool = [id_to_question[qid] for qid in final_pool_ids if qid in id_to_question]
        
        random.shuffle(self.daily_question_pool)
        logging.info(f"本日の問題プールを作成しました: {len(self.daily_question_pool)}問 (復習: {len(review_ids.intersection(final_pool_ids))}, 新規: {len(new_ids.intersection(final_pool_ids))})")

    @tasks.loop(time=PREPARE_QUIZ_TIME)
    async def prepare_daily_questions(self):
        await self._prepare_question_pool_logic()

    @tasks.loop(hours=1)
    async def ask_next_question(self):
        now = datetime.now(JST)
        if not (9 <= now.hour <= 22):
            return
        
        if not self.daily_question_pool:
            return
            
        channel = self.bot.get_channel(STUDY_CHANNEL_ID)
        if not channel: return

        question_data = self.daily_question_pool.pop(0)
        
        options_text = "\n".join([f"**{key})** {value}" for key, value in sorted(question_data['Options'].items())])
        description = f"{question_data['Question']}\n\n{options_text}"
        
        embed = discord.Embed(
            title=f"✍️ 学習クイズ ({QUESTIONS_PER_DAY - len(self.daily_question_pool)}/{QUESTIONS_PER_DAY})",
            description=description,
            color=discord.Color.blue()
        )
        
        view = SingleQuizView(self, question_data)
        await channel.send(embed=embed, view=view)
        logging.info(f"問題を出題しました: ID={question_data['ID']}")

    @prepare_daily_questions.before_loop
    @ask_next_question.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))