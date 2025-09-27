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

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
STUDY_CHANNEL_ID = int(os.getenv("STUDY_CHANNEL_ID", 0))
STUDY_TIME = time(hour=0, minute=25, tzinfo=JST) 
VAULT_STUDY_PATH = "/Study" # Obsidianの教材フォルダ
QUESTIONS_PER_SESSION = 10 # 1回に出題する問題数

class QuizView(discord.ui.View):
    """クイズの選択肢ボタンを持つView"""
    def __init__(self, cog_instance, questions):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.questions = questions
        self.current_question_index = 0
        self.user_answers = []
        self.message = None
        # 最初の問題を作成
        self.embed = self.create_question_embed()

    def create_question_embed(self):
        question_data = self.questions[self.current_question_index]
        self.clear_items() # 以前のボタンをクリア
        
        embed = discord.Embed(
            title=f"第 {self.current_question_index + 1} 問 / {len(self.questions)} 問",
            description=f"**{question_data['question']}**",
            color=discord.Color.blue()
        )
        
        options = question_data['options']
        # 選択肢をアルファベット順（A, B, C, D）に並べ替える
        sorted_options = sorted(options.items())

        for key, value in sorted_options:
            button = discord.ui.Button(label=f"{key}) {value}", style=discord.ButtonStyle.secondary, custom_id=f"answer_{key}")
            button.callback = self.button_callback
            self.add_item(button)
        
        return embed

    async def button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_option_key = interaction.data['custom_id'].split('_')[1]
        
        question_data = self.questions[self.current_question_index]
        is_correct = (selected_option_key.upper() == question_data['answer'].upper())
        
        self.user_answers.append({
            "question_id": question_data['id'],
            "is_correct": is_correct,
            "question_data": question_data # 解説表示のため
        })

        # 次の問題へ
        self.current_question_index += 1
        if self.current_question_index < len(self.questions):
            next_embed = self.create_question_embed()
            await self.message.edit(embed=next_embed, view=self)
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

    def _validate_env_vars(self) -> bool:
        return all([self.dropbox_refresh_token, self.dropbox_vault_path])

    def _parse_study_materials(self, raw_content: str) -> list[dict]:
        """Q&A形式のMarkdownテキストを解析して、問題のリストを返す"""
        questions = []
        # ---で区切られた各問題ブロックを処理
        for block in raw_content.strip().split('---'):
            if not block.strip():
                continue
            
            try:
                question_data = {}
                options = {}
                
                # 正規表現で行を解析
                question_data['id'] = re.search(r'ID:\s*(.*)', block, re.IGNORECASE).group(1).strip()
                question_data['question'] = re.search(r'Question:\s*(.*)', block, re.IGNORECASE).group(1).strip()
                
                options_block = re.search(r'Options:\s*\n(.*?)\nAnswer:', block, re.DOTALL | re.IGNORECASE).group(1)
                for option_line in options_block.strip().split('\n'):
                    match = re.match(r'-\s*([A-D])\)\s*(.*)', option_line.strip(), re.IGNORECASE)
                    if match:
                        options[match.group(1).upper()] = match.group(2).strip()
                question_data['options'] = options
                
                question_data['answer'] = re.search(r'Answer:\s*(.*)', block, re.IGNORECASE).group(1).strip().upper()
                question_data['explanation'] = re.search(r'Explanation:\s*(.*)', block, re.DOTALL | re.IGNORECASE).group(1).strip()
                
                questions.append(question_data)
            except AttributeError:
                # パースに失敗したブロックはスキップ
                logging.warning(f"問題ブロックの解析に失敗しました。スキップします:\n---\n{block[:100]}...\n---")
                continue
        return questions

    async def get_all_questions_from_vault(self) -> list[dict]:
        """/Study フォルダ内の全教材を解析して、全ての問題リストを返す"""
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

    @tasks.loop(time=STUDY_TIME)
    async def daily_quiz(self):
        channel = self.bot.get_channel(STUDY_CHANNEL_ID)
        if not channel: return
        
        all_questions = await self.get_all_questions_from_vault()
        user_progress = await self.get_user_progress()
        
        if not all_questions:
            await channel.send("教材が見つかりませんでした。Obsidianの`/Study`フォルダにQ&A形式の教材ファイルを追加してください。")
            return

        today_str = datetime.now(JST).date().isoformat()
        
        # 1. 復習すべき問題を選ぶ
        review_question_ids = {q_id for q_id, data in user_progress.items() if data.get("next_review_date") <= today_str}
        
        # 2. 未学習の問題を選ぶ
        answered_ids = set(user_progress.keys())
        new_question_ids = {q['id'] for q in all_questions if q['id'] not in answered_ids}
        
        # 3. 出題リストを作成 (復習優先)
        questions_to_ask_ids = list(review_question_ids)
        
        # 4. 残りを新しい問題からランダムに選ぶ
        remaining_slots = QUESTIONS_PER_SESSION - len(questions_to_ask_ids)
        if remaining_slots > 0:
            questions_to_ask_ids.extend(random.sample(sorted(list(new_question_ids)), min(remaining_slots, len(new_question_ids))))
        
        if not questions_to_ask_ids:
            await channel.send("今日出題する問題はありませんでした。お疲れ様でした！")
            return
            
        # 問題IDから問題データを取得
        questions_to_ask_data = [q for q in all_questions if q['id'] in questions_to_ask_ids]
        random.shuffle(questions_to_ask_data) # 出題順をシャッフル

        try:
            view = QuizView(self, questions_to_ask_data)
            message = await channel.send("✍️ 今日の学習クイズです！", embed=view.embed, view=view)
            view.message = message
        except Exception as e:
            await channel.send(f"クイズの表示中にエラーが発生しました: {e}")
            logging.error(f"クイズ表示エラー: {e}", exc_info=True)

    async def end_quiz_session(self, user: discord.User, answers: list, message: discord.Message):
        progress = await self.get_user_progress()
        correct_count = 0
        
        result_lines = []
        for i, ans in enumerate(answers):
            q_data = ans["question_data"]
            is_correct = ans["is_correct"]
            
            if is_correct:
                correct_count += 1
                streak = progress.get(q_data['id'], {}).get("correct_streak", 0) + 1
                result_lines.append(f"✅ **第{i+1}問**: 正解！")
            else:
                streak = 0
                result_lines.append(f"❌ **第{i+1}問**: 不正解... (正解: {q_data['answer']})")
            
            progress[q_data['id']] = {
                "last_answered": datetime.now(JST).date().isoformat(),
                "correct_streak": streak,
                "next_review_date": self.get_next_review_date(streak)
            }

        await self.save_user_progress(progress)
        
        embed = discord.Embed(
            title="クイズ終了！",
            description=f"**結果: {correct_count} / {len(answers)} 正解**\n\n" + "\n".join(result_lines),
            color=discord.Color.green() if correct_count > len(answers) / 2 else discord.Color.red()
        )
        await message.edit(content="お疲れ様でした！", embed=embed, view=None)

async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))