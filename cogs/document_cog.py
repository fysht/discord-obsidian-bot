import os
import discord
from discord.ext import commands
from google.genai import types
import logging
import datetime

from config import JST
from prompts import PROMPT_DOCUMENT_DRAFTING


class DocumentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.document_channel_id = int(os.getenv("DOCUMENT_CHANNEL_ID", "0"))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    # スレッド作成時の自動メッセージを削除し、ユーザーが最初に話しかける仕様に変更

    async def _build_conversation_context(
        self, thread: discord.Thread, limit: int = 30
    ):
        """スレッド内の会話履歴を取得してGeminiの形式に変換する"""
        messages = []
        async for msg in thread.history(limit=limit, oldest_first=False):
            if not msg.content:
                continue
            role = "model" if msg.author.id == self.bot.user.id else "user"
            messages.append(
                types.Content(
                    role=role, parts=[types.Part.from_text(text=msg.content)]
                )
            )
        return list(reversed(messages))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # スレッド内での発言かつ、親チャンネルがDOCUMENT_CHANNEL_IDであるか確認
        if not isinstance(message.channel, discord.Thread):
            return

        if message.channel.parent_id != self.document_channel_id:
            return

        # Geminiクライアントのチェック
        if not self.gemini_client:
            await message.channel.send("ごめんね、AIの設定（Gemini）が有効になってないみたい💦")
            return

        async with message.channel.typing():
            # 会話履歴を取得
            contents = await self._build_conversation_context(
                message.channel, limit=20
            )

            # ツールの定義
            function_tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="save_document",
                            description="文書の作成が完了した段階で呼び出し、生成されたMarkdown文書をObsidian（Google Drive）へ保存する。",
                            parameters=types.Schema(
                                type=types.Type.OBJECT,
                                properties={
                                    "title": types.Schema(
                                        type=types.Type.STRING,
                                        description="保存するファイル名（拡張子不要）",
                                    ),
                                    "content": types.Schema(
                                        type=types.Type.STRING,
                                        description="Markdown形式の文書のフルテキスト",
                                    ),
                                },
                                required=["title", "content"],
                            ),
                        )
                    ]
                )
            ]

            try:
                # 今回は精度の高い返答を行わせるため、flashモデルを利用
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=PROMPT_DOCUMENT_DRAFTING,
                        tools=function_tools,
                    ),
                )

                # Tool実行が求められた場合
                if response.function_calls:
                    for function_call in response.function_calls:
                        if function_call.name == "save_document":
                            title = function_call.args["title"]
                            content = function_call.args["content"]
                            
                            # 拡張子の補完
                            if not title.endswith(".md"):
                                title += ".md"

                            # Obsidian同期保存
                            result_msg = await self._save_to_obsidian(title, content)
                            await message.channel.send(result_msg)
                            return

                # 通常の返信
                if response.text:
                    await message.channel.send(response.text.strip())

            except Exception as e:
                logging.error(f"DocumentCogでエラー発生: {e}", exc_info=True)
                await message.channel.send(f"ごめんね、エラーが起きちゃった💦 ({e})")

    async def _save_to_obsidian(self, file_name: str, content: str) -> str:
        """DriveServiceを利用してDocumentsフォルダへ保存する"""
        if not self.drive_service:
            return "あれっ、Google Driveに繋がってないみたい💦保存できなかったよー。"

        service = self.drive_service.get_service()
        if not service:
            return "Google Driveの認証情報を取得できなかったよ💦"

        # ベースとなるDocumentsフォルダを探す
        base_folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, "Documents"
        )
        # なければ作る
        if not base_folder_id:
            base_folder_id = await self.drive_service.create_folder(
                service, self.drive_folder_id, "Documents"
            )

        # ファイルが存在するか確認
        f_id = await self.drive_service.find_file(service, base_folder_id, file_name)

        if f_id:
            # 既存は上書き（または今回は引継書などのケースのため上書きでOK）
            await self.drive_service.update_text(service, f_id, content)
            return f"完成お疲れ様！\n「Documents/{file_name}」を上書き保存しといたよ！"
        else:
            await self.drive_service.upload_text(
                service, base_folder_id, file_name, content
            )
            return f"完成お疲れ様！\n「Documents/{file_name}」としてObsidianに保存しといたよ！"

async def setup(bot: commands.Bot):
    await bot.add_cog(DocumentCog(bot))
            await self.drive_service.update_text(service, f_id, content)
            return f"完成お疲れ様！\n「Documents/{file_name}」を上書き保存しといたよ！"
        else:
            await self.drive_service.upload_text(
                service, base_folder_id, file_name, content
            )
            return f"完成お疲れ様！\n「Documents/{file_name}」としてObsidianに保存しといたよ！"

async def setup(bot: commands.Bot):
    await bot.add_cog(DocumentCog(bot))
