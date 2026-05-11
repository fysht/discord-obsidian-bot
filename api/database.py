import aiosqlite
import datetime
import json
import logging
from pathlib import Path
from config import JST

DB_PATH = Path(__file__).parent.parent / "chat_history.db"

async def restore_db_from_drive(drive_service, drive_folder_id):
    """Google Driveからchat_history.dbをダウンロードして復元する"""
    try:
        service = drive_service.get_service()
        if not service: return
        
        bot_folder_id = await drive_service.find_file(service, drive_folder_id, ".bot")
        if not bot_folder_id: return
        
        file_id = await drive_service.find_file(service, bot_folder_id, "chat_history.db")
        if file_id:
            await drive_service.download_file(service, file_id, str(DB_PATH))
            logging.info("[Database] chat_history.dbをGoogle Driveから復元しました。")
    except Exception as e:
        logging.error(f"[Database] リストアに失敗しました: {e}")

async def backup_db_to_drive(drive_service, drive_folder_id):
    """現在のchat_history.dbをGoogle Driveへ同期・バックアップする"""
    if not DB_PATH.exists(): return
    try:
        service = drive_service.get_service()
        if not service: return
        
        bot_folder_id = await drive_service.find_file(service, drive_folder_id, ".bot")
        if not bot_folder_id:
            bot_folder_id = await drive_service.create_folder(service, drive_folder_id, ".bot")
            
        file_id = await drive_service.find_file(service, bot_folder_id, "chat_history.db")
        if file_id:
            await drive_service.update_file(service, file_id, str(DB_PATH))
        else:
            await drive_service.upload_file(service, bot_folder_id, "chat_history.db", str(DB_PATH), "application/octet-stream")
        logging.info("[Database] chat_history.dbをGoogle Driveにバックアップしました。")
    except Exception as e:
        logging.error(f"[Database] バックアップに失敗しました: {e}")


async def init_db():
    """データベースとテーブルを初期化"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stocked_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                type TEXT NOT NULL,
                title TEXT,
                status TEXT DEFAULT 'unread',
                added_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proactive_alerts_sent (
                key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS english_phrases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phrase TEXT NOT NULL,
                translation TEXT DEFAULT '',
                context TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                scope TEXT DEFAULT 'summary',
                question TEXT NOT NULL,
                answer TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                context TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                answered_at TEXT DEFAULT NULL
            )
        """)

        # API 使用量ログ（コストメーター用）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                model TEXT NOT NULL,
                source TEXT DEFAULT '',
                in_tokens INTEGER DEFAULT 0,
                out_tokens INTEGER DEFAULT 0,
                request_count INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_usage_date ON api_usage(date)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_usage_model ON api_usage(model)"
        )

        # アプリ全体の設定（key-value）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # 新規拡張カラムの追加 (存在しない場合) — ALTER TABLE は冪等にならないので個別 try で吸収
        for ddl in (
            "ALTER TABLE stocked_links ADD COLUMN purpose TEXT DEFAULT ''",
            "ALTER TABLE stocked_links ADD COLUMN summary TEXT DEFAULT ''",
            "ALTER TABLE stocked_links ADD COLUMN memo TEXT DEFAULT ''",
            "ALTER TABLE stocked_links ADD COLUMN target_date TEXT DEFAULT ''",
            "ALTER TABLE stocked_links ADD COLUMN linked_note_url TEXT DEFAULT ''",
            "ALTER TABLE stocked_links ADD COLUMN tags TEXT DEFAULT ''",
            "ALTER TABLE stocked_links ADD COLUMN calendar_event_id TEXT DEFAULT ''",
            "ALTER TABLE messages ADD COLUMN starred INTEGER DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN reply_to INTEGER DEFAULT NULL",
            "ALTER TABLE messages ADD COLUMN label TEXT DEFAULT ''",
            "ALTER TABLE english_phrases ADD COLUMN attempt_count INTEGER DEFAULT 0",
            "ALTER TABLE english_phrases ADD COLUMN correct_count INTEGER DEFAULT 0",
            "ALTER TABLE english_phrases ADD COLUMN last_attempted_at TEXT DEFAULT NULL",
        ):
            try:
                await db.execute(ddl)
            except aiosqlite.OperationalError:
                pass

        await db.commit()


async def save_message(role: str, content: str, reply_to: int | None = None) -> int:
    """メッセージを保存し、生成された messages.id を返す。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO messages (role, content, timestamp, reply_to) VALUES (?, ?, ?, ?)",
            (role, content, now, reply_to),
        )
        await db.commit()
        return cursor.lastrowid


async def delete_message_by_id(message_id: int) -> bool:
    """messages テーブルから 1 件削除。1件削除できたら True。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM messages WHERE id = ?",
            (message_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def toggle_message_star(message_id: int) -> bool | None:
    """starred を反転させ、新しい状態を返す。対象が無ければ None。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT starred FROM messages WHERE id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        new_val = 0 if row["starred"] else 1
        await db.execute(
            "UPDATE messages SET starred = ? WHERE id = ?",
            (new_val, message_id),
        )
        await db.commit()
        return bool(new_val)


async def get_starred_messages(limit: int = 100):
    """お気に入りメッセージ一覧。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, role, content, timestamp, reply_to FROM messages "
            "WHERE starred = 1 ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def set_message_label(message_id: int, label: str) -> bool:
    """メッセージにラベルを設定（空文字で解除）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE messages SET label = ? WHERE id = ?", (label, message_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_labeled_messages(label: str, limit: int = 100):
    """指定ラベルのメッセージ一覧。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, role, content, timestamp, label FROM messages "
            "WHERE label = ? ORDER BY id DESC LIMIT ?",
            (label, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_all_labels():
    """使用中のラベル一覧（重複なし）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT DISTINCT label FROM messages WHERE label != '' ORDER BY label"
        )
        rows = await cursor.fetchall()
        return [row["label"] for row in rows]


async def search_messages(q: str, limit: int = 50):
    """LIKE クエリで内容検索。新しい順に返却。"""
    if not q:
        return []
    pattern = f"%{q}%"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, role, content, timestamp FROM messages "
            "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_history(limit: int = 100):
    """直近の会話履歴を取得。各エントリに id / starred / reply_to を含む。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, role, content, timestamp, starred, reply_to "
            "FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        # 古い順に並び替えて返す
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
                "starred": bool(row["starred"]) if row["starred"] is not None else False,
                "reply_to": row["reply_to"],
            }
            for row in reversed(rows)
        ]


async def get_todays_log():
    """今日の会話ログをテキスト形式で取得"""
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content, timestamp FROM messages WHERE timestamp LIKE ? ORDER BY id",
            (f"{today}%",),
        )
        rows = await cursor.fetchall()
        lines = []
        for row in rows:
            prefix = "[私]" if row["role"] == "user" else "[マネージャー]"
            lines.append(f"{prefix} {row['content']}")
        return "\n".join(lines)

async def clear_history():
    """全会話履歴をリセット（削除）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM messages")
        await db.commit()


# --- Stocked Links 用のCRUD ---

async def add_stocked_link(url: str, link_type: str, title: str = "Untitled"):
    """リンクをストックする"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO stocked_links (url, type, title, added_at) VALUES (?, ?, ?, ?)",
            (url, link_type, title, now),
        )
        await db.commit()

async def get_all_links():
    """ストックリンク一覧を取得（全ステータス、新しい順）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, url, type, title, status, added_at, purpose, summary, memo, target_date, linked_note_url, tags FROM stocked_links ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_link_by_id(link_id: int):
    """IDでリンクを1件取得"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, url, type, title, status, added_at, purpose, summary, memo, target_date, linked_note_url, tags, calendar_event_id FROM stocked_links WHERE id = ?",
            (link_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

async def update_link_details(link_id: int, title: str, purpose: str, summary: str, memo: str, target_date: str, linked_note_url: str, link_type: str, tags: str = "", calendar_event_id: str = ""):
    """リンクの詳細情報を更新する"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """
            UPDATE stocked_links
            SET title = ?, purpose = ?, summary = ?, memo = ?, target_date = ?, linked_note_url = ?, type = ?, tags = ?, calendar_event_id = ?
            WHERE id = ?
            """,
            (title, purpose, summary, memo, target_date, linked_note_url, link_type, tags, calendar_event_id, link_id)
        )
        await db.commit()

async def mark_link_as_saved(link_id: int):
    """リンクを保存済み(saved)に更新"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE stocked_links SET status = 'saved' WHERE id = ?",
            (link_id,)
        )
        await db.commit()

async def delete_stocked_link(link_id: int):
    """リンクを削除"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "DELETE FROM stocked_links WHERE id = ?",
            (link_id,)
        )
        await db.commit()


# --- Push Subscriptions ---

async def add_push_subscription(endpoint: str, p256dh: str, auth: str) -> None:
    """購読情報を保存。endpoint が既存ならキーを上書きする。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, created_at=excluded.created_at
            """,
            (endpoint, p256dh, auth, now),
        )
        await db.commit()


async def remove_push_subscription(endpoint: str) -> None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        await db.commit()


async def get_all_push_subscriptions() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# --- Proactive Alerts dedup ---

async def mark_alert_sent(key: str) -> bool:
    """通知済みなら False を返す（既に送ってある）。新規なら True で記録。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        try:
            await db.execute(
                "INSERT INTO proactive_alerts_sent (key, created_at) VALUES (?, ?)",
                (key, now),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def cleanup_alert_keys(older_than_hours: int = 48) -> None:
    """古い通知キーを掃除する（メモリ肥大防止）"""
    cutoff = (datetime.datetime.now(JST) - datetime.timedelta(hours=older_than_hours)).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM proactive_alerts_sent WHERE created_at < ?", (cutoff,))
        await db.commit()


# --- English Phrases ---

async def add_english_phrase(phrase: str, translation: str = "", context: str = "") -> int:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO english_phrases (phrase, translation, context, created_at) VALUES (?, ?, ?, ?)",
            (phrase, translation, context, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_english_phrases(limit: int = 200) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, phrase, translation, context, created_at, "
            "COALESCE(attempt_count, 0) AS attempt_count, "
            "COALESCE(correct_count, 0) AS correct_count, "
            "last_attempted_at "
            "FROM english_phrases ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def delete_english_phrase(phrase_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM english_phrases WHERE id = ?", (phrase_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_quiz_phrase_pool() -> list[dict]:
    """クイズ出題用に全フレーズの統計を返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, phrase, translation, context, created_at, "
            "COALESCE(attempt_count, 0) AS attempt_count, "
            "COALESCE(correct_count, 0) AS correct_count, "
            "last_attempted_at FROM english_phrases"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def record_quiz_attempt(phrase_id: int, correct: bool) -> bool:
    """クイズ回答を記録。試行/正解カウントと最終試行日時を更新。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE english_phrases SET "
            "attempt_count = COALESCE(attempt_count, 0) + 1, "
            "correct_count = COALESCE(correct_count, 0) + ?, "
            "last_attempted_at = ? WHERE id = ?",
            (1 if correct else 0, now, phrase_id),
        )
        await db.commit()
        return cursor.rowcount > 0


# --- Daily Questions (デイリーサマリー / 日記の質問キュー) ---

async def add_daily_question(date: str, question: str, scope: str = 'summary', context: str = '') -> int:
    """デイリーサマリー生成時に AI が判断に迷った点を質問として保存。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO daily_questions (date, scope, question, context, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (date, scope, question, context, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_questions() -> list[dict]:
    """未回答 + 回答済みだが未確定（status='answered'）の質問一覧を返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, date, scope, question, answer, status, context, created_at, answered_at "
            "FROM daily_questions WHERE status IN ('pending', 'answered') ORDER BY date DESC, id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_questions_by_date(date: str, scope: str = None) -> list[dict]:
    """指定日（・スコープ）の質問を返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if scope:
            cursor = await db.execute(
                "SELECT id, date, scope, question, answer, status, context, created_at, answered_at "
                "FROM daily_questions WHERE date = ? AND scope = ? ORDER BY id ASC",
                (date, scope),
            )
        else:
            cursor = await db.execute(
                "SELECT id, date, scope, question, answer, status, context, created_at, answered_at "
                "FROM daily_questions WHERE date = ? ORDER BY id ASC",
                (date,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def answer_daily_question(qid: int, answer: str) -> bool:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE daily_questions SET answer = ?, status = 'answered', answered_at = ? "
            "WHERE id = ? AND status IN ('pending', 'answered')",
            (answer, now, qid),
        )
        await db.commit()
        return cursor.rowcount > 0


async def resolve_questions(date: str, scope: str = None) -> int:
    """指定日の質問をすべて確定（status='resolved'）にする。サマリー保存完了時に呼ぶ。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        if scope:
            cursor = await db.execute(
                "UPDATE daily_questions SET status = 'resolved' WHERE date = ? AND scope = ? AND status != 'resolved'",
                (date, scope),
            )
        else:
            cursor = await db.execute(
                "UPDATE daily_questions SET status = 'resolved' WHERE date = ? AND status != 'resolved'",
                (date,),
            )
        await db.commit()
        return cursor.rowcount


async def delete_daily_question(qid: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM daily_questions WHERE id = ?", (qid,))
        await db.commit()
        return cursor.rowcount > 0


# --- API Usage (コストメーター) ---

async def record_api_usage(model: str, in_tokens: int, out_tokens: int, source: str = "") -> None:
    """Gemini API 呼び出し 1 回分の使用量を記録する。"""
    if not model:
        return
    in_tokens = max(0, int(in_tokens or 0))
    out_tokens = max(0, int(out_tokens or 0))
    if in_tokens == 0 and out_tokens == 0:
        return  # メタ情報が無い呼び出しは記録しない（誤計測を避ける）
    now = datetime.datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO api_usage (date, model, source, in_tokens, out_tokens, request_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (date_str, model, source or "", in_tokens, out_tokens, now.isoformat()),
        )
        await db.commit()


async def get_api_usage_by_day(start_date: str, end_date: str) -> list[dict]:
    """[start_date, end_date] 範囲を日付×モデル単位で集計して返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT date, model, SUM(in_tokens) AS in_tokens, SUM(out_tokens) AS out_tokens, "
            "SUM(request_count) AS request_count "
            "FROM api_usage WHERE date BETWEEN ? AND ? "
            "GROUP BY date, model ORDER BY date ASC",
            (start_date, end_date),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_api_usage_by_model(start_date: str, end_date: str) -> list[dict]:
    """[start_date, end_date] 範囲をモデル単位で集計して返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT model, SUM(in_tokens) AS in_tokens, SUM(out_tokens) AS out_tokens, "
            "SUM(request_count) AS request_count "
            "FROM api_usage WHERE date BETWEEN ? AND ? "
            "GROUP BY model ORDER BY SUM(out_tokens) DESC",
            (start_date, end_date),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# --- App Settings (key-value) ---

async def get_app_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_app_setting(key: str, value: str) -> None:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )
        await db.commit()