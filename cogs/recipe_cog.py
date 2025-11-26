import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
import datetime
import zoneinfo
import aiohttp
import google.generativeai as genai
import json
import random

# --- Webパーサーインポート ---
try:
    from web_parser import parse_url_with_readability
except ImportError:
    logging.warning("RecipeCog: web_parser が見つかりません。")
    parse_url_with_readability = None

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
RECIPE_INDEX_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/recipe_index.json"

# --- UI Components ---
class RecipeDetailView(discord.ui.View):
    def __init__(self, recipe_data):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="元記事を開く", url=recipe_data.get('source', '')))

class RecipeListView(discord.ui.View):
    def __init__(self, cog, recipes, page=0):
        super().__init__(timeout=180)
        self.cog = cog
        self.recipes = recipes
        self.page = page
        self.items_per_page = 10
        self.total_pages = (len(recipes) - 1) // self.items_per_page + 1
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.page == 0)
        self.next_button.disabled = (self.page >= self.total_pages - 1)
        self.page_count_button.label = f"Page {self.page + 1}/{self.total_pages}"
        
        start = self.page * self.items_per_page
        end = start + self.items_per_page
        current_items = self.recipes[start:end]
        
        options = []
        for i, recipe in enumerate(current_items):
            label = recipe.get('title', '無題')[:90]
            tags = ",".join(recipe.get('tags', []))[:90]
            options.append(discord.SelectOption(
                label=f"{start + i + 1}. {label}",
                description=f"🏷️ {tags}" if tags else "タグなし",
                value=str(start + i)
            ))
        
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)
        
        if options:
            select = discord.ui.Select(placeholder="レシピを選択して詳細を表示...", options=options, row=1)
            select.callback = self.select_callback
            self.add_item(select)

    async def update_message(self, interaction: discord.Interaction):
        self.update_buttons()
        embed = await self.cog.create_recipe_list_embed(self.recipes, self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀️ 前へ", style=discord.ButtonStyle.primary, row=0)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_message(interaction)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_count_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="次へ ▶️", style=discord.ButtonStyle.primary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
            await self.update_message(interaction)

    @discord.ui.button(label="🎲 ランダム", style=discord.ButtonStyle.success, row=0)
    async def random_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.recipes: return
        recipe = random.choice(self.recipes)
        embed = self.cog.create_recipe_detail_embed(recipe)
        await interaction.response.send_message(embed=embed, view=RecipeDetailView(recipe), ephemeral=True)

    async def select_callback(self, interaction: discord.Interaction):
        index = int(interaction.data["values"][0])
        if 0 <= index < len(self.recipes):
            recipe = self.recipes[index]
            embed = self.cog.create_recipe_detail_embed(recipe)
            await interaction.response.send_message(embed=embed, view=RecipeDetailView(recipe), ephemeral=True)

# --- RecipeCog ---
class RecipeCog(commands.Cog, name="RecipeCog"): 
    """Webレシピの情報を抽出・整理し、閲覧機能を提供するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recipe_channel_id = int(os.getenv("RECIPE_CHANNEL_ID", 0))
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.dbx = None
        self.gemini_model = None
        self.is_ready = False

        if not self.gemini_api_key or not self.dropbox_refresh_token or not self.recipe_channel_id:
            logging.error("RecipeCog: 必須の環境変数が不足しています。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret, timeout=300
            )
            self.dbx.users_get_current_account()
            
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-3-pro-preview")
            
            self.is_ready = True
            logging.info("RecipeCog: Initialized successfully.")
        except Exception as e:
            logging.error(f"RecipeCog: Initialization failed: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.recipe_channel_id: return
        if "http" in message.content:
            try:
                if not any(str(r.emoji) == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions):
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != self.recipe_channel_id: return
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        if payload.user_id == self.bot.user.id: return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try: message = await channel.fetch_message(payload.message_id)
        except: return

        if any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI) and r.me for r in message.reactions):
            return

        logging.info(f"Recipe trigger detected: {message.jump_url}")
        try: await message.remove_reaction(payload.emoji, await self.bot.fetch_user(payload.user_id))
        except: pass

        await self._process_recipe_card(message.content.strip(), message)

    async def _process_recipe_card(self, url: str, message: discord.Message):
        try:
            await message.add_reaction(PROCESS_START_EMOJI)
            
            # 1. コンテンツ取得
            content_text = await self._fetch_content(url)
            if not content_text:
                await message.reply("❌ コンテンツの取得に失敗しました。", delete_after=10)
                return

            # 2. AI抽出
            recipe_data = await self._extract_recipe_data_with_ai(content_text, url)
            if not recipe_data:
                await message.reply("❌ レシピ情報の抽出に失敗しました。", delete_after=10)
                return

            # 3. 保存
            filename = await self._save_recipe_to_obsidian(recipe_data)
            await self._update_recipe_index(recipe_data, filename)

            await message.add_reaction(PROCESS_COMPLETE_EMOJI)
            await message.reply(f"🍳 レシピブックに追加しました: **{recipe_data['title']}**", delete_after=10)
            
        except Exception as e:
            logging.error(f"Recipe processing error: {e}", exc_info=True)
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except: pass
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except: pass

    async def _fetch_content(self, url: str) -> str:
        if parse_url_with_readability:
            try:
                loop = asyncio.get_running_loop()
                title, content = await loop.run_in_executor(None, parse_url_with_readability, url)
                return content
            except: pass
        return None

    async def _extract_recipe_data_with_ai(self, text: str, url: str) -> dict:
        prompt = f"""
        以下のテキストから料理レシピの情報を抽出し、JSON形式で出力してください。
        {{
            "title": "料理名",
            "tags": ["メイン食材", "ジャンル", "特性"],
            "ingredients": ["材料1 分量", "材料2 分量"],
            "instructions": ["手順1", "手順2"],
            "description": "簡単な説明",
            "image_url": "画像URL(あれば)"
        }}
        --- テキスト ---
        {text[:15000]}
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            cleaned_text = response.text.strip().replace("```json", "").replace("```", "")
            data = json.loads(cleaned_text)
            data['source'] = url
            data['created_at'] = datetime.datetime.now(JST).isoformat()
            return data
        except Exception as e:
            logging.error(f"AI extraction failed: {e}")
            return None

    async def _save_recipe_to_obsidian(self, data: dict) -> str:
        now = datetime.datetime.now(JST)
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", data['title'])
        filename = f"{safe_title}.md"
        file_path = f"{self.dropbox_vault_path}/Recipes/{filename}"

        tags_str = json.dumps(data['tags'], ensure_ascii=False)
        ing_str = json.dumps(data['ingredients'], ensure_ascii=False)
        
        content = f"""---
title: "{data['title']}"
tags: {tags_str}
ingredients: {ing_str}
source: "{data['source']}"
created: "{now.strftime('%Y-%m-%d')}"
cover: "{data.get('image_url', '')}"
---
# {data['title']}

## 概要
{data.get('description', '')}

## 材料
{chr(10).join([f"- {i}" for i in data['ingredients']])}

## 作り方
{chr(10).join([f"{idx+1}. {step}" for idx, step in enumerate(data['instructions'])])}

---
[Source]({data['source']})
"""
        await asyncio.to_thread(
            self.dbx.files_upload, content.encode('utf-8'), file_path, mode=WriteMode('overwrite')
        )
        return filename

    async def _update_recipe_index(self, data: dict, filename: str):
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, RECIPE_INDEX_PATH)
                index = json.loads(res.content.decode('utf-8'))
            except (ApiError, json.JSONDecodeError):
                index = []

            new_entry = {
                "title": data['title'],
                "tags": data['tags'],
                "filename": filename,
                "source": data['source'],
                "ingredients": data['ingredients'],
                "added_at": data['created_at']
            }
            index = [item for item in index if item['source'] != data['source']]
            index.insert(0, new_entry)

            await asyncio.to_thread(
                self.dbx.files_upload,
                json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'),
                RECIPE_INDEX_PATH,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"Failed to update recipe index: {e}")

    @app_commands.command(name="recipes", description="保存済みのレシピ一覧を表示します。")
    @app_commands.describe(query="検索キーワード")
    async def recipes_command(self, interaction: discord.Interaction, query: str = None):
        await interaction.response.defer(ephemeral=True)
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, RECIPE_INDEX_PATH)
            all_recipes = json.loads(res.content.decode('utf-8'))
        except Exception:
            await interaction.followup.send("❌ レシピデータの読み込みに失敗しました。", ephemeral=True)
            return

        filtered_recipes = all_recipes
        if query:
            q = query.lower()
            filtered_recipes = [
                r for r in all_recipes 
                if q in r['title'].lower() 
                or any(q in t.lower() for t in r.get('tags', []))
                or any(q in i.lower() for i in r.get('ingredients', []))
            ]

        if not filtered_recipes:
            await interaction.followup.send(f"🍳 該当なし: `{query}`", ephemeral=True)
            return

        view = RecipeListView(self, filtered_recipes)
        embed = await self.create_recipe_list_embed(filtered_recipes, 0)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def create_recipe_list_embed(self, recipes, page):
        items_per_page = 10
        start = page * items_per_page
        end = start + items_per_page
        current_items = recipes[start:end]
        total_pages = (len(recipes) - 1) // items_per_page + 1

        embed = discord.Embed(title="🍳 保存済みレシピ一覧", color=discord.Color.orange())
        desc = ""
        for i, recipe in enumerate(current_items):
            global_index = start + i + 1
            tags = " ".join([f"`{t}`" for t in recipe.get('tags', [])[:3]])
            desc += f"**{global_index}. {recipe['title']}**\n{tags}\n\n"
        
        embed.description = desc
        embed.set_footer(text=f"Page {page + 1}/{total_pages} | 全 {len(recipes)} 件")
        return embed

    def create_recipe_detail_embed(self, recipe):
        embed = discord.Embed(title=f"🍽️ {recipe['title']}", url=recipe['source'], color=discord.Color.green())
        ingredients = recipe.get('ingredients', [])
        ing_text = "\n".join([f"• {i}" for i in ingredients[:15]])
        if len(ingredients) > 15: ing_text += "\n..."
        embed.add_field(name="🛒 材料", value=ing_text, inline=False)
        embed.add_field(name="🏷️ タグ", value=", ".join(recipe.get('tags', [])), inline=False)
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(RecipeCog(bot))