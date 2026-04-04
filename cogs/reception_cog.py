import discord
from discord.ext import commands
import os
import re
import logging

from services.webclip_service import WebClipService
from prompts import PROMPT_URL_RECEPTION  # ★ 追加

URL_REGEX = re.compile(r"https?://[^\s]+")


class ReceptionCog(commands.Cog):
    """
    メモチャンネルのURL投稿を監視し、WebClip/YouTubeの即時処理を行うCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_service = bot.drive_service

        gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.webclip_service = WebClipService(self.drive_service, gemini_api_key)
        self.pending_clips = {}

        if self.memo_channel_id == 0:
            logging.warning("[ReceptionCog] MEMO_CHANNEL_ID が設定されていません。")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.memo_channel_id:
            return

        # ★ メモの待機状態かチェック
        if message.author.id in self.pending_clips:
            pending_info = self.pending_clips.pop(message.author.id)
            await message.add_reaction("⏳")
            try:
                # 保留されていた解析情報と、追加のメモを合体して保存
                # 元々のURLに付属していたテキスト(message_content)もある場合は必要ですが、
                # 通常は最初の投稿にURLしか含まれない想定。
                memo_comment = pending_info.get("original_comment", "")
                if memo_comment:
                    memo_comment += "\n" + message.content
                else:
                    memo_comment = message.content

                result = await self.webclip_service.save_parsed_info(
                    pending_info, message, memo_comment
                )
                await message.remove_reaction("⏳", self.bot.user)

                if isinstance(result, dict):
                    partner_cog = self.bot.get_cog("PartnerCog")
                    if partner_cog:
                        prompt_text = PROMPT_URL_RECEPTION.format(
                            url_type=result.get("type", "Webページ"),
                            title=result.get("title", ""),
                        )
                        context_data = f"【保存したURLの情報】\n種類: {result.get('type')}\nタイトル: {result.get('title')}\n保存先: {result.get('folder')}/{result.get('file')}\nユーザーのメモ: {message.content}"
                        await partner_cog.generate_and_send_routine_message(
                            context_data, prompt_text
                        )
            except Exception as e:
                logging.error(f"[ReceptionCog] 保留URL保存エラー: {e}", exc_info=True)
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
            return

        match = URL_REGEX.search(message.content)
        if match:
            url = match.group(0)
            logging.info(f"[ReceptionCog] URLを検知し、処理を開始します: {url}")
            await message.add_reaction("⏳")

            try:
                # まずURL解析のみを実行
                parsed_info = await self.webclip_service.parse_url_info(
                    url, message.content
                )
                parsed_info["original_comment"] = message.content.replace(
                    url, ""
                ).strip()
                await message.remove_reaction("⏳", self.bot.user)

                if parsed_info["is_map"] or parsed_info["is_recipe"]:
                    # 保留状態に移行する
                    self.pending_clips[message.author.id] = parsed_info

                    if parsed_info["is_map"]:
                        reply_msg = f"📍 場所のリンク「{parsed_info['title']}」を受け付けました！\nこの場所についてのメモや保存した理由を教えてください。\n（そのまま発言すると合体してノートに保存されます）"
                    else:
                        reply_msg = f"🍳 レシピのリンク「{parsed_info['title']}」を受け付けました！\nこのレシピで気になるポイントやメモを教えてください。\n（そのまま発言すると合体してノートに保存されます）"

                    await message.reply(reply_msg)
                else:
                    # WebClipやYouTubeは今まで通り直ちに保存
                    await message.add_reaction("⏳")
                    result = await self.webclip_service.save_parsed_info(
                        parsed_info, message, message.content
                    )
                    await message.remove_reaction("⏳", self.bot.user)

                    if isinstance(result, dict):
                        partner_cog = self.bot.get_cog("PartnerCog")
                        if partner_cog:
                            prompt_text = PROMPT_URL_RECEPTION.format(
                                url_type=result.get("type", "Webページ"),
                                title=result.get("title", ""),
                            )
                            context_data = f"【保存したURLの情報】\n種類: {result.get('type')}\nタイトル: {result.get('title')}\n保存先: {result.get('folder')}/{result.get('file')}"
                            await partner_cog.generate_and_send_routine_message(
                                context_data, prompt_text
                            )

            except Exception as e:
                logging.error(f"[ReceptionCog] 処理エラー: {e}", exc_info=True)
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")


async def setup(bot: commands.Bot):
    await bot.add_cog(ReceptionCog(bot))
