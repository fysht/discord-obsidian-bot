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
PREPARE_QUIZ_TIME = time(hour=7, minute=0, tzinfo=JST) # 毎朝7時にその日の問題リストを作成
VAULT_STUDY_PATH = "/Study"
QUESTIONS_PER_DAY = 10

class SingleQuizView(discord.ui.View):
    """1問ごとのクイズの選択肢ボタンを持つView"""
    def __init__(self, cog_instance, question_data):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.question_data = question_data
        self.is_answered = False

        # A, B, C, D ボタンを作成
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

        # 回答を記録
        await self.cog.process_answer(self.question_data['ID'], is_correct)
        
        self.is_answered = True
        
        # 全てのボタンを無効化
        for item in self.children:
            item.disabled = True
            if item.custom_id == interaction.data['custom_id']:
                item.style = discord.ButtonStyle.success if is_correct else discord.ButtonStyle.danger
        
        # 結果を表示
        result_embed = interaction.message.embeds[0]
        result_embed.color = discord.Color.green() if is_correct else discord.Color.red()
        result_embed.title = "✅ 正解！" if is_correct else "❌ 不正解..."
        
        footer_text = f"正解: {self.question_data['Answer']}\n"
        footer_text += textwrap.fill(f"解説: {self.question_data['Explanation']}", width=60)
        result_embed.set_footer(text=footer_text)
        
        await interaction.edit_original_response(embed=result_embed, view=self)
        self.stop()

class StudyCog(commands.Cog):
    """Obsidianの教材データを元に学習クイズを生成するCog"""

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
        """Cogが準備完了したときにタスクを開始する"""
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
        """【修正】一行ごとのJSON Lines形式を解析して、問題のリストを返す"""
        questions = []
        for line in raw_content.strip().split('\n'):
            line = line.strip()
            if not line.startswith('{') or not line.endswith('}'):
                continue
            
            try:
                # 行からJSONを読み込む
                questions.append(json.loads(line))
            except json.JSONDecodeError as e:
                logging.warning(f"JSON行の解析に失敗しました。スキップします: {e}\n行内容: {line[:150]}...")
                continue
        return questions

    async def get_all_questions_from_vault(self) -> list[dict]:
        all_questions = []
        try:
            folder_path = f"{self.dropbox_vault_path}{VAULT_STUDY_PATH}"
            res = self.dbx.files_list_folder(folder_path)
            for entry in res.entries:
                if isinstance(entry, FileMetadata) and entry.name.endswith('.md'):
                    _, content_res = self.dbx.files_download(entry.path_display)
                    raw_content = content_res.content.decode('utf-8')
                    all_questions.extend(self._parse_study_materials(raw_content))
            return all_questions
        except ApiError as e:
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
        if correct_streak <= 0: return (today + timedelta(days=1)).isoformat()
        if correct_streak == 1: return (today + timedelta(days=1)).isoformat()
        if correct_streak == 2: return (today + timedelta(days=7)).isoformat()
        if correct_streak == 3: return (today + timedelta(days=16)).isoformat()
        if correct_streak >= 4: return (today + timedelta(days=35)).isoformat()
        return (today + timedelta(days=1)).isoformat()

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
            logging.warning("教材が見つからないため、問題プールを作成できません。")
            self.daily_question_pool = []
            return

        today_str = datetime.now(JST).date().isoformat()
        review_ids = {q_id for q_id, data in user_progress.items() if data.get("next_review_date") <= today_str}
        answered_ids = set(user_progress.keys())
        new_ids = {q['ID'] for q in all_questions if q['ID'] not in answered_ids}
        
        pool_ids = list(review_ids)
        remaining_slots = QUESTIONS_PER_DAY - len(pool_ids)
        if remaining_slots > 0:
            new_ids_list = sorted(list(new_ids))
            pool_ids.extend(random.sample(new_ids_list, min(remaining_slots, len(new_ids_list))))
        
        self.daily_question_pool = [q for q in all_questions if q['ID'] in pool_ids]
        random.shuffle(self.daily_question_pool)
        logging.info(f"本日の問題プールを作成しました: {len(self.daily_question_pool)}問")

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