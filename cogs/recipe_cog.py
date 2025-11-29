import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode, DownloadError
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
        if recipe_data.get('source'):
            self.add_item(discord.ui.Button(label="元記事を開く", url=recipe_data['source']))

class RecipeDeleteSelect(discord.ui.Select):
    def __init__(self, cog, recipes):
        self.cog = cog
        self.recipes = recipes
        options = []
        for i, recipe in enumerate(recipes[:25]):
            label = recipe.get('title', '無題')[:90]
            options.append(discord.SelectOption(
                label=f"{i+1}. {label}",
                value=str(i),
                description=recipe.get('added_at', '')[:10]
            ))
        
        super().__init__(placeholder="削除するレシピを選択してください...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        index = int(self.values[0])
        if 0 <= index < len(self.recipes):
            target_recipe = self.recipes[index]
            await self.cog._delete_recipe(target_recipe, interaction)
        else:
            await interaction.followup.send("❌ レシピが見つかりませんでした。", ephemeral=True)

class RecipeDeleteView(discord.ui.View):
    def __init__(self, cog, recipes):
        super().__init__(timeout=60)
        self.add_item(RecipeDeleteSelect(cog, recipes))

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

    @discord.ui.button(label="🗑️ 削除", style=discord.ButtonStyle.danger, row=2)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.recipes:
            await interaction.response.send_message("削除できるレシピがありません。", ephemeral=True)
            return
        await interaction.response.send_message("削除するレシピを選択してください:", view=RecipeDeleteView(self.cog, self.recipes), ephemeral=True)

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
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
            
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
        
        if payload.user_id != self.bot.user.id: return

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
            
            embed_title = message.embeds[0].title if message.embeds else None
            page_title, content_text = await self._fetch_content(url)
            final_title = embed_title or page_title or "Untitled Recipe"

            recipe_data = None
            
            if content_text:
                recipe_data = await self._extract_recipe_data_with_ai(content_text, url, final_title)
            
            if not recipe_data:
                logging.info(f"Recipe extraction failed or content unavailable. Falling back to simple mode for {url}")
                recipe_data = {
                    'title': final_title,
                    'source': url,
                    'created_at': datetime.datetime.now(JST).isoformat(),
                    'is_fallback': True,
                    'is_simple': True,
                    'ingredients': [],
                    'instructions': [],
                    'description': "(Extraction failed)",
                    'tags': ["Uncategorized"]
                }

            filename = await self._save_recipe_to_obsidian(recipe_data)
            await self._update_recipe_index(recipe_data, filename)

            await message.add_reaction(PROCESS_COMPLETE_EMOJI)
            
            embed = discord.Embed(title="🍳 Recipe Saved", description=f"**[{recipe_data['title']}]({url})**", color=discord.Color.green())
            
            if recipe_data.get('is_simple'):
                embed.set_footer(text=f"Saved to: Recipes/{filename} (Simple Mode)")
            elif recipe_data.get('is_fallback'):
                embed.set_footer(text=f"Saved to: Recipes/{filename} (Text Mode)")
            else:
                tags = ", ".join(recipe_data.get('tags', []))
                if tags: embed.add_field(name="🏷️ Tags", value=tags)
                ingredients = recipe_data.get('ingredients', [])
                if ingredients:
                    ing_preview = "\n".join([f"• {i}" for i in ingredients[:5]])
                    if len(ingredients) > 5: ing_preview += "\n..."
                    embed.add_field(name="🛒 Ingredients", value=ing_preview)
                embed.set_footer(text=f"Saved to: Recipes/{filename}")
            
            if recipe_data.get('image_url'):
                embed.set_thumbnail(url=recipe_data['image_url'])
                
            await message.reply(embed=embed)
            
        except Exception as e:
            logging.error(f"Recipe processing error: {e}", exc_info=True)
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except: pass
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except: pass

    async def _fetch_content(self, url: str) -> tuple[str | None, str | None]:
        if parse_url_with_readability:
            try:
                loop = asyncio.get_running_loop()
                title, content = await loop.run_in_executor(None, parse_url_with_readability, url)
                return title, content
            except: pass
        return None, None

    async def _extract_recipe_data_with_ai(self, text: str, url: str, page_title: str) -> dict:
        prompt = f"""
        Extract recipe information from the text below and output ONLY JSON.
        
        Output Format:
        {{
            "title": "Recipe Name",
            "tags": ["Main Ingredient", "Cuisine Type"],
            "ingredients": ["Ingredient1 Amount", "Ingredient2 Amount"],
            "instructions": ["Step 1", "Step 2"],
            "description": "Short description",
            "image_url": "Image URL if available"
        }}
        --- Text ---
        {text[:15000]}
        """
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            raw_text = response.text.strip()
            json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                data = json.loads(json_str)
                data['source'] = url
                data['created_at'] = datetime.datetime.now(JST).isoformat()
                data['is_fallback'] = False
                if not data.get('title') or data['title'] == "Recipe Name":
                    data['title'] = page_title
                return data
        except Exception as e:
            logging.warning(f"JSON extraction failed, trying fallback: {e}")

        logging.info("Switching to fallback text extraction.")
        fallback_prompt = f"""
        Organize the following text into a Markdown recipe note for Obsidian.
        No JSON needed. Use the following headers:
        
        # {page_title}
        ## Overview
        (Short description)
        ## Ingredients
        (Bulleted list)
        ## Instructions
        (Numbered list)
        
        --- Text ---
        {text[:15000]}
        """
        try:
            response = await self.gemini_model.generate_content_async(fallback_prompt)
            return {
                'title': page_title,
                'source': url,
                'created_at': datetime.datetime.now(JST).isoformat(),
                'is_fallback': True,
                'markdown_content': response.text.strip()
            }
        except Exception as e:
            logging.error(f"Fallback extraction failed: {e}")
            return None

    async def _save_recipe_to_obsidian(self, data: dict) -> str:
        now = datetime.datetime.now(JST)
        timestamp = now.strftime('%Y%m%d%H%M%S')
        title = data.get("title", "Untitled")
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        filename = f"{timestamp}-{safe_title}.md"
        file_path = f"{self.dropbox_vault_path}/Recipes/{filename}"
        daily_note_date = now.strftime('%Y-%m-%d')

        if data.get('is_simple'):
            tags_str = json.dumps(data.get('tags', []), ensure_ascii=False)
            content = f"""---
title: "{title}"
tags: {tags_str}
source: "{data['source']}"
created: "{daily_note_date}"
---
# {title}
- **Source:** {data['source']}
- **Created:** [[{daily_note_date}]]

> [!info]
> Automatic extraction failed.
"""

        elif data.get('is_fallback'):
            content = f"""{data['markdown_content']}
---
- **Source:** {data['source']}
- **Created:** [[{daily_note_date}]]
"""
        else:
            tags_str = json.dumps(data.get('tags', []), ensure_ascii=False)
            content = f"""---
title: "{title}"
tags: {tags_str}
source: "{data['source']}"
created: "{daily_note_date}"
cover: "{data.get('image_url', '')}"
---
# {title}
## Overview
{data.get('description', '')}
## Ingredients
{chr(10).join([f"- {i}" for i in data.get('ingredients', [])])}
## Instructions
{chr(10).join([f"{idx+1}. {step}" for idx, step in enumerate(data.get('instructions', []))])}
---
[Source]({data['source']})
[[{daily_note_date}]]
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

            index = [item for item in index if item.get('filename') != filename]
            
            new_entry = {
                "title": data['title'],
                "tags": data.get('tags', []),
                "filename": filename,
                "source": data['source'],
                "ingredients": data.get('ingredients', []),
                "added_at": data['created_at']
            }
            index.insert(0, new_entry)

            await asyncio.to_thread(
                self.dbx.files_upload,
                json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'),
                RECIPE_INDEX_PATH,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"Failed to update recipe index: {e}")

    async def _delete_recipe(self, recipe_entry: dict, interaction: discord.Interaction):
        filename = recipe_entry.get('filename')
        if not filename:
            await interaction.followup.send("❌ Filename missing.", ephemeral=True)
            return
            
        file_path = f"{self.dropbox_vault_path}/Recipes/{filename}"
        
        try:
            await asyncio.to_thread(self.dbx.files_delete_v2, file_path)
            
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, RECIPE_INDEX_PATH)
                index = json.loads(res.content.decode('utf-8'))
                new_index = [r for r in index if r.get('filename') != filename]
                
                await asyncio.to_thread(
                    self.dbx.files_upload,
                    json.dumps(new_index, ensure_ascii=False, indent=2).encode('utf-8'),
                    RECIPE_INDEX_PATH,
                    mode=WriteMode('overwrite')
                )
            except Exception as e_idx:
                logging.error(f"Index update error: {e_idx}")
            
            await interaction.followup.send(f"🗑️ Deleted recipe: {recipe_entry.get('title')}", ephemeral=True)
            
        except ApiError as e:
            if isinstance(e.error, dropbox.files.DeleteError) and e.error.is_path_lookup() and e.error.get_path_lookup().is_not_found():
                 await interaction.followup.send("⚠️ File not found, but removed from index.", ephemeral=True)
            else:
                logging.error(f"Delete error: {e}")
                await interaction.followup.send("❌ Error deleting recipe.", ephemeral=True)

    @app_commands.command(name="recipes", description="Show saved recipes.")
    @app_commands.describe(query="Search keyword")
    async def recipes_command(self, interaction: discord.Interaction, query: str = None):
        await interaction.response.defer(ephemeral=True)
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, RECIPE_INDEX_PATH)
            all_recipes = json.loads(res.content.decode('utf-8'))
        except Exception as e:
            if isinstance(e, ApiError) and isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                 await interaction.followup.send("📂 No recipes saved yet.", ephemeral=True)
            else:
                 logging.error(f"Recipe load error: {e}")
                 await interaction.followup.send("❌ Failed to load recipes.", ephemeral=True)
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
            await interaction.followup.send(f"🍳 No matches for: `{query}`", ephemeral=True)
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

        embed = discord.Embed(title="🍳 Recipe List", color=discord.Color.orange())
        desc = ""
        for i, recipe in enumerate(current_items):
            global_index = start + i + 1
            tags = " ".join([f"`{t}`" for t in recipe.get('tags', [])[:3]])
            desc += f"**{global_index}. {recipe['title']}**\n{tags}\n\n"
        
        embed.description = desc
        embed.set_footer(text=f"Page {page + 1}/{total_pages} | Total {len(recipes)}")
        return embed

    def create_recipe_detail_embed(self, recipe):
        embed = discord.Embed(title=f"🍽️ {recipe['title']}", url=recipe['source'], color=discord.Color.green())
        ingredients = recipe.get('ingredients', [])
        if ingredients:
            ing_text = "\n".join([f"• {i}" for i in ingredients[:15]])
            if len(ingredients) > 15: ing_text += "\n..."
            embed.add_field(name="🛒 Ingredients", value=ing_text, inline=False)
        
        tags = recipe.get('tags', [])
        if tags:
            embed.add_field(name="🏷️ Tags", value=", ".join(tags), inline=False)
        
        embed.set_footer(text=f"Added: {recipe.get('added_at', '')[:10]}")
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(RecipeCog(bot))