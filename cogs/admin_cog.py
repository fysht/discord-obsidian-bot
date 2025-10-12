import discord
from discord.ext import commands
from discord import app_commands
import logging

class AdminCog(commands.Cog):
    """管理者向けのコマンドを格納したCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="purge", description="指定したチャンネルのメッセージをすべて削除します。")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """チャンネル内のすべてのメッセージを削除するコマンド"""
        await interaction.response.defer(ephemeral=True)
        try:
            await channel.purge()
            await interaction.followup.send(f"✅ {channel.mention} のメッセージをすべて削除しました。")
            logging.info(f"{interaction.user} が {channel.name} のメッセージをすべて削除しました。")
        except discord.Forbidden:
            await interaction.followup.send("❌ メッセージを削除する権限がありません。")
        except Exception as e:
            await interaction.followup.send(f"❌ エラーが発生しました: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))