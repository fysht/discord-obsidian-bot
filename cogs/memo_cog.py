import os
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import logging
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
from datetime import datetime, timezone, timedelta
import json
import re
import aiohttp 

# --- 共通処理インポート ---
from obsidian_handler import add_memo_async
from web_parser import parse_url_with_readability

# --- 定数定義 ---
try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
    JST = timezone(timedelta(hours=+9), "JST")

# --- チャンネルID ---
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))

# --- リアクション絵文字 ---
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
PROCESS_FETCHING_EMOJI = '⏱️' 

# ピン留めニュース機能用
PINNED_NEWS_REACTION = '📰'
PINNED_NEWS_JSON_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/pinned_news_memos.json"

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')


# --- ピン留め削除用View ---
class PinnedListDeleteView(discord.ui.View):
    def __init__(self, cog, pinned_memos):
        super().__init__(timeout=300)
        self.cog = cog
        
        # セレクトメニューのオプションを作成 (最新25件まで)
        # DiscordのSelectメニューの上限が25件のため
        options = []
        for memo in list(reversed(pinned_memos))[:25]:
            msg_id = memo.get('id')
            content = memo.get('content', '内容なし').replace('\n', ' ')
            
            # 表示用ラベルの作成 (日付 + 内容の抜粋)
            date_str = memo.get('pinned_at', '')
            try:
                dt = datetime.fromisoformat(date_str)
                date_disp = dt.strftime('%m/%d %H:%M')
            except:
                date_disp = "??"
            
            label = f"{date_disp}: {content[:20]}"
            description = content[:50] + "..." if len(content) > 50 else content
            
            options.append(discord.SelectOption(
                label=label,
                value=msg_id,
                description=description
            ))

        if not options:
            self.add_item(discord.ui.Select(
                placeholder="ピン留めされたメモはありません",
                disabled=True,
                options=[discord.SelectOption(label="none", value="none")]
            ))
        else:
            select = discord.ui.Select(
                placeholder="削除するメモを選択してください (複数可)",
                min_values=1,
                max_values=len(options),
                options=options
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected_ids = interaction.data["values"]
        
        if not selected_ids:
            return

        async with self.cog.pinned_news_lock:
            try:
                # 現在のリストを再取得
                current_list = await self.cog._get_pinned_news()
                initial_count = len(current_list)
                
                # 選択されたIDを除外
                new_list = [m for m in current_list if m.get('id') not in selected_ids]
                
                if len(new_list) < initial_count:
                    await self.cog._save_pinned_news(new_list)
                    deleted_count = initial_count - len(new_list)
                    await interaction.followup.send(f"✅ {deleted_count} 件のメモをピン留めから削除しました。", ephemeral=True)
                    
                    # 元のメッセージからリアクションを削除する試み (視覚的な同期のため)
                    channel = self.cog.bot.get_channel(MEMO_CHANNEL_ID)
                    if channel:
                        for msg_id in selected_ids:
                            try:
                                msg = await channel.fetch_message(int(msg_id))
                                # ユーザー自身のリアクションを消すのは権限的に難しい場合があるため、
                                # Botが付けたリアクションがあれば消す、あるいはゴミ箱リアクションを一瞬つけて消す
                                await msg.remove_reaction(PINNED_NEWS_REACTION, interaction.user)
                            except Exception:
                                pass # メッセージが見つからない、権限がない等は無視
                else:
                    await interaction.followup.send("⚠️ 削除対象が見つかりませんでした（既に削除されている可能性があります）。", ephemeral=True)
            
            except Exception as e:
                logging.error(f"ピン留め削除中にエラー: {e}", exc_info=True)
                await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)


# Cog Class
class MemoCog(commands.Cog):
    """
    Discordの#memoチャンネルを監視し、テキストメモを保存するCog
    (備忘録保存機能 + ピン留めニュース機能)
    """
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession() 
        
        # Dropboxクライアントの初期化 (ニュースピン留め機能用)
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dbx = None
        self.pinned_news_lock = asyncio.Lock() # JSONファイルのRMW操作を保護

        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
                self.dbx.users_get_current_account() # 接続テスト
                logging.info("MemoCog: Dropboxクライアント (ピン留めニュース用) が正常に初期化されました。")
            except Exception as e:
                logging.error(f"MemoCog: Dropboxクライアントの初期化に失敗: {e}")
                self.dbx = None
        else:
            logging.warning("MemoCog: Dropbox認証情報が不足しているため、ピン留めニュース機能(📰)は無効です。")

        logging.info("MemoCog: Initialized.")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # ピン留めニュースJSONをDropboxから取得
    async def _get_pinned_news(self) -> list:
        """Dropboxからピン留めニュースのリストを取得する"""
        if not self.dbx: return []
        try:
            _, res = self.dbx.files_download(PINNED_NEWS_JSON_PATH)
            data = json.loads(res.content.decode('utf-8'))
            return data if isinstance(data, list) else []
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.info(f"ピン留めニュースファイル ({PINNED_NEWS_JSON_PATH}) が見つかりません。新規作成します。")
                return []
            logging.error(f"ピン留めニュースの読み込みに失敗: {e}")
            return []
        except (json.JSONDecodeError, Exception) as e:
            logging.error(f"ピン留めニュースの解析に失敗: {e}")
            return []

    # ピン留めニュースJSONをDropboxに保存
    async def _save_pinned_news(self, pinned_list: list):
        """Dropboxにピン留めニュースのリストを保存する"""
        if not self.dbx: return
        try:
            content = json.dumps(pinned_list, ensure_ascii=False, indent=2).encode('utf-8')
            self.dbx.files_upload(content, PINNED_NEWS_JSON_PATH, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"ピン留めニュースの保存に失敗: {e}")

    # --- ピン留めリスト表示コマンド (削除機能付き) ---
    @app_commands.command(name="pinned_list", description="ピン留め中のメモ一覧を表示・削除します。")
    async def pinned_list(self, interaction: discord.Interaction):
        if interaction.channel_id != MEMO_CHANNEL_ID:
             await interaction.response.send_message(f"このコマンドは <#{MEMO_CHANNEL_ID}> でのみ実行できます。", ephemeral=True)
             return
        
        await interaction.response.defer(ephemeral=True)
        
        # 最新のデータを取得
        pinned_memos = await self._get_pinned_news()
        
        if not pinned_memos:
            await interaction.followup.send("📌 現在ピン留めされているメモはありません。", ephemeral=True)
            return
            
        # 埋め込みメッセージの作成
        embed = discord.Embed(title="📌 ピン留めメモ一覧", description="削除したいメモは下のメニューから選択してください。", color=discord.Color.gold())
        
        # 最新10件を表示
        for i, memo in enumerate(reversed(pinned_memos)):
            if i >= 10: break
            content = memo.get('content', '')
            short_content = (content[:60] + '...') if len(content) > 60 else content
            msg_id = memo.get('id')
            msg_link = f"https://discord.com/channels/{interaction.guild_id}/{MEMO_CHANNEL_ID}/{msg_id}"
            
            date_str = memo.get('pinned_at', '')
            try:
                dt = datetime.fromisoformat(date_str)
                date_display = dt.strftime('%Y/%m/%d %H:%M')
            except:
                date_display = "日時不明"

            embed.add_field(
                name=f"{i+1}. {date_display}",
                value=f"{short_content}\n[メッセージへ移動]({msg_link})",
                inline=False
            )
        
        if len(pinned_memos) > 10:
            embed.set_footer(text=f"他 {len(pinned_memos) - 10} 件... (メニューからは25件まで選択可能)")

        # 削除用Viewを付与して送信
        view = PinnedListDeleteView(self, pinned_memos)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """#memo チャンネルに投稿されたメッセージを備忘録として保存"""
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return

        content = message.content.strip()
        if not content:
            return

        url_match = URL_REGEX.search(content)
        
        if url_match:
            logging.info(f"URL detected in message {message.id}. Saving as simple bookmark memo.")
            try:
                await message.add_reaction(PROCESS_FETCHING_EMOJI) 
            except discord.HTTPException: pass

            url_from_content = url_match.group(0) 
            url_to_save = url_from_content      
            title = "タイトル不明"               
            
            try:
                # --- Discord Embedの待機と取得 ---
                logging.info(f"Waiting 7s for Discord embed for {url_from_content}...")
                await asyncio.sleep(7) 
                
                full_url_from_embed = None
                title_from_embed = None
                
                try:
                    fetched_message = await message.channel.fetch_message(message.id)
                    if fetched_message.embeds:
                        embed = fetched_message.embeds[0]
                        if embed.url:
                            full_url_from_embed = embed.url
                        if embed.title:
                            title_from_embed = embed.title
                except (discord.NotFound, discord.Forbidden) as e:
                     logging.warning(f"Failed to re-fetch message {message.id} for embed: {e}")
                
                # --- 保存するURLとタイトルの決定 ---
                if full_url_from_embed:
                    url_to_save = full_url_from_embed
                
                if title_from_embed and "http" not in title_from_embed:
                    title = title_from_embed
                else:
                    logging.info(f"Embed title unusable. Falling back to web_parser for {url_to_save}...")
                    loop = asyncio.get_running_loop()
                    parsed_title, _ = await loop.run_in_executor(
                        None, parse_url_with_readability, url_to_save
                    )
                    if parsed_title and parsed_title != "No Title Found":
                        title = parsed_title
                    else:
                         if title_from_embed:
                             title = title_from_embed

                memo_content_to_save = f"{title}\n{url_to_save}"

                await add_memo_async(
                    content=memo_content_to_save,
                    author=str(message.author),
                    created_at=message.created_at.isoformat(),
                    message_id=message.id,
                    context="Discord Memo Channel (URL Bookmark)", 
                    category="Memo" 
                )
                
                await message.remove_reaction(PROCESS_FETCHING_EMOJI, self.bot.user)
                await message.add_reaction(PROCESS_COMPLETE_EMOJI) 
                logging.info(f"Successfully saved URL bookmark (ID: {message.id}), Title: {title}")
            
            except Exception as e:
                logging.error(f"Failed to parse URL title or save bookmark (ID: {message.id}): {e}", exc_info=True)
                try:
                    await message.remove_reaction(PROCESS_FETCHING_EMOJI, self.bot.user)
                    await message.add_reaction(PROCESS_ERROR_EMOJI)
                except discord.HTTPException: pass
            
        else:
            # URLが含まれない場合（通常のテキストメモ）
            logging.info(f"Text memo detected in message {message.id}. Saving via obsidian_handler.")
            try:
                await add_memo_async(
                    content=content,
                    author=str(message.author),
                    created_at=message.created_at.isoformat(), 
                    message_id=message.id,
                    context="Discord Memo Channel", 
                    category="Memo" 
                )
                await message.add_reaction(PROCESS_COMPLETE_EMOJI) 
            except Exception as e:
                logging.error(f"Failed to save text memo (ID: {message.id}) using add_memo_async: {e}", exc_info=True)
                await message.add_reaction(PROCESS_ERROR_EMOJI)

    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """ユーザーが付けたリアクションに応じて処理を分岐 (現在はピン留めのみ)"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        emoji = str(payload.emoji)

        # ピン留めのみ監視
        if emoji != PINNED_NEWS_REACTION:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"元のメッセージ {payload.message_id} の取得に失敗しました。")
            return

        # ユーザーリアクションはすぐに削除
        try:
            user = await self.bot.fetch_user(payload.user_id)
            if user:
                await message.remove_reaction(payload.emoji, user)
        except discord.HTTPException:
            pass

        # --- 📰 (ピン留めニュース) 処理 ---
        if emoji == PINNED_NEWS_REACTION:
            if not self.dbx:
                logging.warning(f"ピン留めリアクション (📰) が押されましたが、Dropboxが未初期化のためスキップします (Msg: {message.id})")
                await message.add_reaction(PROCESS_ERROR_EMOJI)
                await asyncio.sleep(3)
                await message.remove_reaction(PROCESS_ERROR_EMOJI, self.bot.user)
                return

            async with self.pinned_news_lock:
                try:
                    pinned_list = await self._get_pinned_news()
                    
                    # 既に存在するかチェック
                    if any(item.get("id") == str(message.id) for item in pinned_list):
                        logging.warning(f"メッセージ {message.id} は既にピン留めされています。")
                        return

                    new_pin = {
                        "id": str(message.id),
                        "content": message.content,
                        "author": str(message.author),
                        "pinned_at": datetime.now(JST).isoformat()
                    }
                    pinned_list.append(new_pin)
                    await self._save_pinned_news(pinned_list)
                    
                    await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    logging.info(f"メッセージ {message.id} をピン留めニュースとして保存しました。")
                
                except Exception as e:
                    logging.error(f"ピン留めニュースの保存中にエラー: {e}", exc_info=True)
                    await message.add_reaction(PROCESS_ERROR_EMOJI)

    # ピン留め解除 (リアクション削除) の監視
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """ユーザーが 📰 リアクションを削除した際の処理 (ピン留め解除)"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return
        
        if str(payload.emoji) != PINNED_NEWS_REACTION:
            return
        
        if not self.dbx: return
            
        logging.info(f"ピン留め解除リアクションを検知 (Msg: {payload.message_id})。")

        async with self.pinned_news_lock:
            try:
                pinned_list = await self._get_pinned_news()
                message_id_to_remove = str(payload.message_id)
                
                initial_count = len(pinned_list)
                filtered_list = [item for item in pinned_list if item.get("id") != message_id_to_remove]
                
                if len(filtered_list) < initial_count:
                    await self._save_pinned_news(filtered_list)
                    logging.info(f"メッセージ {message_id_to_remove} をピン留めニュースから削除しました。")
                    
                    # 元メッセージに一時的にリアクション
                    try:
                        channel = self.bot.get_channel(payload.channel_id)
                        if channel:
                            message = await channel.fetch_message(payload.message_id)
                            await message.add_reaction("🗑️")
                            await asyncio.sleep(5)
                            await message.remove_reaction("🗑️", self.bot.user)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                        logging.warning(f"ピン留め解除のリアクション操作に失敗: {e}")
            
            except Exception as e:
                logging.error(f"ピン留めニュースの削除中にエラー: {e}", exc_info=True)


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    await bot.add_cog(MemoCog(bot))