import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import csv
import json
import io
import os
import random
import asyncio
import google.generativeai as genai
from datetime import datetime
import uuid
import logging # 追加

# ==========================================
# 設定・定数
# ==========================================
DB_NAME = 'study_bot.db'
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

TITLES = [
    (1, "受験生"), (5, "補助者見習い"), (10, "ベテラン補助者"),
    (20, "司法書士の卵"), (30, "新人司法書士"), (50, "中堅司法書士"),
    (70, "特定司法書士"), (100, "登記の神")
]

# ==========================================
# Cogクラス定義
# ==========================================
class StudyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("STUDY_CHANNEL_ID", 0)) # 追加: チャンネルID読み込み
        self.init_db()

    def init_db(self):
        """データベース初期化"""
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        # 問題マスタ (HTML版との同期のためUUIDを使用)
        c.execute('''CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT UNIQUE,
            category_uuid TEXT,
            category_name TEXT,
            question TEXT,
            answer TEXT,
            explanation TEXT,
            point TEXT
        )''')
        
        # ユーザー進捗
        # Note: user_idごとに進捗を管理。question_uuidで紐づけ
        c.execute('''CREATE TABLE IF NOT EXISTS user_progress (
            user_id INTEGER,
            question_uuid TEXT,
            solved_count INTEGER DEFAULT 0,
            correct_count INTEGER DEFAULT 0,
            needs_review INTEGER DEFAULT 0,
            memo TEXT DEFAULT '',
            last_result TEXT, 
            PRIMARY KEY (user_id, question_uuid)
        )''')
        
        # ユーザープロフィール
        c.execute('''CREATE TABLE IF NOT EXISTS user_profile (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            max_combo INTEGER DEFAULT 0,
            current_combo INTEGER DEFAULT 0,
            title TEXT DEFAULT '受験生'
        )''')
        conn.commit()
        conn.close()

    # --- ヘルパー関数 ---
    
    async def ask_gemini(self, prompt):
        """Gemini APIへの問い合わせ"""
        if not GEMINI_API_KEY:
            return "⚠️ APIキーが設定されていません。"
        try:
            model = genai.GenerativeModel("gemini-2.5-pro")
            # ブロッキング防止のためexecutorで実行
            response = await asyncio.to_thread(model.generate_content, prompt)
            return response.text
        except Exception as e:
            return f"⚠️ エラーが発生しました: {str(e)}"

    def get_title(self, level):
        current_title = "受験生"
        for lv, name in TITLES:
            if level >= lv:
                current_title = name
        return current_title

    def get_profile(self, user_id):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('SELECT xp, level, max_combo, current_combo, title FROM user_profile WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        
        if not row:
            self.create_profile(user_id)
            return {'xp': 0, 'level': 1, 'max_combo': 0, 'current_combo': 0, 'title': '受験生'}
        
        return {'xp': row[0], 'level': row[1], 'max_combo': row[2], 'current_combo': row[3], 'title': row[4]}

    def create_profile(self, user_id):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO user_profile (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()

    def update_xp(self, user_id, is_correct):
        prof = self.get_profile(user_id)
        xp_gain = 0
        new_combo = 0
        leveled_up = False
        
        if is_correct:
            new_combo = prof['current_combo'] + 1
            xp_gain = 10 + min(new_combo * 2, 20)
        else:
            new_combo = 0
            xp_gain = 1
            
        new_xp = prof['xp'] + xp_gain
        new_level = prof['level']
        
        req_xp = new_level * 100
        while new_xp >= req_xp:
            new_xp -= req_xp
            new_level += 1
            req_xp = new_level * 100
            leveled_up = True
            
        current_title = self.get_title(new_level)
        max_combo = max(prof['max_combo'], new_combo)
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''UPDATE user_profile SET xp=?, level=?, max_combo=?, current_combo=?, title=? WHERE user_id=?''',
                  (new_xp, new_level, max_combo, new_combo, current_title, user_id))
        conn.commit()
        conn.close()
        return {'gain': xp_gain, 'leveled_up': leveled_up, 'combo': new_combo, 'title': current_title, 'level': new_level}

    def update_progress(self, user_id, q_uuid, is_correct):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('SELECT solved_count, correct_count FROM user_progress WHERE user_id=? AND question_uuid=?', (user_id, q_uuid))
        row = c.fetchone()
        
        last_res = 'correct' if is_correct else 'incorrect'
        
        if row:
            new_solved = row[0] + 1
            new_correct = row[1] + (1 if is_correct else 0)
            c.execute('''UPDATE user_progress SET solved_count=?, correct_count=?, last_result=? 
                         WHERE user_id=? AND question_uuid=?''', (new_solved, new_correct, last_res, user_id, q_uuid))
        else:
            new_correct = 1 if is_correct else 0
            c.execute('''INSERT INTO user_progress (user_id, question_uuid, solved_count, correct_count, last_result) 
                         VALUES (?, ?, 1, ?, ?)''', (user_id, q_uuid, new_correct, last_res))
        conn.commit()
        conn.close()

    def get_progress(self, user_id, q_uuid):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('SELECT solved_count, correct_count, needs_review, memo FROM user_progress WHERE user_id=? AND question_uuid=?', (user_id, q_uuid))
        row = c.fetchone()
        conn.close()
        if row:
            return {'solved': row[0], 'correct': row[1], 'review': bool(row[2]), 'memo': row[3]}
        return {'solved': 0, 'correct': 0, 'review': False, 'memo': ''}

    # ==========================================
    # コマンド
    # ==========================================

    @commands.command(name='restore_from_json')
    async def restore_json_cmd(self, ctx):
        """HTML版のバックアップJSONファイルを読み込んで同期します"""
        # 追加: チャンネル制限
        if ctx.channel.id != self.channel_id:
            return

        if not ctx.message.attachments:
            await ctx.send("❌ JSONファイルを添付してください。")
            return
        
        file = ctx.message.attachments[0]
        if not file.filename.endswith('.json'):
            await ctx.send("❌ JSONファイルのみ対応しています。")
            return

        try:
            data_bytes = await file.read()
            data = json.loads(data_bytes.decode('utf-8'))
            
            if 'questions' not in data:
                await ctx.send("❌ データの形式が正しくありません（HTML版のバックアップファイルを使用してください）")
                return

            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            user_id = ctx.author.id

            # カテゴリマップ作成 (HTML版のID -> タイトル)
            cat_map = {cat['id']: cat['title'] for cat in data.get('categories', [])}

            # 問題同期
            count = 0
            for q in data.get('questions', []):
                q_uuid = str(q.get('id'))
                cat_id = q.get('categoryId')
                cat_name = cat_map.get(cat_id, '未分類')
                
                # 問題マスタ更新/挿入
                c.execute("SELECT id FROM questions WHERE uuid=?", (q_uuid,))
                if c.fetchone():
                    c.execute("""UPDATE questions SET category_name=?, question=?, answer=?, explanation=?, point=? 
                                 WHERE uuid=?""", 
                              (cat_name, q['question'], q['answer'], q['explanation'], q['point'], q_uuid))
                else:
                    c.execute("""INSERT INTO questions (uuid, category_uuid, category_name, question, answer, explanation, point) 
                                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                              (q_uuid, cat_id, cat_name, q['question'], q['answer'], q['explanation'], q['point']))
                
                # 進捗同期
                if 'stats' in q:
                    stats = q['stats']
                    memo = q.get('memo', '')
                    c.execute("""INSERT OR REPLACE INTO user_progress 
                                 (user_id, question_uuid, solved_count, correct_count, needs_review, memo, last_result)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                              (user_id, q_uuid, stats.get('solved', 0), stats.get('correct', 0), 
                               1 if stats.get('needsReview') else 0, memo, stats.get('lastResult')))
                count += 1

            # プレイヤー同期
            if 'player' in data:
                p = data['player']
                c.execute("""UPDATE user_profile SET xp=?, level=?, max_combo=? WHERE user_id=?""",
                          (p.get('xp', 0), p.get('level', 1), p.get('maxCombo', 0), user_id))
                # 新規ユーザーならINSERT
                if c.rowcount == 0:
                    c.execute("""INSERT INTO user_profile (user_id, xp, level, max_combo) VALUES (?, ?, ?, ?)""",
                              (user_id, p.get('xp', 0), p.get('level', 1), p.get('maxCombo', 0)))

            conn.commit()
            conn.close()
            await ctx.send(f"✅ 同期完了！ {count}問のデータを読み込みました。")

        except Exception as e:
            await ctx.send(f"❌ エラーが発生しました: {e}")

    @commands.command(name='export_to_json')
    async def export_json_cmd(self, ctx):
        """現在の学習データをHTML版用JSONとして書き出します"""
        # 追加: チャンネル制限
        if ctx.channel.id != self.channel_id:
            return

        user_id = ctx.author.id
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("SELECT * FROM questions")
        db_qs = [dict(r) for r in c.fetchall()]
        
        c.execute("SELECT * FROM user_progress WHERE user_id=?", (user_id,))
        prog_map = {r['question_uuid']: dict(r) for r in c.fetchall()}
        
        c.execute("SELECT * FROM user_profile WHERE user_id=?", (user_id,))
        profile = c.fetchone()
        conn.close()

        # 変換
        cats_set = set()
        export_qs = []
        
        for q in db_qs:
            cat_uuid = q['category_uuid'] or 'default'
            cat_name = q['category_name'] or '未分類'
            cats_set.add((cat_uuid, cat_name))
            
            prog = prog_map.get(q['uuid'], {})
            
            # HTML版のID形式に合わせる（数値なら数値、文字列なら文字列）
            qid = q['uuid']
            try:
                if float(qid).is_integer(): qid = int(float(qid))
                else: qid = float(qid)
            except: pass

            export_qs.append({
                "id": qid,
                "categoryId": cat_uuid,
                "question": q['question'],
                "answer": q['answer'],
                "explanation": q['explanation'],
                "point": q['point'],
                "memo": prog.get('memo', ''),
                "stats": {
                    "solved": prog.get('solved_count', 0),
                    "correct": prog.get('correct_count', 0),
                    "incorrect": (prog.get('solved_count', 0) - prog.get('correct_count', 0)),
                    "needsReview": bool(prog.get('needs_review', 0)),
                    "lastResult": prog.get('last_result')
                }
            })

        export_cats = [{"id": cid, "title": cname, "createdAt": datetime.now().isoformat()} for cid, cname in cats_set]
        
        export_player = {
            "level": profile['level'] if profile else 1,
            "xp": profile['xp'] if profile else 0,
            "maxCombo": profile['max_combo'] if profile else 0,
            "totalSolved": 0 
        }

        export_data = {
            "categories": export_cats,
            "questions": export_qs,
            "player": export_player,
            "lastSaved": datetime.now().isoformat()
        }

        json_str = json.dumps(export_data, ensure_ascii=False, indent=2)
        f = io.BytesIO(json_str.encode('utf-8'))
        file = discord.File(f, filename=f"study_backup_{datetime.now().strftime('%Y%m%d')}.json")
        
        await ctx.send("📦 HTML版用のバックアップファイルです。", file=file)

    @commands.command(name='import')
    async def import_csv_cmd(self, ctx):
        """CSVファイルをインポート (互換性のため残存)"""
        # 追加: チャンネル制限
        if ctx.channel.id != self.channel_id:
            return

        if not ctx.message.attachments: return await ctx.send("CSVを添付してください")
        att = ctx.message.attachments[0]
        if not att.filename.endswith('.csv'): return await ctx.send("CSVのみ")
        
        await ctx.send("カテゴリ名を入力:")
        try:
            msg = await self.bot.wait_for('message', check=lambda m:m.author==ctx.author, timeout=60)
            cat_name = msg.content
        except: return await ctx.send("タイムアウト")
        
        data = await att.read()
        text = data.decode('utf-8')
        if text.startswith('\ufeff'): text = text[1:]
        reader = list(csv.reader(io.StringIO(text)))
        start = 1 if len(reader)>0 and ("整理番号" in reader[0][0] or "問題" in reader[0][1]) else 0
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        cat_uuid = str(uuid.uuid4())[:8]
        count = 0
        for row in reader[start:]:
            if len(row)<3: continue
            q_uuid = str(uuid.uuid4())
            c.execute("INSERT INTO questions (uuid, category_uuid, category_name, question, answer, explanation, point) VALUES (?,?,?,?,?,?,?)",
                      (q_uuid, cat_uuid, cat_name, row[1], row[2], row[3] if len(row)>3 else "", row[4] if len(row)>4 else ""))
            count+=1
        conn.commit()
        conn.close()
        await ctx.send(f"登録完了: {count}件")

    @app_commands.command(name="quiz", description="学習メニュー")
    async def quiz_cmd(self, interaction: discord.Interaction):
        # 追加: チャンネル制限
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return
        await interaction.response.send_message("📚 学習メニュー", view=QuizMenuView(self))

    @app_commands.command(name="stats", description="ステータス確認")
    async def stats_cmd(self, interaction: discord.Interaction):
        # 追加: チャンネル制限
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return
        prof = self.get_profile(interaction.user.id)
        embed = discord.Embed(title=f"📊 {interaction.user.display_name}", color=discord.Color.purple())
        embed.add_field(name="称号", value=prof['title'], inline=False)
        embed.add_field(name="Lv", value=f"{prof['level']}", inline=True)
        embed.add_field(name="XP", value=f"{prof['xp']}", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reset_stats", description="履歴リセット")
    async def reset_cmd(self, interaction: discord.Interaction):
        # 追加: チャンネル制限
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM user_progress WHERE user_id=?", (interaction.user.id,))
        conn.commit()
        conn.close()
        await interaction.response.send_message("履歴をリセットしました", ephemeral=True)

# ==========================================
# UI Views
# ==========================================
class QuizMenuView(discord.ui.View):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.selected_category = None
        self.selected_mode = None
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT DISTINCT category_name FROM questions")
        cats = [row[0] for row in c.fetchall()]
        conn.close()
        
        if cats:
            self.add_item(CategorySelect(cats))
        else:
            self.add_item(discord.ui.Button(label="データなし", disabled=True))
        self.add_item(ModeSelect())
        self.add_item(StartButton())

    async def start_quiz(self, interaction):
        user_id = interaction.user.id
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        query = """
            SELECT q.*, 
                   COALESCE(u.solved_count, 0) as solved,
                   COALESCE(u.correct_count, 0) as correct,
                   COALESCE(u.needs_review, 0) as needs_review
            FROM questions q
            LEFT JOIN user_progress u ON q.uuid = u.question_uuid AND u.user_id = ?
        """
        params = [user_id]
        if self.selected_category != "all":
            query += " WHERE q.category_name = ?"
            params.append(self.selected_category)
            
        c.execute(query, tuple(params))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        
        targets = []
        if self.selected_mode == "random":
            targets = rows
            random.shuffle(targets)
        elif self.selected_mode == "incorrect":
            targets = [r for r in rows if r['correct'] < r['solved']]
            random.shuffle(targets)
        elif self.selected_mode == "review":
            targets = [r for r in rows if r['needs_review']]
            random.shuffle(targets)
        elif self.selected_mode == "unanswered":
            targets = [r for r in rows if r['solved'] == 0]
            random.shuffle(targets)
        elif self.selected_mode == "low_accuracy":
            targets = sorted(rows, key=lambda x: (x['correct']/x['solved'] if x['solved']>0 else 0))

        if not targets:
            return await interaction.followup.send("問題がありません", ephemeral=True)
            
        await interaction.followup.send(f"🚀 クイズ開始 (全{len(targets)}問)", ephemeral=True)
        await QuizView(self.cog, targets, interaction.user).send_question(interaction.channel)

class CategorySelect(discord.ui.Select):
    def __init__(self, cats):
        opts = [discord.SelectOption(label=c, value=c) for c in cats][:24]
        opts.insert(0, discord.SelectOption(label="すべて", value="all"))
        super().__init__(placeholder="分野", options=opts)
    async def callback(self, interaction):
        self.view.selected_category = self.values[0]
        await interaction.response.defer()

class ModeSelect(discord.ui.Select):
    def __init__(self):
        opts = [
            discord.SelectOption(label="ランダム", value="random"),
            discord.SelectOption(label="間違えた問題", value="incorrect"),
            discord.SelectOption(label="要復習", value="review"),
            discord.SelectOption(label="未回答", value="unanswered"),
            discord.SelectOption(label="正答率順", value="low_accuracy"),
        ]
        super().__init__(placeholder="モード", options=opts)
    async def callback(self, interaction):
        self.view.selected_mode = self.values[0]
        await interaction.response.defer()

class StartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="スタート", style=discord.ButtonStyle.green)
    async def callback(self, interaction):
        if not self.view.selected_category or not self.view.selected_mode:
            return await interaction.response.send_message("選択してください", ephemeral=True)
        await interaction.response.defer()
        await self.view.start_quiz(interaction)

class QuizView(discord.ui.View):
    def __init__(self, cog, questions, user):
        super().__init__(timeout=None)
        self.cog = cog
        self.questions = questions
        self.user = user
        self.index = 0
        self.correct = 0

    async def send_question(self, channel):
        if self.index >= len(self.questions):
            return await channel.send(f"🏆 終了！ 正解数: {self.correct}/{len(self.questions)}")
        
        q = self.questions[self.index]
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT solved_count, correct_count FROM user_progress WHERE user_id=? AND question_uuid=?", (self.user.id, q['uuid']))
        row = c.fetchone()
        conn.close()
        
        solved = row[0] if row else 0
        cor = row[1] if row else 0
        rate = f"{int(cor/solved*100)}%" if solved else "-"
        
        embed = discord.Embed(title=f"Q.{self.index+1} {q['category_name']}", description=q['question'], color=discord.Color.blue())
        embed.set_footer(text=f"正答率: {rate}")
        
        self.clear_items()
        self.add_item(AnsBtn("〇", "〇", q))
        self.add_item(AnsBtn("×", "×", q))
        self.add_item(AbortBtn())
        await channel.send(embed=embed, view=self)

class AnsBtn(discord.ui.Button):
    def __init__(self, label, val, q):
        super().__init__(label=label, style=discord.ButtonStyle.primary if label=="〇" else discord.ButtonStyle.danger)
        self.val = val
        self.q = q
    async def callback(self, interaction):
        user_ans = self.val
        real_ans = self.q['answer'].replace("○","〇").replace("X","×").strip()
        is_correct = user_ans in real_ans
        
        self.view.cog.update_progress(interaction.user.id, self.q['uuid'], is_correct)
        res = self.view.cog.update_xp(interaction.user.id, is_correct)
        if is_correct: self.view.correct += 1
        
        embed = discord.Embed(
            title="⭕ 正解" if is_correct else "❌ 不正解",
            color=discord.Color.green() if is_correct else discord.Color.red()
        )
        
        embed.add_field(name="解答", value=self.q['answer'], inline=False)
        embed.add_field(name="解説", value=self.q['explanation'], inline=False)
        
        if self.q.get('point'):
            embed.add_field(name="ポイント", value=self.q['point'], inline=False)
            
        embed.set_footer(text=f"+{res['gain']}XP | Lv.{res['level']}")
        
        await interaction.response.send_message(embed=embed, view=ExpView(self.view, self.q))
        self.view.stop()

class AbortBtn(discord.ui.Button):
    def __init__(self):
        super().__init__(label="終了", style=discord.ButtonStyle.secondary)
    async def callback(self, interaction):
        correct = self.view.correct
        total = self.view.index
        rate = f"{int(correct/total*100)}%" if total > 0 else "0%"
        
        await interaction.response.send_message(f"🏆 学習終了！ 正解数: {correct}/{total} ({rate})")
        self.view.stop()

class ExpView(discord.ui.View):
    def __init__(self, parent, q):
        super().__init__(timeout=None)
        self.parent = parent
        self.q = q

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.success)
    async def next(self, interaction, button):
        self.parent.index += 1
        await interaction.response.defer()
        await self.parent.send_question(interaction.channel)
        self.stop()

    @discord.ui.button(label="📝 メモ", style=discord.ButtonStyle.secondary)
    async def memo(self, interaction, button):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT memo FROM user_progress WHERE user_id=? AND question_uuid=?", (interaction.user.id, self.q['uuid']))
        row = c.fetchone()
        conn.close()
        await interaction.response.send_modal(MemoModal(self.q['uuid'], row[0] if row else ""))

    @discord.ui.button(label="🤖 AI解説", style=discord.ButtonStyle.primary)
    async def ai(self, interaction, button):
        await interaction.response.defer(thinking=True)
        prompt = f"問題:{self.q['question']}\n正解:{self.q['answer']}\n解説:{self.q['explanation']}\n\nこの問題について、具体例を用いて初心者にもわかりやすく解説してください。"
        resp = await self.parent.cog.ask_gemini(prompt)
        await interaction.followup.send(f"**🤖 AI解説**\n{resp[:1900]}", ephemeral=True, view=AIChatView(self.parent.cog, self.q))

class AIChatView(discord.ui.View):
    def __init__(self, cog, q_data):
        super().__init__(timeout=None)
        self.cog = cog
        self.q_data = q_data
    @discord.ui.button(label="さらに質問", style=discord.ButtonStyle.secondary)
    async def ask(self, interaction, button):
        await interaction.response.send_modal(QuestionModal(self.cog, self.q_data))

class QuestionModal(discord.ui.Modal, title="AIへ質問"):
    def __init__(self, cog, q_data):
        super().__init__()
        self.cog = cog
        self.q_data = q_data
        self.q = discord.ui.TextInput(label="質問", style=discord.TextStyle.paragraph)
        self.add_item(self.q)
    async def on_submit(self, interaction):
        await interaction.response.defer(thinking=True)
        prompt = f"前回の問題:{self.q_data['question']}\n質問:{self.q.value}\n答えてください"
        resp = await self.cog.ask_gemini(prompt)
        await interaction.followup.send(f"Q. {self.q.value}\nA. {resp[:1900]}", ephemeral=True, view=AIChatView(self.cog, self.q_data))

class MemoModal(discord.ui.Modal, title="メモ"):
    def __init__(self, uuid, current):
        super().__init__()
        self.uuid = uuid
        self.memo = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, default=current, required=False)
        self.add_item(self.memo)
    async def on_submit(self, interaction):
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT * FROM user_progress WHERE user_id=? AND question_uuid=?", (interaction.user.id, self.uuid))
        if c.fetchone():
            c.execute("UPDATE user_progress SET memo=? WHERE user_id=? AND question_uuid=?", (self.memo.value, interaction.user.id, self.uuid))
        else:
            c.execute("INSERT INTO user_progress (user_id, question_uuid, memo) VALUES (?, ?, ?)", (interaction.user.id, self.uuid, self.memo.value))
        conn.commit()
        conn.close()
        await interaction.response.send_message("保存しました", ephemeral=True)

async def setup(bot):
    if int(os.getenv("STUDY_CHANNEL_ID", 0)) == 0:
        logging.error("StudyCog: STUDY_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    await bot.add_cog(StudyCog(bot))