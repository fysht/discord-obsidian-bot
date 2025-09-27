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
import google.generativeai as genai
import asyncio

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
STUDY_CHANNEL_ID = int(os.getenv("STUDY_CHANNEL_ID", 0))
STUDY_TIME = time(hour=23, minute=45, tzinfo=JST) # 毎晩20時に出題
VAULT_STUDY_PATH = "/Study" # Obsidianの教材フォルダ
QUESTIONS_PER_SESSION = 10 # 1回に出題する問題数

class QuizView(discord.ui.View):
    """クイズの選択肢ボタンを持つView"""
    def __init__(self, cog_instance, questions):
        super().__init__(timeout=None) # タイムアウトを無効化
        self.cog = cog_instance
        self.questions = questions
        self.current_question_index = 0
        self.user_answers = []
        self.message = None
        self.create_question_embed()

    def create_question_embed(self):
        question_data = self.questions[self.current_question_index]
        self.clear_items() # ボタンをクリア
        
        embed = discord.Embed(
            title=f"第 {self.current_question_index + 1} 問",
            description=question_data['question'],
            color=discord.Color.blue()
        )
        
        options = question_data['options']
        for i, option in enumerate(options):
            button = discord.ui.Button(label=option, style=discord.ButtonStyle.secondary, custom_id=f"answer_{i}")
            button.callback = self.button_callback
            self.add_item(button)
        
        self.embed = embed

    async def button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_option_index = int(interaction.data['custom_id'].split('_')[1])
        
        question_data = self.questions[self.current_question_index]
        is_correct = (selected_option_index == question_data['answer'])
        self.user_answers.append({
            "question_id": question_data['id'],
            "is_correct": is_correct
        })

        # 次の問題へ
        self.current_question_index += 1
        if self.current_question_index < len(self.questions):
            self.create_question_embed()
            await self.message.edit(embed=self.embed, view=self)
        else:
            # クイズ終了
            await self.cog.end_quiz_session(interaction.user, self.user_answers, self.message)

class StudyCog(commands.Cog):
    """Obsidianの教材データを元に学習クイズを生成するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_ready = False
        self._load_env_vars()
        
        if not self._validate_env_vars():
            logging.error("StudyCog: 必須の環境変数が不足。Cogを無効化します。")
            return
        try:
            self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            self.is_ready = True
            if STUDY_CHANNEL_ID != 0:
                self.daily_quiz.start()
        except Exception as e:
            logging.error(f"StudyCogの初期化中にエラー: {e}", exc_info=True)

    def _load_env_vars(self):
        self.dropbox_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path=os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.dropbox_app_key=os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret=os.getenv("DROPBOX_APP_SECRET")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

    def _validate_env_vars(self) -> bool:
        return all([self.dropbox_refresh_token, self.dropbox_vault_path, self.gemini_api_key])

    async def get_all_study_materials(self) -> dict:
        """/Study フォルダ内の全教材データを取得"""
        materials = {}
        try:
            folder_path = f"{self.dropbox_vault_path}{VAULT_STUDY_PATH}"
            res = self.dbx.files_list_folder(folder_path)
            for entry in res.entries:
                if isinstance(entry, FileMetadata) and entry.name.endswith('.md'):
                    subject = entry.name.replace('.md', '')
                    _, content_res = self.dbx.files_download(entry.path_display)
                    materials[subject] = content_res.content.decode('utf-8')
            return materials
        except ApiError as e:
            logging.error(f"教材フォルダの読み込みに失敗: {e}")
            return {}

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
        """エビングハウスの忘却曲線に基づき、次の復習日を計算"""
        today = datetime.now(JST).date()
        if correct_streak <= 0: return (today + timedelta(days=1)).isoformat()
        if correct_streak == 1: return (today + timedelta(days=1)).isoformat()
        if correct_streak == 2: return (today + timedelta(days=7)).isoformat()
        if correct_streak == 3: return (today + timedelta(days=16)).isoformat()
        if correct_streak >= 4: return (today + timedelta(days=35)).isoformat()
        return (today + timedelta(days=1)).isoformat()

    @tasks.loop(time=STUDY_TIME)
    async def daily_quiz(self):
        channel = self.bot.get_channel(STUDY_CHANNEL_ID)
        if not channel: return
        
        all_materials = await self.get_all_study_materials()
        user_progress = await self.get_user_progress()
        
        if not all_materials:
            await channel.send("教材が見つかりませんでした。")
            return

        # 今日レビューすべき問題を選ぶ
        today_str = datetime.now(JST).date().isoformat()
        review_questions = []
        for q_id, data in user_progress.items():
            if data.get("next_review_date") == today_str:
                review_questions.append(q_id)
        
        # 新しい問題を選ぶ
        num_new_questions = QUESTIONS_PER_SESSION - len(review_questions)
        
        prompt = f"""
        あなたは教師です。以下の教材内容と学習履歴に基づき、ユーザーの知識をテストするための高品質な4択問題を{QUESTIONS_PER_SESSION}問生成してください。

        # 指示
        - まず、以下の「復習問題リスト」から問題を優先的に出題してください。
        - 次に、不足分を「教材」から新しい問題として生成してください。
        - 各問題には、ユニークなID（例: itpass_20240101_01）を付けてください。
        - 出力は必ずJSON形式のリストにしてください。

        # 復習問題リスト
        {json.dumps(review_questions, ensure_ascii=False)}

        # 教材
        {json.dumps(all_materials, ensure_ascii=False, indent=2)}

        # 学習履歴（参考）
        {json.dumps(user_progress, ensure_ascii=False, indent=2)}
        
        # 出力形式 (JSONリスト)
        [
          {{
            "id": "ユニークな問題ID",
            "question": "問題文",
            "options": ["選択肢1", "選択肢2", "選択肢3", "選択肢4"],
            "answer": 0, // 正解のインデックス (0-3)
            "explanation": "なぜその答えになるのかの簡潔な解説"
          }}
        ]
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            questions = json.loads(response.text.strip())
            
            view = QuizView(self, questions)
            message = await channel.send("✍️ 今日の学習クイズです！", embed=view.embed, view=view)
            view.message = message
        except Exception as e:
            await channel.send(f"問題の生成に失敗しました: {e}")
            logging.error(f"クイズ生成エラー: {e}", exc_info=True)

    async def end_quiz_session(self, user: discord.User, answers: list, message: discord.Message):
        """クイズセッションを終了し、結果を表示・保存する"""
        progress = await self.get_user_progress()
        correct_count = 0
        
        result_description = ""
        
        for ans in answers:
            q_id = ans["question_id"]
            is_correct = ans["is_correct"]
            
            if is_correct:
                correct_count += 1
                streak = progress.get(q_id, {}).get("correct_streak", 0) + 1
                result_description += f"✅ **{q_id}**: 正解！\n"
            else:
                streak = 0
                result_description += f"❌ **{q_id}**: 不正解...\n"
            
            progress[q_id] = {
                "last_answered": datetime.now(JST).date().isoformat(),
                "correct_streak": streak,
                "next_review_date": self.get_next_review_date(streak)
            }

        await self.save_user_progress(progress)
        
        embed = discord.Embed(
            title="クイズ終了！",
            description=f"**結果: {correct_count} / {len(answers)} 正解**\n\n{result_description}",
            color=discord.Color.green() if correct_count > len(answers) / 2 else discord.Color.red()
        )
        await message.edit(content="お疲れ様でした！", embed=embed, view=None)

async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))