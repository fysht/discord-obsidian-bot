import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import json
import random
from datetime import datetime, time, timedelta
import zoneinfo
import dropbox
from dropbox.files import FileMetadata, DownloadError, WriteMode
from dropbox.exceptions import ApiError
import asyncio
import re
import textwrap

#  utils.obsidian_utilsからupdate_sectionをインポート
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
STUDY_CHANNEL_ID = int(os.getenv("STUDY_CHANNEL_ID", 0))
PREPARE_QUIZ_TIME = time(hour=7, minute=0, tzinfo=JST)
VAULT_STUDY_PATH = "/Study"
QUESTIONS_PER_DAY = 30
REVIEW_NOTE_PATH = "/Study/復習リスト.md" # 復習用ノートのパス

class SaveOptionView(discord.ui.View):
    """特定の選択肢の解説を保存するためのView"""
    def __init__(self, cog_instance, question_data, option_key):
        super().__init__(timeout=180) # 3分で無効化
        self.cog = cog_instance
        self.question_data = question_data
        self.option_key = option_key

    @discord.ui.button(label="この解説を保存", style=discord.ButtonStyle.secondary, emoji="🔖")
    async def save_option(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.save_option_for_review(self.question_data, self.option_key)
            button.disabled = True
            button.label = "保存済み"
            button.style = discord.ButtonStyle.success
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(f"選択肢 {self.option_key} の解説を復習リストに保存しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"復習リストへの保存中にエラー: {e}", exc_info=True)
            await interaction.followup.send("❌ 復習リストへの保存中にエラーが発生しました。", ephemeral=True)
        self.stop()

class ExplanationView(discord.ui.View):
    """回答後に各選択肢の解説を表示・保存するためのView"""
    def __init__(self, cog_instance, question_data):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.question_data = question_data

        for key in sorted(question_data['Options'].keys()):
            button = discord.ui.Button(label=f"📖 解説 {key}", style=discord.ButtonStyle.secondary, custom_id=f"explain_{key}")
            button.callback = self.explanation_callback
            self.add_item(button)
    
    async def explanation_callback(self, interaction: discord.Interaction):
        key = interaction.data['custom_id'].split('_')[1]
        option_data = self.question_data['Options'][key]
        is_correct = (key.upper() == self.question_data['Answer'].upper())
        result_text = "✅ 正解です" if is_correct else "❌ 不正解です"

        embed = discord.Embed(
            title=f"選択肢 {key} の解説",
            description=f"**{result_text}**\n\n{option_data['Explanation']}",
            color=discord.Color.green() if is_correct else discord.Color.red()
        )
        # 解説メッセージの中に「保存」ボタンを持つViewを追加
        save_view = SaveOptionView(self.cog, self.question_data, key)
        await interaction.response.send_message(embed=embed, view=save_view, ephemeral=True)


class AnswerView(discord.ui.View):
    """問題に回答するための初期View"""
    def __init__(self, cog_instance, question_data):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.question_data = question_data

        for key in sorted(question_data['Options'].keys()):
            button = discord.ui.Button(label=key, style=discord.ButtonStyle.secondary, custom_id=f"answer_{key}")
            button.callback = self.answer_callback
            self.add_item(button)

    async def answer_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            selected_option_key = interaction.data['custom_id'].split('_')[1]
            is_correct = (selected_option_key.upper() == self.question_data['Answer'].upper())

            # 間隔反復のために回答結果を記録
            await self.cog.process_answer(self.question_data['ID'], is_correct)

            # Embedを更新
            result_embed = interaction.message.embeds[0]
            result_embed.color = discord.Color.green() if is_correct else discord.Color.red()
            result_embed.title = "✅ 正解！" if is_correct else "❌ 不正解..."
            result_embed.set_footer(text=f"正解: {self.question_data['Answer']} | 下のボタンから各選択肢の解説を確認できます。")

            # Viewを解説モードに切り替え
            explanation_view = ExplanationView(self.cog, self.question_data)
            await interaction.edit_original_response(embed=result_embed, view=explanation_view)
        
        except Exception as e:
            logging.error(f"回答処理中にエラー: {e}", exc_info=True)
            if not interaction.is_done():
                await interaction.followup.send("❌ 回答の処理中にエラーが発生しました。", ephemeral=True)

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
        if not raw_content: return questions
        content = raw_content.replace('\xa0', ' ').lstrip("\ufeff")
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(content):
            m = re.search(r'\{', content[idx:])
            if not m: break
            start = idx + m.start()
            try:
                obj, end = decoder.raw_decode(content, start)
                if isinstance(obj, dict): questions.append(obj)
                idx = end
            except Exception as e:
                logging.warning(f"JSONパース中にスキップ: {e}")
                idx = start + 1
        return questions

    async def get_all_questions_from_vault(self) -> list[dict]:
        all_questions = []
        try:
            folder_path = f"{self.dropbox_vault_path}{VAULT_STUDY_PATH}"
            res = self.dbx.files_list_folder(folder_path)
            entries = list(res.entries)
            while getattr(res, "has_more", False):
                res = self.dbx.files_list_folder_continue(res.cursor)
                entries.extend(res.entries)
            for entry in entries:
                if isinstance(entry, FileMetadata) and entry.name.endswith('.md'):
                    if entry.path_display.endswith(REVIEW_NOTE_PATH): continue
                    try:
                        _, content_res = self.dbx.files_download(entry.path_display)
                        raw_content = content_res.content.decode('utf-8')
                        all_questions.extend(self._parse_study_materials(raw_content))
                    except Exception as e:
                        logging.error(f"ファイル処理中にエラー: {entry.path_display}", exc_info=True)
            logging.info(f"合計読み込み問題数 = {len(all_questions)}")
            return all_questions
        except Exception as e:
            logging.error(f"教材フォルダの読み込みに失敗", exc_info=True)
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
        try:
            progress_data = json.dumps(progress, indent=2, ensure_ascii=False).encode('utf-8')
            self.dbx.files_upload(progress_data, path, mode=WriteMode('overwrite'))
        except Exception:
            logging.error(f"学習進捗の保存に失敗: {path}", exc_info=True)

    def get_next_review_date(self, correct_streak: int) -> str:
        today = datetime.now(JST).date()
        days_to_add = {0: 1, 1: 1, 2: 7, 3: 16}.get(correct_streak, 35)
        return (today + timedelta(days=days_to_add)).isoformat()

    async def process_answer(self, q_id: str, is_correct: bool):
        progress = await self.get_user_progress()
        streak = (progress.get(q_id, {}).get("correct_streak", 0) + 1) if is_correct else 0
        progress[q_id] = {
            "last_answered": datetime.now(JST).date().isoformat(),
            "correct_streak": streak,
            "next_review_date": self.get_next_review_date(streak)
        }
        await self.save_user_progress(progress)

    async def _prepare_question_pool_logic(self):
        all_questions = await self.get_all_questions_from_vault()
        user_progress = await self.get_user_progress()
        if not all_questions:
            self.daily_question_pool = []
            return
        today_str = datetime.now(JST).date().isoformat()
        review_ids = {q_id for q_id, data in user_progress.items() if data.get("next_review_date") <= today_str}
        answered_ids = set(user_progress.keys())
        new_ids = {q['ID'] for q in all_questions if 'ID' in q and q['ID'] not in answered_ids}
        pool_ids = list(review_ids)
        remaining_slots = QUESTIONS_PER_DAY - len(pool_ids)
        if remaining_slots > 0:
            new_ids_list = sorted(list(new_ids))
            if new_ids_list:
                sample_size = min(remaining_slots, len(new_ids_list))
                pool_ids.extend(random.sample(new_ids_list, sample_size))
        self.daily_question_pool = [q for q in all_questions if 'ID' in q and q['ID'] in pool_ids]
        random.shuffle(self.daily_question_pool)
        logging.info(f"本日の問題プールを作成しました: {len(self.daily_question_pool)}問")

    @tasks.loop(time=PREPARE_QUIZ_TIME)
    async def prepare_daily_questions(self):
        await self._prepare_question_pool_logic()

    @tasks.loop(hours=1)
    async def ask_next_question(self):
        now = datetime.now(JST)
        if not (9 <= now.hour <= 22) or not self.daily_question_pool:
            return
        channel = self.bot.get_channel(STUDY_CHANNEL_ID)
        if not channel: return
        question_data = self.daily_question_pool.pop(0)
        options_text = "\n".join([f"**{key})** {value['Text']}" for key, value in sorted(question_data['Options'].items())])
        description = f"{question_data['Question']}\n\n{options_text}"
        embed = discord.Embed(
            title=f"✍️ 学習クイズ ({QUESTIONS_PER_DAY - len(self.daily_question_pool)}/{QUESTIONS_PER_DAY})",
            description=description,
            color=discord.Color.blue()
        )
        view = AnswerView(self, question_data)
        await channel.send(embed=embed, view=view)

    @prepare_daily_questions.before_loop
    @ask_next_question.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="quiz", description="指定した数の未解答の問題をランダムに出題します。")
    @app_commands.describe(count="出題してほしい問題の数を指定してください。")
    async def custom_quiz(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 10]):
        await interaction.response.defer(ephemeral=True)
        all_questions = await self.get_all_questions_from_vault()
        user_progress = await self.get_user_progress()
        answered_ids = set(user_progress.keys())
        
        valid_unanswered_questions = []
        for q in all_questions:
            if q.get('ID') not in answered_ids:
                options_value = q.get('Options')
                if isinstance(options_value, dict) and options_value:
                    first_option = next(iter(options_value.values()))
                    if isinstance(first_option, dict) and 'Text' in first_option:
                        valid_unanswered_questions.append(q)

        if not valid_unanswered_questions:
            await interaction.followup.send("出題可能な形式の未解答問題がありませんでした。教材データを確認してください。", ephemeral=True)
            return
        
        num_to_ask = min(count, len(valid_unanswered_questions))
        questions_to_ask = random.sample(valid_unanswered_questions, num_to_ask)
        
        await interaction.followup.send(f"クイズを{num_to_ask}問出題します。", ephemeral=True)

        for i, question_data in enumerate(questions_to_ask):
            options_text = "\n".join([f"**{key})** {value['Text']}" for key, value in sorted(question_data['Options'].items())])
            description = f"{question_data['Question']}\n\n{options_text}"
            embed = discord.Embed(title=f"✍️ 実力テスト ({i + 1}/{num_to_ask})", description=description, color=discord.Color.blue())
            view = AnswerView(self, question_data)
            await interaction.channel.send(embed=embed, view=view)
            await asyncio.sleep(2)

    @app_commands.command(name="stop_quiz", description="定時の問題出題を停止します。")
    async def stop_quiz(self, interaction: discord.Interaction):
        if self.ask_next_question.is_running():
            self.ask_next_question.cancel()
            await interaction.response.send_message("✅ 定時の問題出題を停止しました。", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 問題出題は既に停止しています。", ephemeral=True)

    @app_commands.command(name="start_quiz", description="停止した定時の問題出題を再開します。")
    async def start_quiz(self, interaction: discord.Interaction):
        if not self.ask_next_question.is_running():
            self.ask_next_question.start()
            await interaction.response.send_message("✅ 定時の問題出題を再開しました。", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 問題出題は既に実行中です。", ephemeral=True)

    async def save_option_for_review(self, question_data: dict, option_key: str):
        full_path = f"{self.dropbox_vault_path}{REVIEW_NOTE_PATH}"
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        option_data = question_data['Options'][option_key]
        content_to_add = (
            f"### Q: {question_data['Question']} (ID: {question_data['ID']})\n"
            f"- **選択肢 {option_key}**: {option_data['Text']}\n"
            f"- **解説**: {option_data['Explanation']}\n"
            f"---\n"
        )
        section_header = f"## {today_str}"
        try:
            _, res = self.dbx.files_download(full_path)
            current_content = res.content.decode('utf-8')
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                current_content = f"# 復習リスト\n"
            else:
                raise
        new_content = update_section(current_content, content_to_add, section_header)
        self.dbx.files_upload(new_content.encode('utf-8'), full_path, mode=WriteMode('overwrite'))
        logging.info(f"復習リストに問題ID {question_data['ID']} の選択肢 {option_key} を追加しました。")


async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))