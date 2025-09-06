import os
import discord
from discord.ext import commands, tasks
import logging
import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import google.generativeai as genai
import yaml
from io import StringIO

# 外部のFitbitクライアントをインポート
from fitbit_client import FitbitClient

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HEALTH_LOG_TIME = datetime.time(hour=8, minute=0, tzinfo=JST)
SECTION_ORDER = [
    "## Health Metrics", # このCogで追加するセクション
    "## WebClips",
    "## YouTube Summaries",
    "## AI Logs",
    "## Zero-Second Thinking",
    "## Memo"
]

class FitbitCog(commands.Cog):
    """Fitbitのデータを取得し、Obsidianへの記録とAIによる健康アドバイスを行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- .envからの設定読み込み ---
        self.fitbit_client_id = os.getenv("FITBIT_CLIENT_ID")
        self.fitbit_client_secret = os.getenv("FITBIT_CLIENT_SECRET")
        self.fitbit_refresh_token = os.getenv("FITBIT_REFRESH_TOKEN")
        self.fitbit_user_id = os.getenv("FITBIT_USER_ID", "-")
        self.health_log_channel_id = int(os.getenv("HEALTH_LOG_CHANNEL_ID", 0))

        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        # --- クライアントの初期化 ---
        self.is_ready = self._validate_and_init_clients()
        if self.is_ready:
            logging.info("FitbitCog: 正常に初期化されました。")
        else:
            logging.error("FitbitCog: 環境変数が不足しているため、初期化に失敗しました。")

    def _validate_and_init_clients(self):
        """環境変数のチェックとAPIクライアントの初期化を行う"""
        if not all([self.fitbit_client_id, self.fitbit_client_secret, self.fitbit_refresh_token,
                    self.health_log_channel_id, self.dropbox_refresh_token, self.gemini_api_key]):
            return False
        
        self.fitbit_client = FitbitClient(
            self.fitbit_client_id, self.fitbit_client_secret, self.fitbit_refresh_token, self.fitbit_user_id
        )
        self.dbx = dropbox.Dropbox(
            oauth2_refresh_token=self.dropbox_refresh_token,
            app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret
        )
        genai.configure(api_key=self.gemini_api_key)
        self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready and not self.daily_health_log.is_running():
            self.daily_health_log.start()
            logging.info(f"FitbitCog: ヘルスログタスクを {HEALTH_LOG_TIME} に開始します。")

    def cog_unload(self):
        self.daily_health_log.cancel()

    @tasks.loop(time=HEALTH_LOG_TIME)
    async def daily_health_log(self):
        """毎朝8時に実行されるメインタスク"""
        logging.info("FitbitCog: 定期タスクを開始します...")
        try:
            # 1. 対象日の決定 (昨日)
            target_date = datetime.datetime.now(JST).date() - datetime.timedelta(days=1)
            
            # 2. Fitbitから睡眠データを取得
            sleep_data = await self.fitbit_client.get_sleep_data(target_date)
            if not sleep_data:
                logging.warning(f"FitbitCog: {target_date} の睡眠データが取得できませんでした。タスクを終了します。")
                return
            
            # 3. Obsidianにデータを保存
            await self._save_data_to_obsidian(target_date, sleep_data)
            
            # 4. AIアドバイスを生成
            advice_text = await self._generate_ai_advice(target_date, sleep_data)
            
            # 5. Discordに投稿
            channel = self.bot.get_channel(self.health_log_channel_id)
            if channel:
                embed = self._create_discord_embed(target_date, sleep_data, advice_text)
                await channel.send(embed=embed)
                logging.info(f"FitbitCog: {target_date} のヘルスログをDiscordに投稿しました。")
            else:
                logging.error(f"FitbitCog: チャンネルID {self.health_log_channel_id} が見つかりません。")

        except Exception as e:
            logging.error(f"FitbitCog: 定期タスクの実行中にエラーが発生しました: {e}", exc_info=True)

    def _parse_note_content(self, content: str) -> (dict, str):
        """ノートの内容からYAMLフロントマターと本文を分離する"""
        try:
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    frontmatter = yaml.safe_load(StringIO(parts[1])) or {}
                    body = parts[2].lstrip()
                    return frontmatter, body
        except yaml.YAMLError:
            # パースに失敗した場合は全体を本文として扱う
            pass
        return {}, content

    def _update_daily_note_with_ordered_section(self, current_content: str, text_to_add: str, section_header: str) -> str:
        """webclip_cogから流用した、順序を維持してセクションを追加/更新する関数"""
        lines = current_content.split('\n')
        
        try:
            header_index = lines.index(section_header)
            # 既存セクションの内容を新しい内容で置き換える（ヘッダーはそのまま）
            # まず既存のセクション範囲を特定
            end_index = header_index + 1
            while end_index < len(lines) and not lines[end_index].strip().startswith('## '):
                end_index += 1
            # 既存セクションの内容を削除
            del lines[header_index + 1 : end_index]
            # 新しい内容を挿入
            lines.insert(header_index + 1, text_to_add)
            return "\n".join(lines)
        except ValueError:
            # セクションが存在しない場合、正しい位置に新規作成
            new_section_with_header = f"\n{section_header}\n{text_to_add}"
            if not any(s in current_content for s in SECTION_ORDER):
                 return current_content.strip() + "\n" + new_section_with_header

            existing_sections = {line.strip(): i for i, line in enumerate(lines) if line.strip() in SECTION_ORDER}
            new_section_order_index = SECTION_ORDER.index(section_header)

            # 挿入すべき位置を後ろから探す
            insert_after_index = -1
            for i in range(new_section_order_index - 1, -1, -1):
                preceding_header = SECTION_ORDER[i]
                if preceding_header in existing_sections:
                    header_line_index = existing_sections[preceding_header]
                    insert_after_index = header_line_index + 1
                    while insert_after_index < len(lines) and not lines[insert_after_index].strip().startswith('## '):
                        insert_after_index += 1
                    break
            
            if insert_after_index != -1:
                lines.insert(insert_after_index, new_section_with_header)
                return "\n".join(lines).strip()

            # 挿入すべき位置を前から探す
            insert_before_index = -1
            for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
                following_header = SECTION_ORDER[i]
                if following_header in existing_sections:
                    insert_before_index = existing_sections[following_header]
                    break
            
            if insert_before_index != -1:
                lines.insert(insert_before_index, new_section_with_header + "\n")
                return "\n".join(lines).strip()

            # どのセクションも見つからなければ末尾に追加
            return current_content.strip() + "\n" + new_section_with_header

    async def _save_data_to_obsidian(self, target_date: datetime.date, sleep_data: dict):
        """取得したデータをObsidianのデイリーノートに保存する"""
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{target_date.strftime('%Y-%m-%d')}.md"
        
        try:
            _, res = self.dbx.files_download(daily_note_path)
            current_content = res.content.decode('utf-8')
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                current_content = ""
            else:
                raise

        frontmatter, body = self._parse_note_content(current_content)
        
        # YAMLフロントマターの更新
        frontmatter.update({
            'date': target_date.isoformat(),
            'sleep_score': sleep_data.get('score'),
            'total_sleep_minutes': sleep_data.get('minutesAsleep'),
            'time_in_bed_minutes': sleep_data.get('timeInBed'),
            'sleep_efficiency': sleep_data.get('efficiency'),
            'deep_sleep_minutes': sleep_data.get('levels', {}).get('deep'),
            'light_sleep_minutes': sleep_data.get('levels', {}).get('light'),
            'rem_sleep_minutes': sleep_data.get('levels', {}).get('rem'),
            'wake_minutes': sleep_data.get('levels', {}).get('wake')
        })

        # 本文のHealth Metricsセクションの更新
        metrics_text = (
            f"- **Sleep Score:** {sleep_data.get('score', 'N/A')}\n"
            f"- **Total Sleep:** {sleep_data.get('minutesAsleep', 0) // 60}時間 {sleep_data.get('minutesAsleep', 0) % 60}分"
        )
        new_body = self._update_daily_note_with_ordered_section(
            body, metrics_text, "## Health Metrics"
        )
        
        # 新しいファイル内容の結合
        new_daily_content = f"---\n{yaml.dump(frontmatter, allow_unicode=True)}---\n\n{new_body}"
        
        # Dropboxにアップロード
        self.dbx.files_upload(
            new_daily_content.encode('utf-8'),
            daily_note_path,
            mode=WriteMode('overwrite')
        )
        logging.info(f"FitbitCog: {daily_note_path} を更新しました。")

    async def _generate_ai_advice(self, target_date: datetime.date, today_sleep_data: dict) -> str:
        """過去のデータを含めてAIに健康アドバイスを生成させる"""
        history_data = []
        for i in range(1, 8): # 過去7日間
            d = target_date - datetime.timedelta(days=i)
            note_path = f"{self.dropbox_vault_path}/DailyNotes/{d.strftime('%Y-%m-%d')}.md"
            try:
                _, res = self.dbx.files_download(note_path)
                content = res.content.decode('utf-8')
                fm, _ = self._parse_note_content(content)
                if fm and 'sleep_score' in fm:
                    history_data.append(f"- {d.strftime('%Y-%m-%d')}: Sleep Score {fm.get('sleep_score', 'N/A')}, Total Sleep {fm.get('total_sleep_minutes', 'N/A')} minutes")
            except ApiError:
                continue
        
        history_text = "\n".join(reversed(history_data))
        today_text = (
            f"- {target_date.strftime('%Y-%m-%d')} (Today): "
            f"Sleep Score {today_sleep_data.get('score', 'N/A')}, "
            f"Total Sleep {today_sleep_data.get('minutesAsleep', 'N/A')} minutes, "
            f"Deep Sleep {today_sleep_data.get('levels', {}).get('deep', 'N/A')} minutes"
        )

        prompt = f"""
あなたは私の成長をサポートする優秀なヘルスコーチです。
以下の過去1週間の睡眠データ推移を元に、私の健康状態を分析し、改善のための具体的でポジティブなアドバイスをしてください。

# 睡眠データ
{history_text}
{today_text}

# 指示
- 良い点をまず褒めてください。
- 改善できる点を1〜2点、具体的なアクションと共に提案してください。
- 全体的にポジティブで、実行したくなるようなトーンでお願いします。
- アドバイス本文のみを生成してください。
"""
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text
        except Exception as e:
            logging.error(f"FitbitCog: Gemini APIからのアドバイス生成中にエラー: {e}")
            return "AIによるアドバイスの生成中にエラーが発生しました。"

    def _create_discord_embed(self, target_date: datetime.date, sleep_data: dict, advice: str) -> discord.Embed:
        """Discordに投稿するためのEmbedオブジェクトを作成する"""
        title = f"📅 {target_date.strftime('%Y年%m月%d日')}のヘルスレポート"
        
        embed = discord.Embed(
            title=title,
            description=advice,
            color=discord.Color.blue()
        )
        score = sleep_data.get('score', 0)
        minutes = sleep_data.get('minutesAsleep', 0)
        
        embed.add_field(name="🌙 睡眠スコア", value=f"**{score}** 点", inline=True)
        embed.add_field(name="⏰ 合計睡眠時間", value=f"**{minutes // 60}**時間 **{minutes % 60}**分", inline=True)
        
        embed.set_footer(text="Powered by Fitbit & Gemini")
        embed.timestamp = datetime.datetime.now(JST)
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(FitbitCog(bot))