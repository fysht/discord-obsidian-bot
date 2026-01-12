import discord
from discord.ext import commands
import os
import aiohttp
from datetime import datetime
import google.generativeai as genai
import asyncio

# Gemini APIの設定
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# カテゴリ定義（短縮版マーカー）
CATEGORY_MAP = {
    "ZT": {"file": "ZeroSecondThinking.md", "name": "ゼロ秒思考"},
    "ST": {"file": "Study.md", "name": "勉強メモ"},
    "EN": {"file": "English.md", "name": "英語学習"},
    "IV": {"file": "Investment.md", "name": "投資メモ(全般)"},
    "BK": {"file": None, "name": "読書メモ"},       # BookCogへ委譲
    "KB": {"file": None, "name": "個別銘柄メモ"},   # StockCogへ委譲
}

class CategorySelectView(discord.ui.View):
    """AIが判定に迷った場合や、書籍・銘柄選択のためのView"""
    def __init__(self, cog, image_filename, image_path, category, user_id):
        super().__init__(timeout=180)
        self.cog = cog
        self.image_filename = image_filename
        self.image_path = image_path 
        self.category = category
        self.user_id = user_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.select(
        placeholder="追加先を選択...",
        min_values=1, max_values=1,
        options=[discord.SelectOption(label="読み込み中...", value="loading")]
    )
    async def select_item(self, interaction: discord.Interaction, select: discord.ui.Select):
        selected_val = select.values[0]
        await interaction.response.defer()
        
        target_name = "読書メモ" if self.category == "BK" else "銘柄メモ"
        
        # 選択されたノートに画像を追記
        success = await self.cog.append_image_to_target_note(
            selected_val,
            self.image_filename,
            target_name
        )
        
        if success:
            await interaction.followup.send(f"✅ `{os.path.basename(selected_val)}` にメモを追加しました。")
        else:
            await interaction.followup.send("❌ 保存に失敗しました。")

        self.stop()
        if self.message:
            await self.message.edit(view=None)

class HandwrittenMemo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # --- 設定エリア ---
        self.OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Path\To\Your\Obsidian\Vault")
        
        # 画像保存先フォルダ
        self.ATTACHMENT_FOLDER = "99_Attachments"
        
        # 各専用ノートの保存先親フォルダ（Vault直下なら空文字 "" にしてください）
        self.NOTE_PARENT_FOLDER = "00_Log" 

    def get_full_path(self, folder, filename):
        path = os.path.join(self.OBSIDIAN_VAULT_PATH, folder)
        os.makedirs(path, exist_ok=True)
        return os.path.join(path, filename)

    async def detect_marker(self, image_bytes):
        """
        Gemini APIを使用して、画像内の短い識別マーカーを特定する
        """
        try:
            model = genai.GenerativeModel('gemini-2.5-pro')
            image_parts = [{"mime_type": "image/jpeg", "data": image_bytes}]
            
            # 短いマーカーを正確に拾うためのプロンプト
            prompt = (
                "この手書きメモの画像を分析し、分類用の「識別マーカー（2文字のアルファベット）」を探してください。\n\n"
                "**対象マーカーと意味:**\n"
                "- ZT : ゼロ秒思考\n"
                "- ST : 勉強 (Study)\n"
                "- EN : 英語 (English)\n"
                "- IV : 投資 (Invest)\n"
                "- BK : 本・読書 (Book)\n"
                "- KB : 株・銘柄 (Kabu)\n\n"
                "**判定ルール:**\n"
                "1. これらのマーカーは、通常、ページの隅やタイトルの横に**独立して**書かれています（丸で囲まれていることもあります）。\n"
                "2. 文章の中にある単語の一部（例: 'Best'の中の'st'）は無視してください。「分類ラベル」として意図的に書かれたものだけを抽出してください。\n"
                "3. 見つかった場合、そのコード（'ZT'など）のみを返してください。\n"
                "4. 見つからない場合は 'NONE' と返してください。"
            )

            response = await model.generate_content_async([prompt, image_parts[0]])
            result_text = response.text.strip().upper()
            
            # クリーニングとマッチング
            # AIが余計な解説をつけてきた場合に対応するため、キーワードが含まれているか確認
            for key in CATEGORY_MAP.keys():
                # 「ZTです」のような回答や「Marker: ZT」のような回答にも対応
                if key == result_text or f" {key} " in f" {result_text} " or result_text.startswith(key):
                    return key
            return "NONE"

        except Exception as e:
            print(f"OCR Error: {e}")
            return "NONE"

    async def append_image_to_target_note(self, target_file_path, image_filename, header_label):
        """指定されたMarkdownファイルに画像のリンクを追記する"""
        try:
            # Dropbox使用環境判定
            use_dropbox = False
            dbx = None
            stock_cog = self.bot.get_cog("StockCog")
            if stock_cog and hasattr(stock_cog, "dbx") and stock_cog.dbx:
                dbx = stock_cog.dbx
                use_dropbox = True

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
            link_text = f"\n\n## {timestamp} {header_label}\n![[{self.ATTACHMENT_FOLDER}/{image_filename}]]\n"

            if use_dropbox:
                # Dropbox操作
                from dropbox.files import WriteMode
                try:
                    _, res = await asyncio.to_thread(dbx.files_download, target_file_path)
                    content = res.content.decode('utf-8')
                except:
                    # 新規作成
                    content = f"# {os.path.basename(target_file_path).replace('.md', '')}\n"
                
                content += link_text
                await asyncio.to_thread(dbx.files_upload, content.encode('utf-8'), target_file_path, mode=WriteMode('overwrite'))
            else:
                # ローカル操作
                if not os.path.exists(target_file_path):
                     with open(target_file_path, 'w', encoding='utf-8') as f:
                        f.write(f"# {os.path.basename(target_file_path).replace('.md', '')}\n")
                
                with open(target_file_path, 'a', encoding='utf-8') as f:
                    f.write(link_text)
            
            return True
        except Exception as e:
            print(f"Append Error: {e}")
            return False

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image'):
                    await self.process_scanned_image(message, attachment)

    async def process_scanned_image(self, message, attachment):
        """スキャン画像をダウンロードし、マーカー判定に基づいて処理を行う"""
        
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as resp:
                if resp.status != 200: return
                image_bytes = await resp.read()

        processing_msg = await message.channel.send("🔍 画像を解析中...")
        
        # 1. マーカー判定
        marker = await self.detect_marker(image_bytes)
        
        # 2. 画像の保存
        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        original_name, ext = os.path.splitext(attachment.filename)
        # ファイル名にマーカーを含める
        prefix = marker if marker != "NONE" else "Memo"
        image_filename = f"{prefix}_{timestamp_str}{ext}"
        
        # Dropboxかローカルかで保存先を切り替え
        use_dropbox = False
        dbx = None
        stock_cog = self.bot.get_cog("StockCog")
        
        if stock_cog and hasattr(stock_cog, "dbx") and stock_cog.dbx:
            dbx = stock_cog.dbx
            dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
            save_folder = f"{dropbox_vault_path}/{self.ATTACHMENT_FOLDER}"
            save_path_full = f"{save_folder}/{image_filename}"
            use_dropbox = True
            try:
                from dropbox.files import WriteMode
                await asyncio.to_thread(dbx.files_upload, image_bytes, save_path_full, mode=WriteMode('add'))
            except Exception as e:
                await processing_msg.edit(content=f"❌ 画像保存エラー(Dropbox): {e}")
                return
        else:
            save_path_full = self.get_full_path(self.ATTACHMENT_FOLDER, image_filename)
            with open(save_path_full, 'wb') as f:
                f.write(image_bytes)

        # 3. 振り分け処理
        info = CATEGORY_MAP.get(marker)
        
        if marker == "NONE":
            # Inboxノートなどへの追記が必要であればここに記述
            # 現在は保存通知のみ
            await processing_msg.edit(content=f"📁 **通常メモ** として保存しました (`{image_filename}`)。\n(マーカーなし)")
            return

        if marker in ["BK", "KB"]: # BK:本, KB:株
            # 選択メニューを表示
            view = CategorySelectView(self, image_filename, save_path_full, marker, message.author.id)
            
            options = []
            if marker == "BK":
                book_cog = self.bot.get_cog("BookCog")
                if book_cog:
                    # BookCogの実装に合わせてリスト取得
                    books, _ = await book_cog.get_book_list()
                    options = [discord.SelectOption(label=b.name[:90], value=b.path_display) for b in books[:25]]
            
            elif marker == "KB":
                if stock_cog:
                    # StockCogの実装に合わせてリスト取得
                    stocks = await stock_cog._get_stock_list()
                    options = [discord.SelectOption(label=s.name[:90], value=s.path_display) for s in stocks[:25]]

            if not options:
                await processing_msg.edit(content=f"⚠️ {info['name']}のリストが見つかりません。画像は保存されました。")
                return

            view.children[0].options = options
            view.message = await message.channel.send(f"🤔 **{info['name']}** (Marker: {marker}) を検出。\n保存先のノートを選択してください:", view=view)
            await processing_msg.delete()

        else:
            # 専用ノートへの自動追記 (ZT, ST, EN, IV)
            target_filename = info['file']
            
            if use_dropbox:
                dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
                # フォルダ結合時のスラッシュ重複回避
                base = dropbox_vault_path.rstrip('/')
                parent = self.NOTE_PARENT_FOLDER.strip('/')
                if parent:
                    target_path = f"{base}/{parent}/{target_filename}"
                else:
                    target_path = f"{base}/{target_filename}"
            else:
                target_path = self.get_full_path(self.NOTE_PARENT_FOLDER, target_filename)

            success = await self.append_image_to_target_note(target_path, image_filename, info['name'])
            
            if success:
                await processing_msg.edit(content=f"✅ **{info['name']}** (Marker: {marker}) として `{target_filename}` に保存しました。")
            else:
                await processing_msg.edit(content=f"❌ メモの追加に失敗しました。画像は保存されています。")

async def setup(bot):
    await bot.add_cog(HandwrittenMemo(bot))