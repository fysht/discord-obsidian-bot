import os
import discord
from discord.ext import commands, tasks
import logging
import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import yaml
from io import StringIO
import asyncio
from discord import app_commands
from typing import Optional, Dict, Any
import statistics

# FitbitClient の定義部分は省略 (元のコードのまま使用してください)
# from fitbit_client import FitbitClient 

try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SLEEP_REPORT_TIME = datetime.time(hour=8, minute=0, tzinfo=JST)
FULL_HEALTH_REPORT_TIME = datetime.time(hour=22, minute=0, tzinfo=JST)

class FitbitCog(commands.Cog):
    """Fitbitデータを取得・保存し、PartnerCogに通知を依頼するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.fitbit_client_id = os.getenv("FITBIT_CLIENT_ID")
        self.fitbit_client_secret = os.getenv("FITBIT_CLIENT_SECRET")
        self.fitbit_user_id = os.getenv("FITBIT_USER_ID", "-")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        # DropboxとFitbitClientの初期化処理
        self.is_ready = self._validate_and_init_clients()

    def _validate_and_init_clients(self) -> bool:
        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
                app_key=os.getenv("DROPBOX_APP_KEY"), app_secret=os.getenv("DROPBOX_APP_SECRET")
            )
            # FitbitClientのインスタンス化 (元のコードのクラスを使用)
            self.fitbit_client = FitbitClient(
                self.fitbit_client_id, self.fitbit_client_secret, self.dbx, self.fitbit_user_id
            )
            return True
        except Exception as e:
            logging.error(f"FitbitCogのクライアント初期化中にエラー: {e}")
            return False

    # --- _calculate_sleep_score, _process_sleep_data, _format_minutes, _parse_note_content, _save_data_to_obsidian メソッドは元のコードをそのまま配置してください ---
    # （※文字数制限のため内部メソッドは省略しますが、必ずご自身の元のコードのメソッドを残してください）

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            if not self.sleep_report.is_running():
                self.sleep_report.start()
            if not self.full_health_report.is_running():
                self.full_health_report.start()

    def cog_unload(self):
        self.sleep_report.cancel()
        self.full_health_report.cancel()

    @tasks.loop(time=SLEEP_REPORT_TIME)
    async def sleep_report(self):
        """朝8時に睡眠データを収集し、PartnerCogに依頼する"""
        if not self.is_ready: return
        logging.info("FitbitCog: 睡眠レポートタスクを実行します。")
        
        target_date = datetime.datetime.now(JST).date()
        raw_sleep_data = await self.fitbit_client.get_sleep_data(target_date)
        sleep_summary = self._process_sleep_data(raw_sleep_data)

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        if not sleep_summary:
            context_data = "今日の睡眠データ：まだ同期されていません"
            instruction = "「おはようございます！睡眠データがまだ同期されていないみたいです。アプリを開いてみてくださいね」と優しく伝えてください。"
        else:
            sleep_score = sleep_summary.get('sleep_score', 0)
            sleep_time = self._format_minutes(sleep_summary.get('minutesAsleep', 0))
            
            context_data = f"【昨晩の睡眠データ】\nスコア: {sleep_score} / 100\n合計睡眠時間: {sleep_time}"
            instruction = "「睡眠データの速報です！」のような親しみやすい語りかけから始めてください。スコアや時間に対して労いやポジティブなコメントをし、今日も一日元気に過ごせるような一言を添えてください。"

        await partner_cog.generate_and_send_routine_message(context_data, instruction)

    @tasks.loop(time=FULL_HEALTH_REPORT_TIME)
    async def full_health_report(self):
        """夜22時に全データを収集・保存し、PartnerCogに依頼する"""
        if not self.is_ready: return
        logging.info("FitbitCog: 統合ヘルスレポートタスクを実行します。")

        target_date = datetime.datetime.now(JST).date()
        raw_sleep_data, activity_data = await asyncio.gather(
            self.fitbit_client.get_sleep_data(target_date),
            self.fitbit_client.get_activity_summary(target_date)
        )
        sleep_summary = self._process_sleep_data(raw_sleep_data)

        # Obsidianへの保存 (元のメソッド呼び出し)
        await self._save_data_to_obsidian(target_date, sleep_summary, activity_data)

        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog: return

        # PartnerCogへ渡すデータの成形
        sleep_text = f"スコア: {sleep_summary.get('sleep_score', 'N/A')}, 睡眠時間: {self._format_minutes(sleep_summary.get('minutesAsleep', 0))}" if sleep_summary else "データなし"
        activity_text = f"歩数: {activity_data.get('summary', {}).get('steps', 'N/A')}歩, 消費: {activity_data.get('summary', {}).get('caloriesOut', 'N/A')}kcal" if activity_data else "データなし"

        context_data = f"【本日の睡眠】\n{sleep_text}\n【本日の活動】\n{activity_text}"
        instruction = "「今日もお疲れ様でした！」から始まる夜のメッセージを作成してください。今日の健康データ（歩数や睡眠）を振り返り、良かった点を褒め、明日への優しいアドバイスを1つだけ添えてください。"

        await partner_cog.generate_and_send_routine_message(context_data, instruction)

async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))