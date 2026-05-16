"""楽器練習サポート Cog（MVP: ドラム）。

Phase 1 スコープ:
  - 練習セッションの開始/終了/メモ追記（active_session 永続化で再起動耐性）
  - 10分以上の練習で `HabitCog.complete_habit("ドラム練習")` を呼び、習慣＋AI褒めを既存ロジックで発火
  - Obsidian Daily Notes の `## 🥁 Drum Practice` セクションへ自動追記
  - 静的ロードマップ（data/drum_roadmap.json）+ 進捗マージビュー
  - 今日の練習有無を判定するヘルパー（Phase 5 のリマインダで利用）

Phase 2 以降で動画ライブラリ・メニュー生成・リマインダ loop を追加する。
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from discord.ext import commands, tasks

from config import JST, BOT_FOLDER
from utils.obsidian_utils import update_section
from services.webclip_service import WebClipService

PRACTICE_DATA_FILE = "instrument_practice.json"
ROADMAP_FILE_PATH = Path(__file__).parent.parent / "data" / "drum_roadmap.json"
OBSIDIAN_SECTION = "## 🥁 Drum Practice"
DEFAULT_INSTRUMENT = "drum"
DEFAULT_MIN_HABIT_MINUTES = 10
HABIT_NAME = "ドラム練習"

VIDEO_LEVELS = ("beginner", "intermediate", "advanced")
VIDEO_CATEGORIES = (
    "rudiments",
    "groove",
    "fill",
    "song_cover",
    "technique",
    "theory",
)

VIDEO_CLASSIFY_PROMPT = """あなたはドラム指導者です。次のYouTube動画を視聴し、内容を分類してください。
返答は **JSONのみ** で次のキーを必ず含めること:
{{
  "level": "beginner" | "intermediate" | "advanced",
  "category": "rudiments" | "groove" | "fill" | "song_cover" | "technique" | "theory",
  "tags": ["短いタグの配列、最大5個"],
  "summary_jp": "60〜120字でこの動画が何を学べるかを日本語で要約",
  "reason": "分類根拠を100字以内で"
}}

参考メタデータ:
- タイトル: {title}
- チャンネル: {author}
- URL: {url}
"""

TEMPLATE_MENUS = {
    15: [
        ("ウォームアップ・グリップ確認", 3, "脱力した握りでリバウンドを感じる"),
        ("シングル/ダブル ストローク 80BPM", 5, "粒を揃える"),
        ("8ビート 80BPM", 5, "メトロノームと一体感"),
        ("クールダウン", 2, "脱力して仕上げる"),
    ],
    30: [
        ("ウォームアップ", 5, "肩・手首をほぐす"),
        ("ルーディメンツ 4種", 10, "シングル/ダブル/パラディドル/フラム"),
        ("8ビート + フィル", 10, "1〜2小節フィルを混ぜる"),
        ("曲練（レパートリーから1曲）", 5, "通しで叩く"),
    ],
    60: [
        ("ウォームアップ", 5, "脱力で開始"),
        ("ルーディメンツ 6種", 15, "テンポを上げて精度を保つ"),
        ("グルーヴパターン 3種", 15, "ジャンル別フィール"),
        ("フィル練習", 10, "4小節ループでバリエーション"),
        ("曲練", 10, "通しと部分練の組合せ"),
        ("録音&振り返り", 5, "客観的に課題抽出"),
    ],
}

MENU_AI_PROMPT = """あなたはドラム指導者です。以下の状況を踏まえ {minutes} 分の練習メニューを設計してください。

【状況】
- 進行中のマイルストーン: {wip_milestones}
- 直近3セッションのメモ: {recent_memos}
- 取り組み中の曲: {wip_songs}

【要件】
1. 合計時間は {minutes} 分 ±2 分
2. 順序: ウォームアップ → コア練習（進行中マイルストーン直結） → 曲練習 → 振り返り(2分)
3. 各項目に title / minutes / goal を必ず含める

【出力】
**JSON 配列のみ** を返してください。例:
[{{"title": "ウォームアップ", "minutes": 5, "goal": "脱力を意識"}}, ...]
"""


def _empty_data() -> dict:
    return {
        "instrument": DEFAULT_INSTRUMENT,
        "active_session": None,
        "sessions": [],
        "videos": [],
        "pending_videos": [],
        "repertoire": [],
        "roadmap_progress": {},
        "settings": {"min_habit_minutes": DEFAULT_MIN_HABIT_MINUTES},
    }


class InstrumentPracticeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_service = bot.drive_service
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self._roadmap_cache: dict | None = None
        self._data_lock = asyncio.Lock()
        self._reminder_last_run_date = None
        self.practice_reminder_loop.start()

    def cog_unload(self):
        try:
            self.practice_reminder_loop.cancel()
        except Exception:
            pass

    # ---------- データ I/O（habit_cog 流儀） ----------
    async def _load_data(self) -> dict:
        if not self.drive_service:
            return _empty_data()
        service = self.drive_service.get_service()
        if not service:
            return _empty_data()
        b_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, BOT_FOLDER
        )
        if not b_folder:
            b_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, BOT_FOLDER
            )
        f_id = await self.drive_service.find_file(service, b_folder, PRACTICE_DATA_FILE)
        if f_id:
            try:
                raw = await self.drive_service.read_text_file(service, f_id)
                loaded = json.loads(raw)
                merged = _empty_data()
                merged.update(loaded)
                # 設定が部分欠損していても落ちないように補完
                if "settings" not in loaded:
                    merged["settings"] = {"min_habit_minutes": DEFAULT_MIN_HABIT_MINUTES}
                else:
                    merged["settings"].setdefault(
                        "min_habit_minutes", DEFAULT_MIN_HABIT_MINUTES
                    )
                return merged
            except Exception as e:
                logging.error(f"instrument_practice.json の読込失敗: {e}")
        return _empty_data()

    async def _save_data(self, data: dict) -> None:
        if not self.drive_service:
            return
        service = self.drive_service.get_service()
        if not service:
            return
        b_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, BOT_FOLDER
        )
        if not b_folder:
            b_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, BOT_FOLDER
            )
        f_id = await self.drive_service.find_file(service, b_folder, PRACTICE_DATA_FILE)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if f_id:
            await self.drive_service.update_text(service, f_id, content)
        else:
            await self.drive_service.upload_text(
                service, b_folder, PRACTICE_DATA_FILE, content
            )

    def _load_roadmap(self) -> dict:
        if self._roadmap_cache is not None:
            return self._roadmap_cache
        try:
            with open(ROADMAP_FILE_PATH, "r", encoding="utf-8") as f:
                self._roadmap_cache = json.load(f)
        except Exception as e:
            logging.error(f"drum_roadmap.json の読込失敗: {e}")
            self._roadmap_cache = {"instrument": DEFAULT_INSTRUMENT, "phases": []}
        return self._roadmap_cache

    # ---------- セッション管理 ----------
    async def start_session(self, menu: list[str] | None = None) -> dict:
        """練習セッションを開始し、active_session を永続化する。"""
        async with self._data_lock:
            data = await self._load_data()
            now = datetime.now(JST)
            session_id = now.strftime("%Y%m%d-%H%M")
            data["active_session"] = {
                "id": session_id,
                "instrument": DEFAULT_INSTRUMENT,
                "started_at": now.isoformat(),
                "menu": list(menu) if menu else [],
                "memos": [],
            }
            await self._save_data(data)
            return data["active_session"]

    async def add_session_memo(self, text: str) -> bool:
        """進行中セッションにメモを追記する。アクティブセッションが無ければ False。"""
        if not text or not text.strip():
            return False
        async with self._data_lock:
            data = await self._load_data()
            if not data.get("active_session"):
                return False
            data["active_session"].setdefault("memos", []).append(
                {"at": datetime.now(JST).isoformat(), "text": text.strip()}
            )
            await self._save_data(data)
            return True

    async def end_session(self, memo: str = "") -> dict | None:
        """active_session を sessions[] に flush し、10分閾値で習慣完了 + Obsidian書込。

        戻り値: 確定したセッション dict（active_sessionが無ければ None）
        """
        async with self._data_lock:
            data = await self._load_data()
            active = data.get("active_session")
            if not active:
                return None

            now = datetime.now(JST)
            started_at = datetime.fromisoformat(active["started_at"])
            duration_min = max(0, int((now - started_at).total_seconds() // 60))
            threshold = int(
                data.get("settings", {}).get(
                    "min_habit_minutes", DEFAULT_MIN_HABIT_MINUTES
                )
            )

            # メモ統合: 進行中追記 + 終了時メモ
            collected_memos = [m["text"] for m in active.get("memos", []) if m.get("text")]
            if memo and memo.strip():
                collected_memos.append(memo.strip())
            joined_memo = " / ".join(collected_memos)

            session = {
                "id": active["id"],
                "instrument": active.get("instrument", DEFAULT_INSTRUMENT),
                "started_at": active["started_at"],
                "ended_at": now.isoformat(),
                "duration_min": duration_min,
                "menu": active.get("menu", []),
                "memo": joined_memo,
                "video_ids": active.get("video_ids", []),
                "habit_synced": False,
                "obsidian_synced": False,
            }

            data.setdefault("sessions", []).append(session)
            data["active_session"] = None
            # セッションは閾値判定の結果に関わらず先に永続化（途中失敗でも記録が残る）
            await self._save_data(data)

        # ---- ロック解放後の副作用 ----
        habit_msg: str | None = None
        if duration_min >= threshold:
            habit_msg = await self._mark_habit_completed()
            session["habit_synced"] = habit_msg is not None

        try:
            await self._sync_to_obsidian(session)
            session["obsidian_synced"] = True
        except Exception as e:
            logging.error(f"Obsidian書込に失敗: {e}")

        # 状態フラグを反映保存
        async with self._data_lock:
            data2 = await self._load_data()
            for s in data2.get("sessions", []):
                if s.get("id") == session["id"]:
                    s["habit_synced"] = session["habit_synced"]
                    s["obsidian_synced"] = session["obsidian_synced"]
                    break
            await self._save_data(data2)

        # PartnerCog に褒めメッセージを生成依頼（HabitCog が返した instruction を渡す）
        if habit_msg:
            partner = self.bot.get_cog("PartnerCog")
            if partner:
                try:
                    await partner.generate_and_send_routine_message("", habit_msg)
                except Exception as e:
                    logging.error(f"Partner褒め送信に失敗: {e}")

        return session

    async def _mark_habit_completed(self) -> str | None:
        """HabitCog 経由で「ドラム練習」習慣を完了マークする。返り値は Partner 向けの instruction 文字列。"""
        habit_cog = self.bot.get_cog("HabitCog")
        if not habit_cog:
            logging.warning("HabitCog がロードされていないため習慣連携をスキップ")
            return None
        try:
            return await habit_cog.complete_habit(HABIT_NAME)
        except Exception as e:
            logging.error(f"HabitCog.complete_habit 呼び出しに失敗: {e}")
            return None

    # ---------- 状態クエリ ----------
    async def get_state(self) -> dict:
        """PWA / API 向けの軽量ステート。active_session, today_minutes, streak。"""
        data = await self._load_data()
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        today_minutes = 0
        for s in data.get("sessions", []):
            ended = s.get("ended_at", "")
            if ended.startswith(today_str):
                today_minutes += int(s.get("duration_min") or 0)
        return {
            "instrument": data.get("instrument", DEFAULT_INSTRUMENT),
            "active_session": data.get("active_session"),
            "today_minutes": today_minutes,
            "streak_days": self._compute_streak(data),
            "settings": data.get("settings", {}),
        }

    def _compute_streak(self, data: dict) -> int:
        """終了済みセッションがある日を連続日数としてカウント。"""
        date_set = set()
        for s in data.get("sessions", []):
            ended = s.get("ended_at", "")
            if not ended:
                continue
            date_set.add(ended[:10])
        if not date_set:
            return 0
        streak = 0
        cur = datetime.now(JST).date()
        from datetime import timedelta
        while cur.strftime("%Y-%m-%d") in date_set:
            streak += 1
            cur -= timedelta(days=1)
        return streak

    async def _has_practiced_today(self) -> bool:
        """今日、終了済みセッション（duration_min >= 閾値）が1件でもあるか。Phase 5 のリマインダ用。"""
        data = await self._load_data()
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        threshold = int(
            data.get("settings", {}).get("min_habit_minutes", DEFAULT_MIN_HABIT_MINUTES)
        )
        for s in data.get("sessions", []):
            ended = s.get("ended_at", "")
            if ended.startswith(today_str) and int(s.get("duration_min") or 0) >= threshold:
                return True
        return False

    # ---------- ロードマップ ----------
    async def get_roadmap_view(self) -> dict:
        """静的ロードマップに roadmap_progress を重ねたビュー。"""
        roadmap = self._load_roadmap()
        data = await self._load_data()
        progress = data.get("roadmap_progress", {}) or {}

        phases = []
        done_count = 0
        total_count = 0
        for ph in roadmap.get("phases", []):
            milestones = []
            for m in ph.get("milestones", []):
                p = progress.get(m["id"], {}) or {}
                status = p.get("status", "todo")
                milestones.append({
                    **m,
                    "status": status,
                    "completed_at": p.get("completed_at"),
                    "started_at": p.get("started_at"),
                })
                total_count += 1
                if status == "done":
                    done_count += 1
            phases.append({
                "id": ph.get("id"),
                "label": ph.get("label"),
                "description": ph.get("description", ""),
                "milestones": milestones,
            })
        return {
            "instrument": roadmap.get("instrument", DEFAULT_INSTRUMENT),
            "phases": phases,
            "summary": {"done": done_count, "total": total_count},
        }

    async def mark_milestone(self, milestone_id: str, status: str) -> bool:
        """マイルストーンのステータスを更新（"todo"/"wip"/"done"）。"""
        if status not in ("todo", "wip", "done"):
            return False
        async with self._data_lock:
            data = await self._load_data()
            progress = data.setdefault("roadmap_progress", {})
            entry = progress.setdefault(milestone_id, {})
            entry["status"] = status
            now_iso = datetime.now(JST).isoformat()
            if status == "done":
                entry["completed_at"] = now_iso
            elif status == "wip" and "started_at" not in entry:
                entry["started_at"] = now_iso
            await self._save_data(data)
            return True

    # ---------- 動画ライブラリ ----------
    @staticmethod
    def _extract_youtube_video_id(url: str) -> str | None:
        """YouTube URL から video_id を取り出す。対応: youtu.be/<id>, youtube.com/watch?v=<id>, /shorts/<id>。"""
        if not url:
            return None
        try:
            parsed = urlparse(url.strip())
        except Exception:
            return None
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            vid = parsed.path.lstrip("/").split("/")[0]
            return vid or None
        if "youtube.com" in host or "m.youtube.com" in host:
            if parsed.path.startswith("/shorts/"):
                return parsed.path.split("/shorts/")[1].split("/")[0] or None
            qs = parse_qs(parsed.query or "")
            v = qs.get("v", [None])[0]
            return v
        return None

    @staticmethod
    def _classify_video_heuristic(title: str, author: str) -> dict:
        """Gemini が失敗したときのフォールバック。タイトルからざっくり推定。"""
        t = f"{title or ''} {author or ''}".lower()
        level = "beginner"
        if any(k in t for k in ("advanced", "上級", "プロ", "速弾き")):
            level = "advanced"
        elif any(k in t for k in ("intermediate", "中級")):
            level = "intermediate"

        category = "technique"
        if any(k in t for k in ("rudiment", "ルーディメンツ", "パラディドル", "stick")):
            category = "rudiments"
        elif any(k in t for k in ("fill", "フィル", "おかず")):
            category = "fill"
        elif any(k in t for k in ("cover", "カバー", "drum cover", "を叩いて")):
            category = "song_cover"
        elif any(k in t for k in ("groove", "8beat", "16beat", "ビート", "シャッフル", "funk", "jazz")):
            category = "groove"
        elif any(k in t for k in ("theory", "理論", "譜面", "読譜")):
            category = "theory"

        return {
            "level": level,
            "category": category,
            "tags": [],
            "summary_jp": (title or "")[:100],
            "reason": "AI解析に失敗したためタイトルからの簡易推定",
        }

    def _normalize_suggestion(self, raw: dict, title: str, author: str) -> dict:
        """Geminiが返した分類JSONを正規化して既知のenumに丸める。"""
        level = (raw.get("level") or "").lower()
        if level not in VIDEO_LEVELS:
            level = "beginner"
        category = (raw.get("category") or "").lower()
        if category not in VIDEO_CATEGORIES:
            category = "technique"
        tags = raw.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if str(t).strip()][:5]
        summary = (raw.get("summary_jp") or raw.get("summary") or "").strip()
        if not summary:
            summary = (title or "")[:120]
        reason = (raw.get("reason") or "").strip()
        return {
            "level": level,
            "category": category,
            "tags": tags,
            "summary_jp": summary,
            "reason": reason,
        }

    async def propose_video_classification(self, url: str) -> dict | None:
        """YouTube URL を AI 分類して pending_videos に仮保存。提案 dict を返す。"""
        video_id = self._extract_youtube_video_id(url)
        if not video_id:
            return None

        # oEmbed でメタ取得（既存 WebClipService 再利用）
        title = ""
        author = ""
        try:
            webclip = WebClipService(self.drive_service, self.bot.gemini_client)
            info = await webclip.get_youtube_info(url)
            if info:
                title = info.get("title") or ""
                author = info.get("author_name") or ""
        except Exception as e:
            logging.warning(f"YouTube oEmbed 取得失敗: {e}")

        # Gemini 分類（investment_cog の _gemini_with_video を流用）
        suggestion: dict | None = None
        ai_raw = ""
        invest = self.bot.get_cog("InvestmentCog")
        if invest and self.bot.gemini_client:
            try:
                prompt = VIDEO_CLASSIFY_PROMPT.format(title=title, author=author, url=url)
                ai_raw = await invest._gemini_with_video(
                    prompt, url, feature_key="instrument_practice_classify"
                )
                parsed = invest._extract_json(ai_raw) if ai_raw else None
                if parsed:
                    suggestion = self._normalize_suggestion(parsed, title, author)
            except Exception as e:
                logging.warning(f"動画分類のGemini呼出に失敗: {e}")

        if not suggestion:
            suggestion = self._classify_video_heuristic(title, author)

        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        pending_entry = {
            "id": f"pending-{datetime.now(JST).strftime('%Y%m%d-%H%M%S')}-{video_id}",
            "video_id": video_id,
            "url": url,
            "title": title,
            "author": author,
            "thumbnail": thumbnail,
            "ai_suggestion": suggestion,
            "ai_raw": ai_raw,
            "created_at": datetime.now(JST).isoformat(),
        }

        async with self._data_lock:
            data = await self._load_data()
            data.setdefault("pending_videos", []).append(pending_entry)
            await self._save_data(data)

        return pending_entry

    async def confirm_video(self, pending_id: str, overrides: dict | None = None) -> dict | None:
        """pending_videos から videos へ昇格。overrides で AI 提案を上書き可能。"""
        async with self._data_lock:
            data = await self._load_data()
            pending_list = data.get("pending_videos", []) or []
            target = next((p for p in pending_list if p.get("id") == pending_id), None)
            if not target:
                return None

            suggestion = target.get("ai_suggestion", {}) or {}
            level = (overrides or {}).get("level") or suggestion.get("level") or "beginner"
            category = (
                (overrides or {}).get("category") or suggestion.get("category") or "technique"
            )
            tags = (overrides or {}).get("tags")
            if tags is None:
                tags = suggestion.get("tags", [])

            video_entry = {
                "id": f"yt_{target['video_id']}",
                "instrument": DEFAULT_INSTRUMENT,
                "video_id": target["video_id"],
                "url": target["url"],
                "title": target.get("title", ""),
                "author": target.get("author", ""),
                "thumbnail": target.get("thumbnail", ""),
                "level": level,
                "category": category,
                "tags": list(tags) if isinstance(tags, list) else [],
                "summary_jp": (overrides or {}).get("summary_jp") or suggestion.get("summary_jp", ""),
                "added_at": datetime.now(JST).isoformat(),
                "confirmed": True,
                "source": "gemini_v1",
            }

            # 同じ video_id が既にあれば置き換え
            videos = data.setdefault("videos", [])
            videos = [v for v in videos if v.get("video_id") != target["video_id"]]
            videos.append(video_entry)
            data["videos"] = videos

            data["pending_videos"] = [p for p in pending_list if p.get("id") != pending_id]
            await self._save_data(data)
            return video_entry

    async def discard_pending_video(self, pending_id: str) -> bool:
        async with self._data_lock:
            data = await self._load_data()
            before = data.get("pending_videos", []) or []
            after = [p for p in before if p.get("id") != pending_id]
            if len(after) == len(before):
                return False
            data["pending_videos"] = after
            await self._save_data(data)
            return True

    async def list_videos(
        self, level: str | None = None, category: str | None = None
    ) -> list[dict]:
        data = await self._load_data()
        videos = data.get("videos", []) or []
        if level:
            videos = [v for v in videos if v.get("level") == level]
        if category:
            videos = [v for v in videos if v.get("category") == category]
        return videos

    async def list_pending_videos(self) -> list[dict]:
        data = await self._load_data()
        return data.get("pending_videos", []) or []

    async def delete_video(self, video_id: str) -> bool:
        async with self._data_lock:
            data = await self._load_data()
            videos = data.get("videos", []) or []
            after = [v for v in videos if v.get("id") != video_id and v.get("video_id") != video_id]
            if len(after) == len(videos):
                return False
            data["videos"] = after
            await self._save_data(data)
            return True

    # ---------- Obsidian 書き込み ----------
    async def _sync_to_obsidian(self, session: dict) -> None:
        """セッション1件を Daily Notes の `## 🥁 Drum Practice` セクションへ追記する。"""
        if not self.drive_service:
            return
        service = self.drive_service.get_service()
        if not service:
            return

        ended_at = datetime.fromisoformat(session["ended_at"])
        date_str = ended_at.strftime("%Y-%m-%d")
        start_hm = datetime.fromisoformat(session["started_at"]).strftime("%H:%M")
        end_hm = ended_at.strftime("%H:%M")
        duration = session.get("duration_min", 0)
        menu_str = " / ".join(session.get("menu") or []) or "（メニュー未指定）"
        memo = session.get("memo") or ""

        line = f"- {start_hm}-{end_hm} ({duration}分) | {menu_str}"
        if memo:
            line += f" | memo: {memo}"

        daily_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, "DailyNotes"
        )
        if not daily_folder:
            daily_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, "DailyNotes"
            )

        f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
        if f_id:
            content = await self.drive_service.read_text_file(service, f_id)
        else:
            content = f"---\ndate: {date_str}\n---\n\n# Daily Note {date_str}\n"

        new_content = update_section(content, line, OBSIDIAN_SECTION)

        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(
                service, daily_folder, f"{date_str}.md", new_content
            )


    # ---------- 練習メニュー生成 ----------
    @staticmethod
    def _template_menu(minutes: int) -> list[dict]:
        items = TEMPLATE_MENUS.get(minutes) or TEMPLATE_MENUS[30]
        return [{"title": t, "minutes": m, "goal": g} for t, m, g in items]

    async def generate_menu(self, minutes: int, mode: str = "template") -> dict:
        """練習メニューを生成する。

        mode="template": 固定テンプレート
        mode="ai":       Gemini 逆算メニュー。失敗時はテンプレにフォールバック。
        """
        if minutes not in TEMPLATE_MENUS:
            minutes = 30
        if mode != "ai":
            return {"mode": "template", "minutes": minutes, "menu": self._template_menu(minutes)}

        # AI モード
        data = await self._load_data()
        roadmap = await self.get_roadmap_view()
        wip_milestones = [
            m["label"]
            for ph in roadmap.get("phases", [])
            for m in ph.get("milestones", [])
            if m.get("status") == "wip"
        ][:5]
        recent_memos = [
            s.get("memo", "")
            for s in (data.get("sessions") or [])[-3:]
            if s.get("memo")
        ]
        wip_songs = [
            r.get("title", "")
            for r in (data.get("repertoire") or [])
            if r.get("status") == "wip"
        ][:5]

        prompt = MENU_AI_PROMPT.format(
            minutes=minutes,
            wip_milestones=", ".join(wip_milestones) or "(なし)",
            recent_memos=" / ".join(recent_memos) or "(なし)",
            wip_songs=", ".join(wip_songs) or "(なし)",
        )

        invest = self.bot.get_cog("InvestmentCog")
        if not invest or not self.bot.gemini_client:
            return {"mode": "template", "minutes": minutes, "menu": self._template_menu(minutes)}

        try:
            text = await invest._gemini_plain(prompt, feature_key="instrument_practice_menu")
        except Exception as e:
            logging.error(f"メニューAI生成失敗: {e}")
            text = ""

        items_raw = None
        if text:
            # JSON 配列を抽出（_extract_json は dict 抽出なので自前で配列対応）
            import re as _re
            m = _re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, _re.DOTALL)
            candidate = m.group(1) if m else None
            if not candidate:
                start = text.find("[")
                end = text.rfind("]")
                if start != -1 and end != -1 and end > start:
                    candidate = text[start : end + 1]
            if candidate:
                try:
                    items_raw = json.loads(candidate)
                except json.JSONDecodeError:
                    items_raw = None

        if not isinstance(items_raw, list) or not items_raw:
            return {"mode": "template", "minutes": minutes, "menu": self._template_menu(minutes)}

        normalized = []
        for it in items_raw:
            if not isinstance(it, dict):
                continue
            normalized.append({
                "title": str(it.get("title", "")).strip(),
                "minutes": int(it.get("minutes") or 0),
                "goal": str(it.get("goal", "")).strip(),
            })
        if not normalized:
            return {"mode": "template", "minutes": minutes, "menu": self._template_menu(minutes)}

        return {"mode": "ai", "minutes": minutes, "menu": normalized}

    # ---------- リマインダ ----------
    @tasks.loop(minutes=1)
    async def practice_reminder_loop(self):
        """毎分チェック、設定時刻に未練習なら PartnerCog 経由で軽く促す。"""
        try:
            from services.schedule_resolver import is_due
        except Exception:
            return
        try:
            due, today = await is_due(
                "practice_reminder", "19:00", "daily", self._reminder_last_run_date
            )
        except Exception as e:
            logging.debug(f"practice_reminder is_due 失敗: {e}")
            return
        if not due:
            return
        self._reminder_last_run_date = today

        try:
            already = await self._has_practiced_today()
        except Exception:
            already = False
        if already:
            return

        partner = self.bot.get_cog("PartnerCog")
        if not partner:
            return
        try:
            await partner.generate_and_send_routine_message(
                "",
                "今日はまだドラム練習が記録されていません。"
                "LINE風のタメ口で短く明るく背中を押すメッセージを1〜2文で送ってください。"
                "定型文は避け、毎回違う言い回しで。",
            )
        except Exception as e:
            logging.error(f"練習リマインド送信に失敗: {e}")

    @practice_reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(InstrumentPracticeCog(bot))
