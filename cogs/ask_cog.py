import os
import discord
from discord.ext import commands
import logging
import dropbox
from dropbox.files import FileMetadata, FolderMetadata
import google.generativeai as genai

class AskCog(commands.Cog):
    """
    Obsidian Vault内のノートを知識源としてAIと対話するCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数 ---
        self.ask_channel_id = int(os.getenv("ASK_CHANNEL_ID", 0))
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        # --- 初期チェック ---
        if not self.ask_channel_id:
            logging.warning("AskCog: ASK_CHANNEL_IDが設定されていません。")
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("AskCog: Dropboxの認証情報が不足しています。")
        if not self.gemini_api_key:
            logging.warning("AskCog: GEMINI_API_KEYが設定されていません。")
        else:
            genai.configure(api_key=self.gemini_api_key)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # --- 処理実行の条件判定 ---
        if message.author.bot:
            return
        if message.channel.id != self.ask_channel_id:
            return

        # --- 処理フロー ---
        async with message.channel.typing():
            try:
                # 1. Dropboxから全ノートの内容を取得
                logging.info("[AskCog] Dropboxからノートの取得を開始...")
                all_notes_content = await self.get_all_notes_from_dropbox()
                if not all_notes_content:
                    await message.reply("Vault内にノートが見つかりませんでした。")
                    return
                logging.info(f"[AskCog] {len(all_notes_content)} 文字のコンテキストを取得しました。")

                # 2. Gemini APIで回答を生成
                logging.info("[AskCog] Gemini APIへの問い合わせを開始...")
                model = genai.GenerativeModel("gemini-2.5-pro")
                
                prompt = (
                    "あなたは私のアシスタントです。\n"
                    "以下の『コンテキスト』情報だけを使って、質問に答えてください。\n"
                    "コンテキストに情報がない場合は、必ず『分かりません』とだけ答えてください。\n\n"
                    "--- コンテキスト ---\n"
                    f"{all_notes_content}\n\n"
                    "--- 質問 ---\n"
                    f"{message.content}"
                )

                response = await model.generate_content_async(prompt)
                
                if not response.candidates:
                    await message.reply("AIから有効な回答が得られませんでした。（安全フィルター等の可能性があります）")
                    return

                ai_response = response.text

                # 3. 回答をDiscordに投稿
                await message.reply(ai_response)
                logging.info("[AskCog] 回答を投稿しました。")

            except Exception as e:
                logging.error(f"[AskCog] 処理中にエラーが発生しました: {e}", exc_info=True)
                await message.reply(f"エラーが発生しました: {e}")

    async def get_all_notes_from_dropbox(self) -> str:
        """Dropbox Vault内の全Markdownファイルの内容を結合して返す"""
        all_content = []
        try:
            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                # Vault内の全アイテムを再帰的にリスト
                result = dbx.files_list_folder(self.dropbox_vault_path, recursive=True)
                
                for entry in result.entries:
                    if isinstance(entry, FileMetadata) and entry.name.endswith('.md'):
                        try:
                            _, res = dbx.files_download(entry.path_display)
                            # ファイル名の見出しを追加してから内容を追加
                            content = f"# {entry.name.replace('.md', '')}\n\n{res.content.decode('utf-8')}"
                            all_content.append(content)
                        except dropbox.exceptions.ApiError as e:
                            logging.error(f"[AskCog] ファイルのダウンロードに失敗: {entry.path_display}, Error: {e}")
            
            return "\n\n---\n\n".join(all_content)
        except Exception as e:
            logging.error(f"[AskCog] Dropboxの操作中にエラー: {e}")
            raise

async def setup(bot: commands.Bot):
    await bot.add_cog(AskCog(bot))