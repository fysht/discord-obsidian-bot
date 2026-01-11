import discord
from discord.ext import commands
import os
from datetime import datetime
import google.generativeai as genai

# Gemini APIの設定
# handwritten_memo_cog.py と同じAPIキーを使用してください
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
genai.configure(api_key=GEMINI_API_KEY)

class ZeroSecondThinking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.OBSIDIAN_VAULT_PATH = r"C:\Path\To\Your\Obsidian\Vault"
        self.ZT_FOLDER_NAME = "00_ZeroSecondThinking"

    def get_save_path(self, filename):
        folder_path = os.path.join(self.OBSIDIAN_VAULT_PATH, self.ZT_FOLDER_NAME)
        os.makedirs(folder_path, exist_ok=True)
        return os.path.join(folder_path, filename)

    async def generate_zt_themes(self, keyword=None):
        """
        Gemini APIを使用して、ゼロ秒思考のテーマ（タイトル）を生成する
        """
        try:
            model = genai.GenerativeModel('gemini-2.5-pro')
            
            if keyword:
                user_intent = f"「{keyword}」というキーワードに関連して"
            else:
                user_intent = "今、何を書くべきか迷っている状態に対して、頭の中を整理するために"

            # ゼロ秒思考のメソッドに基づいたプロンプト
            prompt = (
                f"あなたは『ゼロ秒思考（赤羽雄二氏提唱）』のメモ書きファシリテーターです。\n"
                f"ユーザーは{user_intent}、1分間で書き出すためのメモのタイトル（テーマ）を求めています。\n"
                "ユーザーの思考を深掘りし、感情や課題を吐き出させるような、具体的で刺激的なタイトルを5つ提案してください。\n\n"
                "**条件:**\n"
                "1. タイトルは疑問形（～はなぜか？、～をどうするか？など）を中心にする。\n"
                "2. 抽象的な言葉だけでなく、具体的で少しドキッとするような切り口も含める。\n"
                "3. 箇条書きで出力する。\n"
                "4. 余計な挨拶は省略し、テーマ案だけを出力する。"
            )

            response = await model.generate_content_async(prompt)
            return response.text.strip()

        except Exception as e:
            print(f"Gemini API Error: {e}")
            return "（AI生成エラー）申し訳ありません。現在テーマを生成できません。手動で設定してください。"

    # --- テーマ設定サポート ---
    @commands.command(name='zt_theme', aliases=['theme'])
    async def suggest_theme(self, ctx, *, text=None):
        """
        ゼロ秒思考のテーマ出しをAIがサポート
        使用例: 
          !zt_theme (完全にランダムなお題)
          !zt_theme 将来の不安 (指定キーワードに関連するお題)
        """
        async with ctx.typing():  # 生成中の「入力中...」表示
            suggestions = await self.generate_zt_themes(text)
        
        header = f"💡 **「{text if text else 'おまかせ'}」に関するゼロ秒思考テーマ案**"
        message = f"{header}\n\n{suggestions}\n\n*気になったものを1つ選んで、1分間で書き殴ってみましょう！*"
        
        await ctx.send(message)

    # --- デジタル入力 ---
    @commands.command(name='zt')
    async def digital_zt(self, ctx, *, content):
        """デジタルテキストでのゼロ秒思考"""
        date_str = datetime.now().strftime('%Y-%m-%d')
        filename = f"{date_str}_ZeroSecondThinking.md"
        save_path = self.get_save_path(filename)
        
        entry = f"\n\n## {datetime.now().strftime('%H:%M')} (Digital)\n{content}\n"
        
        with open(save_path, 'a', encoding='utf-8') as f:
            f.write(entry)
        
        await ctx.message.add_reaction('✅')

async def setup(bot):
    await bot.add_cog(ZeroSecondThinking(bot))