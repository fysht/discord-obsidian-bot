import os
import discord
from discord.ext import commands
import logging
import asyncio
from dotenv import load_dotenv
import re # 正規表現を使うために追加

# --- 1. 設定読み込み ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
# >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
# memo_cog と同じチャンネルID、トリガー絵文字を使用
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))
YOUTUBE_TRIGGER_EMOJI = '▶️' # memo_cog が付ける絵文字
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
# >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


# --- 2. Botの定義 ---
class LocalWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True  # リアクションを検知するためにTrueである必要があります
        intents.guilds = True
        super().__init__(command_prefix="!local!", intents=intents)
        self.youtube_cog = None # youtube_cog のインスタンスを保持

    async def setup_hook(self):
        # 必要なCogだけをロード
        try:
            await self.load_extension("cogs.youtube_cog")
            self.youtube_cog = self.get_cog('YouTubeCog') # インスタンスを取得
            if self.youtube_cog:
                 logging.info("YouTubeCogを読み込み、インスタンスを取得しました。")
            else:
                 logging.error("YouTubeCogのインスタンス取得に失敗しました。")
        except Exception as e:
            logging.error(f"YouTubeCogの読み込みに失敗: {e}", exc_info=True)


    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (Local - YouTube処理担当)")

        if not self.youtube_cog:
             logging.error("YouTubeCogがロードされていないため、処理を開始できません。")
             return

        # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
        # 起動時の未処理スキャンはコメントアウト (必要であれば有効化)
        # logging.info("起動時に未処理のリアクションをスキャンします...")
        # try:
        #     # 起動時に一度だけ、既存のリアクションをまとめて処理する
        #     await self.youtube_cog.process_pending_summaries()
        # except Exception as e:
        #     logging.error(f"起動時のYouTube要約一括処理中にエラー: {e}", exc_info=True)
        # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<

        logging.info(f"リアクション監視モードに移行します。（チャンネル {MEMO_CHANNEL_ID} の {YOUTUBE_TRIGGER_EMOJI} を待ち受けます）")


    # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
    @commands.Cog.listener("on_raw_reaction_add") # Cog内ではなくBotクラス直下でリスナーを定義
    async def on_youtube_trigger_reaction(self, payload: discord.RawReactionActionEvent):
        """memo_cog が付けたYouTube処理トリガーを検知"""
        # 必要なチェック
        if payload.channel_id != MEMO_CHANNEL_ID: return
        if payload.user_id == self.user.id: return # 自分自身のリアクションは無視
        if str(payload.emoji) != YOUTUBE_TRIGGER_EMOJI: return
        if not self.youtube_cog: # Cogがロードされているか確認
            logging.error("YouTubeCogが利用できないため、リアクションを処理できません。")
            return

        channel = self.get_channel(payload.channel_id)
        if not channel:
            logging.error(f"チャンネル {payload.channel_id} が見つかりません。")
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.warning(f"メッセージ {payload.message_id} の取得に失敗しました。")
            return

        # メッセージ本文からYouTube URLを抽出
        url_match = YOUTUBE_URL_REGEX.search(message.content)
        if not url_match:
            logging.warning(f"メッセージ {message.id} にYouTube URLが見つかりませんでした。")
            return

        url = url_match.group(0)
        logging.info(f"リアクション '{YOUTUBE_TRIGGER_EMOJI}' を検知。YouTube処理を開始します: {url}")

        # トリガーリアクションを削除
        try:
            await message.remove_reaction(payload.emoji, self.user) # ボットが付けたリアクションを削除
        except discord.HTTPException:
            logging.warning(f"リアクション {payload.emoji} の削除に失敗: {message.id}")

        # youtube_cog の処理を実行
        try:
            # _perform_summary は成功/失敗を示すリアクション (✅/❌/etc.) を内部で付けるはず
            await self.youtube_cog._perform_summary(url=url, message=message)
            logging.info(f"YouTube処理が完了しました: {url}")
            # _perform_summary内で完了リアクションが付くため、ここでは不要
        except Exception as e:
            logging.error(f"YouTube処理の呼び出し中にエラーが発生: {e}", exc_info=True)
            # エラー発生時も _perform_summary 内で ❌ が付くことを期待
            # もし付かない場合はここで付ける
            try:
                await message.add_reaction('❌')
            except discord.HTTPException: pass # リアクション失敗は無視

    # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


# --- 3. 起動処理 ---
async def main():
    if not TOKEN:
        logging.critical("DISCORD_BOT_TOKENが設定されていません。ローカルワーカーを起動できません。")
        return
    if MEMO_CHANNEL_ID == 0:
        logging.critical("MEMO_CHANNEL_IDが設定されていません。ローカルワーカーを起動できません。")
        return

    bot = LocalWorkerBot()
    try:
        await bot.start(TOKEN)
    except discord.LoginFailure:
         logging.critical("Discordトークンが無効です。ローカルワーカーを起動できません。")
    except Exception as e:
         logging.critical(f"ローカルワーカーの起動中に致命的なエラーが発生しました: {e}", exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("ローカルワーカーを手動でシャットダウンしました。")