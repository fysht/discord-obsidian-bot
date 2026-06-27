import aiosqlite
import datetime
import json
import logging
import re
from pathlib import Path
from typing import Optional
from config import JST

DB_PATH = Path(__file__).parent.parent / "chat_history.db"

def _latest_message_ts(path) -> str:
    """指定 DB ファイルの messages.timestamp の最大値を返す（取得不可なら空文字）。
    timestamp は ISO 風文字列なので辞書順比較で新旧を判定できる。"""
    import sqlite3
    try:
        con = sqlite3.connect(str(path))
        try:
            cur = con.execute("SELECT MAX(timestamp) FROM messages")
            row = cur.fetchone()
        finally:
            con.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


async def restore_db_from_drive(drive_service, drive_folder_id):
    """Google Driveからchat_history.dbをダウンロードして復元する。

    起動毎に無条件で上書きすると、直近のバックアップが間に合っていない場合に
    ローカルの新しいメッセージが「古いDrive版」で潰され、チャットからメッセージが
    消える事故が起きる。これを防ぐため、ローカルが存在する場合は Drive 版を一時
    ファイルに落として「最新メッセージが新しい方」を採用する。"""
    import os
    try:
        service = drive_service.get_service()
        if not service: return

        bot_folder_id = await drive_service.find_file(service, drive_folder_id, ".bot")
        if not bot_folder_id: return

        file_id = await drive_service.find_file(service, bot_folder_id, "chat_history.db")
        if not file_id: return

        # ローカルDBが無ければ無条件で復元（ホストのディスクが揮発した直後など）
        if not DB_PATH.exists():
            await drive_service.download_file(service, file_id, str(DB_PATH))
            logging.info("[Database] chat_history.dbをGoogle Driveから復元しました（ローカル無し）。")
            return

        # ローカルがある場合は新旧比較してから採否を決める
        tmp_path = str(DB_PATH) + ".drive_tmp"
        await drive_service.download_file(service, file_id, tmp_path)
        local_ts = _latest_message_ts(DB_PATH)
        drive_ts = _latest_message_ts(tmp_path)
        if drive_ts > local_ts:
            os.replace(tmp_path, str(DB_PATH))
            logging.info(f"[Database] Drive版が新しいため復元しました（local={local_ts!r} < drive={drive_ts!r}）。")
        else:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            logging.info(f"[Database] ローカルの方が新しいため復元をスキップしました（local={local_ts!r} >= drive={drive_ts!r}）。")
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

        # 習慣（旧 habit_data.json から移行）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                frequency_days INTEGER DEFAULT 1,
                weekdays TEXT DEFAULT '[]',
                trigger TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS habit_logs (
                habit_id TEXT NOT NULL,
                date TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (habit_id, date)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_habit_logs_date ON habit_logs(date)"
        )

        # エラーログ（バックグラウンド task の失敗を可視化するため）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS error_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                traceback TEXT DEFAULT ''
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log(created_at)"
        )

        # Gmail インボックス（要約キャッシュ + 状態管理）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gmail_inbox (
                id TEXT PRIMARY KEY,
                thread_id TEXT DEFAULT '',
                subject TEXT DEFAULT '',
                from_addr TEXT DEFAULT '',
                received_at TEXT NOT NULL,
                snippet TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                importance TEXT DEFAULT 'medium',
                state TEXT DEFAULT 'pending',
                notified INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gmail_state ON gmail_inbox(state)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gmail_received ON gmail_inbox(received_at)"
        )

        # YouTube 登録チャンネル（subscriptions.list の結果をキャッシュ）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS youtube_channels (
                channel_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                added_at TEXT NOT NULL
            )
        """)

        # YouTube 新着動画（RSS から取り込み・状態管理）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id TEXT PRIMARY KEY,
                channel_id TEXT DEFAULT '',
                channel_title TEXT DEFAULT '',
                title TEXT DEFAULT '',
                url TEXT DEFAULT '',
                published_at TEXT DEFAULT '',
                state TEXT DEFAULT 'new',
                notified INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_youtube_state ON youtube_videos(state)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_youtube_published ON youtube_videos(published_at)"
        )

        # 視聴セッション（ダラ見対策）: 自動記録(webhook)/宣言タイマー/見る前チェックインを統合
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watch_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app TEXT DEFAULT 'youtube',
                source TEXT DEFAULT 'webhook',
                reason TEXT DEFAULT '',
                declared_minutes INTEGER DEFAULT 0,
                started_at TEXT NOT NULL,
                ended_at TEXT DEFAULT '',
                duration_sec INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                reminded INTEGER DEFAULT 0,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_watch_status ON watch_sessions(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_watch_date ON watch_sessions(date)"
        )

        # 支出ログ（大きな支出メモ）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT DEFAULT 'その他',
                vendor TEXT DEFAULT '',
                payment_method TEXT DEFAULT '',
                memo TEXT DEFAULT '',
                receipt_drive_id TEXT DEFAULT '',
                is_large INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date)"
        )

        # 食事ログ
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                meal_type TEXT DEFAULT '',
                name TEXT NOT NULL,
                calories INTEGER DEFAULT 0,
                protein_g REAL DEFAULT 0,
                fat_g REAL DEFAULT 0,
                carbs_g REAL DEFAULT 0,
                memo TEXT DEFAULT '',
                image_drive_id TEXT DEFAULT '',
                advice TEXT DEFAULT '',
                restaurant TEXT DEFAULT '',
                ordered_items TEXT DEFAULT '',
                price INTEGER DEFAULT 0,
                source TEXT DEFAULT '',
                companions TEXT DEFAULT '',
                rating INTEGER DEFAULT 0,
                restaurant_url TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(date)"
        )
        # 既存DBのマイグレーション（後付け列を ALTER で追加。存在すれば無視）
        for col, ddl in [
            ("restaurant", "ALTER TABLE meals ADD COLUMN restaurant TEXT DEFAULT ''"),
            ("ordered_items", "ALTER TABLE meals ADD COLUMN ordered_items TEXT DEFAULT ''"),
            ("price", "ALTER TABLE meals ADD COLUMN price INTEGER DEFAULT 0"),
            ("source", "ALTER TABLE meals ADD COLUMN source TEXT DEFAULT ''"),
            ("companions", "ALTER TABLE meals ADD COLUMN companions TEXT DEFAULT ''"),
            ("rating", "ALTER TABLE meals ADD COLUMN rating INTEGER DEFAULT 0"),
            ("restaurant_url", "ALTER TABLE meals ADD COLUMN restaurant_url TEXT DEFAULT ''"),
            ("expense_id", "ALTER TABLE meals ADD COLUMN expense_id INTEGER DEFAULT NULL"),
        ]:
            try:
                await db.execute(ddl)
            except Exception:
                pass  # 列がすでに存在

        # 多読プラン（書籍ごとの段階的な読み方プラン）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reading_plans (
                book_title TEXT PRIMARY KEY,
                passes_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # 株価 OHLCV キャッシュ（日本株スクリーナー用）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_ohlcv (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER,
                PRIMARY KEY (code, date)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ohlcv_code_date ON stock_ohlcv(code, date)"
        )

        # スクリーナーの非同期ジョブ管理
        await db.execute("""
            CREATE TABLE IF NOT EXISTS screener_jobs (
                job_id TEXT PRIMARY KEY,
                style TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress_current INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                current_ticker TEXT DEFAULT '',
                candidates_json TEXT DEFAULT '',
                report_markdown TEXT DEFAULT '',
                saved_as TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_screener_jobs_status ON screener_jobs(status)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                sector TEXT DEFAULT '',
                source TEXT DEFAULT '',
                memo TEXT DEFAULT '',
                added_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS screener_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                styles TEXT DEFAULT '[]',
                combine_mode TEXT DEFAULT 'any',
                universe TEXT DEFAULT '',
                applied_filters TEXT DEFAULT '{}',
                candidates TEXT DEFAULT '[]',
                qualitative_report TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_screener_runs_created ON screener_runs(created_at DESC)"
        )

        # 投資判断の事後検証（買い/売りが正しかったかを 20/60 営業日後に答え合わせ）。
        # 売買成立時に診断スナップショット（テクニカル状態＋推奨＋利確目安＋約定価格）を記録し、
        # 期日到来後に「その後のリターン − 同期間ベンチマーク超過」で採点して学習に回す。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS decision_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decided_at TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT DEFAULT '',
                market TEXT DEFAULT 'JP',
                trade_action TEXT NOT NULL,
                rec_action TEXT DEFAULT '',
                trend_state TEXT DEFAULT '',
                price_at_decision REAL,
                score REAL,
                blended_score REAL,
                signals TEXT DEFAULT '[]',
                projection TEXT DEFAULT '{}',
                style TEXT DEFAULT '',
                checkpoints TEXT DEFAULT '{}',
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_decision_reviews_status ON decision_reviews(status, decided_at)"
        )

        # マネージャー通知ログ（長文の自動通知をチャットから分離して保存）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manager_notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_manager_notices_created ON manager_notices(created_at DESC)"
        )

        # 撮影画像の保管（写真／書類を分けて整理）。実体は Google Drive、ここは索引。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'photo',
                drive_id TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_items_created ON media_items(created_at DESC)"
        )

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
            # 実行済み/キャンセル済みの提案ボタン（ACTIONペイロード）を保持し、再描画時に復活させない
            "ALTER TABLE messages ADD COLUMN consumed_actions TEXT DEFAULT ''",
            "ALTER TABLE english_phrases ADD COLUMN attempt_count INTEGER DEFAULT 0",
            "ALTER TABLE english_phrases ADD COLUMN correct_count INTEGER DEFAULT 0",
            "ALTER TABLE english_phrases ADD COLUMN last_attempted_at TEXT DEFAULT NULL",
            # Gmail インボックスに「Obsidian保存済み」を示す列を追加
            "ALTER TABLE gmail_inbox ADD COLUMN saved_drive_id TEXT DEFAULT ''",
            "ALTER TABLE gmail_inbox ADD COLUMN saved_at TEXT DEFAULT ''",
            "ALTER TABLE manager_notices ADD COLUMN is_read INTEGER DEFAULT 0",
            # YouTube新着のオンデマンド要約をキャッシュする列（押した動画だけ生成）
            "ALTER TABLE youtube_videos ADD COLUMN summary TEXT DEFAULT ''",
            # 視聴判断用の短い要約とは別の、保存用（後から読み返す）の詳しい要約
            "ALTER TABLE youtube_videos ADD COLUMN detail_summary TEXT DEFAULT ''",
            # 支出の内訳（何にいくら使ったか）を保持する列
            "ALTER TABLE expenses ADD COLUMN breakdown TEXT DEFAULT ''",
        ):
            try:
                await db.execute(ddl)
            except aiosqlite.OperationalError:
                pass

        # 一度きりのキャッシュ無効化: yfinance を auto_adjust=True に切替えたため
        # 過去にキャッシュした無調整 OHLCV は分割・配当を反映しておらず
        # チャート（調整済み表示）と乖離する。schema_marker で 1 回だけクリア。
        cur = await db.execute(
            "SELECT value FROM app_settings WHERE key = ?", ("stock_ohlcv_adjusted_v1",)
        )
        row = await cur.fetchone()
        if not row:
            try:
                await db.execute("DELETE FROM stock_ohlcv")
                _now_iso = datetime.datetime.now(JST).isoformat()
                await db.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    ("stock_ohlcv_adjusted_v1", "1", _now_iso),
                )
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
            "SELECT id, role, content, timestamp, starred, reply_to, consumed_actions "
            "FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()

        def _consumed(row):
            raw = row["consumed_actions"] if "consumed_actions" in row.keys() else ""
            if not raw:
                return []
            try:
                val = json.loads(raw)
                return val if isinstance(val, list) else []
            except (ValueError, TypeError):
                return []

        # 古い順に並び替えて返す
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
                "starred": bool(row["starred"]) if row["starred"] is not None else False,
                "reply_to": row["reply_to"],
                "consumed_actions": _consumed(row),
            }
            for row in reversed(rows)
        ]


async def mark_message_action_consumed(message_id: int, action_payload: str) -> bool:
    """提案ボタン（ACTIONペイロード）を実行/キャンセル済みとして記録する。
    再描画時にそのボタンを復活させないために使う。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT consumed_actions FROM messages WHERE id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        try:
            current = json.loads(row["consumed_actions"]) if row["consumed_actions"] else []
            if not isinstance(current, list):
                current = []
        except (ValueError, TypeError):
            current = []
        if action_payload not in current:
            current.append(action_payload)
        await db.execute(
            "UPDATE messages SET consumed_actions = ? WHERE id = ?",
            (json.dumps(current, ensure_ascii=False), message_id),
        )
        await db.commit()
        return True


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

async def get_todays_user_messages(date_str: str | None = None) -> list[str]:
    """指定日（既定は今日）に『ユーザー自身が送った』メッセージ本文を時系列で返す。
    一日の終わりの振り返り（メッセージのモチベーション維持）用。"""
    day = date_str or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT content FROM messages WHERE role = 'user' AND timestamp LIKE ? ORDER BY id",
            (f"{day}%",),
        )
        rows = await cursor.fetchall()
        out = []
        for row in rows:
            text = (row["content"] or "").strip()
            # ACTION/QUESTIONS マーカーや内部タグを除いた素の本文だけを対象にする
            text = re.sub(r"\[(?:ACTION|QUESTIONS):[^\]]+\]", "", text).strip()
            if text:
                out.append(text)
        return out


async def clear_history():
    """全会話履歴をリセット（削除）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM messages")
        await db.commit()


# --- Stocked Links 用のCRUD ---

async def add_stocked_link(url: str, link_type: str, title: str = "Untitled"):
    """リンクをストックする。新規レコードのIDを返す。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO stocked_links (url, type, title, added_at) VALUES (?, ?, ?, ?)",
            (url, link_type, title, now),
        )
        await db.commit()
        return cursor.lastrowid

async def get_all_links():
    """ストックリンク一覧を取得（全ステータス、古い順＝最新が下）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, url, type, title, status, added_at, purpose, summary, memo, target_date, linked_note_url, tags FROM stocked_links ORDER BY id ASC"
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


# --- 多読プラン (Reading Plans) ---

DEFAULT_READING_PASSES = [
    "目次・全体像をつかむ",
    "各章のまとめ・結論だけ読む",
    "気になる章をじっくり読む",
    "全体を通して読む",
    "要点を読み返して定着させる",
]


async def get_reading_plan(book_title: str):
    """書籍の多読プランを取得。無ければデフォルト5段階で作成して返す。"""
    title = (book_title or "").strip()
    if not title:
        return []
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT passes_json FROM reading_plans WHERE book_title = ?", (title,)
        )
        row = await cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                pass
        passes = [
            {"label": label, "done": False, "done_at": None}
            for label in DEFAULT_READING_PASSES
        ]
        now = datetime.datetime.now(JST).isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO reading_plans (book_title, passes_json, updated_at) VALUES (?, ?, ?)",
            (title, json.dumps(passes, ensure_ascii=False), now),
        )
        await db.commit()
        return passes


async def update_reading_plan(book_title: str, passes: list):
    """書籍の多読プラン（段階リスト）を保存する。"""
    title = (book_title or "").strip()
    if not title:
        return
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT OR REPLACE INTO reading_plans (book_title, passes_json, updated_at) VALUES (?, ?, ?)",
            (title, json.dumps(passes or [], ensure_ascii=False), now),
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
            "SELECT * FROM ("
            "  SELECT id, phrase, translation, context, created_at, "
            "  COALESCE(attempt_count, 0) AS attempt_count, "
            "  COALESCE(correct_count, 0) AS correct_count, "
            "  last_attempted_at "
            "  FROM english_phrases ORDER BY created_at DESC LIMIT ?"
            ") ORDER BY created_at ASC, id ASC",
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


async def resolve_question_by_id(qid: int) -> bool:
    """単一の質問を確定（status='resolved'）にする。
    記録系スコープ（meal/expense 等）でログ保存が完了した質問を、
    再回答による二重保存を防ぐために閉じる用途。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE daily_questions SET status = 'resolved' WHERE id = ?",
            (int(qid),),
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


async def delete_daily_questions_by_date(date: str, scopes: list[str] | None = None) -> int:
    """指定日の質問をまとめて削除する。scopes 指定時はその scope のみ。
    「今日の記録」で日付ごとに未回答質問を一括破棄するために使う。削除件数を返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            cursor = await db.execute(
                f"DELETE FROM daily_questions WHERE date = ? AND scope IN ({placeholders})",
                (date, *scopes),
            )
        else:
            cursor = await db.execute(
                "DELETE FROM daily_questions WHERE date = ?", (date,)
            )
        await db.commit()
        return cursor.rowcount


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


# --- Manager Notices (長文自動通知のログ) ---

async def add_manager_notice(category: str, title: str, body: str) -> int:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO manager_notices (category, title, body, created_at) VALUES (?, ?, ?, ?)",
            (category, title, body, now),
        )
        await db.commit()
        return cursor.lastrowid


async def list_manager_notices(limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, category, title, body, created_at, COALESCE(is_read, 0) AS is_read "
            "FROM manager_notices ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def set_manager_notice_read(notice_id: int, is_read: bool) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE manager_notices SET is_read = ? WHERE id = ?",
            (1 if is_read else 0, int(notice_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_manager_notice(notice_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM manager_notices WHERE id = ?", (int(notice_id),)
        )
        await db.commit()
        return cursor.rowcount > 0


# --- Media (撮影画像: 写真／書類) ---

async def add_media_item(kind: str, drive_id: str, filename: str, title: str, date: str) -> int:
    now = datetime.datetime.now(JST).isoformat()
    k = kind if kind in ("photo", "document") else "photo"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO media_items (kind, drive_id, filename, title, date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (k, drive_id, filename, title, date, now),
        )
        await db.commit()
        return cursor.lastrowid


async def list_media_items(kind: str | None = None, limit: int = 200) -> list[dict]:
    # 直近 limit 件を取りつつ、表示は古い順（最新が下）にする。
    # 内側で created_at DESC LIMIT で最新N件を確保し、外側で昇順に並べ替える。
    cols = "id, kind, drive_id, filename, title, date, created_at"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if kind in ("photo", "document"):
            cursor = await db.execute(
                f"SELECT {cols} FROM (SELECT {cols} FROM media_items WHERE kind = ? "
                "ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC, id ASC",
                (kind, int(limit)),
            )
        else:
            cursor = await db.execute(
                f"SELECT {cols} FROM (SELECT {cols} FROM media_items "
                "ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC, id ASC",
                (int(limit),),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_media_item(item_id: int, kind: str | None = None, title: str | None = None) -> bool:
    sets, params = [], []
    if kind in ("photo", "document"):
        sets.append("kind = ?")
        params.append(kind)
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if not sets:
        return False
    params.append(int(item_id))
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            f"UPDATE media_items SET {', '.join(sets)} WHERE id = ?", params
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_media_item(item_id: int) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, kind, drive_id, filename, title, date, created_at "
            "FROM media_items WHERE id = ?",
            (int(item_id),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_media_item(item_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM media_items WHERE id = ?", (int(item_id),)
        )
        await db.commit()
        return cursor.rowcount > 0


# --- Meals (食事ログ) ---

async def add_meal(
    date: str, time: str, name: str,
    meal_type: str = "",
    calories: int = 0,
    protein_g: float = 0.0,
    fat_g: float = 0.0,
    carbs_g: float = 0.0,
    memo: str = "",
    image_drive_id: str = "",
    advice: str = "",
    restaurant: str = "",
    ordered_items: str = "",
    price: int = 0,
    source: str = "",
    companions: str = "",
    rating: int = 0,
    restaurant_url: str = "",
) -> int:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO meals "
            "(date, time, meal_type, name, calories, protein_g, fat_g, carbs_g, memo, image_drive_id, advice, "
            " restaurant, ordered_items, price, source, companions, rating, restaurant_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, time, meal_type, name, int(calories or 0), float(protein_g or 0), float(fat_g or 0),
             float(carbs_g or 0), memo, image_drive_id, advice,
             restaurant, ordered_items, int(price or 0), source, companions, int(rating or 0),
             restaurant_url, now),
        )
        await db.commit()
        return cursor.lastrowid


_MEAL_COLS = (
    "id, date, time, meal_type, name, calories, protein_g, fat_g, carbs_g, memo, image_drive_id, advice, "
    "restaurant, ordered_items, price, source, companions, rating, restaurant_url, created_at"
)


async def get_meals_by_date(date: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT {_MEAL_COLS} FROM meals WHERE date = ? ORDER BY time ASC, id ASC",
            (date,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_meals_by_range(start_date: str, end_date: str) -> list[dict]:
    """指定期間の食事ログを取得（新しい日付順）。献立提案などの履歴参照用。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT {_MEAL_COLS} FROM meals WHERE date BETWEEN ? AND ? ORDER BY date DESC, time DESC, id DESC",
            (start_date, end_date),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_meal(meal_id: int, fields: dict) -> bool:
    if not fields:
        return False
    allowed = {
        "date", "time", "meal_type", "name", "calories", "protein_g", "fat_g", "carbs_g", "memo", "advice",
        "restaurant", "ordered_items", "price", "source", "companions", "rating", "restaurant_url",
    }
    sets = []
    values = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        values.append(v)
    if not sets:
        return False
    values.append(meal_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            f"UPDATE meals SET {', '.join(sets)} WHERE id = ?",
            tuple(values),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_meal(meal_id: int) -> dict | None:
    """1 件の食事ログを取得（外食金額の支出連携で現状値を読むのに使う）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, date, time, name, restaurant, price, "
            "COALESCE(expense_id, 0) AS expense_id "
            "FROM meals WHERE id = ?",
            (int(meal_id),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def set_meal_expense_id(meal_id: int, expense_id) -> bool:
    """食事ログに連携支出の id を保存（解除は None）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE meals SET expense_id = ? WHERE id = ?",
            (int(expense_id) if expense_id else None, int(meal_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_meal(meal_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        await db.commit()
        return cursor.rowcount > 0


# --- Expenses (大きな支出メモ) ---

async def add_expense(
    date: str, amount: int, category: str = "その他",
    vendor: str = "", payment_method: str = "", memo: str = "",
    receipt_drive_id: str = "", is_large: bool = False, breakdown: str = "",
) -> int:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO expenses (date, amount, category, vendor, payment_method, memo, receipt_drive_id, is_large, breakdown, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, int(amount or 0), category or "その他", vendor or "", payment_method or "",
             memo or "", receipt_drive_id or "", 1 if is_large else 0, breakdown or "", now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_expenses_by_range(start_date: str, end_date: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, date, amount, category, vendor, payment_method, memo, receipt_drive_id, is_large, breakdown, created_at "
            "FROM expenses WHERE date BETWEEN ? AND ? ORDER BY date ASC, id ASC",
            (start_date, end_date),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_expense(expense_id: int, fields: dict) -> bool:
    if not fields:
        return False
    allowed = {"date", "amount", "category", "vendor", "payment_method", "memo", "is_large", "receipt_drive_id", "breakdown"}
    sets = []
    values = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        if k == "is_large":
            values.append(1 if v else 0)
        else:
            values.append(v)
    if not sets:
        return False
    values.append(expense_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            f"UPDATE expenses SET {', '.join(sets)} WHERE id = ?",
            tuple(values),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_expense(expense_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        await db.commit()
        return cursor.rowcount > 0


# --- Gmail Inbox ---

async def gmail_get(message_id: str) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM gmail_inbox WHERE id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def gmail_upsert(message: dict) -> None:
    """Gmail メッセージを保存または更新する。要約・重要度は別途 update する想定。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO gmail_inbox (id, thread_id, subject, from_addr, received_at, snippet, summary, importance, state, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?) "
            "ON CONFLICT(id) DO UPDATE SET thread_id = excluded.thread_id, subject = excluded.subject, "
            "from_addr = excluded.from_addr, received_at = excluded.received_at, snippet = excluded.snippet",
            (
                message["id"],
                message.get("thread_id", ""),
                message.get("subject", ""),
                message.get("from_addr", ""),
                message.get("received_at", now),
                message.get("snippet", ""),
                message.get("summary", ""),
                message.get("importance", "medium"),
                now,
            ),
        )
        await db.commit()


async def gmail_update(message_id: str, **fields) -> bool:
    if not fields:
        return False
    allowed = {"summary", "importance", "state", "notified", "subject", "from_addr", "snippet", "saved_drive_id", "saved_at"}
    sets = []
    values = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        values.append(v)
    if not sets:
        return False
    values.append(message_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            f"UPDATE gmail_inbox SET {', '.join(sets)} WHERE id = ?",
            tuple(values),
        )
        await db.commit()
        return cursor.rowcount > 0


async def gmail_delete_by_id(message_id: str) -> bool:
    """指定 message_id の Gmail レコードを物理削除する（Gmail 側で削除されたもの同期用）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM gmail_inbox WHERE id = ?",
            (message_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def gmail_list_active_ids() -> list[str]:
    """state='pending' または 'archived' の Gmail メッセージID一覧を返す（trashed は除外）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT id FROM gmail_inbox WHERE state IN ('pending', 'archived')"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def gmail_list(state: str = "pending", limit: int = 50) -> list[dict]:
    """state='pending' / 'archived' / 'trashed' / 'all' を指定して一覧取得。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if state and state != "all":
            cursor = await db.execute(
                "SELECT * FROM gmail_inbox WHERE state = ? "
                "ORDER BY received_at DESC LIMIT ?",
                (state, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM gmail_inbox ORDER BY received_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# --- Stock OHLCV Cache (日本株スクリーナー用) ---

async def upsert_ohlcv_rows(code: str, rows: list[dict]) -> int:
    """OHLCV 行群を upsert する。rows は {date, open, high, low, close, volume} のリスト。"""
    if not rows:
        return 0
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executemany(
            "INSERT INTO stock_ohlcv (code, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(code, date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume",
            [
                (
                    code,
                    r.get("date"),
                    r.get("open"),
                    r.get("high"),
                    r.get("low"),
                    r.get("close"),
                    r.get("volume"),
                )
                for r in rows
            ],
        )
        await db.commit()
        return len(rows)


async def get_ohlcv_range(code: str, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """code の OHLCV を [start_date, end_date] で日付昇順に取得。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT date, open, high, low, close, volume FROM stock_ohlcv WHERE code = ?"
        params: list = [code]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date ASC"
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_ohlcv_latest_date(code: str) -> str | None:
    """code の OHLCV キャッシュの最新日付を返す。なければ None。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT MAX(date) FROM stock_ohlcv WHERE code = ?", (code,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None


# --- Screener Jobs ---

async def screener_job_create(job_id: str, style: str, total: int) -> None:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO screener_jobs (job_id, style, status, progress_current, progress_total, created_at, updated_at) "
            "VALUES (?, ?, 'queued', 0, ?, ?, ?)",
            (job_id, style, int(total), now, now),
        )
        await db.commit()


async def screener_job_update(job_id: str, **fields) -> bool:
    if not fields:
        return False
    allowed = {
        "status", "progress_current", "progress_total", "current_ticker",
        "candidates_json", "report_markdown", "saved_as", "error",
    }
    sets = []
    values = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        values.append(v)
    if not sets:
        return False
    sets.append("updated_at = ?")
    values.append(datetime.datetime.now(JST).isoformat())
    values.append(job_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            f"UPDATE screener_jobs SET {', '.join(sets)} WHERE job_id = ?",
            tuple(values),
        )
        await db.commit()
        return cursor.rowcount > 0


async def screener_job_get(job_id: str) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM screener_jobs WHERE job_id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def screener_job_count_active() -> int:
    """実行中ジョブ件数（同時実行数の制御用）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM screener_jobs WHERE status IN ('queued', 'running')"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def screener_jobs_list_active() -> list[dict]:
    """実行中（queued/running）ジョブの一覧。キャンセル対象の特定に使う。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM screener_jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def screener_job_latest_done(style: str) -> dict | None:
    """指定 style の done ジョブのうち最新の1件を返す（『前回の結果を見る』で
    16:15 日次スクリーニング結果を引くため）。created_at の降順で先頭。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM screener_jobs WHERE style = ? AND status = 'done' "
            "ORDER BY created_at DESC LIMIT 1",
            (style,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# =========================================================
# Watchlist (注目銘柄)
# =========================================================

async def watchlist_add(code: str, name: str = "", sector: str = "", source: str = "", memo: str = "") -> bool:
    """注目銘柄を追加。既に存在すれば name/sector/source を上書きする。"""
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO watchlist (code, name, sector, source, memo, added_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET
                   name=excluded.name,
                   sector=excluded.sector,
                   source=excluded.source""",
            (code, name, sector, source, memo, now),
        )
        await db.commit()
        return True


async def watchlist_remove(code: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        await db.commit()
        return cursor.rowcount > 0


async def watchlist_list() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT code, name, sector, source, memo, added_at FROM watchlist ORDER BY added_at ASC, code ASC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def watchlist_update_memo(code: str, memo: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("UPDATE watchlist SET memo = ? WHERE code = ?", (memo, code))
        await db.commit()
        return cursor.rowcount > 0


# =========================================================
# Screener Runs (保存済みスクリーニング結果)
# =========================================================

async def screener_run_save(
    title: str,
    styles: list,
    combine_mode: str,
    universe: str,
    applied_filters: dict,
    candidates: list,
    qualitative_report: str = "",
) -> int:
    import json as _json
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """INSERT INTO screener_runs
               (title, styles, combine_mode, universe, applied_filters, candidates, qualitative_report, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title or "",
                _json.dumps(styles or [], ensure_ascii=False),
                combine_mode or "any",
                universe or "",
                _json.dumps(applied_filters or {}, ensure_ascii=False),
                _json.dumps(candidates or [], ensure_ascii=False),
                qualitative_report or "",
                now,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def screener_run_update_qualitative(run_id: int, qualitative_report: str) -> bool:
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE screener_runs SET qualitative_report = ?, updated_at = ? WHERE id = ?",
            (qualitative_report or "", now, run_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def screener_run_list() -> list[dict]:
    """概要のみ返す（candidates 件数, has_report のフラグ）。"""
    import json as _json
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, styles, combine_mode, universe, candidates, qualitative_report, created_at "
            "FROM screener_runs ORDER BY created_at ASC, id ASC"
        )
        rows = await cursor.fetchall()
        out = []
        for r in rows:
            try:
                styles = _json.loads(r["styles"] or "[]")
            except Exception:
                styles = []
            try:
                cands = _json.loads(r["candidates"] or "[]")
                cand_count = len(cands)
            except Exception:
                cand_count = 0
            out.append({
                "id": r["id"],
                "title": r["title"] or "",
                "styles": styles,
                "combine_mode": r["combine_mode"] or "any",
                "universe": r["universe"] or "",
                "candidate_count": cand_count,
                "has_report": bool((r["qualitative_report"] or "").strip()),
                "created_at": r["created_at"],
            })
        return out


async def screener_run_get(run_id: int) -> Optional[dict]:
    import json as _json
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM screener_runs WHERE id = ?", (run_id,)
        )
        r = await cursor.fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["styles"] = _json.loads(d.get("styles") or "[]")
        except Exception:
            d["styles"] = []
        try:
            d["applied_filters"] = _json.loads(d.get("applied_filters") or "{}")
        except Exception:
            d["applied_filters"] = {}
        try:
            d["candidates"] = _json.loads(d.get("candidates") or "[]")
        except Exception:
            d["candidates"] = []
        return d


async def screener_run_delete(run_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM screener_runs WHERE id = ?", (run_id,))
        await db.commit()
        return cursor.rowcount > 0


# =========================================================
# Decision Reviews (投資判断の事後検証ループ)
# =========================================================

def _decision_row_to_dict(r) -> dict:
    """signals/projection/checkpoints を JSON 復元して dict 化する。"""
    import json as _json
    d = dict(r)
    for k, empty in (("signals", "[]"), ("projection", "{}"), ("checkpoints", "{}")):
        try:
            d[k] = _json.loads(d.get(k) or empty)
        except Exception:
            d[k] = [] if k == "signals" else {}
    return d


async def decision_review_save(payload: dict) -> int:
    """売買時の判断スナップショットを保存する。"""
    import json as _json
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """INSERT INTO decision_reviews
               (decided_at, code, name, market, trade_action, rec_action, trend_state,
                price_at_decision, score, blended_score, signals, projection, style,
                checkpoints, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.get("decided_at") or now,
                str(payload.get("code") or ""),
                payload.get("name") or "",
                payload.get("market") or "JP",
                payload.get("trade_action") or "",
                payload.get("rec_action") or "",
                payload.get("trend_state") or "",
                payload.get("price_at_decision"),
                payload.get("score"),
                payload.get("blended_score"),
                _json.dumps(payload.get("signals") or [], ensure_ascii=False),
                _json.dumps(payload.get("projection") or {}, ensure_ascii=False),
                payload.get("style") or "",
                _json.dumps(payload.get("checkpoints") or {}, ensure_ascii=False),
                payload.get("status") or "open",
                now,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def decision_review_list(status: Optional[str] = None, limit: int = 200) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cursor = await db.execute(
                "SELECT * FROM decision_reviews WHERE status = ? "
                "ORDER BY decided_at DESC, id DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM decision_reviews ORDER BY decided_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [_decision_row_to_dict(r) for r in rows]


async def decision_review_list_pending() -> list[dict]:
    """検証未完了（open / partial）の判断を古い順に返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM decision_reviews WHERE status IN ('open', 'partial') "
            "ORDER BY decided_at ASC, id ASC"
        )
        rows = await cursor.fetchall()
        return [_decision_row_to_dict(r) for r in rows]


async def decision_review_update_checkpoints(review_id: int, checkpoints: dict, status: str) -> bool:
    import json as _json
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE decision_reviews SET checkpoints = ?, status = ?, updated_at = ? WHERE id = ?",
            (_json.dumps(checkpoints or {}, ensure_ascii=False), status, now, review_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def decision_review_delete(review_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM decision_reviews WHERE id = ?", (review_id,))
        await db.commit()
        return cursor.rowcount > 0


async def gmail_count_unnotified_high() -> int:
    """high 重要度かつ未通知の件数（バッジ表示などに使う）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM gmail_inbox WHERE state = 'pending' AND importance = 'high'"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def gmail_count_pending() -> int:
    """未処理（state='pending'）メールの総件数。バッジ表示・溜まり通知に使う。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM gmail_inbox WHERE state = 'pending'"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


# ===== YouTube 登録チャンネル新着 =====

async def youtube_upsert_channels(rows: list[dict]) -> int:
    """登録チャンネル一覧を upsert する（title は最新で更新、enabled は既存値を保持）。
    rows は [{channel_id, title}]。登録件数を返す。"""
    if not rows:
        return 0
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executemany(
            "INSERT INTO youtube_channels (channel_id, title, enabled, added_at) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title",
            [(r.get("channel_id"), (r.get("title") or ""), now) for r in rows if r.get("channel_id")],
        )
        await db.commit()
        return len(rows)


async def youtube_list_channels(enabled_only: bool = False) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT channel_id, title, enabled, added_at FROM youtube_channels"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY title COLLATE NOCASE ASC"
        cursor = await db.execute(q)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def youtube_set_channel_enabled(channel_id: str, enabled: bool) -> bool:
    """チャンネルの新着取り込み ON/OFF（ミュート）を切り替える。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE youtube_channels SET enabled = ? WHERE channel_id = ?",
            (1 if enabled else 0, channel_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def youtube_upsert_video(v: dict) -> bool:
    """新着動画を 1 件登録する。既に存在すれば無視（INSERT OR IGNORE）。
    新規に追加されたら True を返す＝「新着」判定の肝。"""
    vid = v.get("video_id")
    if not vid:
        return False
    now = datetime.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO youtube_videos "
            "(id, channel_id, channel_title, title, url, published_at, state, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'new', 0, ?)",
            (
                vid, v.get("channel_id") or "", v.get("channel_title") or "",
                v.get("title") or "", v.get("url") or "", v.get("published_at") or "", now,
            ),
        )
        await db.commit()
        return cursor.rowcount > 0


async def youtube_list_videos(state: str = "new", limit: int = 50) -> list[dict]:
    """state 指定で動画一覧を新しい順に返す（state='all' で全件）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if state and state != "all":
            cursor = await db.execute(
                "SELECT * FROM youtube_videos WHERE state = ? "
                "ORDER BY published_at DESC LIMIT ?",
                (state, max(1, min(int(limit or 50), 200))),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM youtube_videos ORDER BY published_at DESC LIMIT ?",
                (max(1, min(int(limit or 50), 200)),),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def youtube_update_video_state(video_id: str, state: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE youtube_videos SET state = ? WHERE id = ?", (state, video_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def youtube_mark_notified(video_ids: list[str]) -> int:
    if not video_ids:
        return 0
    async with aiosqlite.connect(str(DB_PATH)) as db:
        placeholders = ",".join("?" for _ in video_ids)
        cursor = await db.execute(
            f"UPDATE youtube_videos SET notified = 1 WHERE id IN ({placeholders})",
            tuple(video_ids),
        )
        await db.commit()
        return cursor.rowcount


async def youtube_count_new() -> int:
    """未視聴（state='new'）の新着件数。バッジ表示に使う。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM youtube_videos WHERE state = 'new'"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def youtube_list_unnotified_new() -> list[dict]:
    """未通知かつ未視聴（state='new' AND notified=0）の動画一覧。ダイジェスト通知用。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM youtube_videos WHERE state = 'new' AND notified = 0 "
            "ORDER BY published_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def youtube_get_video(video_id: str) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM youtube_videos WHERE id = ?", (video_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def youtube_set_video_summary(video_id: str, summary: str) -> bool:
    """オンデマンド生成した動画要約をキャッシュする。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE youtube_videos SET summary = ? WHERE id = ?", (summary, video_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def youtube_set_video_detail_summary(video_id: str, detail_summary: str) -> bool:
    """保存用（後から読み返す）の詳しい要約を保存する。視聴判断用の短い要約とは別枠。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE youtube_videos SET detail_summary = ? WHERE id = ?",
            (detail_summary, video_id),
        )
        await db.commit()
        return cursor.rowcount > 0


# ===== 視聴セッション（ダラ見対策） =====

async def watch_create_session(
    app: str, source: str, declared_minutes: int = 0, reason: str = "",
) -> int:
    """視聴セッションを開始（active）。id を返す。"""
    now = datetime.datetime.now(JST)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO watch_sessions "
            "(app, source, reason, declared_minutes, started_at, status, date, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                app or "youtube", source or "webhook", reason or "",
                int(declared_minutes or 0), now.isoformat(),
                now.strftime("%Y-%m-%d"), now.isoformat(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def watch_get(session_id: int) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM watch_sessions WHERE id = ?", (int(session_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def watch_get_active(app: str | None = None) -> dict | None:
    """直近の active セッションを返す（app 指定でその種別のみ）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if app:
            cursor = await db.execute(
                "SELECT * FROM watch_sessions WHERE status = 'active' AND app = ? "
                "ORDER BY id DESC LIMIT 1",
                (app,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM watch_sessions WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1"
            )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def watch_list_active() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM watch_sessions WHERE status = 'active' ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def watch_finalize(session_id: int, duration_sec: int, status: str = "done") -> bool:
    now = datetime.datetime.now(JST)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE watch_sessions SET ended_at = ?, duration_sec = ?, status = ? "
            "WHERE id = ? AND status = 'active'",
            (now.isoformat(), int(duration_sec or 0), status, int(session_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def watch_set_reason(session_id: int, reason: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE watch_sessions SET reason = ? WHERE id = ?",
            (reason or "", int(session_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def watch_mark_reminded(session_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE watch_sessions SET reminded = 1 WHERE id = ?", (int(session_id),)
        )
        await db.commit()
        return cursor.rowcount > 0


async def watch_extend(session_id: int, add_minutes: int) -> bool:
    """宣言時間を延長し、回収リマインドを再予約する（reminded を戻す）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "UPDATE watch_sessions SET declared_minutes = declared_minutes + ?, reminded = 0 "
            "WHERE id = ? AND status = 'active'",
            (int(add_minutes or 0), int(session_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def watch_list_by_date(date: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM watch_sessions WHERE date = ? ORDER BY id DESC", (date,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def watch_today_total_minutes(date: str) -> int:
    """その日の完了セッションの合計分（done/abandoned の duration を合算）。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(duration_sec), 0) FROM watch_sessions "
            "WHERE date = ? AND status != 'active'",
            (date,),
        )
        row = await cursor.fetchone()
        return int((row[0] or 0) // 60) if row else 0

# ===== Error log =====

async def record_error(source: str, message: str, traceback_text: str = "") -> None:
    """バックグラウンドタスクや非同期処理での失敗を可視化するため DB に記録する。"""
    import datetime as _dt
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                "INSERT INTO error_log (created_at, source, message, traceback) VALUES (?, ?, ?, ?)",
                (
                    _dt.datetime.now(JST).isoformat(),
                    str(source)[:200],
                    str(message)[:2000],
                    str(traceback_text or "")[:8000],
                ),
            )
            await db.commit()
    except Exception:
        # ログ記録自体が失敗してもアプリは止めない
        import logging as _logging
        _logging.exception("record_error itself failed")


async def get_recent_errors(limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, created_at, source, message, traceback FROM error_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]



# ===== 習慣（habit_data.json から DB へ移行）=====

async def habit_load_all() -> dict:
    """habit_data.json と互換の形 `{"habits": [...], "logs": {date: [habit_id,...]}}` で返す。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, frequency_days, weekdays, trigger FROM habits ORDER BY sort_order ASC, id ASC"
        )
        rows = await cursor.fetchall()
        habits = []
        for r in rows:
            try:
                wd = json.loads(r["weekdays"] or "[]")
            except Exception:
                wd = []
            habits.append({
                "id": r["id"],
                "name": r["name"],
                "frequency_days": int(r["frequency_days"] or 1),
                "weekdays": wd,
                "trigger": r["trigger"] or "",
            })
        log_cursor = await db.execute("SELECT habit_id, date FROM habit_logs")
        logs: dict = {}
        for r in await log_cursor.fetchall():
            logs.setdefault(r["date"], []).append(r["habit_id"])
        return {"habits": habits, "logs": logs}


async def habit_save_all(data: dict) -> None:
    """`{"habits": [...], "logs": {...}}` を DB に丸ごと保存（差分計算はせず置き換え）。"""
    import datetime as _dt
    habits = data.get("habits") or []
    logs = data.get("logs") or {}
    now_iso = _dt.datetime.now(JST).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # 既存削除→挿入で同期（件数が少ないため十分高速）
        await db.execute("DELETE FROM habits")
        await db.execute("DELETE FROM habit_logs")
        for idx, h in enumerate(habits):
            try:
                wd_json = json.dumps(h.get("weekdays") or [])
            except Exception:
                wd_json = "[]"
            await db.execute(
                "INSERT INTO habits (id, name, frequency_days, weekdays, trigger, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(h.get("id") or idx + 1),
                    str(h.get("name") or ""),
                    int(h.get("frequency_days") or 1),
                    wd_json,
                    str(h.get("trigger") or ""),
                    idx,
                ),
            )
        for date_str, ids in logs.items():
            for hid in ids or []:
                try:
                    await db.execute(
                        "INSERT OR IGNORE INTO habit_logs (habit_id, date, completed_at) VALUES (?, ?, ?)",
                        (str(hid), str(date_str), now_iso),
                    )
                except Exception:
                    pass
        await db.commit()


async def habit_has_any_data() -> bool:
    """habits テーブルに 1件でも存在するか。移行判定用。"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT 1 FROM habits LIMIT 1")
        return (await cursor.fetchone()) is not None
