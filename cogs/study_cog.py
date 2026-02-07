import os
import discord
from discord.ext import commands
import sqlite3
import random
import logging
from datetime import datetime
# --- 新しいライブラリ ---
from google import genai
# ----------------------

DB_NAME = "study_data.db"

class StudyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # --- Client初期化 ---
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None
        # ------------------
        
        self.init_db()

    def init_db(self):
        with sqlite3.connect(DB_NAME) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS quiz_history
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          question TEXT, answer TEXT, user_answer TEXT,
                          is_correct INTEGER, timestamp TEXT)''')
            conn.commit()

    @commands.command(name="quiz")
    async def generate_quiz(self, ctx, topic: str):
        if not self.gemini_client:
            await ctx.send("API Key Error.")
            return

        async with ctx.typing():
            prompt = f"「{topic}」に関する4択クイズを1問作成してください。\nフォーマット:\nQ: [問題文]\n1. [選択肢1]\n2. [選択肢2]\n3. [選択肢3]\n4. [選択肢4]\nA: [正解番号]"
            try:
                # --- 生成メソッド変更 ---
                response = await self.gemini_client.aio.models.generate_content(
                    model='gemini-2.5-pro',
                    contents=prompt
                )
                text = response.text.strip()
                # ----------------------
                
                # 簡単なパース
                lines = text.split('\n')
                question_text = "\n".join([l for l in lines if not l.startswith('A:')])
                answer_line = next((l for l in lines if l.startswith('A:')), "A: 1")
                correct_answer = answer_line.split(':')[1].strip()

                self.current_quiz = {"correct": correct_answer}
                await ctx.send(f"**Topic: {topic}**\n{question_text}\n\n(回答は `!ans 1` のように送信)")
                
            except Exception as e:
                await ctx.send(f"Error: {e}")

    @commands.command(name="ans")
    async def answer_quiz(self, ctx, num: str):
        if not hasattr(self, 'current_quiz'): return
        
        correct = self.current_quiz["correct"]
        is_correct = (num.strip() == correct)
        
        msg = "✅ 正解！" if is_correct else f"❌ 不正解... 正解は {correct}"
        await ctx.send(msg)
        
        # 履歴保存
        with sqlite3.connect(DB_NAME) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO quiz_history (question, answer, user_answer, is_correct, timestamp) VALUES (?, ?, ?, ?, ?)",
                      ("Last Quiz", correct, num, 1 if is_correct else 0, datetime.now().isoformat()))
            conn.commit()

async def setup(bot):
    await bot.add_cog(StudyCog(bot))