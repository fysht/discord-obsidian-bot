import os
import json
import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
from openai import AsyncOpenAI
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
from tts_view import TTSView


class EnglishLearning(commands.Cog):
    def __init__(self, bot, openai_api_key, gemini_api_key, dropbox_token):
        self.bot = bot
        self.openai_client = AsyncOpenAI(api_key=openai_api_key)
        genai.configure(api_key=gemini_api_key)
        self.model = genai.GenerativeModel("gemini-2.5-pro")
        self.dbx = dropbox.Dropbox(dropbox_token)
        self.session_dir = "/english_sessions"
        logging.info("EnglishLearning Cog initialized.")

    def _get_session_path(self, user_id: int) -> str:
        return f"{self.session_dir}/{user_id}.json"

    @app_commands.command(name="english_chat", description="AIと英会話を始めます")
    async def english_chat(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)

        session = await self._load_session_from_dropbox(user_id)

        if session:
            logging.info(f"セッション再開: {session_path}")
            chat = self.model.start_chat(history=session)
            response = await asyncio.wait_for(chat.send_message_async("Welcome back! Let's continue our conversation."), timeout=60)
            response_text = response.text if response and hasattr(response, "text") else "Hi again!"
            await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
        else:
            logging.info(f"新規セッション開始: {session_path}")
            chat = self.model.start_chat(history=[])
            initial_prompt = "Hi! I'm your AI English partner. Let's chat! How's it going?"

            try:
                response = await asyncio.wait_for(chat.send_message_async(initial_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi! Let's chat."
                await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
            except asyncio.TimeoutError:
                logging.error("初回応答タイムアウト")
                response_text = "Sorry, response timed out. How are you?"
                await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))
            except Exception as e_init:
                logging.error(f"初回応答生成失敗: {e_init}", exc_info=True)
                response_text = "Sorry, error starting chat. How are you?"
                await interaction.followup.send(f"**AI:** {response_text}", view=TTSView(response_text, self.openai_client))

        try:
            await interaction.followup.send("終了は `/end`", ephemeral=True, delete_after=60)
        except discord.HTTPException:
            pass

    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx:
            return None

        session_path = self._get_session_path(user_id)
        try:
            logging.info(f"Loading session from: {session_path}")
            _, res = await asyncio.to_thread(self.dbx.files_download, session_path)

            try:
                return json.loads(res.content)
            except json.JSONDecodeError as json_e:
                logging.error(f"JSON解析失敗 ({session_path}): {json_e}")
                return None

        except ApiError as e:
            if (
                isinstance(e.error, DownloadError)
                and e.error.is_path()
                and e.error.get_path().is_not_found()
            ):
                logging.info(f"Session file not found for {user_id}")
                return None
            logging.error(f"Dropbox APIエラー ({session_path}): {e}")
            return None

        except Exception as e:
            logging.error(f"セッション読込エラー ({session_path}): {e}", exc_info=True)
            return None

    async def _save_session_to_dropbox(self, user_id: int, history: list):
        if not self.dbx:
            return

        session_path = self._get_session_path(user_id)
        try:
            serializable_history = []
            for turn in history:
                role = getattr(turn, "role", None)
                parts = getattr(turn, "parts", [])
                if role and parts:
                    part_texts = [getattr(p, "text", str(p)) for p in parts]
                    serializable_history.append({"role": role, "parts": part_texts})

            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode("utf-8")
            await asyncio.to_thread(
                self.dbx.files_upload,
                content,
                session_path,
                mode=WriteMode("overwrite"),
            )
            logging.info(f"Saved session to: {session_path}")

        except Exception as e:
            logging.error(f"セッション保存失敗 ({session_path}): {e}", exc_info=True)

    @app_commands.command(name="end", description="英会話を終了します")
    async def end_chat(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        try:
            await asyncio.to_thread(self.dbx.files_delete_v2, session_path)
            await interaction.followup.send("セッションを終了しました。お疲れさまでした！")
        except ApiError as e:
            logging.error(f"セッション削除失敗 ({session_path}): {e}")
            await interaction.followup.send("セッション削除に失敗しました。")
        except Exception as e:
            logging.error(f"英会話終了エラー: {e}", exc_info=True)
            await interaction.followup.send("エラーが発生しました。")

    async def _generate_chat_review(self, history: list) -> str:
        """英会話の内容を要約・評価"""
        try:
            text_summary = "Summarize and give feedback on this conversation in English:\n"
            for turn in history[-10:]:
                role = getattr(turn, "role", "unknown")
                parts = getattr(turn, "parts", [])
                text_content = " ".join(getattr(p, "text", "") for p in parts)
                text_summary += f"{role.upper()}: {text_content}\n"

            review_response = await asyncio.wait_for(
                self.model.generate_content_async(text_summary), timeout=60
            )
            return review_response.text if hasattr(review_response, "text") else "No review generated."
        except asyncio.TimeoutError:
            logging.warning("レビュー生成タイムアウト")
            return "The review generation timed out."
        except Exception as e:
            logging.error(f"レビュー生成エラー: {e}", exc_info=True)
            return "An error occurred while generating the review."


async def setup(bot):
    await bot.add_cog(
        EnglishLearning(
            bot,
            os.getenv("OPENAI_API_KEY"),
            os.getenv("GEMINI_API_KEY"),
            os.getenv("DROPBOX_TOKEN"),
        )
    )