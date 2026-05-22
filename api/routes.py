import os
import re
import logging
import asyncio
import datetime
import json
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Header, UploadFile, File, Form
from pydantic import BaseModel

from api.database import (
    save_message, get_history, add_stocked_link, get_all_links,
    get_link_by_id, update_link_details, mark_link_as_saved,
    delete_stocked_link, get_todays_log, clear_history,
    delete_message_by_id, toggle_message_star, get_starred_messages,
    search_messages, add_push_subscription, remove_push_subscription,
    add_english_phrase, get_english_phrases, delete_english_phrase,
    set_message_label, get_labeled_messages, get_all_labels,
    get_quiz_phrase_pool, record_quiz_attempt,
    add_daily_question, get_pending_questions, get_questions_by_date,
    answer_daily_question, resolve_questions, delete_daily_question,
    get_reading_plan, update_reading_plan,
)
from api import notification_service
from services.info_service import InfoService
from web_parser import fetch_maps_info, parse_url_with_readability
from config import (
    JST,
    require_env,
    TIMEOUT_HTTP_SHORT,
    TIMEOUT_HTTP_DEFAULT,
    TIMEOUT_PLAYWRIGHT,
)
from utils.async_utils import safe_create_task

router = APIRouter(prefix="/api")

API_KEY = require_env("PWA_API_KEY")

_sleep_trend_cache: dict = {"data": None, "expires_at": None}
_dashboard_sleep_cache: dict = {"data": None, "expires_at": None}
_fitbit_semaphore: asyncio.Semaphore | None = None

# Fitbit データ用の永続キャッシュ（過去日のレコードをディスクに保存）
from pathlib import Path as _Path
_FITBIT_CACHE_PATH = _Path(__file__).parent.parent / "fitbit_cache.json"
_fitbit_cache_lock = asyncio.Lock()
_FITBIT_TODAY_TTL_SECONDS = 30 * 60  # 当日分は 30 分有効

_FITBIT_METRICS = (
    "sleep_score", "total_sleep_minutes", "time_in_bed_minutes",
    "deep_sleep_minutes", "rem_sleep_minutes", "light_sleep_minutes",
    "wake_sleep_minutes", "steps", "calories_out", "resting_heart_rate",
    "distance_km", "active_minutes_very", "active_minutes_fairly",
    "active_minutes_lightly", "sedentary_minutes",
    "hr_zone_fat_burn_minutes", "hr_zone_cardio_minutes", "hr_zone_peak_minutes",
)


def _fitbit_cache_load() -> dict:
    if not _FITBIT_CACHE_PATH.exists():
        return {}
    try:
        with open(_FITBIT_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fitbit_cache_save(cache: dict) -> None:
    try:
        with open(_FITBIT_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"fitbit_cache save error: {e}")


def _fitbit_record(stats: dict) -> dict:
    """get_stats() の結果から、キャッシュ・API レスポンス用の最小レコードを抽出。"""
    if not stats:
        return {}
    return {k: stats.get(k) for k in _FITBIT_METRICS if stats.get(k) is not None}


async def _fitbit_get_or_fetch(fitbit_service, target_date) -> dict:
    """指定日の stats をキャッシュ経由で取得。当日は短い TTL、過去日は無期限。"""
    import time as _time
    date_str = target_date.strftime("%Y-%m-%d")
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    is_today = date_str == today_str

    async with _fitbit_cache_lock:
        cache = _fitbit_cache_load()
        entry = cache.get(date_str)
        if entry:
            if not is_today:
                return entry.get("stats", {})
            # 当日: TTL チェック
            fetched_at = entry.get("fetched_at", 0)
            if (_time.time() - fetched_at) < _FITBIT_TODAY_TTL_SECONDS:
                return entry.get("stats", {})

    # キャッシュ未ヒット: API から取得
    async with _get_fitbit_semaphore():
        stats = await fitbit_service.get_stats(target_date)
    record = _fitbit_record(stats) if stats else {}

    async with _fitbit_cache_lock:
        cache = _fitbit_cache_load()
        cache[date_str] = {"stats": record, "fetched_at": _time.time()}
        # 古いエントリの掃除（180 日以上前）
        cutoff = (datetime.datetime.now(JST).date() - datetime.timedelta(days=180)).strftime("%Y-%m-%d")
        cache = {k: v for k, v in cache.items() if k >= cutoff}
        _fitbit_cache_save(cache)
    return record


async def fitbit_cache_prefetch(fitbit_service, days: int = 14) -> int:
    """Bot 側のスケジューラから呼ばれる事前取得関数。N 日分をキャッシュへ書き込み、件数を返す。"""
    days = max(1, min(days, 30))
    now_dt = datetime.datetime.now(JST)
    count = 0
    for i in range(days - 1, -1, -1):
        date = now_dt.date() - datetime.timedelta(days=i)
        try:
            await _fitbit_get_or_fetch(fitbit_service, date)
            count += 1
        except Exception as e:
            logging.debug(f"fitbit_cache_prefetch error for {date}: {e}")
    return count


def _get_fitbit_semaphore() -> asyncio.Semaphore:
    global _fitbit_semaphore
    if _fitbit_semaphore is None:
        _fitbit_semaphore = asyncio.Semaphore(2)
    return _fitbit_semaphore

async def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="認証に失敗しました。")

class ChatRequest(BaseModel):
    message: str
    reply_to_id: Optional[int] = None
    english_mode: bool = False
    client_msg_id: Optional[str] = None  # 二重送信防止用の冪等キー（フロントから付与）

# 冪等キー → (timestamp, ChatResponse) のキャッシュ。5 秒以内の同 ID 再送は弾く
_CHAT_IDEMPOTENCY_CACHE: dict = {}
_CHAT_IDEMPOTENCY_WINDOW_SEC = 5.0

class ChatResponse(BaseModel):
    reply: str
    user_message_id: Optional[int] = None
    assistant_message_id: Optional[int] = None
    translation: Optional[str] = None

class AuthRequest(BaseModel):
    password: str

APP_PASSWORD = require_env("PWA_PASSWORD")

@router.post("/auth")
async def authenticate(req: AuthRequest):
    if req.password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="パスワードが正しくありません。")
    return {"api_key": API_KEY}

async def _fetch_link_meta(url: str) -> dict:
    import aiohttp
    import re as _re
    import urllib.parse

    title = "Untitled"
    link_type = "web"

    # YouTube判定とタイトル取得強化
    if "youtube.com" in url or "youtu.be" in url:
        link_type = "youtube"
        try:
            safe_url = urllib.parse.quote(url, safe='')
            oembed = f"https://www.youtube.com/oembed?url={safe_url}&format=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(oembed, timeout=aiohttp.ClientTimeout(total=TIMEOUT_HTTP_SHORT)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title", "YouTube Video")
                        recipe_kw = ["レシピ", "作り方", "材料", "献立", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw):
                            link_type = "recipe"
        except Exception as e:
            logging.debug(f"YouTube oEmbed失敗: {e}")
        return {"title": title, "type": link_type}

    if "maps.google.com" in url or "maps.app.goo.gl" in url or "goo.gl/maps" in url or "/maps/" in url:
        link_type = "map"
        try:
            place_name, _ = await fetch_maps_info(url)
            if place_name and place_name != "Google Maps Location":
                title = place_name
        except Exception as e:
            logging.debug(f"Maps情報取得失敗: {e}")
        return {"title": title, "type": link_type}

    if "amazon.co.jp" in url or "amzn.to" in url or "amazon.com" in url:
        link_type = "book"
        try:
            pw_title, _ = await asyncio.wait_for(parse_url_with_readability(url), timeout=TIMEOUT_PLAYWRIGHT)
            if pw_title and pw_title not in ("No Title Found", "Untitled", ""):
                t = pw_title
                # Amazon固有のゴミを除去
                t = _re.sub(r"Amazon\.co\.jp\s*[:：]\s*", "", t)
                t = _re.sub(r"Amazon\.com\s*[:：]\s*", "", t)
                t = _re.sub(r"\s*\|\s*Amazon.*$", "", t)
                t = _re.sub(r"\s*:\s*Amazon.*$", "", t)
                # 「 | 著者名」以降を削除
                t = _re.sub(r"\s*[|｜]\s*.+$", "", t)
                # 【...】【...】などの補足を削除
                t = _re.sub(r"\s*【[^】]*】\s*$", "", t)
                # 「：副題」など長すぎる副題を削除（40文字超の場合メインタイトルのみ）
                colon_match = _re.match(r"^(.{3,40})[：:].+$", t)
                if colon_match:
                    t = colon_match.group(1)
                title = t.strip() or title
        except Exception as e:
            logging.error(f"Amazon Playwright fetch failed for {url}: {e}")
        return {"title": title if title else "Untitled", "type": link_type}

    recipe_domains = ["cookpad.com", "kurashiru.com", "delishkitchen.tv", "macaro-ni.jp",
                      "orangepage.net", "lettuceclub.net", "kyounoryouri.jp", "ajinomoto.co.jp"]
    if any(d in url for d in recipe_domains): link_type = "recipe"

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            }
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT_HTTP_DEFAULT), allow_redirects=True) as response:
                if response.status == 200:
                    html = await response.text(errors="replace")
                    match = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
                    if match:
                        title = match.group(1).strip()[:200]
                    if link_type == "web":
                        recipe_kw = ["レシピ", "作り方", "献立", "材料", "recipe", "cooking"]
                        if any(k in title.lower() for k in recipe_kw): link_type = "recipe"
                        elif "材料" in html[:5000] and "作り方" in html[:5000]: link_type = "recipe"
    except Exception as e: logging.error(f"Link meta fetch failed for {url}: {e}")

    return {"title": title if title else "Untitled", "type": link_type}


# --- Obsidian同期用共通関数 (英語表記統一) ---
async def sync_link_to_obsidian(chat_service, title: str, link_type: str, url: str,
                                purpose: str="", target_date: str="", memo: str="", summary: str="",
                                is_update: bool = False, old_title: str = ""):
    """リンク情報をObsidianに作成・更新する"""
    if not chat_service or not chat_service.drive_service: return
    service = chat_service.drive_service.get_service()
    if not service: return
    
    import re
    now = datetime.datetime.now(JST)
    folder_map = {"youtube": "YouTube", "recipe": "Recipes", "web": "WebClips", "map": "Places", "book": "BookNotes"}
    section_map = {"youtube": "## 📺 YouTube", "recipe": "## 🍳 Recipes", "web": "## 🔗 WebClips", "map": "## 🗺 Places", "book": "## 📖 Reading Log"}
    
    folder_name = folder_map.get(link_type, "WebClips")
    section_header = section_map.get(link_type, "## 🔗 WebClips")
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:80] or "Untitled"
    
    # 既存ファイルの検索ロジック (タイムスタンプの有無に関わらずタイトルで判定)
    existing_id = None
    target_filename = f"{safe_title}.md"

    # old_titleが指定されている場合はそちらでも検索（タイトル変更時の対応）
    search_titles = [safe_title]
    if old_title and old_title != title:
        safe_old = re.sub(r'[\\/*?:"<>|]', "", old_title)[:80]
        if safe_old:
            search_titles.append(safe_old)

    try:
        drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        f_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
        if not f_id: f_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

        for search_title in search_titles:
            q_title = search_title.replace("'", "\\'")
            query = f"'{f_id}' in parents and name contains '{q_title}' and trashed = false"
            results = await asyncio.to_thread(lambda: service.files().list(q=query, fields="files(id, name)").execute())
            for f in results.get("files", []):
                fname = f["name"]
                if fname == f"{search_title}.md" or fname.endswith(f"-{search_title}.md"):
                    existing_id = f["id"]
                    target_filename = fname
                    break
            if existing_id:
                break
    except Exception as e:
        logging.error(f"Obsidian Search Error: {e}")
        return

    if not existing_id:
        if is_update:
            return # 詳細編集時は新規作成しない
        if link_type != "book":
            timestamp = now.strftime("%Y%m%d%H%M%S")
            target_filename = f"{timestamp}-{safe_title}.md"

    daily_note_date = now.strftime("%Y-%m-%d")
    
    # Markdownコンテンツ作成 (空行を追加してセテックス見出し化を防止)
    note_content = f"# {title}\n\n"
    if purpose: note_content += f"**🎯 Purpose:** {purpose}\n"
    if target_date: note_content += f"**📅 Target Date:** {target_date}\n"
    if memo: note_content += f"**📝 Memo:** {memo}\n"
    if summary: note_content += f"\n**💡 Summary:**\n{summary}\n"
    
    if url: note_content += f"\n---\n## Link\n{url}\n\n"
    note_content += f"\n---\nSaved: {now.strftime('%Y-%m-%d %H:%M')}\n[[{daily_note_date}]]"

    try:
        if existing_id:
            # 書籍ノートの場合、読書メモ機能で蓄積された `## 📖 Reading Log` セクションが
            # is_update=True の上書きで消えるのを防ぐため、既存セクションを保持する。
            if link_type == "book":
                try:
                    old_content = await chat_service.drive_service.read_text_file(service, existing_id)
                    rm = re.search(r"## 📖 Reading Log\n(.*?)(?=\n## |\Z)", old_content, re.DOTALL)
                    reading_log = rm.group(1).strip() if rm else ""
                    if reading_log and "## 📖 Reading Log" not in note_content:
                        note_content += f"\n\n## 📖 Reading Log\n{reading_log}\n"
                except Exception as e:
                    logging.error(f"Reading Log preservation error: {e}")
            await chat_service.drive_service.update_text(service, existing_id, note_content)
            return # 更新時はデイリーノートへの追記をスキップ
        else:
            await chat_service.drive_service.upload_text(service, f_id, target_filename, note_content)

        # デイリーノートへの追記 (新規作成時のみ)
        link_str = f"- [[{folder_name}/{target_filename.replace('.md', '')}|{title}]]"
        daily_fid = await chat_service.drive_service.find_file(service, drive_root, "DailyNotes")
        if daily_fid:
            df_id = await chat_service.drive_service.find_file(service, daily_fid, f"{daily_note_date}.md")
            from utils.obsidian_utils import update_section
            if df_id:
                cur = await chat_service.drive_service.read_text_file(service, df_id)
                if link_str not in cur: await chat_service.drive_service.update_text(service, df_id, update_section(cur, link_str, section_header))
            else:
                initial_content = f"---\ndate: {daily_note_date}\n---\n\n# Daily Note {daily_note_date}\n\n{section_header}\n{link_str}\n"
                await chat_service.drive_service.upload_text(service, daily_fid, f"{daily_note_date}.md", initial_content)
    except Exception as e: logging.error(f"Obsidian Sync Error: {e}")


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(req: ChatRequest):
    from api import app
    import re
    import time as _time_mod
    from api.database import backup_db_to_drive

    # 冪等キーによる二重送信ガード
    if req.client_msg_id:
        now_ts = _time_mod.time()
        # 古いエントリを掃除
        expired = [k for k, (ts, _) in _CHAT_IDEMPOTENCY_CACHE.items() if now_ts - ts > _CHAT_IDEMPOTENCY_WINDOW_SEC]
        for k in expired:
            _CHAT_IDEMPOTENCY_CACHE.pop(k, None)
        cached = _CHAT_IDEMPOTENCY_CACHE.get(req.client_msg_id)
        if cached and (now_ts - cached[0]) <= _CHAT_IDEMPOTENCY_WINDOW_SEC:
            # 同じ冪等キーの再リクエスト → 前回のレスポンスをそのまま返す
            return cached[1]
        # 進行中のキーは仮押さえ（後でレスポンスを上書き保存）
        _CHAT_IDEMPOTENCY_CACHE[req.client_msg_id] = (now_ts, None)

    # ユーザーが反応した → 保留中の定時通知があれば1件放出する
    try:
        await notification_service.mark_user_responded()
    except Exception as e:
        logging.debug(f"mark_user_responded failed: {e}")

    url_match = re.search(r"https?://[^\s]+", req.message)
    if url_match:
        url = url_match.group(0)
        try:
            meta = await _fetch_link_meta(url)
            link_id = await add_stocked_link(url, meta["type"], meta["title"])

            # Obsidianへの即時作成
            chat_service = getattr(app.state, "chat_service", None)
            if chat_service:
                await sync_link_to_obsidian(chat_service, meta["title"], meta["type"], url)

            type_label = {"web": "🌐 ウェブ", "youtube": "📺 YouTube", "recipe": "🍳 レシピ", "map": "🗺️ マップ", "book": "📚 書籍"}.get(meta["type"], "🔗 リンク")
            reply = f"「{meta['title']}」を{type_label}としてストックし、ノートを作成しました。"
            if link_id:
                reply += f"\n[ACTION:open_link(id={link_id})]"
            user_id = await save_message("user", req.message, reply_to=req.reply_to_id)
            asst_id = await notification_service.save_message_and_notify("assistant", reply)
            _resp = ChatResponse(reply=reply, user_message_id=user_id, assistant_message_id=asst_id)
            if req.client_msg_id:
                _CHAT_IDEMPOTENCY_CACHE[req.client_msg_id] = (_time_mod.time(), _resp)
            return _resp
        except Exception as e:
            logging.error(f"Link stock failed, falling back to AI: {e}")

    bot = getattr(app.state, "bot", None)
    if not bot: raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog: raise HTTPException(status_code=503, detail="AIコアがロードされていません。")

    user_id = await save_message("user", req.message, reply_to=req.reply_to_id)

    from google.genai import types
    db_history = await get_history(limit=15)
    history_messages = []
    _today_date = datetime.datetime.now(JST).date()
    for m in reversed(db_history[1:]):
        role = "model" if m["role"] == "assistant" else "user"
        # 各履歴メッセージに日時タグを付ける。これがないと AI が
        # 「昨日の朝の『今日の予定』メッセージ」を今日のものと誤認し、
        # 過去の予定を本日の予定として提示してしまう。
        text = m["content"]
        ts = m.get("timestamp")
        if ts:
            try:
                _mdt = datetime.datetime.fromisoformat(ts)
                if _mdt.date() == _today_date:
                    _tag = _mdt.strftime("今日 %H:%M")
                else:
                    _tag = _mdt.strftime("%Y-%m-%d %H:%M")
                text = f"[{_tag}]\n{text}"
            except Exception:
                pass
        history_messages.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))

    # 返信先のコンテキストをプロンプト前置で添付
    user_message = req.message
    translation = None
    if req.reply_to_id:
        try:
            quoted = next((m for m in db_history if m.get("id") == req.reply_to_id), None)
            if quoted:
                snippet = quoted["content"][:300]
                user_message = f"[返信先メッセージ: 「{snippet}」]\n\n{req.message}"
        except Exception as e:
            logging.debug(f"reply context attach failed: {e}")

    # ENモード: 日本語入力を英訳してからAIに送る
    import re as _re
    is_japanese = bool(_re.search(r'[ぁ-ん゠-ヿ一-鿿]', req.message))
    if req.english_mode and is_japanese:
        try:
            from google.genai import types as _types
            gemini_client = getattr(bot, "gemini_client", None)
            if gemini_client:
                from services.gemini_model_resolver import resolve_gemini_model as _rgm
                _m = await _rgm("english_translate", default_pro=False)
                trans_resp = await gemini_client.aio.models.generate_content(
                    model=_m,
                    contents=f"Translate the following Japanese text to natural English. Output only the English translation, nothing else.\n\n{req.message}"
                )
                translation = trans_resp.text.strip()
                if req.reply_to_id:
                    user_message = f"[Replying to: 「{snippet}」]\n\n{translation}" if 'snippet' in dir() else translation
                else:
                    user_message = translation
        except Exception as e:
            logging.debug(f"EN mode translation failed: {e}")

    # 英語メッセージのフィードバック（ENモードOFF、英語入力時）
    english_feedback_hint = ""
    if not req.english_mode and not is_japanese and _re.search(r'[a-zA-Z]', req.message) and len(req.message) > 5:
        english_feedback_hint = "\n\n[SYSTEM HINT: The user has written in English. Naturally include brief, encouraging feedback on their English (grammar, naturalness, word choice) within your response. Keep feedback short and positive.]"

    reply = await partner_cog.generate_response_for_app(
        user_message + english_feedback_hint, history_messages, english_mode=req.english_mode
    )
    asst_id = await notification_service.save_message_and_notify("assistant", reply)

    if bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-chat",
        )

    _resp = ChatResponse(reply=reply, user_message_id=user_id, assistant_message_id=asst_id, translation=translation)
    if req.client_msg_id:
        _CHAT_IDEMPOTENCY_CACHE[req.client_msg_id] = (_time_mod.time(), _resp)
    return _resp

@router.get("/history", dependencies=[Depends(verify_api_key)])
async def history(limit: int = 100):
    return {"messages": await get_history(limit=limit)}


class PermanentNoteConfirmRequest(BaseModel):
    title: str
    content: str


@router.post("/permanent_notes/confirm", dependencies=[Depends(verify_api_key)])
async def permanent_notes_confirm(req: PermanentNoteConfirmRequest):
    """ユーザーが永久ノートの確認モーダルで『保存』を押した時に呼ばれる。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service:
        raise HTTPException(status_code=503, detail="ChatService 未初期化")
    title = (req.title or "").strip()
    content = (req.content or "").strip()
    if not title:
        return {"ok": False, "error": "タイトルが空です"}
    try:
        msg = await chat_service._create_permanent_note(title, content)
        return {"ok": True, "message": msg}
    except Exception as e:
        logging.error(f"permanent_notes confirm error: {e}")
        return {"ok": False, "error": "保存処理でエラー"}

@router.get("/dashboard", dependencies=[Depends(verify_api_key)])
async def dashboard():
    from api import app
    import datetime
    import re

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service: return {"tasks": [], "alter_log": "", "error": "サービス未接続"}

    sleep_stats = {"score": "N/A", "duration": "N/A"}
    service = chat_service.drive_service.get_service()
    if not service: return {"tasks": [], "alter_log": "", "sleep": sleep_stats}

    now = datetime.datetime.now(JST)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    display_date = f"{now.year}年{now.month}月{now.day}日 ({weekdays[now.weekday()]})"
    today_str = now.strftime("%Y-%m-%d")

    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    content = ""
    if folder_id:
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if f_id:
            try:
                content = await chat_service.drive_service.read_text_file(service, f_id)
            except Exception as e:
                logging.debug(f"daily note read failed: {e}")

    tasks = []
    task_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if task_match:
        for line in task_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- "):
                # タスク形式 ([x], [/]) と タイムライン形式の両方に対応
                cb_match = re.search(r"- \[(.)\] (.*)", line)
                if cb_match:
                    tasks.append({"text": cb_match.group(2), "done": (cb_match.group(1) == 'x')})
                else:
                    # タイムライン形式 (HH:mm - HH:mm活動名)
                    tasks.append({"text": line[2:].strip(), "is_log": True})

    alter_log = ""
    daily_journal = ""
    next_actions = ""
    mit_items: list[str] = []

    def extract_section(text, header):
        m = re.search(rf"{re.escape(header)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def extract_alter_log(text):
        m = re.search(r"## 💡 Insights & Thoughts\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m: m = re.search(r"## 🪞 Alter Log\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not m: m = re.search(r"## 🕵️ AI Assessment\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    alter_log = extract_alter_log(content)
    alter_log_date = today_str if alter_log else ""
    daily_journal = extract_section(content, "## 📔 Daily Journal") or ""
    daily_journal_date = today_str if daily_journal else ""
    next_actions_raw = extract_section(content, "## 🚀 Next Actions") or ""
    next_actions = next_actions_raw
    mit_raw = extract_section(content, "## 🎯 MIT") or ""
    # フロント側で完了/未完了を判定するため、`[ ] text` / `[x] text` の形式で返す。
    # （行頭の "- " のみ削除して `[ ]`/`[x]` プレフィックスは残す）
    mit_items = [
        re.sub(r"^-\s*", "", l).strip()
        for l in mit_raw.splitlines()
        if l.strip().startswith("- [")
    ]

    # 当日のログがない場合、昨日のノートから取得を試みる
    yesterday_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if folder_id:
        if not alter_log or not daily_journal:
            y_fid = await chat_service.drive_service.find_file(service, folder_id, f"{yesterday_str}.md")
            if y_fid:
                try:
                    y_content = await chat_service.drive_service.read_text_file(service, y_fid)
                    if not alter_log:
                        alter_log = extract_alter_log(y_content)
                        if alter_log:
                            alter_log_date = yesterday_str
                    if not daily_journal:
                        dj = extract_section(y_content, "## 📔 Daily Journal")
                        if dj:
                            daily_journal = dj
                            daily_journal_date = yesterday_str
                    if not next_actions:
                        na = extract_section(y_content, "## 🚀 Next Actions")
                        if na: next_actions = na
                except Exception as e:
                    logging.debug(f"yesterday content fetch failed: {e}")

    if not alter_log:
        alter_log = "本日の観察ログはまだ生成されていません"

    g_calendar = []
    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        g_calendar = await chat_service.calendar_service.get_raw_events_for_date(today_str)

    google_tasks_work = []
    google_tasks_private = []
    habits = []
    if hasattr(chat_service, "tasks_service") and chat_service.tasks_service:
        try:
            work_uncompleted = await chat_service.tasks_service.get_raw_tasks("仕事")
            work_done_today = await chat_service.tasks_service.get_completed_tasks_today("仕事")
            google_tasks_work = work_uncompleted + [
                {"id": f"done_w_{i}", "title": t, "notes": "", "completed": True}
                for i, t in enumerate(work_done_today)
            ]

            private_uncompleted = await chat_service.tasks_service.get_raw_tasks("プライベート")
            private_done_today = await chat_service.tasks_service.get_completed_tasks_today("プライベート")
            google_tasks_private = private_uncompleted + [
                {"id": f"done_p_{i}", "title": t, "notes": "", "completed": True}
                for i, t in enumerate(private_done_today)
            ]

            habits = await chat_service.tasks_service.get_raw_tasks("習慣")
        except Exception as e:
            logging.debug(f"google tasks fetch failed: {e}")

    try:
        weather_data = await bot.info_service.get_weather() if hasattr(bot, "info_service") else await InfoService().get_weather()
        raw_news = await (bot.info_service.get_news(limit=5) if hasattr(bot, "info_service") else InfoService().get_news(limit=5))
        news = []
        for n in raw_news:
            if isinstance(n, dict):
                news.append({"title": n.get("title", ""), "link": n.get("link", "#")})
            else:
                parts = str(n).split('\n')
                if len(parts) >= 2: news.append({"title": parts[0], "link": parts[1]})
                else: news.append({"title": str(n), "link": "#"})
    except Exception:
        weather_data = {"summary": "取得失敗"}
        news = []

    fitbit_cog = bot.get_cog("FitbitCog")
    if fitbit_cog and fitbit_cog.is_ready:
        now_dt = datetime.datetime.now(JST)
        cached = _dashboard_sleep_cache
        if cached["data"] and cached["expires_at"] and now_dt < cached["expires_at"]:
            sleep_stats = cached["data"]
        else:
            try:
                async with _get_fitbit_semaphore():
                    stats = await fitbit_cog.fitbit_service.get_stats(now_dt.date())
                if stats:
                    score = stats.get("sleep_score")
                    raw_duration = stats.get("total_sleep_minutes")
                    sleep_stats = {"score": score or "N/A", "duration": fitbit_cog._format_minutes(raw_duration) if raw_duration else "N/A"}
                    cached["data"] = sleep_stats
                    cached["expires_at"] = now_dt + datetime.timedelta(minutes=10)
            except Exception as e:
                logging.debug(f"fitbit dashboard stats failed: {e}")

    return {
        "tasks": tasks, "alter_log": alter_log, "date": display_date, "g_calendar": g_calendar,
        "google_tasks_work": google_tasks_work, "google_tasks_private": google_tasks_private,
        "habits": habits, "weather": weather_data, "news": news, "sleep": sleep_stats,
        "daily_journal": daily_journal,
        "daily_journal_date": daily_journal_date,
        "alter_log_date": alter_log_date,
        "next_actions": next_actions,
        "mit": mit_items,
    }

class TaskActionRequest(BaseModel):
    action: str
    old_text: str = ""
    new_text: str = ""
    line_index: int = -1  # ライフログ行インデックス（編集/削除用）

@router.post("/task_action", dependencies=[Depends(verify_api_key)])
async def task_action(req: TaskActionRequest):
    from api import app
    import datetime
    import re
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service: raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
    file_name = f"{today_str}.md"
    f_id = await chat_service.drive_service.find_file(service, folder_id, file_name)

    content = f"# Daily Note {today_str}\n"
    if f_id:
        try: content = await chat_service.drive_service.read_text_file(service, f_id)
        except Exception: pass

    if req.action == "create":
        content = update_section(content, f"- [/] {req.new_text}", "## 🪟 Lifelog")
    elif req.action in ("edit_log", "delete_log"):
        # ライフログ行の編集/削除（line_index で特定）
        lifelog_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        if lifelog_match:
            section_start_pos = content.index("## 🪟 Lifelog\n") + len("## 🪟 Lifelog\n")
            section_text = lifelog_match.group(1)
            section_lines = section_text.split("\n")
            log_lines = [(idx, line) for idx, line in enumerate(section_lines) if line.strip().startswith("- ")]
            if 0 <= req.line_index < len(log_lines):
                target_idx, _old_line = log_lines[req.line_index]
                if req.action == "delete_log":
                    section_lines.pop(target_idx)
                elif req.action == "edit_log" and req.new_text:
                    section_lines[target_idx] = f"- {req.new_text}"
                new_section = "\n".join(section_lines)
                content = content[:section_start_pos] + new_section + content[section_start_pos + len(section_text):]
    else:
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith("- ") and req.old_text and req.old_text in line:
                if req.action == "delete": lines.pop(i)
                elif req.action == "update":
                    prefix = line[:6]
                    lines[i] = f"{prefix}{req.new_text}" if "[" in prefix and "]" in prefix else line.replace(req.old_text, req.new_text, 1)
                elif req.action == "toggle":
                    lines[i] = line.replace("- [x]", "- [/]", 1) if "- [x]" in line else line.replace("- [/]", "- [x]", 1)
                break
        content = '\n'.join(lines)

    if f_id: await chat_service.drive_service.update_text(service, f_id, content)
    else: await chat_service.drive_service.upload_text(service, folder_id, file_name, content)
    return {"status": "success"}

@router.post("/reset_history", dependencies=[Depends(verify_api_key)])
async def reset_history():
    from api import app
    from api.database import backup_db_to_drive

    await clear_history()

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-reset",
        )

    return {"status": "success"}

class CalendarActionRequest(BaseModel):
    action: str
    event_id: Optional[str] = None
    summary: str = None
    description: str = None
    start_time: str = None
    end_time: str = None

@router.post("/calendar_action", dependencies=[Depends(verify_api_key)])
async def calendar_action(req: CalendarActionRequest):
    from api import app
    import datetime
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.calendar_service: raise HTTPException(status_code=503, detail="カレンダーサービス未設定")
    
    if req.action == "add":
        start = req.start_time or datetime.datetime.now(JST).strftime("%Y-%m-%d 10:00:00")
        end = req.end_time or (datetime.datetime.strptime(start[:19], "%Y-%m-%d %H:%M:%S") + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S") if " " in start else start
        res = await bot.calendar_service.create_event(req.summary, start, end, req.description or "")
    elif req.action == "delete":
        res = await bot.calendar_service.delete_event(req.event_id)
    elif req.action == "update":
        res = await bot.calendar_service.update_event(req.event_id, summary=req.summary, description=req.description)
    else: res = "不明なアクションです"
    return {"status": "success", "message": res}

class GTaskActionRequest(BaseModel):
    action: str
    task_id: Optional[str] = None
    title: str = None
    completed: bool = None
    list_name: str = None
    due: Optional[str] = None  # RFC3339 (YYYY-MM-DDTHH:MM:SS.000Z) または YYYY-MM-DD

@router.post("/google_tasks_action", dependencies=[Depends(verify_api_key)])
async def google_tasks_action(req: GTaskActionRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service: raise HTTPException(status_code=503, detail="タスクサービス未設定")

    if req.action == "add":
        res = await bot.tasks_service.add_task(req.title, list_name=req.list_name, due=req.due)
    elif req.action == "delete":
        res = await bot.tasks_service.delete_task(req.task_id, list_name=req.list_name)
    elif req.action == "update":
        res = await bot.tasks_service.update_task(req.task_id, title=req.title, due=req.due, list_name=req.list_name)
    elif req.action == "toggle":
        res = await bot.tasks_service.update_task(req.task_id, completed=req.completed, list_name=req.list_name)
    else:
        res = "不明なアクションです"
    return {"status": "success", "message": res}

class GTaskMoveRequest(BaseModel):
    task_id: str
    previous_task_id: Optional[str] = None
    list_name: str = None
    parent: Optional[str] = None  # 親タスクIDを指定するとサブタスク化

@router.post("/google_tasks_move", dependencies=[Depends(verify_api_key)])
async def google_tasks_move(req: GTaskMoveRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service: raise HTTPException(status_code=503, detail="タスクサービス未設定")
    res = await bot.tasks_service.move_task(
        req.task_id, req.previous_task_id, req.list_name, parent=req.parent or None
    )
    return {"status": "success", "message": res}

@router.get("/sleep_trend", dependencies=[Depends(verify_api_key)])
async def sleep_trend():
    from api import app
    now_dt = datetime.datetime.now(JST)
    cached = _sleep_trend_cache
    if cached["data"] and cached["expires_at"] and now_dt < cached["expires_at"]:
        return cached["data"]

    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"trend": []}
    fitbit_cog = bot.get_cog("FitbitCog")
    if not fitbit_cog or not fitbit_cog.is_ready:
        return {"trend": []}

    async def fetch_day(i):
        date = now_dt.date() - datetime.timedelta(days=i)
        try:
            async with _get_fitbit_semaphore():
                stats = await fitbit_cog.fitbit_service.get_stats(date)
            if stats:
                return {
                    "date": date.strftime("%m/%d"),
                    "score": stats.get("sleep_score"),
                    "duration": stats.get("total_sleep_minutes"),
                }
        except Exception:
            pass
        return {"date": date.strftime("%m/%d"), "score": None, "duration": None}

    results = []
    for i in range(6, -1, -1):
        results.append(await fetch_day(i))

    result = {"trend": results}
    cached["data"] = result
    # 直近2日のいずれかのスコアがnullの場合は短時間だけキャッシュ（データ同期待ち）
    recent_missing = any(results[i].get("score") is None for i in [-1, -2] if i + len(results) >= 0)
    ttl = datetime.timedelta(minutes=2) if recent_missing else datetime.timedelta(minutes=10)
    cached["expires_at"] = now_dt + ttl
    return result

@router.post("/daily_report", dependencies=[Depends(verify_api_key)])
async def daily_report():
    return {"message": "日次整理が完了しました。"}

def _parse_habit_trigger(notes: str) -> tuple[str, str]:
    """notes 先頭行が "⏰ <trigger>" なら trigger と残りに分割。なければ ('', notes)"""
    if not notes:
        return "", ""
    lines = notes.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("⏰"):
        trigger = first[1:].lstrip(" ：:").strip()
        rest = "\n".join(lines[1:]).lstrip("\n")
        return trigger, rest
    return "", notes


def _serialize_habit_notes(trigger: str, rest: str) -> str:
    trigger = (trigger or "").strip()
    rest = rest or ""
    if trigger:
        if rest:
            return f"⏰ {trigger}\n\n{rest}"
        return f"⏰ {trigger}"
    return rest


@router.get("/google_tasks", dependencies=[Depends(verify_api_key)])
async def get_google_tasks(list_name: str = "仕事"):
    """指定リストのタスク（未完了 + 本日完了分）を軽量に返す。
    並び替え後の再描画など、ダッシュボード全体を再取得せずに該当リストだけを更新したいときに使う。
    """
    from api import app
    bot = getattr(app.state, "bot", None)
    tasks_service = getattr(bot, "tasks_service", None) if bot else None
    if not tasks_service:
        return {"tasks": []}
    try:
        uncompleted = await tasks_service.get_raw_tasks(list_name)
        done_today = await tasks_service.get_completed_tasks_today(list_name)
        tasks = uncompleted + [
            {"id": f"done_{list_name}_{i}", "title": t, "notes": "", "completed": True}
            for i, t in enumerate(done_today)
        ]
        return {"tasks": tasks}
    except Exception as e:
        logging.debug(f"get_google_tasks({list_name}) error: {e}")
        return {"tasks": []}


@router.get("/habits", dependencies=[Depends(verify_api_key)])
async def get_habits():
    from api import app
    import datetime
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    tasks_service = getattr(bot, "tasks_service", None) if bot else None

    if not tasks_service:
        return {"habits": [], "today_done": [], "streaks": {}}

    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")

    # Google Tasks「習慣」リストをマスターとして取得
    raw_uncompleted = await tasks_service.get_raw_tasks("習慣")
    completed_today_titles = await tasks_service.get_completed_tasks_today("習慣")

    # name -> (task_id, trigger_from_notes) のマップ（未完了タスクのみ）
    # trigger は habit_data 側を優先するが、移行のため notes も後方互換で読む
    task_meta_by_name = {}
    for t in raw_uncompleted:
        trig_notes, _ = _parse_habit_trigger(t.get("notes", ""))
        task_meta_by_name[t["title"]] = {"task_id": t["id"], "trigger_notes": trig_notes}

    # 未完了 + 今日完了済み = 今日表示すべき全習慣
    all_names = [t["title"] for t in raw_uncompleted] + completed_today_titles
    if not all_names:
        return {"habits": [], "today_done": [], "streaks": {}}

    def _meta(name):
        return task_meta_by_name.get(name, {"task_id": "", "trigger_notes": ""})

    if not habit_cog:
        habits_list = []
        for i, n in enumerate(all_names):
            m = _meta(n)
            habits_list.append({
                "id": str(i), "name": n, "frequency_days": 1,
                "trigger": m["trigger_notes"], "task_id": m["task_id"],
            })
        today_done = [str(i) for i, n in enumerate(all_names) if n in completed_today_titles]
        return {"habits": habits_list, "today_done": today_done, "streaks": {}}

    # HabitCog データと同期（Google Tasks にあってHabitCog にないものを追加）
    data = await habit_cog._load_data()
    changed = False
    for name in all_names:
        existing = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if not existing:
            existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
            new_id = str(max(existing_ids) + 1)
            data["habits"].append({"id": new_id, "name": name, "frequency_days": 1, "trigger": ""})
            changed = True

    # 後方互換: habit_data の trigger が空で、Google Tasks notes に trigger があれば移行
    for h in data["habits"]:
        if not h.get("trigger"):
            m = _meta(h["name"])
            if m.get("trigger_notes"):
                h["trigger"] = m["trigger_notes"]
                changed = True

    # 今日の完了ログに Google Tasks 完了済みを反映
    if today_str not in data["logs"]:
        data["logs"][today_str] = []
    for name in completed_today_titles:
        matching = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if matching and matching["id"] not in data["logs"][today_str]:
            data["logs"][today_str].append(matching["id"])
            changed = True

    if changed:
        await habit_cog._save_data(data)

    today_logs = data.get("logs", {}).get(today_str, [])
    today_date = datetime.datetime.now(JST).date()

    def _is_due_today(habit_data: dict, h_id: str) -> bool:
        freq = habit_data.get("frequency_days", 1)
        if freq <= 1:
            return True
        # 直近の完了日を探す
        for i in range(1, 90):
            d = (today_date - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            if h_id in data.get("logs", {}).get(d, []):
                days_since = i
                return days_since >= freq
        return True  # 一度も完了していなければ今日が対象

    # Google Tasks の順序を維持してレスポンスを組み立てる
    habits_list = []
    streaks = {}
    for name in all_names:
        matching = next((h for h in data["habits"] if h["name"].lower() == name.lower()), None)
        if matching:
            m = _meta(name)
            freq = matching.get("frequency_days", 1)
            due_today = _is_due_today(matching, matching["id"])
            # trigger は habit_data 側を優先（永続化）
            trigger_val = matching.get("trigger", "") or m.get("trigger_notes", "")
            weekdays = matching.get("weekdays") or []
            # 曜日指定がある場合は今日が指定曜日かどうかも反映
            if weekdays and today_date.weekday() not in weekdays:
                due_today = False
            habits_list.append({
                "id": matching["id"],
                "name": matching["name"],
                "frequency_days": freq,
                "weekdays": weekdays,
                "trigger": trigger_val,
                "task_id": m["task_id"],
                "due_today": due_today,
            })
            streaks[matching["id"]] = habit_cog._get_habit_stats(data, matching["id"], today_str)

    return {"habits": habits_list, "today_done": today_logs, "streaks": streaks}

class HabitCompleteRequest(BaseModel): habit_name: str
@router.post("/habits/complete", dependencies=[Depends(verify_api_key)])
async def complete_habit(req: HabitCompleteRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"status": "error", "message": "HabitCog not available"}
    result_msg = await habit_cog.complete_habit(req.habit_name)
    return {"status": "success", "message": result_msg}

@router.post("/habits/uncomplete", dependencies=[Depends(verify_api_key)])
async def uncomplete_habit(req: HabitCompleteRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"status": "error", "message": "HabitCog not available"}
    result_msg = await habit_cog.uncomplete_habit(req.habit_name)
    return {"status": "success", "message": result_msg}

class HabitAddRequest(BaseModel):
    name: str
    frequency_days: int = 1
    weekdays: Optional[List[int]] = None  # 0=月..6=日, None or 空 → 毎日

@router.post("/habits/add", dependencies=[Depends(verify_api_key)])
async def add_habit(req: HabitAddRequest):
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        raise HTTPException(status_code=503, detail="HabitCog不在")

    # weekdays をバリデーション（0..6 のみ、ソート＋重複排除）
    weekdays = sorted({d for d in (req.weekdays or []) if isinstance(d, int) and 0 <= d <= 6})

    data = await habit_cog._load_data()
    existing = next((h for h in data["habits"] if h["name"].lower() == req.name.lower()), None)
    if not existing:
        existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
        new_id = str(max(existing_ids) + 1)
        data["habits"].append({
            "id": new_id,
            "name": req.name,
            "frequency_days": req.frequency_days,
            "weekdays": weekdays,
        })
        await habit_cog._save_data(data)
    else:
        # 既存習慣の weekdays 更新（毎日↔曜日指定の切替えを許容）
        existing["weekdays"] = weekdays
        await habit_cog._save_data(data)

    if hasattr(bot, "tasks_service") and bot.tasks_service:
        await bot.tasks_service.add_task(req.name, list_name="習慣")

    return {"status": "success"}

@router.post("/habits/update", dependencies=[Depends(verify_api_key)])
async def update_habit(req: BaseModel):
    raise HTTPException(status_code=501, detail="この機能は未実装です。")


class HabitTriggerRequest(BaseModel):
    habit_name: str
    trigger: str = ""


@router.post("/habits/trigger", dependencies=[Depends(verify_api_key)])
async def set_habit_trigger(req: HabitTriggerRequest):
    """習慣の trigger（いつやるか）を habit_data.json に永続化する。
    Bot 再起動や Google Tasks の翌日リセットでも残るよう、保存先は HabitCog のデータ。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        raise HTTPException(status_code=503, detail="HabitCog 未起動")

    data = await habit_cog._load_data()
    target = next(
        (h for h in data["habits"] if h["name"] == req.habit_name),
        None,
    )
    if not target:
        target = next(
            (h for h in data["habits"] if req.habit_name.lower() in h["name"].lower()),
            None,
        )
    if not target:
        # 未登録の習慣にトリガーが設定されたケース → 新規エントリを作成
        existing_ids = [int(h["id"]) for h in data["habits"]] if data["habits"] else [0]
        new_id = str(max(existing_ids) + 1)
        target = {"id": new_id, "name": req.habit_name, "frequency_days": 1, "trigger": ""}
        data["habits"].append(target)

    target["trigger"] = req.trigger.strip()
    await habit_cog._save_data(data)
    return {"status": "success", "trigger": target["trigger"]}

@router.post("/habits/delete", dependencies=[Depends(verify_api_key)])
async def delete_habit_endpoint(req: BaseModel):
    raise HTTPException(status_code=501, detail="この機能は未実装です。")

@router.get("/habits/history", dependencies=[Depends(verify_api_key)])
async def get_habit_history(days: int = 28):
    import datetime as dt
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"history": []}
    days = max(1, min(days, 180))
    data = await habit_cog._load_data()
    today = dt.datetime.now(JST).date()
    total_habits = len(data.get("habits", []))
    history = []
    for i in range(days - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        done = len(data.get("logs", {}).get(d_str, []))
        rate = (done / total_habits) if total_habits > 0 else 0.0
        history.append({"date": d.strftime("%m/%d"), "rate": round(rate, 2), "done": done, "total": total_habits})
    return {"history": history}


@router.get("/habits/gantt", dependencies=[Depends(verify_api_key)])
async def get_habit_gantt(days: int = 90):
    """各習慣ごとの達成履歴をガントチャート用に返す。"""
    import datetime as dt
    from api import app
    bot = getattr(app.state, "bot", None)
    habit_cog = bot.get_cog("HabitCog") if bot else None
    if not habit_cog:
        return {"habits": [], "dates": []}
    days = max(7, min(days, 180))
    data = await habit_cog._load_data()
    today = dt.datetime.now(JST).date()
    dates = [(today - dt.timedelta(days=i)) for i in range(days - 1, -1, -1)]
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    logs = data.get("logs", {})
    habits = []
    for h in data.get("habits", []):
        h_id = h["id"]
        cells = [1 if h_id in logs.get(ds, []) else 0 for ds in date_strs]
        habits.append({
            "id": h_id,
            "name": h["name"],
            "cells": cells,
        })
    return {
        "habits": habits,
        "dates": [d.strftime("%m/%d") for d in dates],
        "date_strs": date_strs,
    }


@router.get("/task_candidates", dependencies=[Depends(verify_api_key)])
async def task_candidates():
    """タスク開始用はGoogle Tasksの「タスク候補」リストから取得。
    終了用は実行中ライフログタスク + タスク候補リスト（開始忘れ対応）。"""
    from api import app
    import datetime
    from config import JST
    import re

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service: return {"start": [], "end": []}

    # --- 開始候補: Google Tasks「タスク候補」リスト ---
    start_candidates = []
    tasks_service = getattr(chat_service, "tasks_service", None)
    if tasks_service:
        try:
            raw_tasks = await tasks_service.get_raw_tasks("タスク候補")
            start_candidates = [t["title"] for t in raw_tasks if t.get("title")]
        except Exception as e:
            logging.debug(f"タスク候補リスト取得失敗: {e}")

    # --- 終了候補: 実行中タスクを優先表示し、その後にタスク候補リストを連結 ---
    running = []
    if chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
            if folder_id:
                today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
                if f_id:
                    content = await chat_service.drive_service.read_text_file(service, f_id)
                    lifelog_match = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
                    if lifelog_match:
                        for line in lifelog_match.group(1).split("\n"):
                            line = line.strip()
                            if "▶" in line:
                                m = re.search(r"▶\s*(.+)$", line)
                                if m:
                                    running.append(m.group(1).strip())
        except Exception as e:
            logging.debug(f"終了候補（実行中）取得失敗: {e}")

    # 実行中 + タスク候補（重複除去・順序保持）
    end_candidates = list(dict.fromkeys(running + start_candidates))

    return {
        "start": start_candidates,
        "end": end_candidates,
        "running": running,
    }

@router.get("/book_notes", dependencies=[Depends(verify_api_key)])
async def get_book_notes(title: str):
    return {"title": title, "content": "Book notes content..."}

@router.get("/links", dependencies=[Depends(verify_api_key)])
async def get_links():
    return {"links": await get_all_links()}

class LinkCreateRequest(BaseModel):
    title: str = "Untitled"
    url: str = ""
    type: str = "web"

@router.post("/links", dependencies=[Depends(verify_api_key)])
async def create_link(req: LinkCreateRequest):
    """手動でのリンク（レシピ等）追加"""
    from api import app
    await add_stocked_link(req.url, req.type, req.title)
    
    links = await get_all_links()
    if not links: raise HTTPException(status_code=500)
    new_link = links[0]
    
    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, req.title, req.type, req.url)

    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-link-create",
        )

    return {"status": "success", "link_id": new_link["id"]}

class LinkUpdateRequest(BaseModel):
    title: str = ""
    purpose: str = ""
    summary: str = ""
    memo: str = ""
    target_date: str = ""
    linked_note_url: str = ""
    type: str = ""
    add_to_calendar: bool = False
    tags: str = ""

@router.put("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def update_link(link_id: int, req: LinkUpdateRequest):
    from api import app
    import datetime

    link = await get_link_by_id(link_id)
    if not link: raise HTTPException(status_code=404, detail="リンク未検出")

    old_title = link["title"] or ""
    new_title = req.title or old_title
    new_type = req.type or link["type"]
    existing_cal_event_id = link.get("calendar_event_id", "")

    # カレンダー処理（重複防止）
    new_cal_event_id = existing_cal_event_id
    if req.add_to_calendar and req.target_date:
        bot = getattr(app.state, "bot", None)
        if bot and bot.calendar_service:
            prefix = {"map": "🗺️[行]", "recipe": "🍳[食]", "book": "📚[本]"}.get(new_type, "📎[記]")
            cal_body = {
                "summary": f"{prefix} {new_title}",
                "description": f"目的: {req.purpose}\nメモ: {req.memo}\nURL: {link['url']}",
                "start": {"date": req.target_date},
                "end": {"date": (datetime.datetime.strptime(req.target_date, "%Y-%m-%d") + datetime.timedelta(days=1)).strftime("%Y-%m-%d")},
            }
            try:
                cal_svc = bot.calendar_service.get_service()
                if existing_cal_event_id:
                    cal_svc.events().update(calendarId="primary", eventId=existing_cal_event_id, body=cal_body).execute()
                else:
                    result = cal_svc.events().insert(calendarId="primary", body=cal_body).execute()
                    new_cal_event_id = result.get("id", "")
            except Exception as e:
                logging.warning(f"link calendar add/update failed: {e}")

    # DB更新
    await update_link_details(link_id, new_title, req.purpose, req.summary, req.memo, req.target_date, req.linked_note_url, new_type, req.tags, new_cal_event_id)

    # Obsidian更新 (Drive) — old_titleを渡してUntitled→新タイトルの更新に対応
    chat_service = getattr(app.state, "chat_service", None)
    await sync_link_to_obsidian(chat_service, new_title, new_type, link["url"], req.purpose, req.target_date, req.memo, req.summary, is_update=True, old_title=old_title)

    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-link-update",
        )

    return {"status": "success"}

@router.delete("/links/{link_id}", dependencies=[Depends(verify_api_key)])
async def delete_link(link_id: int):
    from api import app
    await delete_stocked_link(link_id)

    # クラウドバックアップ
    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        from api.database import backup_db_to_drive
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-link-delete",
        )

    return {"status": "success"}


# ===== 手書きメモ読み取り・保存 =====

class NoteFromImageRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    hint: str = ""

@router.post("/note_from_image", dependencies=[Depends(verify_api_key)])
async def note_from_image(req: NoteFromImageRequest):
    import base64
    from google.genai import types
    from api import app

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足情報: {req.hint}" if req.hint else ""
    prompt = f"""この手書きメモの画像を読み取り、以下のJSON形式で返してください。
文字が読みにくい場合は文脈から補完してください。{hint_text}

{{
  "transcription": "文字起こし（原文に近い形）",
  "structured_content": "整理・構造化した内容（Markdown形式。箇条書きや見出しを使い読みやすく。必要なら補足も加える）",
  "category": "work か study か idea か task か other のいずれか",
  "subject": "categoryがstudyの場合の科目名（例: 数学、英語）。それ以外は空文字",
  "action_items": ["タスク・TODOがあれば文字列の配列で。なければ空配列"]
}}"""

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = types.Part.from_text(text=prompt)
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("memo_image", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=types.Content(role="user", parts=[image_part, text_part]),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"note_from_image error: {e}")
        raise HTTPException(status_code=500, detail=f"読み取りに失敗しました: {str(e)}")


@router.get("/notes/list", dependencies=[Depends(verify_api_key)])
async def get_notes_list():
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"notes": []}

    service = chat_service.drive_service.get_service()
    if not service:
        return {"notes": []}

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    notes = []

    # 今日のデイリーノートを先頭に固定
    notes.append({
        "id": "TODAY_DAILY",
        "name": f"今日のデイリーノート ({today_str})",
        "folder": "DailyNotes",
        "filename": f"{today_str}.md",
    })

    # StudyLogs 内のファイルを取得
    try:
        study_folder = await chat_service.drive_service.find_file(service, drive_root, "StudyLogs")
        if study_folder:
            results = await asyncio.to_thread(
                lambda: service.files().list(
                    q=f"'{study_folder}' in parents and trashed = false",
                    fields="files(id, name)",
                    orderBy="modifiedTime desc",
                ).execute()
            )
            for f in results.get("files", []):
                display = f["name"].replace("_ノート.md", "").replace(".md", "")
                notes.append({
                    "id": f["id"],
                    "name": display,
                    "folder": "StudyLogs",
                    "filename": f["name"],
                })
    except Exception as e:
        logging.error(f"notes/list StudyLogs error: {e}")

    return {"notes": notes}


@router.get("/notes/search", dependencies=[Depends(verify_api_key)])
async def search_notes(q: str = "", limit: int = 8):
    """永久ノート（Notes フォルダ）からタイトル類似のファイルを検索する。"""
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"candidates": []}
    service = chat_service.drive_service.get_service()
    if not service:
        return {"candidates": []}
    q = (q or "").strip()
    if len(q) < 1:
        return {"candidates": []}

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    candidates = []
    try:
        notes_folder = await chat_service.drive_service.find_file(service, drive_root, "Notes")
        if not notes_folder:
            return {"candidates": []}
        q_esc = q.replace("'", "\\'")
        # Drive の name contains は単語単位なので、緩めに前方一致＋全件取得して
        # Python 側で類似度ソートする。
        results = await asyncio.to_thread(
            lambda: service.files().list(
                q=f"'{notes_folder}' in parents and trashed = false",
                fields="files(id, name, modifiedTime)",
                orderBy="modifiedTime desc",
                pageSize=200,
            ).execute()
        )
        files = results.get("files", [])

        def display_name(fname: str) -> str:
            # 例: 20240517123000-タイトル.md -> タイトル
            base = fname[:-3] if fname.endswith(".md") else fname
            import re as _re
            return _re.sub(r"^\d{8,14}-", "", base)

        q_lower = q.lower()
        scored = []
        for f in files:
            disp = display_name(f["name"])
            disp_lower = disp.lower()
            if q_lower in disp_lower:
                # 前方一致を優先
                score = 100 - disp_lower.index(q_lower) - (0 if disp_lower.startswith(q_lower) else 10)
            else:
                # 文字 overlap 簡易スコア
                overlap = sum(1 for ch in set(q_lower) if ch in disp_lower)
                if overlap < max(1, len(set(q_lower)) // 2):
                    continue
                score = overlap
            scored.append((score, {
                "id": f["id"],
                "name": disp,
                "folder": "Notes",
                "filename": f["name"],
                "modified": f.get("modifiedTime", ""),
            }))
        scored.sort(key=lambda x: (-x[0], x[1]["name"]))
        candidates = [s[1] for s in scored[:max(1, min(limit, 20))]]
    except Exception as e:
        logging.error(f"notes/search error: {e}")
    return {"candidates": candidates}


class SaveNoteRequest(BaseModel):
    mode: str  # "new" or "append"
    content: str
    action_items: List[str] = []
    # 新規の場合
    title: str = ""
    category: str = "other"
    subject: str = ""
    # 追記の場合
    target_id: str = ""
    target_folder: str = ""
    target_filename: str = ""

@router.post("/save_note", dependencies=[Depends(verify_api_key)])
async def save_note(req: SaveNoteRequest):
    from api import app
    from utils.obsidian_utils import update_section

    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="サービス未接続")

    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    drive_root = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    now = datetime.datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.strftime("%Y-%m-%d")

    try:
        if req.mode == "append":
            folder_name = req.target_folder
            filename = req.target_filename

            folder_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
            if not folder_id:
                folder_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

            file_id = await chat_service.drive_service.find_file(service, folder_id, filename)

            if file_id:
                existing = await chat_service.drive_service.read_text_file(service, file_id)
            else:
                existing = f"# {filename.replace('.md','')}\n"

            if folder_name == "DailyNotes":
                section = "## 💡 Insights & Thoughts"
            else:
                section = "## 📝 Learning Log"

            new_content = update_section(existing, f"*{now_str} 追記*\n{req.content}", section)

            if file_id:
                await chat_service.drive_service.update_text(service, file_id, new_content)
            else:
                await chat_service.drive_service.upload_text(service, folder_id, filename, new_content)

        else:
            # 新規作成
            title = req.title or f"メモ_{now_str}"

            if req.category == "study":
                folder_name = "StudyLogs"
                subject = req.subject or title
                filename = f"{subject}_ノート.md"
                initial = (
                    f"---\ntitle: {subject} 学習ノート\ndate: {today_str}\ntags: [study]\n---\n\n"
                    f"# {subject} 学習ノート\n\n## 📝 Learning Log\n"
                )
                section = "## 📝 Learning Log"
            elif req.category == "reading":
                # 読書メモは BookNotes フォルダに書籍タイトル単位で集約する
                import re as _re
                folder_name = "BookNotes"
                safe_title = _re.sub(r'[\\/*?:"<>|]', "", title)[:80] or "Untitled"
                filename = f"{safe_title}.md"
                initial = (
                    f"---\ntitle: {safe_title}\ndate: {today_str}\ntags: [book]\n---\n\n"
                    f"# {safe_title}\n\n## 📖 Reading Log\n"
                )
                section = "## 📖 Reading Log"
            else:
                folder_name = "Notes"
                filename = f"{now.strftime('%Y%m%d%H%M%S')}-{title[:40]}.md"
                initial = f"---\ntitle: {title}\ndate: {today_str}\n---\n\n# {title}\n"
                section = None

            folder_id = await chat_service.drive_service.find_file(service, drive_root, folder_name)
            if not folder_id:
                folder_id = await chat_service.drive_service.create_folder(service, drive_root, folder_name)

            existing_id = await chat_service.drive_service.find_file(service, folder_id, filename)

            if existing_id:
                existing = await chat_service.drive_service.read_text_file(service, existing_id)
                if section:
                    new_content = update_section(existing, f"*{now_str}*\n{req.content}", section)
                else:
                    new_content = existing + f"\n\n*{now_str}*\n{req.content}\n"
                await chat_service.drive_service.update_text(service, existing_id, new_content)
            else:
                if section:
                    new_content = update_section(initial, f"*{now_str}*\n{req.content}", section)
                else:
                    new_content = initial + f"\n{req.content}\n\nSaved: {now_str}\n"
                await chat_service.drive_service.upload_text(service, folder_id, filename, new_content)

        # action_items を Google Tasks に追加
        if req.action_items and bot and getattr(bot, "tasks_service", None):
            list_name = "仕事" if req.category == "work" else "プライベート"
            for item in req.action_items:
                if item.strip():
                    try:
                        await bot.tasks_service.add_task(item.strip(), list_name=list_name)
                    except Exception as e:
                        logging.error(f"save_note task add error: {e}")

        # 読書ノートはデイリーノートの Reading Log にもリンクを追記
        if req.mode == "new" and req.category == "reading":
            try:
                import re as _re
                safe_title = _re.sub(r'[\\/*?:"<>|]', "", req.title or "Untitled")[:80] or "Untitled"
                daily_link = f"- [[BookNotes/{safe_title}|{safe_title}]]"
                daily_fid = await chat_service.drive_service.find_file(service, drive_root, "DailyNotes")
                if daily_fid:
                    df_id = await chat_service.drive_service.find_file(service, daily_fid, f"{today_str}.md")
                    if df_id:
                        cur = await chat_service.drive_service.read_text_file(service, df_id)
                        if daily_link not in cur:
                            await chat_service.drive_service.update_text(
                                service, df_id, update_section(cur, daily_link, "## 📖 Reading Log")
                            )
                    else:
                        # 当日のデイリーノートが無ければ作成
                        initial_dn = f"---\ndate: {today_str}\n---\n\n# Daily Note {today_str}\n"
                        await chat_service.drive_service.upload_text(
                            service, daily_fid, f"{today_str}.md",
                            update_section(initial_dn, daily_link, "## 📖 Reading Log"),
                        )
            except Exception as e:
                logging.error(f"save_note reading daily link error: {e}")

    except Exception as e:
        logging.error(f"save_note error: {e}")
        raise HTTPException(status_code=500, detail=f"保存に失敗しました: {str(e)}")

    return {"status": "success"}


# ===== メッセージ操作 (削除 / star / 検索) =====

@router.delete("/messages/{message_id}", dependencies=[Depends(verify_api_key)])
async def delete_message(message_id: int):
    """会話履歴から1件削除し、Driveバックアップを起動。"""
    from api import app
    from api.database import backup_db_to_drive

    ok = await delete_message_by_id(message_id)
    if not ok:
        raise HTTPException(status_code=404, detail="該当メッセージが見つかりません。")

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-msg-delete",
        )
    return {"status": "success"}


@router.post("/messages/{message_id}/star", dependencies=[Depends(verify_api_key)])
async def star_message(message_id: int):
    """お気に入りトグル。新しい状態を返す。"""
    from api import app
    from api.database import backup_db_to_drive

    new_state = await toggle_message_star(message_id)
    if new_state is None:
        raise HTTPException(status_code=404, detail="該当メッセージが見つかりません。")

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-msg-star",
        )
    return {"status": "success", "starred": new_state}


@router.get("/messages/starred", dependencies=[Depends(verify_api_key)])
async def list_starred_messages(limit: int = 100):
    return {"messages": await get_starred_messages(limit=limit)}


@router.get("/messages/search", dependencies=[Depends(verify_api_key)])
async def search_messages_endpoint(q: str = "", limit: int = 50):
    if not q.strip():
        return {"results": []}
    rows = await search_messages(q.strip(), limit=limit)
    return {"results": rows}


# ===== 手書きメモ: 複数画像対応 =====

class ImagePayload(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"


class NoteFromImagesRequest(BaseModel):
    images: List[ImagePayload]
    hint: str = ""


@router.post("/note_from_images", dependencies=[Depends(verify_api_key)])
async def note_from_images(req: NoteFromImagesRequest):
    """複数画像を1ノートに統合読み取り。"""
    import base64
    from google.genai import types
    from api import app

    if not req.images:
        raise HTTPException(status_code=400, detail="画像が指定されていません。")

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足情報: {req.hint}" if req.hint else ""
    prompt = f"""これら {len(req.images)} 枚の手書きメモ画像をまとめて読み取り、1つのノートとして以下のJSON形式で返してください。
画像の順番がメモの流れを表します。読みにくい箇所は文脈から補完してください。{hint_text}

{{
  "transcription": "全画像を統合した文字起こし（原文に近い形）",
  "structured_content": "整理・構造化した内容（Markdown形式。箇条書きや見出しを使い、複数画像の内容を統合する）",
  "category": "work か study か idea か task か other のいずれか",
  "subject": "categoryがstudyの場合の科目名（例: 数学、英語）。それ以外は空文字",
  "action_items": ["タスク・TODOがあれば文字列の配列で。なければ空配列"]
}}"""

    try:
        parts = []
        for img in req.images:
            image_bytes = base64.b64decode(img.image_base64)
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=img.mime_type))
        parts.append(types.Part.from_text(text=prompt))
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("memo_image", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=types.Content(role="user", parts=parts),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"note_from_images error: {e}")
        raise HTTPException(status_code=500, detail=f"読み取りに失敗しました: {str(e)}")


# ===== ストックリンク一括既読化 =====

class LinkBulkStatusRequest(BaseModel):
    link_ids: List[int]
    status: str = "saved"


# ===== MIT (Most Important Tasks) =====

class MitSetRequest(BaseModel):
    items: List[str]


@router.post("/mit_set", dependencies=[Depends(verify_api_key)])
async def mit_set(req: MitSetRequest):
    """今日の MIT を DailyNote の `## 🎯 MIT` セクションに書き込む。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    msg = await partner_cog._set_mit_to_obsidian(req.items)
    return {"status": "success", "message": msg}


class DailyJournalUpdate(BaseModel):
    text: str


@router.get("/daily_journal", dependencies=[Depends(verify_api_key)])
async def daily_journal_get():
    """今日の日記（## 📔 Daily Journal セクション）の本文を返す。"""
    import re as _re
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"text": ""}
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            return {"text": ""}
        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if not f_id:
            return {"text": ""}
        content = await chat_service.drive_service.read_text_file(service, f_id)
        m = _re.search(r"## 📔 Daily Journal\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        return {"text": m.group(1).strip() if m else ""}
    except Exception as e:
        logging.debug(f"daily_journal_get error: {e}")
        return {"text": ""}


@router.post("/daily_journal", dependencies=[Depends(verify_api_key)])
async def daily_journal_set(req: DailyJournalUpdate):
    """今日の日記（## 📔 Daily Journal セクション）を上書き保存する（Obsidian反映）。"""
    import re as _re
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Drive サービス未設定")
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            folder_id = await chat_service.drive_service.create_folder(
                service, chat_service.drive_folder_id, "DailyNotes"
            )
        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if f_id:
            content = await chat_service.drive_service.read_text_file(service, f_id)
        else:
            content = f"---\ndate: {today_str}\n---\n\n# Daily Note {today_str}\n"

        new_text = (req.text or "").strip()
        section_header = "## 📔 Daily Journal"
        # 既存セクションを置き換え or 新規追加
        pattern = _re.compile(rf"{_re.escape(section_header)}\n.*?(?=\n## |\Z)", _re.DOTALL)
        replacement = f"{section_header}\n{new_text}" if new_text else section_header
        if pattern.search(content):
            new_content = pattern.sub(replacement, content, count=1)
        else:
            # 既存になければ utils.update_section で正しい位置に追加
            from utils.obsidian_utils import update_section
            new_content = update_section(content, new_text, section_header)

        if f_id:
            await chat_service.drive_service.update_text(service, f_id, new_content)
        else:
            await chat_service.drive_service.upload_text(
                service, folder_id, f"{today_str}.md", new_content
            )
        return {"status": "success"}
    except Exception as e:
        logging.error(f"daily_journal_set error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mit_get", dependencies=[Depends(verify_api_key)])
async def mit_get():
    """今日の MIT のみを軽量に取得する。設定モーダルの初期値表示に使う。"""
    import re as _re
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"items": []}
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            return {"items": []}
        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{today_str}.md")
        if not f_id:
            return {"items": []}
        content = await chat_service.drive_service.read_text_file(service, f_id)
        m = _re.search(r"## 🎯 MIT\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        if not m:
            return {"items": []}
        items = []
        for line in m.group(1).splitlines():
            line = line.strip()
            mm = _re.match(r"-\s*\[([ xX])\]\s*(.+)$", line)
            if mm:
                items.append({"text": mm.group(2).strip(), "done": mm.group(1).lower() == "x"})
        return {"items": items}
    except Exception as e:
        logging.debug(f"mit_get error: {e}")
        return {"items": []}


@router.post("/mit_rollover", dependencies=[Depends(verify_api_key)])
async def mit_rollover():
    """今日の未達 MIT を翌日に持ち越す。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    msg = await partner_cog._rollover_mit()
    return {"status": "success", "message": msg}


class MitToggleRequest(BaseModel):
    index: int


@router.post("/mit_toggle", dependencies=[Depends(verify_api_key)])
async def mit_toggle(req: MitToggleRequest):
    """今日のMITの `index` 番目（0始まり）の完了/未完了をトグルする。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 不在")
    result = await partner_cog._toggle_mit_in_obsidian(req.index)
    if result.get("status") != "success":
        raise HTTPException(status_code=400, detail=result.get("message", "MIT toggle 失敗"))
    return result


# ===== Web Push 通知 =====

class PushSubscriptionRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


@router.get("/vapid_public_key")
async def vapid_public_key():
    """VAPID 公開鍵を返す。サブスクリプション時にフロントが SW に渡す。
    認証不要にしているのは、未ログイン状態でも SW 登録時に取得したいため
    （秘密性は無く、漏れても問題ない値）。"""
    return {"key": notification_service.get_public_key(), "configured": notification_service.is_configured()}


@router.post("/push/subscribe", dependencies=[Depends(verify_api_key)])
async def push_subscribe(req: PushSubscriptionRequest):
    if not req.endpoint or not req.p256dh or not req.auth:
        raise HTTPException(status_code=400, detail="購読情報が不完全です。")
    await add_push_subscription(req.endpoint, req.p256dh, req.auth)
    return {"status": "success"}


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@router.post("/push/unsubscribe", dependencies=[Depends(verify_api_key)])
async def push_unsubscribe(req: PushUnsubscribeRequest):
    await remove_push_subscription(req.endpoint)
    return {"status": "success"}


@router.post("/push/test", dependencies=[Depends(verify_api_key)])
async def push_test():
    """通知テスト送信。設定確認用。"""
    count = await notification_service.send_push("通知テスト", "通知が届けば設定はOKだよ！")
    return {"status": "success", "delivered": count}


@router.post("/links/bulk_status", dependencies=[Depends(verify_api_key)])
async def bulk_update_link_status(req: LinkBulkStatusRequest):
    """複数リンクのステータスを一括更新する。"""
    from api import app
    from api.database import backup_db_to_drive

    if not req.link_ids:
        return {"status": "success", "updated": 0}
    # mark_link_as_saved は単一更新だが、一括で繰り返す（件数高々100オーダー想定）
    for lid in req.link_ids:
        try:
            await mark_link_as_saved(lid)
        except Exception as e:
            logging.warning(f"bulk_status update {lid} failed: {e}")

    bot = getattr(app.state, "bot", None)
    if bot and bot.drive_service:
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        safe_create_task(
            backup_db_to_drive(bot.drive_service, folder_id),
            name="db-backup-bulk-status",
        )
    return {"status": "success", "updated": len(req.link_ids)}


# ===== タスク分解 (AI Task Breakdown) =====

class TaskBreakdownRequest(BaseModel):
    message: str


class TaskBreakdownApplyRequest(BaseModel):
    list_name: str = "プライベート"
    subtasks: List[dict]
    parent_title: Optional[str] = ""


@router.get("/tasks_for_breakdown", dependencies=[Depends(verify_api_key)])
async def tasks_for_breakdown():
    """既存タスクから分解候補を返す（仕事＋プライベート、未完了のみ）"""
    from api import app
    bot = getattr(app.state, "bot", None)
    tasks_service = getattr(bot, "tasks_service", None) if bot else None
    if not tasks_service:
        return {"tasks": []}

    result = []
    for list_name in ["仕事", "プライベート"]:
        try:
            raw = await tasks_service.get_raw_tasks(list_name)
            for t in raw:
                result.append({
                    "id": t["id"],
                    "title": t["title"],
                    "list_name": list_name,
                })
        except Exception as e:
            logging.debug(f"tasks_for_breakdown list {list_name}: {e}")
    return {"tasks": result}


@router.post("/task_breakdown", dependencies=[Depends(verify_api_key)])
async def task_breakdown(req: TaskBreakdownRequest):
    """親タスクをAIでサブタスクに分解する。"""
    from api import app
    from prompts import PROMPT_TASK_BREAKDOWN
    from google.genai import types as gtypes

    parent = (req.message or "").strip()
    if not parent:
        raise HTTPException(status_code=400, detail="タスク内容を指定してください")

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = PROMPT_TASK_BREAKDOWN.replace("{parent_task}", parent)
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("task_breakdown", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        data = json.loads(response.text or "{}")
        subtasks = data.get("subtasks", [])
        if not isinstance(subtasks, list):
            subtasks = []
        return {"subtasks": subtasks, "parent": parent}
    except Exception as e:
        logging.error(f"task_breakdown error: {e}")
        raise HTTPException(status_code=500, detail=f"タスク分解に失敗しました: {str(e)}")


@router.post("/task_breakdown/apply", dependencies=[Depends(verify_api_key)])
async def task_breakdown_apply(req: TaskBreakdownApplyRequest):
    """分解結果を Google Tasks に追加する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.tasks_service:
        raise HTTPException(status_code=503, detail="タスクサービス未設定")

    list_name = req.list_name or "プライベート"
    added = 0
    for st in req.subtasks:
        title = (st.get("title") or "").strip()
        if not title:
            continue
        estimate = st.get("estimate")
        notes = f"⏱ {estimate}" if estimate else ""
        if req.parent_title:
            notes = f"親: {req.parent_title}\n{notes}".strip()
        try:
            await bot.tasks_service.add_task(title, list_name=list_name, notes=notes)
            added += 1
        except TypeError:
            await bot.tasks_service.add_task(title, list_name=list_name)
            added += 1
        except Exception as e:
            logging.error(f"task add error: {e}")

    return {"status": "success", "added": added, "message": f"{added}件をタスクに追加したよ！"}


# ===== 読書機能 (Reading) =====

class ReadingMemoRequest(BaseModel):
    book_title: str
    memo: str


class ReadingPromptRequest(BaseModel):
    book_title: str
    previous_prompts: List[str] = []
    current_pass: str = ""


@router.get("/reading/books", dependencies=[Depends(verify_api_key)])
async def reading_books():
    """読書候補となる書籍一覧を返す。
    1) ストック済みリンクの type=='book'
    2) BookNotes フォルダ内の既存ノート（過去に読書ログがある書籍）"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)

    books_from_links = []
    try:
        links = await get_all_links()
        for link in links:
            if link.get("type") == "book":
                books_from_links.append({
                    "title": link.get("title", "Untitled"),
                    "source": "stock",
                    "link_id": link.get("id"),
                    "url": link.get("url", ""),
                })
    except Exception as e:
        logging.debug(f"links fetch error: {e}")

    books_from_notes = []
    if chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            if service:
                folder_id = await chat_service.drive_service.find_file(
                    service, chat_service.drive_folder_id, "BookNotes"
                )
                if folder_id:
                    query = f"'{folder_id}' in parents and mimeType='text/markdown' and trashed=false"
                    results = await asyncio.to_thread(
                        lambda: service.files().list(
                            q=query, fields="files(id, name, modifiedTime)",
                            orderBy="modifiedTime desc", pageSize=30
                        ).execute()
                    )
                    for f in results.get("files", []):
                        title = f["name"].replace(".md", "")
                        if not any(b["title"] == title for b in books_from_links):
                            books_from_notes.append({
                                "title": title,
                                "source": "notes",
                                "link_id": None,
                                "url": "",
                            })
        except Exception as e:
            logging.debug(f"book notes fetch: {e}")

    return {"books": books_from_links + books_from_notes}


@router.post("/reading/save", dependencies=[Depends(verify_api_key)])
async def reading_save(req: ReadingMemoRequest):
    """読書メモを書籍ノートに保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    book_cog = bot.get_cog("BookCog") if bot else None
    if not book_cog:
        raise HTTPException(status_code=503, detail="BookCog不在")

    title = (req.book_title or "").strip() or "無題の書籍"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")

    ok = await book_cog.append_book_memo(title, memo)
    if not ok:
        raise HTTPException(status_code=500, detail="保存に失敗しました")
    return {"status": "success", "message": f"「{title}」のノートに保存したよ。"}


@router.post("/reading/prompt", dependencies=[Depends(verify_api_key)])
async def reading_prompt(req: ReadingPromptRequest):
    """読書中のマネージャーからの問いかけを生成する。"""
    from api import app
    from prompts import PROMPT_BOOK_READING_PROMPT
    from google.genai import types as gtypes

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prev = "\n".join(f"- {p}" for p in (req.previous_prompts or [])) or "（まだなし）"
    prompt = PROMPT_BOOK_READING_PROMPT.replace(
        "{book_title}", req.book_title or "無題"
    ).replace("{previous_prompts}", prev).replace(
        "{current_pass}", req.current_pass or "（指定なし）"
    )

    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("book_prompt", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=prompt,
        )
        text = (response.text or "").strip()
        return {"prompt": text}
    except Exception as e:
        logging.error(f"reading_prompt error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reading/log", dependencies=[Depends(verify_api_key)])
async def reading_log(book_title: str):
    """書籍ノートに蓄積された過去の読書メモ（Reading Log セクション）を返す。"""
    from api import app
    import re
    chat_service = getattr(app.state, "chat_service", None)
    title = (book_title or "").strip()
    log_text = ""
    if title and chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            if service:
                folder_id = await chat_service.drive_service.find_file(
                    service, chat_service.drive_folder_id, "BookNotes"
                )
                if folder_id:
                    f_id = await chat_service.drive_service.find_file(
                        service, folder_id, f"{title}.md"
                    )
                    if f_id:
                        content = await chat_service.drive_service.read_text_file(service, f_id)
                        m = re.search(r"## 📖 Reading Log\n(.*?)(?=\n## |\Z)", content or "", re.DOTALL)
                        if m:
                            log_text = m.group(1).strip()
        except Exception as e:
            logging.debug(f"reading_log fetch error: {e}")
    return {"book_title": title, "log": log_text}


class ReadingPlanRequest(BaseModel):
    book_title: str
    passes: List[dict] = []


@router.get("/reading/plan", dependencies=[Depends(verify_api_key)])
async def reading_plan_get(book_title: str):
    """書籍の多読プラン（段階リスト）を返す。無ければデフォルト5段階で作成。"""
    passes = await get_reading_plan(book_title)
    return {"book_title": (book_title or "").strip(), "passes": passes}


@router.put("/reading/plan", dependencies=[Depends(verify_api_key)])
async def reading_plan_put(req: ReadingPlanRequest):
    """書籍の多読プランを保存する（段階の編集・完了状態）。"""
    title = (req.book_title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="book_titleが空です")
    await update_reading_plan(title, req.passes)
    return {"status": "success", "book_title": title, "passes": req.passes}


# ===== 勉強機能 (Study) =====

class StudyMemoRequest(BaseModel):
    subject: str
    memo: str


@router.get("/study/subjects", dependencies=[Depends(verify_api_key)])
async def study_subjects():
    """既存の学習科目一覧を返す。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"subjects": []}

    service = chat_service.drive_service.get_service()
    if not service:
        return {"subjects": []}

    subjects = []
    try:
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "StudyLogs"
        )
        if folder_id:
            query = f"'{folder_id}' in parents and trashed=false"
            results = await asyncio.to_thread(
                lambda: service.files().list(
                    q=query, fields="files(id, name, modifiedTime)",
                    orderBy="modifiedTime desc", pageSize=30
                ).execute()
            )
            for f in results.get("files", []):
                name = f["name"]
                # ファイル名は "{subject}_ノート.md" 形式
                subject = name.replace("_ノート.md", "").replace(".md", "")
                subjects.append(subject)
    except Exception as e:
        logging.debug(f"study subjects fetch: {e}")
    return {"subjects": subjects}


@router.post("/study/save", dependencies=[Depends(verify_api_key)])
async def study_save(req: StudyMemoRequest):
    """学習メモを科目ノートに保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    study_cog = bot.get_cog("StudyCog") if bot else None
    if not study_cog:
        raise HTTPException(status_code=503, detail="StudyCog不在")

    subject = (req.subject or "").strip() or "雑記"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")

    ok = await study_cog.append_study_memo(subject, memo)
    if not ok:
        raise HTTPException(status_code=500, detail="保存に失敗しました")
    return {"status": "success", "message": f"「{subject}」の学習ノートに保存したよ。"}


# ===== ゼロ秒思考機能 (Zero Second Thinking) =====

class ZTThemeRequest(BaseModel):
    context: str = ""


class ZTDeepDiveRequest(BaseModel):
    original_theme: str
    user_memo: str


class ZTSaveRequest(BaseModel):
    theme: str
    memo: str
    session_id: Optional[str] = None  # 同一セッション（深掘り含む）を1つのノートに集約


@router.post("/zerosec/themes", dependencies=[Depends(verify_api_key)])
async def zerosec_themes(req: ZTThemeRequest):
    """ゼロ秒思考のテーマ候補を5つ返す。"""
    from api import app
    from prompts import PROMPT_ZT_THEMES_DETAILED
    from google.genai import types as gtypes

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = PROMPT_ZT_THEMES_DETAILED.replace(
        "{context}", req.context or "（特になし）"
    )
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("zt_themes", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        data = json.loads(response.text or "{}")
        themes = data.get("themes", [])
        if not isinstance(themes, list):
            themes = []
        return {"themes": themes[:5]}
    except Exception as e:
        logging.error(f"zerosec_themes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/zerosec/deep_dive", dependencies=[Depends(verify_api_key)])
async def zerosec_deep_dive(req: ZTDeepDiveRequest):
    """ユーザーが書いたメモから、深掘り用の追加テーマを5つ生成する。"""
    from api import app
    from prompts import PROMPT_ZT_DEEP_DIVE
    from google.genai import types as gtypes

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = (PROMPT_ZT_DEEP_DIVE
              .replace("{original_theme}", req.original_theme or "")
              .replace("{user_memo}", req.user_memo or ""))
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("zt_deep_dive", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        data = json.loads(response.text or "{}")
        themes = data.get("themes", [])
        if not isinstance(themes, list):
            themes = []
        return {"themes": themes[:5]}
    except Exception as e:
        logging.error(f"zerosec_deep_dive error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/zerosec/save", dependencies=[Depends(verify_api_key)])
async def zerosec_save(req: ZTSaveRequest):
    """ゼロ秒思考のメモをノートに保存し、ライフログにも記録する。
    session_id があれば同一ファイルに追記、なければ新規作成。"""
    from api import app
    import re as _re

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    service = chat_service.drive_service.get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Drive未接続")

    theme = (req.theme or "").strip() or "無題のテーマ"
    memo = (req.memo or "").strip()
    if not memo:
        raise HTTPException(status_code=400, detail="メモが空です")

    folder_id = await chat_service.drive_service.find_file(
        service, chat_service.drive_folder_id, "ZeroSecondThinking"
    )
    if not folder_id:
        folder_id = await chat_service.drive_service.create_folder(
            service, chat_service.drive_folder_id, "ZeroSecondThinking"
        )

    now = datetime.datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    # session_id が指定されていれば既存ファイルに追記
    session_id = req.session_id or now.strftime("%Y%m%d%H%M%S")
    safe_theme = _re.sub(r'[\\/*?:"<>|]', "", theme)[:60]
    file_name = f"{today_str}_{session_id}_{safe_theme}.md"

    # 既存セッションの場合は session_id プレフィックスでファイル検索
    existing_id = None
    if req.session_id:
        try:
            query = (f"'{folder_id}' in parents and trashed=false "
                     f"and name contains '{session_id}'")
            results = await asyncio.to_thread(
                lambda: service.files().list(
                    q=query, fields="files(id, name)"
                ).execute()
            )
            files = results.get("files", [])
            if files:
                existing_id = files[0]["id"]
                file_name = files[0]["name"]
        except Exception as e:
            logging.debug(f"zt existing file lookup: {e}")

    formatted_memo = memo.replace("\n", "\n> ")
    section_block = (
        f"\n## 🧠 {time_str} {theme}\n\n> {formatted_memo}\n"
    )

    if existing_id:
        existing = await chat_service.drive_service.read_text_file(service, existing_id)
        new_content = (existing or "").rstrip() + "\n" + section_block
        await chat_service.drive_service.update_text(service, existing_id, new_content)
    else:
        header = (
            f"---\ntitle: ゼロ秒思考 {today_str} {time_str}\n"
            f"date: {today_str}\ntags: [zero_second_thinking]\n---\n\n"
            f"# ゼロ秒思考セッション ({today_str} {time_str})\n"
            + section_block
        )
        await chat_service.drive_service.upload_text(service, folder_id, file_name, header)

    # ライフログにも記録（PartnerCog 経由）
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if partner_cog:
        try:
            await partner_cog._log_life_activity_to_obsidian(
                f"ゼロ秒思考: {theme[:30]}", "end"
            )
        except Exception as e:
            logging.debug(f"zt lifelog error: {e}")

    return {
        "status": "success",
        "session_id": session_id,
        "message": f"「{theme}」のメモを保存したよ。",
    }


@router.post("/zerosec/log_start", dependencies=[Depends(verify_api_key)])
async def zerosec_log_start(req: ZTThemeRequest):
    """ゼロ秒思考の開始をライフログに記録する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    partner_cog = bot.get_cog("PartnerCog") if bot else None
    if not partner_cog:
        return {"status": "skipped"}
    try:
        theme = (req.context or "テーマ未指定")[:30]
        await partner_cog._log_life_activity_to_obsidian(
            f"ゼロ秒思考: {theme}", "start"
        )
    except Exception as e:
        logging.debug(f"zt start log: {e}")
    return {"status": "success"}


# ===== タスク整理 =====

class TaskTriageRequest(BaseModel):
    list_name: str = "仕事"

@router.post("/task_triage", dependencies=[Depends(verify_api_key)])
async def task_triage(req: TaskTriageRequest):
    """指定リストのタスクをAIに整理提案させる。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not getattr(chat_service, "tasks_service", None):
        raise HTTPException(status_code=503, detail="Tasks サービス未接続")

    tasks = await chat_service.tasks_service.get_raw_tasks(req.list_name)
    if not tasks:
        return {"reply": f"「{req.list_name}」リストに未完了タスクがないよ。"}

    task_list_str = "\n".join(
        f"- {t['title']}" + (f" (締切: {t['due'][:10]})" if t.get('due') else "")
        for t in tasks
    )

    gemini_client = getattr(bot, "gemini_client", None) if bot else None
    if not gemini_client:
        return {"reply": f"「{req.list_name}」のタスク一覧:\n{task_list_str}\n\n（AI分析は現在利用できません）"}

    prompt = (
        f"あなたはタスク管理のプロフェッショナルです。\n"
        f"以下の「{req.list_name}」リストのタスクを分析し、整理提案をしてください。\n\n"
        f"タスク一覧:\n{task_list_str}\n\n"
        f"以下の観点で提案してください:\n"
        f"1. 優先度（高/中/低）の分類\n"
        f"2. 完了・削除を推奨するタスク\n"
        f"3. グループ化・統合できるタスク\n"
        f"4. 今日取り組むべきタスクのおすすめ\n\n"
        f"簡潔かつ実用的に日本語で回答してください。"
    )

    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("task_organize", default_pro=False)
        response = await gemini_client.aio.models.generate_content(
            model=_m, contents=prompt
        )
        reply = response.text.strip() if response.text else "分析に失敗しました。"
    except Exception as e:
        logging.error(f"Task triage AI error: {e}")
        reply = f"AI分析でエラーが発生しました。\n\nタスク一覧:\n{task_list_str}"

    return {"reply": reply}


# ===== ロケーションログ 手動同期 =====

class LocationSyncRequest(BaseModel):
    date: str = ""
    date_from: str = ""
    date_to: str = ""


@router.post("/location_log/sync", dependencies=[Depends(verify_api_key)])
async def location_log_sync(req: LocationSyncRequest):
    """指定日付（または日付範囲）のロケーションログをGoogle DriveのTimeline JSONから同期する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    cog = bot.get_cog("LocationLogCog")
    if not cog:
        raise HTTPException(status_code=503, detail="LocationLogCogが利用できません。")

    # 日付範囲が指定された場合は各日をループ同期
    date_from = (req.date_from or "").strip()
    date_to = (req.date_to or "").strip()
    single_date = (req.date or "").strip()

    if date_from and date_to:
        try:
            start = datetime.datetime.strptime(date_from, "%Y-%m-%d")
            end = datetime.datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="日付形式が不正です (YYYY-MM-DD)")
        if (end - start).days > 14:
            raise HTTPException(status_code=400, detail="最大14日間まで同期できます")
        if end < start:
            start, end = end, start

        results = []
        current = start
        while current <= end:
            d_str = current.strftime("%Y-%m-%d")
            try:
                r = await cog.perform_manual_sync(d_str)
                results.append(f"{d_str}: {r}")
            except Exception as e:
                results.append(f"{d_str}: エラー - {e}")
            current += datetime.timedelta(days=1)
        return {"status": "success", "message": "\n".join(results)}
    else:
        target_date = single_date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
        result = await cog.perform_manual_sync(target_date)
        return {"status": "success", "message": result}


# ===== 天気場所 =====

@router.get("/weather", dependencies=[Depends(verify_api_key)])
async def get_weather_data(location: str = ""):
    """指定場所の天気データを返す。locationはYahoo!天気コード (例: 33/6710)。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    info_svc = getattr(bot, "info_service", None) if bot else None
    if not info_svc:
        info_svc = InfoService()
    loc = location.strip() or None
    data = await info_svc.get_weather(location=loc)
    return data


@router.get("/weather/locations")
async def get_weather_locations():
    """利用可能な天気の場所一覧を返す（岡山 北部/南部のみ）。"""
    from services.info_service import YAHOO_WEATHER_REGIONS
    return {"regions": YAHOO_WEATHER_REGIONS}


# ===== 英語フレーズ帳 =====

class PhraseSaveRequest(BaseModel):
    phrase: str
    translation: str = ""
    context: str = ""

@router.get("/english_phrases", dependencies=[Depends(verify_api_key)])
async def list_english_phrases():
    phrases = await get_english_phrases()
    return {"phrases": phrases}

@router.post("/english_phrases", dependencies=[Depends(verify_api_key)])
async def save_english_phrase(req: PhraseSaveRequest):
    phrase_id = await add_english_phrase(req.phrase.strip(), req.translation.strip(), req.context.strip())
    return {"id": phrase_id}


class PhraseBulkItem(BaseModel):
    phrase: str
    translation: str = ""
    context: str = ""


class PhraseBulkRequest(BaseModel):
    phrases: List[PhraseBulkItem]


@router.post("/english_phrases/bulk", dependencies=[Depends(verify_api_key)])
async def save_english_phrases_bulk(req: PhraseBulkRequest):
    """複数の英語フレーズを一括保存する。長押しメッセージから複数文を選択保存する用途。"""
    saved_ids = []
    for item in req.phrases:
        phrase = (item.phrase or "").strip()
        if not phrase:
            continue
        pid = await add_english_phrase(
            phrase,
            (item.translation or "").strip(),
            (item.context or "").strip(),
        )
        if pid:
            saved_ids.append(pid)
    return {"saved": len(saved_ids), "ids": saved_ids}

@router.delete("/english_phrases/{phrase_id}", dependencies=[Depends(verify_api_key)])
async def remove_english_phrase(phrase_id: int):
    deleted = await delete_english_phrase(phrase_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="フレーズが見つかりません")
    return {"deleted": True}


@router.get("/english_phrases/quiz", dependencies=[Depends(verify_api_key)])
async def english_phrases_quiz():
    """正解率の低いフレーズを優先して 1 問返す。"""
    import random
    pool = await get_quiz_phrase_pool()
    if not pool:
        raise HTTPException(status_code=404, detail="フレーズが登録されていません")

    now = datetime.datetime.now(JST)

    def priority(p: dict) -> float:
        attempts = p.get("attempt_count") or 0
        correct = p.get("correct_count") or 0
        # 未試行は最優先
        if attempts == 0:
            return 999.0
        rate = correct / attempts
        # 経過日数ボーナス（最終試行から離れるほど優先）
        days_since = 0.0
        last = p.get("last_attempted_at")
        if last:
            try:
                last_dt = datetime.datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=JST)
                days_since = (now - last_dt).total_seconds() / 86400.0
            except Exception:
                pass
        return (1.0 - rate) * 0.7 + min(days_since, 30.0) / 30.0 * 0.3

    pool_sorted = sorted(pool, key=priority, reverse=True)
    top = pool_sorted[: min(8, len(pool_sorted))]
    chosen = random.choice(top)
    # 4 択用の誤答候補
    distractors = [p["phrase"] for p in pool if p["id"] != chosen["id"] and p.get("phrase")]
    random.shuffle(distractors)
    options = [chosen["phrase"]] + distractors[:3]
    random.shuffle(options)
    return {
        "id": chosen["id"],
        "phrase": chosen["phrase"],
        "translation": chosen.get("translation", ""),
        "context": chosen.get("context", ""),
        "options": options,
        "attempt_count": chosen.get("attempt_count", 0),
        "correct_count": chosen.get("correct_count", 0),
    }


class QuizAnswerRequest(BaseModel):
    phrase_id: int
    correct: bool


@router.post("/english_phrases/answer", dependencies=[Depends(verify_api_key)])
async def english_phrases_answer(req: QuizAnswerRequest):
    """クイズの正解/不正解を記録する。"""
    ok = await record_quiz_attempt(req.phrase_id, req.correct)
    if not ok:
        raise HTTPException(status_code=404, detail="フレーズが見つかりません")
    return {"status": "success"}


class TranslateSaveRequest(BaseModel):
    text: str

@router.post("/english_phrases/translate_and_save", dependencies=[Depends(verify_api_key)])
async def translate_and_save_phrase(req: TranslateSaveRequest):
    """ユーザーのテキスト（日本語）を英訳してフレーズ帳に保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.gemini_client:
        raise HTTPException(status_code=503, detail="AIサービス未接続")
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("english_translate", default_pro=False)
        resp = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=f"Translate the following Japanese text to natural, everyday English. Output only the English translation.\n\n{req.text}"
        )
        translation = resp.text.strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"翻訳に失敗しました: {e}")

    phrase_id = await add_english_phrase(translation, req.text, req.text[:300])
    return {"id": phrase_id, "phrase": translation, "translation": req.text}


# ===== メッセージコレクション =====

class LabelRequest(BaseModel):
    label: str

@router.post("/messages/{message_id}/label", dependencies=[Depends(verify_api_key)])
async def set_label(message_id: int, req: LabelRequest):
    ok = await set_message_label(message_id, req.label.strip())
    if not ok:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません")
    return {"ok": True, "label": req.label.strip()}

@router.get("/messages/collections", dependencies=[Depends(verify_api_key)])
async def list_collections():
    labels = await get_all_labels()
    return {"collections": labels}

@router.get("/messages/labeled", dependencies=[Depends(verify_api_key)])
async def labeled_messages(label: str = ""):
    if not label:
        raise HTTPException(status_code=400, detail="labelを指定してください")
    msgs = await get_labeled_messages(label)
    return {"messages": msgs, "label": label}


# ===== Fitbit全データ =====

@router.get("/fitbit_all_data", dependencies=[Depends(verify_api_key)])
async def fitbit_all_data(days: int = 14):
    """過去N日分のFitbitデータを返す（最大30日）。
    過去日はディスクキャッシュから即時応答、当日は 30 分 TTL でキャッシュする。
    全主要メトリクスを返却するためグラフ表示にも使える。"""
    from api import app
    days = max(1, min(days, 30))
    bot = getattr(app.state, "bot", None)
    fitbit_cog = bot.get_cog("FitbitCog") if bot else None
    if not fitbit_cog or not fitbit_cog.is_ready:
        return {"data": []}

    now_dt = datetime.datetime.now(JST)
    results = []
    for i in range(days - 1, -1, -1):
        date = now_dt.date() - datetime.timedelta(days=i)
        record = {}
        try:
            record = await _fitbit_get_or_fetch(fitbit_cog.fitbit_service, date)
        except Exception as e:
            logging.debug(f"fitbit fetch fail {date}: {e}")
        raw_dur = record.get("total_sleep_minutes")
        row = {
            "date": date.strftime("%m/%d"),
            "date_full": date.strftime("%Y-%m-%d"),
            "sleep_duration": fitbit_cog._format_minutes(raw_dur) if raw_dur else None,
        }
        for k in _FITBIT_METRICS:
            row[k] = record.get(k)
        # 後方互換用エイリアス
        row["calories"] = record.get("calories_out")
        results.append(row)

    return {"data": results}


# ===== ブリーフィング =====

@router.post("/briefing", dependencies=[Depends(verify_api_key)])
async def briefing():
    """朝（12時前）はモーニングブリーフィング、午後以降はイブニングレビューを生成する。"""
    from api import app
    import datetime

    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    now = datetime.datetime.now(JST)
    is_morning = now.hour < 12
    briefing_type = "morning" if is_morning else "evening"

    # コンテキスト収集
    context_parts = []

    # 今日の予定
    if hasattr(chat_service, "calendar_service") and chat_service.calendar_service:
        try:
            events = await chat_service.calendar_service.get_raw_events_for_date(now.strftime("%Y-%m-%d"))
            if events:
                ev_str = "\n".join(f"- {e.get('summary', '?')} ({e.get('start_time', '?')}〜{e.get('end_time', '?')})" for e in events[:10])
                context_parts.append(f"今日の予定:\n{ev_str}")
        except Exception:
            pass

    # タスク
    if hasattr(chat_service, "tasks_service") and chat_service.tasks_service:
        try:
            for ln in ["仕事", "プライベート"]:
                tasks = await chat_service.tasks_service.get_raw_tasks(ln)
                if tasks:
                    t_str = "\n".join(
                        f"- {t['title']}" + (f" (締切: {t['due'][:10]})" if t.get('due') else "")
                        for t in tasks[:8]
                    )
                    context_parts.append(f"{ln}タスク:\n{t_str}")
        except Exception:
            pass

    # 天気
    try:
        info_svc = getattr(bot, "info_service", None)
        if info_svc:
            w = await info_svc.get_weather()
            if w and w.get("summary") not in ("取得失敗", None):
                context_parts.append(f"天気: {w.get('summary', '不明')} (最高{w.get('max_temp','--')}℃ / 最低{w.get('min_temp','--')}℃)")
    except Exception:
        pass

    # ライフログ（夕方レビュー用）
    if not is_morning and chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(service, chat_service.drive_folder_id, "DailyNotes")
            if folder_id:
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{now.strftime('%Y-%m-%d')}.md")
                if f_id:
                    content = await chat_service.drive_service.read_text_file(service, f_id)
                    import re
                    m = re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
                    if m:
                        context_parts.append(f"今日のライフログ:\n{m.group(1).strip()[:500]}")
        except Exception:
            pass

    context = "\n\n".join(context_parts) if context_parts else "情報が取得できませんでした。"

    gemini_client = getattr(bot, "gemini_client", None) if bot else None
    if not gemini_client:
        return {"reply": f"現在の情報:\n{context}", "type": briefing_type}

    if is_morning:
        prompt = (
            f"あなたはユーザーの秘書AIです。今は{now.strftime('%Y年%m月%d日 %H:%M')}です。\n"
            f"以下の情報を元に、朝のブリーフィングを簡潔に作成してください。\n\n"
            f"{context}\n\n"
            f"ブリーフィング内容:\n"
            f"1. 今日の天気のひとこと\n"
            f"2. 今日の予定サマリー\n"
            f"3. 優先タスクの提案（上位3つ）\n"
            f"4. 今日のひとことアドバイス\n\n"
            f"親しみやすく、簡潔に日本語で回答してください。"
        )
    else:
        prompt = (
            f"あなたはユーザーの秘書AIです。今は{now.strftime('%Y年%m月%d日 %H:%M')}です。\n"
            f"以下の情報を元に、今日の振り返りレビューを作成してください。\n\n"
            f"{context}\n\n"
            f"レビュー内容:\n"
            f"1. 今日の活動サマリー\n"
            f"2. 良かった点\n"
            f"3. 明日に向けての提案\n\n"
            f"親しみやすく、簡潔に日本語で回答してください。"
        )

    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("daily_review", default_pro=False)
        response = await gemini_client.aio.models.generate_content(
            model=_m, contents=prompt
        )
        reply = response.text.strip() if response.text else "ブリーフィング生成に失敗しました。"
    except Exception as e:
        logging.error(f"Briefing AI error: {e}")
        reply = f"AI生成でエラーが発生しました。\n\n{context}"

    return {"reply": reply, "type": briefing_type}


# ===========================================================
# デイリーサマリー（1日の統合ログ）と質問キュー
# ===========================================================

async def _collect_daily_context(date_str: str) -> dict:
    """指定日の各種データを集約してデイリーサマリー生成用コンテキストを返す。"""
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    bot = getattr(app.state, "bot", None)
    ctx = {
        "date": date_str,
        "calendar": "",
        "weather": "",
        "lifelog": "",
        "location": "",
        "fitbit": "",
        "chat_log": "",
        "mit": "",
        "meals": "",
    }

    # 食事ログ（栄養アドバイス用）
    try:
        from api.database import get_meals_by_date
        meals = await get_meals_by_date(date_str)
        if meals:
            meal_lines = []
            total = {"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0}
            for m in meals:
                meal_lines.append(
                    f"- {m['time']} {m['name']}: {m['calories']}kcal "
                    f"(P{m['protein_g']:.0f}/F{m['fat_g']:.0f}/C{m['carbs_g']:.0f})"
                )
                total["calories"] += m["calories"] or 0
                total["protein_g"] += m["protein_g"] or 0
                total["fat_g"] += m["fat_g"] or 0
                total["carbs_g"] += m["carbs_g"] or 0
            meal_lines.append(
                f"\n当日合計: {total['calories']}kcal "
                f"(P{total['protein_g']:.0f}/F{total['fat_g']:.0f}/C{total['carbs_g']:.0f})"
            )
            ctx["meals"] = "\n".join(meal_lines)
    except Exception as e:
        logging.debug(f"summary meals error: {e}")

    # MIT セクション（達成状況含む）を Daily Note から抽出
    if chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(
                service, chat_service.drive_folder_id, "DailyNotes"
            )
            if folder_id:
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{date_str}.md")
                if f_id:
                    content = await chat_service.drive_service.read_text_file(service, f_id)
                    import re as _re
                    mm = _re.search(r"## 🎯 MIT\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
                    if mm:
                        ctx["mit"] = mm.group(1).strip()
        except Exception as e:
            logging.debug(f"summary mit error: {e}")

    # カレンダー予定
    if chat_service and getattr(chat_service, "calendar_service", None):
        try:
            events = await chat_service.calendar_service.get_raw_events_for_date(date_str)
            if events:
                ctx["calendar"] = "\n".join(
                    f"- {e.get('summary', '?')}（{e.get('start_time', '?')}〜{e.get('end_time', '?')}）"
                    for e in events[:30]
                )
        except Exception as e:
            logging.debug(f"summary calendar error: {e}")

    # 天気
    if bot and getattr(bot, "info_service", None):
        try:
            w = await bot.info_service.get_weather()
            if w and w.get("summary") not in ("取得失敗", None):
                ctx["weather"] = (
                    f"{w.get('summary', '?')} / 最高 {w.get('max_temp', '--')}℃ "
                    f"最低 {w.get('min_temp', '--')}℃"
                )
        except Exception as e:
            logging.debug(f"summary weather error: {e}")

    # ライフログ・位置情報（DailyNoteから）
    if chat_service and chat_service.drive_service:
        try:
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(
                service, chat_service.drive_folder_id, "DailyNotes"
            )
            if folder_id:
                f_id = await chat_service.drive_service.find_file(service, folder_id, f"{date_str}.md")
                if f_id:
                    note_content = await chat_service.drive_service.read_text_file(service, f_id)
                    import re as _re
                    m = _re.search(r"## 🪟 Lifelog\n(.*?)(?=\n## |\Z)", note_content, _re.DOTALL)
                    if m:
                        ctx["lifelog"] = m.group(1).strip()
                    m = _re.search(r"## 📍 Location History\n(.*?)(?=\n## |\Z)", note_content, _re.DOTALL)
                    if m:
                        ctx["location"] = m.group(1).strip()
        except Exception as e:
            logging.debug(f"summary lifelog/location error: {e}")

    # Fitbit
    fitbit_cog = bot.get_cog("FitbitCog") if bot else None
    if fitbit_cog and getattr(fitbit_cog, "is_ready", False):
        try:
            target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            stats = await _fitbit_get_or_fetch(fitbit_cog.fitbit_service, target_date)
            if stats:
                lines = []
                if stats.get("steps") is not None:
                    lines.append(f"歩数: {stats['steps']}")
                if stats.get("calories_out") is not None:
                    lines.append(f"消費カロリー: {stats['calories_out']}")
                if stats.get("total_sleep_minutes") is not None:
                    lines.append(f"総睡眠時間: {stats['total_sleep_minutes']}分")
                if stats.get("sleep_score") is not None:
                    lines.append(f"睡眠スコア: {stats['sleep_score']}")
                if stats.get("resting_heart_rate") is not None:
                    lines.append(f"安静時心拍: {stats['resting_heart_rate']}")
                ctx["fitbit"] = " / ".join(lines)
        except Exception as e:
            logging.debug(f"summary fitbit error: {e}")

    # チャットログ
    try:
        log = await get_todays_log()
        # 長すぎる場合は末尾 6000 文字程度に切り詰め
        if log and len(log) > 6000:
            log = log[-6000:]
        ctx["chat_log"] = log or ""
    except Exception:
        pass

    return ctx


def _format_daily_summary_context(ctx: dict) -> str:
    """コンテキスト dict を Gemini に渡す Markdown 文字列に整形。"""
    parts = [f"# 対象日: {ctx['date']}"]
    if ctx.get("mit"):
        parts.append(f"## 今日のMIT（達成状況含む。- [ ] が未達、- [x] が達成）\n{ctx['mit']}")
    if ctx.get("weather"):
        parts.append(f"## 天気\n{ctx['weather']}")
    if ctx.get("calendar"):
        parts.append(f"## カレンダー予定\n{ctx['calendar']}")
    if ctx.get("lifelog"):
        parts.append(f"## ライフログ\n{ctx['lifelog']}")
    if ctx.get("meals"):
        parts.append(f"## 食事（栄養観点で1〜2行コメントを必ず入れる）\n{ctx['meals']}")
    if ctx.get("location"):
        parts.append(f"## 移動履歴\n{ctx['location']}")
    if ctx.get("fitbit"):
        parts.append(f"## Fitbit\n{ctx['fitbit']}")
    if ctx.get("chat_log"):
        parts.append(f"## マネージャーとの会話（要約してOK）\n{ctx['chat_log']}")
    return "\n\n".join(parts)


async def _generate_daily_summary(date_str: str, answers: dict | None = None) -> dict:
    """Gemini を使ってサマリーを生成し、必要なら質問を返す。
    answers: {qid: text} の形で既存質問への回答を渡せる。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not bot.gemini_client:
        return {"summary": "", "questions": [], "error": "AI が接続されていません"}

    ctx = await _collect_daily_context(date_str)
    ctx_text = _format_daily_summary_context(ctx)

    answer_text = ""
    if answers:
        answer_lines = []
        existing_qs = await get_questions_by_date(date_str, scope='summary')
        q_by_id = {q['id']: q for q in existing_qs}
        for qid_str, ans in answers.items():
            try:
                qid = int(qid_str)
            except (TypeError, ValueError):
                continue
            q = q_by_id.get(qid)
            if q and ans:
                answer_lines.append(f"Q: {q['question']}\nA: {ans}")
        if answer_lines:
            answer_text = "\n\n## 既知の補足回答\n" + "\n\n".join(answer_lines)

    prompt = (
        "あなたはユーザーのマネージャーです。1日の統合ログ（デイリーサマリー）を Markdown で書きます。\n"
        "このサマリーはアプリの画面と Obsidian の `## 📅 Daily Summary` セクションに**同じ内容で**保存され、"
        "他の `## 📔 Daily Journal` `## 💡 Insights & Thoughts` `## 🚀 Next Actions` セクションとは別に表示されます。\n\n"
        "【重要】重複を避けるため、以下のルールを守ってください：\n"
        "1. Daily Journal は「Lifelog ＋ 客観データから生成された俯瞰的な振り返り日記」が別途保存されています。\n"
        "   サマリーでは Lifelog の単純な書き起こしは避け、**会話と出来事から見えた重要なトピックのまとめ**にフォーカスしてください。\n"
        "2. AI による洞察 (Insights) や明日のアクション (Next Actions) は別セクションに保存されるため、サマリー本文には含めないでください。\n"
        "3. サマリーは **1日全体を俯瞰する短いダイジェスト（合計 5〜8 行程度）** とし、`## MIT 進捗` `## 朝` `## 昼` `## 夜` のような小見出し（H2/H3）で時間帯やテーマに分割してはいけません。\n"
        "   見出しなしの箇条書き（`- ` で始まる行）または短い段落で、流れるように記述してください。\n"
        "4. **冒頭の最初の箇条書き 1〜2 行で必ず MIT（最重要タスク）の進捗・達成度に触れること**。例: `- MIT は 3/3 達成。特に〇〇が早めに片付いた。` のように本文に溶け込ませる。\n"
        "5. その後に、その日の主要な出来事・会話のハイライト・気づきを淡々と並べる。時系列で並べるのは構わないが「朝/昼/夜」のような明示的なラベルは付けない。\n"
        "6. 推測ではなく事実に基づいて記述してください。\n"
        "7. 判断に迷う点（例: 出来事の意図、感情の解釈、欠けている情報、MIT 未達の理由）があれば JSON の `questions` フィールドに\n"
        "   ユーザーへの具体的な質問として列挙してください（質問数は最大 5 件）。**質問が必要な場合は推測で穴埋めせず、必ず質問してください。\n"
        "   質問が 1 件でも残っているなら、ユーザーが回答するまでサマリーは保留扱いになります。**\n\n"
        "## 出力フォーマット (JSON)\n"
        "```json\n"
        "{\n"
        "  \"summary\": \"- MIT は 2/3 達成。残りの〇〇は明日に持ち越し。\\n- 午前は△△の作業に集中。会議では□□が議題に。\\n- 午後は◇◇でリフレッシュ。夕方は▲▲を片付けた。\\n- 全体として◎◎な1日だった。\",\n"
        "  \"questions\": [\"MIT に関する質問1\", \"質問2\"]\n"
        "}\n"
        "```\n\n"
        "## 入力データ\n" + ctx_text + answer_text
    )

    try:
        from google.genai import types as _gt
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("daily_summary", default_pro=True)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=prompt,
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text or "{}")
    except Exception as e:
        logging.error(f"daily summary generation error: {e}")
        return {"summary": "", "questions": [], "error": str(e)}

    summary = (data.get("summary") or "").strip()
    questions = data.get("questions") or []
    if not isinstance(questions, list):
        questions = []
    questions = [q for q in (str(x).strip() for x in questions) if q][:5]

    return {"summary": summary, "questions": questions}


async def _save_daily_summary_to_obsidian(date_str: str, summary_md: str) -> bool:
    """サマリーを DailyNote の `## 📅 Daily Summary` セクションへ書き込む。

    既存セクションがどの位置にあっても、いったん削除してから `update_section` で
    `SECTION_ORDER` に従った正しい位置（振り返りグループの先頭）に再挿入する。
    """
    import re as _re
    from api import app
    from utils.obsidian_utils import update_section
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return False
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            folder_id = await chat_service.drive_service.create_folder(
                service, chat_service.drive_folder_id, "DailyNotes"
            )
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{date_str}.md")
        if f_id:
            content = await chat_service.drive_service.read_text_file(service, f_id)
        else:
            content = f"---\ndate: {date_str}\n---\n\n# Daily Note {date_str}\n"

        section_header = "## 📅 Daily Summary"
        clean = (summary_md or "").strip()
        if not clean:
            # 空文字での上書きは既存サマリーを誤って消す事故になりやすいので、
            # 何もせず終了する（明示的な削除は手動編集で空保存する経路に任せる）
            return True
        # 既存の Daily Summary セクションを削除（位置ズレを是正するため）
        pattern = _re.compile(rf"{_re.escape(section_header)}\n?.*?(?=\n## |\Z)", _re.DOTALL)
        content = pattern.sub("", content)
        # SECTION_ORDER に従って正しい位置（振り返りグループ先頭）に挿入
        new_content = update_section(content, clean, section_header)

        if f_id:
            await chat_service.drive_service.update_text(service, f_id, new_content)
        else:
            await chat_service.drive_service.upload_text(
                service, folder_id, f"{date_str}.md", new_content
            )
        return True
    except Exception as e:
        logging.error(f"_save_daily_summary_to_obsidian error: {e}")
        return False


async def _save_manager_qa_to_obsidian(date_str: str) -> bool:
    """その日の質問と回答ペアを `## 🤝 Manager Q&A` セクションへ書き出す。
    answer が空の質問はスキップする。質問が0件ならセクションを作らない。"""
    import re as _re
    from api import app
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return False

    qa_items = await get_questions_by_date(date_str, scope='summary')
    answered = [q for q in qa_items if (q.get("answer") or "").strip()]
    if not answered:
        return False

    lines = []
    for q in answered:
        question = (q.get("question") or "").strip()
        answer = (q.get("answer") or "").strip()
        if not question or not answer:
            continue
        lines.append(f"- **Q:** {question}")
        ans_lines = answer.splitlines()
        if not ans_lines:
            ans_lines = [answer]
        lines.append(f"  - **A:** {ans_lines[0]}")
        for extra in ans_lines[1:]:
            if extra.strip():
                lines.append(f"    {extra}")
    if not lines:
        return False
    qa_text = "\n".join(lines)

    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            folder_id = await chat_service.drive_service.create_folder(
                service, chat_service.drive_folder_id, "DailyNotes"
            )
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{date_str}.md")
        if f_id:
            content = await chat_service.drive_service.read_text_file(service, f_id)
        else:
            content = f"---\ndate: {date_str}\n---\n\n# Daily Note {date_str}\n"

        section_header = "## 🤝 Manager Q&A"
        replacement = f"{section_header}\n{qa_text}"
        pattern = _re.compile(rf"{_re.escape(section_header)}\n.*?(?=\n## |\Z)", _re.DOTALL)
        if pattern.search(content):
            new_content = pattern.sub(replacement, content, count=1)
        else:
            from utils.obsidian_utils import update_section
            new_content = update_section(content, qa_text, section_header)

        if f_id:
            await chat_service.drive_service.update_text(service, f_id, new_content)
        else:
            await chat_service.drive_service.upload_text(
                service, folder_id, f"{date_str}.md", new_content
            )
        return True
    except Exception as e:
        logging.error(f"_save_manager_qa_to_obsidian error: {e}")
        return False


class DailySummaryUpdate(BaseModel):
    text: str
    date: Optional[str] = None


@router.post("/daily_summary", dependencies=[Depends(verify_api_key)])
async def daily_summary_set(req: DailySummaryUpdate):
    """ユーザーが手動で編集したデイリーサマリーを Obsidian へ保存する。"""
    date_str = req.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        saved = await _save_daily_summary_to_obsidian(date_str, req.text or "")
        if saved:
            await _save_manager_qa_to_obsidian(date_str)
            await resolve_questions(date_str, scope='summary')
            return {"ok": True, "saved": True, "date": date_str}
        raise HTTPException(status_code=500, detail="保存に失敗しました")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"daily_summary_set error: {e}")
        raise HTTPException(status_code=500, detail=f"保存に失敗しました: {e}")


async def _read_summary_for_date(chat_service, date: str) -> str:
    """指定日の Daily Note から `## 📅 Daily Summary` の本文を読み取って返す。"""
    import re as _re
    try:
        service = chat_service.drive_service.get_service()
        folder_id = await chat_service.drive_service.find_file(
            service, chat_service.drive_folder_id, "DailyNotes"
        )
        if not folder_id:
            return ""
        f_id = await chat_service.drive_service.find_file(service, folder_id, f"{date}.md")
        if not f_id:
            return ""
        content = await chat_service.drive_service.read_text_file(service, f_id)
        m = _re.search(r"## 📅 Daily Summary\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        if m:
            return m.group(1).strip()
    except Exception as e:
        logging.debug(f"_read_summary_for_date error ({date}): {e}")
    return ""


@router.get("/daily_summary", dependencies=[Depends(verify_api_key)])
async def daily_summary_get(date: str = ""):
    """指定日のデイリーサマリーを返す。
    date 未指定時は今日のものが無ければ昨日のものへ自動フォールバック。"""
    from api import app
    explicit_date = bool(date)
    if not date:
        date = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"text": "", "questions": [], "fallback": False, "date": date}
    try:
        text = await _read_summary_for_date(chat_service, date)
        fallback_date = None
        # 日付未指定で今日のサマリーが無ければ、直近 7 日を遡って最新の確定済みサマリーを探す
        if not text and not explicit_date:
            base = datetime.datetime.strptime(date, "%Y-%m-%d").date()
            for offset in range(1, 8):
                prev = (base - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
                t = await _read_summary_for_date(chat_service, prev)
                if t:
                    text = t
                    fallback_date = prev
                    break
        actual_date = fallback_date or date
        questions = await get_questions_by_date(actual_date, scope='summary')
        return {
            "date": actual_date,
            "requested_date": date,
            "text": text,
            "questions": questions,
            "fallback": fallback_date is not None,
        }
    except Exception as e:
        logging.debug(f"daily_summary_get error: {e}")
        return {"text": "", "questions": [], "fallback": False, "date": date}


class DailySummaryGenerateRequest(BaseModel):
    date: Optional[str] = None
    answers: Optional[dict] = None
    finalize: bool = False  # True なら質問が無くてもそのまま Obsidian に保存


@router.post("/daily_summary/generate", dependencies=[Depends(verify_api_key)])
async def daily_summary_generate(req: DailySummaryGenerateRequest):
    """サマリーを生成。質問がある場合は DB に登録して返す。
    finalize=True または質問が空の場合は Obsidian に保存して質問を resolved にする。"""
    date_str = req.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")

    # 既存の未確定質問に回答が来ていれば反映
    if req.answers:
        for qid_str, ans in req.answers.items():
            if not ans:
                continue
            try:
                qid = int(qid_str)
            except (TypeError, ValueError):
                continue
            await answer_daily_question(qid, str(ans))

    result = await _generate_daily_summary(date_str, answers=req.answers)
    summary = result.get("summary", "")
    new_questions = result.get("questions", [])

    # 既存の pending/answered と重複しない新規質問だけ DB に追加
    existing = await get_questions_by_date(date_str, scope='summary')
    existing_texts = {q["question"].strip() for q in existing}
    added_question_ids = []
    for q in new_questions:
        if q.strip() in existing_texts:
            continue
        qid = await add_daily_question(date_str, q.strip(), scope='summary')
        added_question_ids.append(qid)

    # 質問が一つもない、または finalize 指定時は確定保存
    pending = await get_questions_by_date(date_str, scope='summary')
    unanswered = [q for q in pending if q["status"] in ("pending",)]
    will_finalize = req.finalize or (not new_questions and not unanswered)

    saved = False
    if summary and will_finalize:
        saved = await _save_daily_summary_to_obsidian(date_str, summary)
        if saved:
            await _save_manager_qa_to_obsidian(date_str)
            await resolve_questions(date_str, scope='summary')

    return {
        "date": date_str,
        "summary": summary,
        "questions": await get_questions_by_date(date_str, scope='summary'),
        "saved": saved,
        "error": result.get("error"),
    }


@router.get("/daily_questions/pending", dependencies=[Depends(verify_api_key)])
async def daily_questions_pending():
    """未回答の質問一覧。"""
    qs = await get_pending_questions()
    return {"questions": qs}


class DailyAnswerRequest(BaseModel):
    answer: str


@router.post("/daily_questions/{qid}/answer", dependencies=[Depends(verify_api_key)])
async def daily_questions_answer(qid: int, req: DailyAnswerRequest):
    ok = await answer_daily_question(qid, req.answer)
    if not ok:
        raise HTTPException(status_code=404, detail="質問が見つかりません")

    # morning_mit スコープの質問への回答は、そのまま今日の MIT として
    # Obsidian の DailyNote に確定書き込みする（夜の振り返りと同様の即時反映）。
    try:
        pending = await get_pending_questions()
        q = next((x for x in pending if x.get("id") == qid), None)
        if q and q.get("scope") == "morning_mit":
            import re as _re
            lines = []
            for ln in (req.answer or "").splitlines():
                ln = _re.sub(r"^\s*[0-9]+[.)、]\s*", "", ln).strip()
                ln = _re.sub(r"^[-・*]\s*", "", ln).strip()
                if ln:
                    lines.append(ln[:60])
            items = lines[:3]
            if items:
                from api import app
                bot = getattr(app.state, "bot", None)
                partner_cog = bot.get_cog("PartnerCog") if bot else None
                if partner_cog:
                    result_msg = await partner_cog._set_mit_to_obsidian(items)
                    await resolve_questions(q["date"], scope="morning_mit")
                    confirm = (
                        "📌 今日のMITを確定したよ！\n"
                        + "\n".join(f"{i}. {it}" for i, it in enumerate(items, 1))
                        + "\n\n" + (result_msg or "")
                    ).strip()
                    await notification_service.save_message_and_notify(
                        "assistant", confirm, title="📌 今日のMIT確定",
                    )
    except Exception as e:
        logging.error(f"morning_mit answer 反映エラー: {e}")

    return {"status": "success"}


@router.delete("/daily_questions/{qid}", dependencies=[Depends(verify_api_key)])
async def daily_questions_delete(qid: int):
    ok = await delete_daily_question(qid)
    if not ok:
        raise HTTPException(status_code=404, detail="質問が見つかりません")
    return {"status": "success"}


# ============================================================
# コストメーター (API 利用料金の可視化)
# ============================================================

@router.get("/cost_summary", dependencies=[Depends(verify_api_key)])
async def cost_summary(days: int = 30):
    """直近 N 日分のコスト集計を返す。既定 30 日。"""
    from services import cost_meter_service
    days = max(1, min(int(days or 30), 365))
    end = datetime.datetime.now(JST).date()
    start = end - datetime.timedelta(days=days - 1)
    data = await cost_meter_service.summary(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    # 今月分も併記（しきい値判定のため）
    month_start = end.replace(day=1).strftime("%Y-%m-%d")
    this_month = await cost_meter_service.summary(month_start, end.strftime("%Y-%m-%d"))
    infra = await cost_meter_service.get_infra_cost_jpy()
    threshold = await cost_meter_service.get_monthly_threshold_jpy()
    return {
        **data,
        "this_month_jpy": this_month["total_jpy"],
        "this_month_in_tokens": this_month["total_in_tokens"],
        "this_month_out_tokens": this_month["total_out_tokens"],
        "infra_cost_jpy_per_month": infra,
        "monthly_threshold_jpy": threshold,
        "monthly_total_jpy_including_infra": this_month["total_jpy"] + infra,
    }


class CostSettingsRequest(BaseModel):
    usd_jpy_rate: Optional[float] = None
    monthly_threshold_jpy: Optional[float] = None
    auto_downgrade_to_flash: Optional[bool] = None
    infra_cost_jpy_per_month: Optional[float] = None


@router.get("/cost_settings", dependencies=[Depends(verify_api_key)])
async def cost_settings_get():
    from services import cost_meter_service
    return await cost_meter_service.get_settings()


@router.post("/cost_settings", dependencies=[Depends(verify_api_key)])
async def cost_settings_set(req: CostSettingsRequest):
    from services import cost_meter_service
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    return await cost_meter_service.update_settings(payload)


# ============================================================
# Gmail インボックス (要約一覧 + 削除/既読操作)
# ============================================================

@router.get("/gmail/inbox", dependencies=[Depends(verify_api_key)])
async def gmail_inbox(state: str = "pending", limit: int = 50):
    """`state` は pending / archived / trashed / all。"""
    from api.database import gmail_list, gmail_count_unnotified_high
    state = state.strip().lower() if state else "pending"
    if state not in ("pending", "archived", "trashed", "all"):
        state = "pending"
    rows = await gmail_list(state=state, limit=max(1, min(int(limit or 50), 200)))
    return {
        "state": state,
        "items": rows,
        "high_pending_count": await gmail_count_unnotified_high(),
    }


@router.post("/gmail/{message_id}/read", dependencies=[Depends(verify_api_key)])
async def gmail_mark_read(message_id: str):
    """Gmail 側で既読化し、DB の state を 'archived' に。"""
    from api import app
    from api.database import gmail_update
    bot = getattr(app.state, "bot", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail:
        raise HTTPException(status_code=503, detail="Gmail未接続")
    ok = await gmail.mark_as_read(message_id)
    if ok:
        await gmail_update(message_id, state="archived")
    return {"ok": ok}


@router.post("/gmail/{message_id}/trash", dependencies=[Depends(verify_api_key)])
async def gmail_trash(message_id: str):
    """Gmail でゴミ箱に移動し、DB の state を 'trashed' に。"""
    from api import app
    from api.database import gmail_update
    bot = getattr(app.state, "bot", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail:
        raise HTTPException(status_code=503, detail="Gmail未接続")
    ok = await gmail.trash(message_id)
    if ok:
        await gmail_update(message_id, state="trashed")
    return {"ok": ok}


@router.post("/gmail/{message_id}/save", dependencies=[Depends(verify_api_key)])
async def gmail_save_to_obsidian(message_id: str):
    """重要メールを Google Drive (Obsidian) の `Emails/{YYYY-MM}/` に Markdown として保存。"""
    from api import app
    from api.database import gmail_get, gmail_update
    bot = getattr(app.state, "bot", None)
    chat_service = getattr(app.state, "chat_service", None)
    gmail = getattr(bot, "gmail_service", None) if bot else None
    if not gmail or not chat_service or not chat_service.drive_service:
        raise HTTPException(status_code=503, detail="Gmail / Drive未接続")

    record = await gmail_get(message_id)
    if not record:
        raise HTTPException(status_code=404, detail="DBに該当メールがありません")

    # 既存保存済みならスキップ（再保存はしない）
    if record.get("saved_drive_id"):
        return {"ok": True, "drive_id": record["saved_drive_id"], "already_saved": True}

    # Gmail から本文を取り直す（DB には先頭プレビューしか保存していないため）
    full = await gmail.get_message(message_id)
    body_excerpt = ((full or {}).get("body") or "")[:5000]

    received_at = record.get("received_at") or datetime.datetime.now(JST).isoformat()
    try:
        date_part = received_at[:10] if len(received_at) >= 10 else datetime.datetime.now(JST).strftime("%Y-%m-%d")
        month_part = date_part[:7]
    except Exception:
        date_part = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        month_part = date_part[:7]

    subject = (record.get("subject") or "(件名なし)")
    safe_subject = "".join(c if c.isalnum() or c in " 　-_.()[]" else "_" for c in subject)[:80].strip().replace(" ", "_") or "email"
    file_name = f"{date_part}_{safe_subject}.md"

    importance = record.get("importance") or "medium"
    summary = record.get("summary") or ""
    from_addr = record.get("from_addr") or ""

    md_body = (
        "---\n"
        f"title: {subject}\n"
        f"from: {from_addr}\n"
        f"date: {received_at}\n"
        f"importance: {importance}\n"
        f"gmail_id: {message_id}\n"
        f"gmail_thread_id: {record.get('thread_id', '')}\n"
        "tags: [email]\n"
        "---\n\n"
        f"# {subject}\n\n"
        f"- **差出人**: {from_addr}\n"
        f"- **受信日時**: {received_at}\n"
        f"- **重要度**: {importance}\n"
        f"- **Gmail で開く**: https://mail.google.com/mail/u/0/#all/{record.get('thread_id', '') or message_id}\n\n"
        "## マネージャー要約\n"
        f"{summary or '（要約なし）'}\n\n"
        "## 本文（抜粋）\n"
        "```\n"
        f"{body_excerpt}\n"
        "```\n"
    )

    try:
        service = chat_service.drive_service.get_service()
        if not service:
            return {"ok": False, "error": "Drive未接続"}
        root = chat_service.drive_folder_id
        emails_folder = await chat_service.drive_service.find_file(service, root, "Emails")
        if not emails_folder:
            emails_folder = await chat_service.drive_service.create_folder(service, root, "Emails")
        month_folder = await chat_service.drive_service.find_file(service, emails_folder, month_part)
        if not month_folder:
            month_folder = await chat_service.drive_service.create_folder(service, emails_folder, month_part)

        drive_id = await chat_service.drive_service.upload_text(
            service, month_folder, file_name, md_body
        )
        if not drive_id:
            return {"ok": False, "error": "Drive 書き込みに失敗"}
        await gmail_update(
            message_id,
            saved_drive_id=drive_id,
            saved_at=datetime.datetime.now(JST).isoformat(),
        )
        return {"ok": True, "drive_id": drive_id, "file_name": file_name}
    except Exception as e:
        logging.error(f"gmail_save_to_obsidian error: {e}")
        return {"ok": False, "error": "保存に失敗しました"}


@router.post("/gmail/refresh", dependencies=[Depends(verify_api_key)])
async def gmail_refresh():
    """ユーザー操作による手動ポーリング起動。新着の取り込みを即時実行。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Bot未起動")
    cog = bot.get_cog("GmailWatchCog")
    if not cog:
        return {"ok": False, "error": "GmailWatchCog 未ロード"}
    try:
        await cog._run()
        return {"ok": True}
    except Exception as e:
        logging.error(f"gmail_refresh error: {e}")
        return {"ok": False, "error": "ポーリングに失敗しました"}


# ============================================================
# 支出ログ (Expenses) ＋ レシート Vision 解析
# ============================================================

EXPENSE_CATEGORIES = ["食費", "交通費", "娯楽", "衣服", "家電", "医療", "教育", "通信", "光熱費", "投資", "その他"]
SETTING_EXPENSE_LARGE_THRESHOLD = "expense_large_threshold_jpy"
DEFAULT_LARGE_THRESHOLD_JPY = 5000


async def _get_large_threshold() -> int:
    from api.database import get_app_setting
    raw = await get_app_setting(SETTING_EXPENSE_LARGE_THRESHOLD, str(DEFAULT_LARGE_THRESHOLD_JPY))
    try:
        v = int(float(raw))
        return v if v > 0 else DEFAULT_LARGE_THRESHOLD_JPY
    except (TypeError, ValueError):
        return DEFAULT_LARGE_THRESHOLD_JPY


class ReceiptAnalyzeRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"


@router.post("/expenses/analyze", dependencies=[Depends(verify_api_key)])
async def expenses_analyze(req: ReceiptAnalyzeRequest):
    """レシート写真から日付・店名・合計金額・支払方法を Gemini Vision で抽出（保存はしない）。"""
    import base64
    from google.genai import types as _gt
    from api import app

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = (
        "このレシート画像を読み取り、必ず以下の JSON 形式だけを返してください。前置きや説明は禁止。\n\n"
        "{\n"
        '  "date": "YYYY-MM-DD（読み取れなければ空文字）",\n'
        '  "vendor": "店名（読み取れた文字を最大40文字。空可）",\n'
        '  "amount": 合計金額(int, 円。税込み合計を優先),\n'
        '  "category": "食費 / 交通費 / 娯楽 / 衣服 / 家電 / 医療 / 教育 / 通信 / 光熱費 / 投資 / その他 のいずれか",\n'
        '  "payment_method": "現金 / クレジット / 電子マネー / QR / 不明 のいずれか",\n'
        '  "memo": "備考（主な購入品 1〜2 個。空可）",\n'
        '  "confidence": "high / medium / low"\n'
        "}\n"
        "amount は数字のみ。レシート以外の画像なら confidence='low' で空相当の値を入れる。"
    )

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = _gt.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = _gt.Part.from_text(text=prompt)
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("receipt_ocr", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[image_part, text_part]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        return {"ok": True, "result": json.loads(response.text)}
    except Exception as e:
        logging.error(f"expenses_analyze error: {e}")
        raise HTTPException(status_code=500, detail=f"解析失敗: {str(e)}")


class ReceiptUploadRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    date: Optional[str] = None  # ファイル名用 (YYYY-MM)


@router.post("/expenses/receipt_upload", dependencies=[Depends(verify_api_key)])
async def expenses_receipt_upload(req: ReceiptUploadRequest):
    """レシート画像を Google Drive (`/Expenses/YYYY-MM/`) に保存して file_id を返す。"""
    import base64
    import tempfile
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"ok": False, "error": "Drive未接続"}

    date_str = req.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    month_str = date_str[:7] if len(date_str) >= 7 else datetime.datetime.now(JST).strftime("%Y-%m")
    try:
        service = chat_service.drive_service.get_service()
        if not service:
            return {"ok": False, "error": "Drive未接続"}
        root = chat_service.drive_folder_id
        expenses_folder = await chat_service.drive_service.find_file(service, root, "Expenses")
        if not expenses_folder:
            expenses_folder = await chat_service.drive_service.create_folder(service, root, "Expenses")
        month_folder = await chat_service.drive_service.find_file(service, expenses_folder, month_str)
        if not month_folder:
            month_folder = await chat_service.drive_service.create_folder(service, expenses_folder, month_str)

        # base64 → 一時ファイル → Drive
        suffix = ".jpg" if "jpeg" in (req.mime_type or "") or "jpg" in (req.mime_type or "") else ".png"
        timestamp = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            tf.write(base64.b64decode(req.image_base64))
            tmp_path = tf.name
        try:
            file_id = await chat_service.drive_service.upload_file(
                service, month_folder, f"receipt_{timestamp}{suffix}", tmp_path, mime_type=req.mime_type or "image/jpeg"
            )
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return {"ok": True, "drive_id": file_id}
    except Exception as e:
        logging.error(f"expenses_receipt_upload error: {e}")
        return {"ok": False, "error": "アップロード失敗"}


class ExpenseSaveRequest(BaseModel):
    amount: int
    date: Optional[str] = None
    category: str = "その他"
    vendor: str = ""
    payment_method: str = ""
    memo: str = ""
    receipt_drive_id: str = ""


@router.post("/expenses", dependencies=[Depends(verify_api_key)])
async def expenses_save(req: ExpenseSaveRequest):
    """支出を保存。閾値超過なら is_large=1 を立て、Lifelog 追記と通知を実行。"""
    from api import app
    from api.database import add_expense

    now = datetime.datetime.now(JST)
    date = req.date or now.strftime("%Y-%m-%d")
    threshold = await _get_large_threshold()
    is_large = req.amount >= threshold

    expense_id = await add_expense(
        date=date, amount=req.amount, category=req.category, vendor=req.vendor,
        payment_method=req.payment_method, memo=req.memo,
        receipt_drive_id=req.receipt_drive_id, is_large=is_large,
    )

    # 大きな支出だけ Lifelog と通知（小さなものはノイズになるので除外）
    if is_large:
        try:
            bot = getattr(app.state, "bot", None)
            partner_cog = bot.get_cog("PartnerCog") if bot else None
            if partner_cog and date == now.strftime("%Y-%m-%d"):
                note_content = await partner_cog._get_todays_obsidian_note()
                if not note_content:
                    note_content = f"# Daily Note {date}\n"
                time_str = now.strftime("%H:%M")
                vendor_str = req.vendor or req.category or "支出"
                line = f"- {time_str} 💴 大きな支出: {vendor_str} ¥{req.amount:,}"
                if req.memo:
                    line += f"（{req.memo}）"
                from utils.obsidian_utils import update_section
                updated = update_section(note_content, line, "## 🪟 Lifelog")
                await partner_cog._save_todays_obsidian_note(updated)
        except Exception as e:
            logging.debug(f"expenses_save lifelog append failed: {e}")

        try:
            await notification_service.send_push(
                title="💴 大きな支出を記録",
                body=f"¥{req.amount:,}（{req.vendor or req.category}）。閾値 ¥{threshold:,} を超えました。",
                url="/?openExpenses=1",
            )
        except Exception:
            pass

    return {"ok": True, "id": expense_id, "is_large": is_large, "threshold": threshold}


@router.get("/expenses", dependencies=[Depends(verify_api_key)])
async def expenses_list(year: Optional[int] = None, month: Optional[int] = None):
    """指定月（既定: 今月）の支出一覧と集計を返す。"""
    from api.database import get_expenses_by_range
    import calendar as _cal
    now = datetime.datetime.now(JST)
    y = year or now.year
    m = month or now.month
    days_in_month = _cal.monthrange(y, m)[1]
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y:04d}-{m:02d}-{days_in_month:02d}"
    rows = await get_expenses_by_range(start, end)

    total = sum(r["amount"] for r in rows)
    by_category: dict[str, int] = {}
    for r in rows:
        c = r["category"] or "その他"
        by_category[c] = by_category.get(c, 0) + r["amount"]
    by_category_list = sorted(
        [{"category": k, "amount": v} for k, v in by_category.items()],
        key=lambda x: x["amount"], reverse=True,
    )
    threshold = await _get_large_threshold()
    return {
        "year": y, "month": m,
        "start": start, "end": end,
        "expenses": rows,
        "total": total,
        "by_category": by_category_list,
        "large_threshold": threshold,
        "categories": EXPENSE_CATEGORIES,
    }


class ExpensePatchRequest(BaseModel):
    date: Optional[str] = None
    amount: Optional[int] = None
    category: Optional[str] = None
    vendor: Optional[str] = None
    payment_method: Optional[str] = None
    memo: Optional[str] = None


@router.patch("/expenses/{expense_id}", dependencies=[Depends(verify_api_key)])
async def expenses_patch(expense_id: int, req: ExpensePatchRequest):
    from api.database import update_expense
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "amount" in fields:
        # 金額変更時は is_large フラグも再計算
        threshold = await _get_large_threshold()
        fields["is_large"] = fields["amount"] >= threshold
    ok = await update_expense(expense_id, fields)
    if not ok:
        raise HTTPException(status_code=404, detail="支出が見つかりません")
    return {"ok": True}


@router.delete("/expenses/{expense_id}", dependencies=[Depends(verify_api_key)])
async def expenses_delete(expense_id: int):
    from api.database import delete_expense
    ok = await delete_expense(expense_id)
    if not ok:
        raise HTTPException(status_code=404, detail="支出が見つかりません")
    return {"ok": True}


class ExpenseThresholdRequest(BaseModel):
    threshold_jpy: int


@router.get("/expenses/threshold", dependencies=[Depends(verify_api_key)])
async def expenses_threshold_get():
    return {"threshold_jpy": await _get_large_threshold()}


@router.post("/expenses/threshold", dependencies=[Depends(verify_api_key)])
async def expenses_threshold_set(req: ExpenseThresholdRequest):
    from api.database import set_app_setting
    v = max(0, int(req.threshold_jpy or 0))
    await set_app_setting(SETTING_EXPENSE_LARGE_THRESHOLD, str(v))
    return {"ok": True, "threshold_jpy": v}


# ============================================================
# 食事ログ (Meal log) ＋ Gemini Vision による解析
# ============================================================

class MealAnalyzeRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    hint: str = ""


@router.post("/meals/analyze", dependencies=[Depends(verify_api_key)])
async def meals_analyze(req: MealAnalyzeRequest):
    """食事の写真から料理名・推定カロリー・PFC を抽出（保存はしない）。"""
    import base64
    from google.genai import types as _gt
    from api import app

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足: {req.hint}" if req.hint else ""
    prompt = (
        "この食事の写真から栄養情報を推定し、必ず以下の JSON 形式だけを返してください。\n"
        f"前置きや説明は禁止。{hint_text}\n\n"
        "{\n"
        '  "name": "料理名（複数なら『+』でつなぐ。例: 唐揚げ定食 + 味噌汁）",\n'
        '  "meal_type": "breakfast / lunch / dinner / snack のいずれか（時間帯がわからなければ best effort）",\n'
        '  "calories": 推定カロリー(kcal, int),\n'
        '  "protein_g": タンパク質(g, number),\n'
        '  "fat_g": 脂質(g, number),\n'
        '  "carbs_g": 炭水化物(g, number),\n'
        '  "confidence": "high / medium / low",\n'
        '  "memo": "気づいたこと（量が多い・野菜が少ない 等）を1〜2行"\n'
        "}\n"
        "推定根拠が乏しいときは confidence='low' とし、数値は控えめに。"
    )

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = _gt.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = _gt.Part.from_text(text=prompt)
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("meal_image", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[image_part, text_part]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text)
        return {"ok": True, "result": data}
    except Exception as e:
        logging.error(f"meals_analyze error: {e}")
        raise HTTPException(status_code=500, detail=f"解析失敗: {str(e)}")


class MealSaveRequest(BaseModel):
    name: str
    time: Optional[str] = None  # HH:MM。未指定なら現在時刻
    date: Optional[str] = None  # YYYY-MM-DD。未指定なら今日
    meal_type: str = ""
    calories: int = 0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carbs_g: float = 0.0
    memo: str = ""
    image_drive_id: str = ""


@router.post("/meals", dependencies=[Depends(verify_api_key)])
async def meals_save(req: MealSaveRequest):
    """食事ログを保存。Lifelog にも `- HH:MM 🍽 ...` で追記する。"""
    from api import app
    from api.database import add_meal

    now = datetime.datetime.now(JST)
    date = req.date or now.strftime("%Y-%m-%d")
    time = req.time or now.strftime("%H:%M")
    name = (req.name or "").strip() or "食事"

    meal_id = await add_meal(
        date=date, time=time, name=name,
        meal_type=(req.meal_type or "").strip(),
        calories=req.calories,
        protein_g=req.protein_g, fat_g=req.fat_g, carbs_g=req.carbs_g,
        memo=req.memo or "",
        image_drive_id=req.image_drive_id or "",
    )

    # Lifelog に追記（今日分のみ）
    try:
        if date == now.strftime("%Y-%m-%d"):
            chat_service = getattr(app.state, "chat_service", None)
            bot = getattr(app.state, "bot", None)
            partner_cog = bot.get_cog("PartnerCog") if bot else None
            if partner_cog and chat_service and chat_service.drive_service:
                kcal_text = f"（推定{req.calories}kcal）" if req.calories else ""
                line = f"- {time} 🍽 {name}{kcal_text}"
                note_content = await partner_cog._get_todays_obsidian_note()
                if not note_content:
                    note_content = f"# Daily Note {date}\n"
                from utils.obsidian_utils import update_section
                updated = update_section(note_content, line, "## 🪟 Lifelog")
                await partner_cog._save_todays_obsidian_note(updated)
    except Exception as e:
        logging.debug(f"meals_save lifelog append failed: {e}")

    return {"ok": True, "id": meal_id, "date": date, "time": time}


@router.get("/meals", dependencies=[Depends(verify_api_key)])
async def meals_list(date: str = ""):
    from api.database import get_meals_by_date
    if not date:
        date = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    rows = await get_meals_by_date(date)
    # 当日合計
    total = {
        "calories": sum(r["calories"] or 0 for r in rows),
        "protein_g": round(sum(r["protein_g"] or 0 for r in rows), 1),
        "fat_g": round(sum(r["fat_g"] or 0 for r in rows), 1),
        "carbs_g": round(sum(r["carbs_g"] or 0 for r in rows), 1),
    }
    return {"date": date, "meals": rows, "total": total}


class MealPatchRequest(BaseModel):
    date: Optional[str] = None
    time: Optional[str] = None
    meal_type: Optional[str] = None
    name: Optional[str] = None
    calories: Optional[int] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carbs_g: Optional[float] = None
    memo: Optional[str] = None


@router.patch("/meals/{meal_id}", dependencies=[Depends(verify_api_key)])
async def meals_patch(meal_id: int, req: MealPatchRequest):
    from api.database import update_meal
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    ok = await update_meal(meal_id, fields)
    if not ok:
        raise HTTPException(status_code=404, detail="食事が見つかりません")
    return {"ok": True}


@router.delete("/meals/{meal_id}", dependencies=[Depends(verify_api_key)])
async def meals_delete(meal_id: int):
    from api.database import delete_meal
    ok = await delete_meal(meal_id)
    if not ok:
        raise HTTPException(status_code=404, detail="食事が見つかりません")
    return {"ok": True}


@router.post("/meals/advice", dependencies=[Depends(verify_api_key)])
async def meals_advice(date: str = ""):
    """指定日（既定: 今日）の食事ログを Gemini に渡し、栄養観点でのマネージャーアドバイスを返す。"""
    from api import app
    from api.database import get_meals_by_date
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")
    if not date:
        date = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    meals = await get_meals_by_date(date)
    if not meals:
        return {"ok": False, "error": "食事の記録がまだありません"}
    lines = []
    total_kcal = total_p = total_f = total_c = 0
    for m in meals:
        lines.append(
            f"- {m['time']} {m['name']}: {m['calories']}kcal "
            f"(P{m['protein_g']}/F{m['fat_g']}/C{m['carbs_g']})"
            + (f" — {m['memo']}" if m["memo"] else "")
        )
        total_kcal += m["calories"] or 0
        total_p += m["protein_g"] or 0
        total_f += m["fat_g"] or 0
        total_c += m["carbs_g"] or 0
    body = "\n".join(lines)
    prompt = (
        "あなたはユーザー専属のマネージャー兼栄養アドバイザーです。\n"
        f"今日（{date}）の食事ログから、栄養バランスの観点で短くアドバイスしてください。\n\n"
        f"## 食事ログ\n{body}\n\n"
        f"## 当日合計\nカロリー: {total_kcal}kcal / P: {total_p:.0f}g / F: {total_f:.0f}g / C: {total_c:.0f}g\n\n"
        "【ルール】\n"
        "- 文体はマネージャーらしいタメ口で 3〜5 行\n"
        "- 「足りない/多い」を 1 つだけ具体的に指摘し、明日に向けた小さな提案を 1 つ\n"
        "- カロリー数値だけでなくPFCバランスにも触れる\n"
        "- 否定的な強い言葉は使わず、励まし基調で\n"
    )
    try:
        from google.genai import types as _gt
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("meal_advice", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=prompt,
            config=_gt.GenerateContentConfig(),
        )
        advice = (response.text or "").strip()
        return {"ok": True, "advice": advice, "total": {
            "calories": total_kcal,
            "protein_g": round(total_p, 1),
            "fat_g": round(total_f, 1),
            "carbs_g": round(total_c, 1),
        }}
    except Exception as e:
        logging.error(f"meals_advice error: {e}")
        return {"ok": False, "error": "アドバイス生成に失敗しました"}


# ============================================================
# Lifelog activity (Bot の log_life_activity と同形式で記録)
# ============================================================

class ThoughtReflectionRequest(BaseModel):
    theme: str
    summary: str = ""
    next_step: str = ""


@router.post("/thought_reflection", dependencies=[Depends(verify_api_key)])
async def thought_reflection_save(req: ThoughtReflectionRequest):
    """壁打ちメモを Obsidian に保存する（ボタン経由のみで呼ばれる想定）。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジン未初期化")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 未ロード")
    try:
        msg = await partner_cog._save_thought_reflection_to_obsidian(
            req.theme or "無題",
            req.summary or "",
            req.next_step or "",
        )
        return {"ok": True, "message": msg}
    except Exception as e:
        logging.error(f"thought_reflection_save error: {e}")
        return {"ok": False, "error": "保存失敗"}


class LifelogActivityRequest(BaseModel):
    activity_name: str
    status: str  # 'start' / 'end'


@router.post("/lifelog_activity", dependencies=[Depends(verify_api_key)])
async def lifelog_activity(req: LifelogActivityRequest):
    """`- HH:MM ▶ 活動名` 開始 → `- HH:MM - HH:MM 活動名` 終了 の標準形で記録。
    瞑想など、開始-終了がある活動を Bot の log_life_activity と統一フォーマットで残すための API。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジン未初期化")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 未ロード")
    status = req.status.strip().lower()
    if status not in ("start", "end"):
        return {"ok": False, "error": "status は 'start' か 'end'"}
    try:
        msg = await partner_cog._log_life_activity_to_obsidian(req.activity_name, status)
        return {"ok": True, "message": msg}
    except Exception as e:
        logging.error(f"lifelog_activity error: {e}")
        return {"ok": False, "error": "保存失敗"}


# ============================================================
# 朝のマネージャー MIT 提案 (morning_mit)
# ============================================================

@router.get("/morning_mit/pending", dependencies=[Depends(verify_api_key)])
async def morning_mit_pending():
    """今日の朝のMIT提案で未確定（resolved以外）のものを返す。
    返り値: { date, qid, candidates: [str, str, str] } または { date } のみ"""
    import json as _json
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        qs = await get_questions_by_date(today_str, scope='morning_mit')
        unresolved = [q for q in qs if q["status"] != 'resolved']
        if not unresolved:
            return {"date": today_str, "qid": None, "candidates": []}
        q = unresolved[0]
        cands = []
        try:
            ctx = _json.loads(q.get("context") or "{}")
            cands = ctx.get("candidates") or []
        except Exception:
            cands = []
        return {"date": today_str, "qid": q["id"], "candidates": cands}
    except Exception as e:
        logging.error(f"morning_mit_pending error: {e}")
        return {"date": today_str, "qid": None, "candidates": []}


class MorningMitConfirmRequest(BaseModel):
    items: list[str]
    qid: Optional[int] = None


@router.post("/morning_mit/confirm", dependencies=[Depends(verify_api_key)])
async def morning_mit_confirm(req: MorningMitConfirmRequest):
    """ユーザーが朝のMIT候補を編集して確定したものをObsidianに書き込む。"""
    from api import app
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    items = [s.strip() for s in (req.items or []) if s and s.strip()][:3]
    if not items:
        return {"ok": False, "error": "MIT が空です"}
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジン未初期化")
    partner_cog = bot.get_cog("PartnerCog")
    if not partner_cog:
        raise HTTPException(status_code=503, detail="PartnerCog 未ロード")
    try:
        result_msg = await partner_cog._set_mit_to_obsidian(items)
        # 質問を resolved に
        try:
            await resolve_questions(today_str, scope='morning_mit')
        except Exception:
            pass
        return {"ok": True, "message": result_msg, "date": today_str}
    except Exception as e:
        logging.error(f"morning_mit_confirm error: {e}")
        return {"ok": False, "error": "保存に失敗"}


# ============================================================
# Investment (投資サポート) エンドポイント群
# ============================================================

def _get_investment_cog():
    """InvestmentCogをbotから取得する。ロード前なら503で返す。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    cog = bot.get_cog("InvestmentCog")
    if not cog:
        raise HTTPException(status_code=503, detail="InvestmentCogがロードされていません。")
    return cog


class InvestmentTickerRequest(BaseModel):
    ticker: str


class InvestmentEarningsRequest(BaseModel):
    ticker: str
    register_calendar: bool = True


class InvestmentCEORequest(BaseModel):
    ticker: str
    video_url: str
    video_title: Optional[str] = ""


class InvestmentConstitutionUpdateRequest(BaseModel):
    content: str


class InvestmentConstitutionInitRequest(BaseModel):
    force: bool = False


@router.post("/investment/sentiment", dependencies=[Depends(verify_api_key)])
async def investment_sentiment():
    cog = _get_investment_cog()
    return await cog.run_market_sentiment()


@router.post("/investment/snapshot", dependencies=[Depends(verify_api_key)])
async def investment_snapshot(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_stock_snapshot(req.ticker)


@router.post("/investment/audit", dependencies=[Depends(verify_api_key)])
async def investment_audit(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_stock_audit(req.ticker)


@router.post("/investment/earnings_schedule", dependencies=[Depends(verify_api_key)])
async def investment_earnings_schedule(req: InvestmentEarningsRequest):
    cog = _get_investment_cog()
    return await cog.run_earnings_schedule(req.ticker, register_calendar=req.register_calendar)


@router.post("/investment/earnings_documents", dependencies=[Depends(verify_api_key)])
async def investment_earnings_documents(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_earnings_documents(req.ticker)


class InvestmentEarningsDocSaveUrlRequest(BaseModel):
    ticker: str
    url: str
    label: str = ""


@router.post("/investment/earnings_documents/save_url", dependencies=[Depends(verify_api_key)])
async def investment_earnings_documents_save_url(req: InvestmentEarningsDocSaveUrlRequest):
    cog = _get_investment_cog()
    return await cog.save_earnings_document_from_url(req.ticker, req.url, label=req.label)


@router.post("/investment/earnings_documents/save_file", dependencies=[Depends(verify_api_key)])
async def investment_earnings_documents_save_file(
    ticker: str = Form(...),
    label: str = Form(""),
    file: UploadFile = File(...),
):
    cog = _get_investment_cog()
    content = await file.read()
    mime = file.content_type or ""
    return await cog.save_earnings_document_from_bytes(
        ticker, content, filename=file.filename or "document", label=label, mime=mime
    )


@router.post("/investment/ceo_check", dependencies=[Depends(verify_api_key)])
async def investment_ceo_check(req: InvestmentCEORequest):
    cog = _get_investment_cog()
    return await cog.run_ceo_crosscheck(
        req.ticker, req.video_url, video_title=req.video_title or ""
    )


@router.get("/investment/constitution", dependencies=[Depends(verify_api_key)])
async def investment_constitution_get():
    cog = _get_investment_cog()
    return await cog.run_get_constitution()


@router.post("/investment/constitution", dependencies=[Depends(verify_api_key)])
async def investment_constitution_update(req: InvestmentConstitutionUpdateRequest):
    cog = _get_investment_cog()
    return await cog.run_update_constitution(req.content)


@router.post("/investment/constitution/init", dependencies=[Depends(verify_api_key)])
async def investment_constitution_init(req: InvestmentConstitutionInitRequest):
    cog = _get_investment_cog()
    return await cog.run_init_constitution(force=req.force)


@router.get("/investment/history/{category}", dependencies=[Depends(verify_api_key)])
async def investment_history(category: str, limit: int = 20):
    cog = _get_investment_cog()
    return await cog.list_history(category, limit=limit)


@router.get("/investment/history/{category}/{file_id}", dependencies=[Depends(verify_api_key)])
async def investment_history_item(category: str, file_id: str):
    cog = _get_investment_cog()
    return await cog.read_history_item(category, file_id)


# ----- 同業比較 / ニュース / 配当 / リスク / 憲法レビュー -----

@router.post("/investment/peer_comparison", dependencies=[Depends(verify_api_key)])
async def investment_peer_comparison(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_peer_comparison(req.ticker)


@router.post("/investment/news_sentiment", dependencies=[Depends(verify_api_key)])
async def investment_news_sentiment(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_news_sentiment(req.ticker)


class InvestmentDividendRequest(BaseModel):
    ticker: str
    register_calendar: bool = True


@router.post("/investment/dividend", dependencies=[Depends(verify_api_key)])
async def investment_dividend(req: InvestmentDividendRequest):
    cog = _get_investment_cog()
    return await cog.run_dividend_schedule(
        req.ticker, register_calendar=req.register_calendar
    )


@router.post("/investment/risk_assessment", dependencies=[Depends(verify_api_key)])
async def investment_risk():
    cog = _get_investment_cog()
    return await cog.run_risk_assessment()


class InvestmentReviewRequest(BaseModel):
    lookback_days: int = 180


@router.post("/investment/constitution_review", dependencies=[Depends(verify_api_key)])
async def investment_constitution_review(req: InvestmentReviewRequest):
    cog = _get_investment_cog()
    return await cog.run_constitution_review(lookback_days=req.lookback_days)


# ----- ポートフォリオ -----

class PortfolioAddRequest(BaseModel):
    ticker: str
    shares: float
    avg_cost: float
    name: Optional[str] = None
    sector: Optional[str] = None
    currency: Optional[str] = None
    notes: Optional[str] = None


class PortfolioRemoveRequest(BaseModel):
    code: str
    shares: Optional[float] = None  # None なら全数売却


class PortfolioEditRequest(BaseModel):
    code: str
    shares: Optional[float] = None
    avg_cost: Optional[float] = None
    name: Optional[str] = None
    sector: Optional[str] = None
    currency: Optional[str] = None
    notes: Optional[str] = None


@router.get("/investment/portfolio", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_list():
    cog = _get_investment_cog()
    return await cog.portfolio_list()


@router.post("/investment/portfolio/add", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_add(req: PortfolioAddRequest):
    cog = _get_investment_cog()
    return await cog.portfolio_add(req.dict())


@router.post("/investment/portfolio/remove", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_remove(req: PortfolioRemoveRequest):
    cog = _get_investment_cog()
    return await cog.portfolio_remove(req.code, shares=req.shares)


@router.post("/investment/portfolio/edit", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_edit(req: PortfolioEditRequest):
    cog = _get_investment_cog()
    payload = req.dict(exclude_none=True)
    code = payload.pop("code")
    return await cog.portfolio_update(code, **payload)


@router.get("/investment/portfolio/transactions", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_transactions(limit: int = 100):
    cog = _get_investment_cog()
    return await cog.portfolio_transactions(limit=limit)


# ----- 投資日記 -----

class JournalAddRequest(BaseModel):
    title: Optional[str] = ""
    content: str
    ticker: Optional[str] = ""
    action: Optional[str] = ""   # buy / sell / hold / observe
    emotion: Optional[str] = ""


@router.get("/investment/journal", dependencies=[Depends(verify_api_key)])
async def investment_journal_list(limit: int = 50):
    cog = _get_investment_cog()
    return await cog.journal_list(limit=limit)


@router.post("/investment/journal/add", dependencies=[Depends(verify_api_key)])
async def investment_journal_add(req: JournalAddRequest):
    cog = _get_investment_cog()
    return await cog.journal_add(req.dict())


@router.get("/investment/journal/{filename}", dependencies=[Depends(verify_api_key)])
async def investment_journal_get(filename: str):
    cog = _get_investment_cog()
    return await cog.journal_get(filename)


@router.put("/investment/journal/{filename}", dependencies=[Depends(verify_api_key)])
async def investment_journal_edit(filename: str, req: JournalAddRequest):
    cog = _get_investment_cog()
    return await cog.journal_edit(filename, req.dict())


@router.delete("/investment/journal/{filename}", dependencies=[Depends(verify_api_key)])
async def investment_journal_delete(filename: str):
    cog = _get_investment_cog()
    return await cog.journal_delete(filename)


class JournalAnalyzeRequest(BaseModel):
    limit: int = 30


@router.post("/investment/journal/analyze", dependencies=[Depends(verify_api_key)])
async def investment_journal_analyze(req: JournalAnalyzeRequest):
    cog = _get_investment_cog()
    return await cog.journal_analyze_pattern(limit=req.limit)


# ----- アラート -----

class AlertAddRequest(BaseModel):
    ticker: Optional[str] = ""
    type: str  # per_below / per_above / price_below / price_above / drop_pct / rise_pct / earnings_within_days
    threshold: float
    enabled: bool = True
    memo: Optional[str] = ""


class AlertToggleRequest(BaseModel):
    rule_id: int
    enabled: bool


class AlertRemoveRequest(BaseModel):
    rule_id: int


@router.get("/investment/alerts", dependencies=[Depends(verify_api_key)])
async def investment_alerts_list():
    cog = _get_investment_cog()
    return await cog.alerts_list()


@router.post("/investment/alerts/add", dependencies=[Depends(verify_api_key)])
async def investment_alerts_add(req: AlertAddRequest):
    cog = _get_investment_cog()
    return await cog.alerts_add(req.dict())


@router.post("/investment/alerts/toggle", dependencies=[Depends(verify_api_key)])
async def investment_alerts_toggle(req: AlertToggleRequest):
    cog = _get_investment_cog()
    return await cog.alerts_toggle(req.rule_id, req.enabled)


@router.post("/investment/alerts/remove", dependencies=[Depends(verify_api_key)])
async def investment_alerts_remove(req: AlertRemoveRequest):
    cog = _get_investment_cog()
    return await cog.alerts_remove(req.rule_id)


@router.post("/investment/alerts/check", dependencies=[Depends(verify_api_key)])
async def investment_alerts_check():
    cog = _get_investment_cog()
    return await cog.alerts_check_now()


# ============================================================
# Screener (銘柄スクリーナー) エンドポイント群
# ============================================================

def _get_screener_cog():
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    cog = bot.get_cog("ScreenerCog")
    if not cog:
        raise HTTPException(status_code=503, detail="ScreenerCogがロードされていません。")
    return cog


class ScreenerRunRequest(BaseModel):
    styles: Optional[List[str]] = None
    style: Optional[str] = None  # backward compat
    universe: str = "topix500"
    top_n: int = 10
    min_market_cap_jpy: Optional[int] = None
    exclude_sectors: Optional[List[str]] = None
    # {style_name: [enabled_filter_keys]} - スタイルごとに ON にする構成要素を指定
    filter_overrides: Optional[dict] = None
    # "any"=OR（いずれか合致）, "all"=AND（すべて合致）
    combine_mode: str = "any"


class ScreenerAnalyzeRequest(BaseModel):
    styles: Optional[List[str]] = None
    style: Optional[str] = None  # backward compat
    candidates: List[dict]
    use_pro: bool = False


@router.get("/investment/screener/styles", dependencies=[Depends(verify_api_key)])
async def screener_styles():
    cog = _get_screener_cog()
    return await cog.list_styles()


@router.get("/investment/screener/universes", dependencies=[Depends(verify_api_key)])
async def screener_universes():
    cog = _get_screener_cog()
    return await cog.list_universes()


def _json_sanitize(obj):
    """dict/list を再帰的に走査し、NaN/Inf を None に置換して JSON 互換にする。"""
    import math

    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize(v) for v in obj]
    return obj


@router.post("/investment/screener/run", dependencies=[Depends(verify_api_key)])
async def screener_run(req: ScreenerRunRequest):
    cog = _get_screener_cog()
    styles = req.styles or ([req.style] if req.style else [])
    if not styles:
        raise HTTPException(status_code=422, detail="styles または style を1つ以上指定してください")
    result = await cog.run_multi_screening(
        styles=styles,
        top_n=req.top_n,
        universe_name=req.universe,
        min_market_cap_jpy=req.min_market_cap_jpy,
        exclude_sectors=req.exclude_sectors,
        filter_overrides=req.filter_overrides,
        combine_mode=req.combine_mode,
    )
    # NaN/Inf は JSON 非互換でシリアライズに失敗するため None に正規化する
    return _json_sanitize(result)


@router.post("/investment/screener/analyze", dependencies=[Depends(verify_api_key)])
async def screener_analyze(req: ScreenerAnalyzeRequest):
    cog = _get_screener_cog()
    styles = req.styles or ([req.style] if req.style else [])
    if not styles:
        raise HTTPException(status_code=422, detail="styles または style を1つ以上指定してください")
    return await cog.start_qualitative_analysis(
        styles=styles,
        candidates=req.candidates,
        use_pro=req.use_pro,
    )


@router.get("/investment/screener/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def screener_job(job_id: str):
    cog = _get_screener_cog()
    return await cog.get_job_status(job_id)


class ScreenerCrossFilterRequest(BaseModel):
    candidates: List[dict]
    secondary_style: str
    enabled_filters: Optional[List[str]] = None


@router.post("/investment/screener/cross_filter", dependencies=[Depends(verify_api_key)])
async def screener_cross_filter(req: ScreenerCrossFilterRequest):
    cog = _get_screener_cog()
    if not req.candidates:
        raise HTTPException(status_code=422, detail="candidates は1件以上必要です")
    if not req.secondary_style:
        raise HTTPException(status_code=422, detail="secondary_style を指定してください")
    return await cog.apply_secondary_style(
        candidates=req.candidates,
        secondary_style=req.secondary_style,
        enabled_filters=req.enabled_filters,
    )


# =========================================================
# 注目銘柄 (Watchlist)
# =========================================================

class WatchlistAddRequest(BaseModel):
    code: str
    name: str = ""
    sector: str = ""
    source: str = ""
    memo: str = ""


class WatchlistMemoRequest(BaseModel):
    memo: str


@router.get("/investment/watchlist", dependencies=[Depends(verify_api_key)])
async def watchlist_get():
    from api.database import watchlist_list
    items = await watchlist_list()
    return {"ok": True, "items": items}


@router.post("/investment/watchlist", dependencies=[Depends(verify_api_key)])
async def watchlist_post(req: WatchlistAddRequest):
    from api.database import watchlist_add
    if not req.code:
        raise HTTPException(status_code=422, detail="code は必須です")
    await watchlist_add(req.code, req.name, req.sector, req.source, req.memo)
    return {"ok": True}


@router.delete("/investment/watchlist/{code}", dependencies=[Depends(verify_api_key)])
async def watchlist_delete(code: str):
    from api.database import watchlist_remove
    ok = await watchlist_remove(code)
    return {"ok": ok}


@router.put("/investment/watchlist/{code}/memo", dependencies=[Depends(verify_api_key)])
async def watchlist_memo(code: str, req: WatchlistMemoRequest):
    from api.database import watchlist_update_memo
    ok = await watchlist_update_memo(code, req.memo)
    return {"ok": ok}


# =========================================================
# 保存済みスクリーニング結果 (Screener Runs)
# =========================================================

class ScreenerRunSaveRequest(BaseModel):
    title: Optional[str] = ""
    styles: Optional[List[str]] = None
    combine_mode: Optional[str] = "any"
    universe: Optional[str] = ""
    applied_filters: Optional[dict] = None
    candidates: Optional[List[dict]] = None
    qualitative_report: Optional[str] = ""


@router.post("/investment/screener/runs", dependencies=[Depends(verify_api_key)])
async def screener_runs_save(req: ScreenerRunSaveRequest):
    from api.database import screener_run_save
    run_id = await screener_run_save(
        title=req.title or "",
        styles=req.styles or [],
        combine_mode=req.combine_mode or "any",
        universe=req.universe or "",
        applied_filters=req.applied_filters or {},
        candidates=req.candidates or [],
        qualitative_report=req.qualitative_report or "",
    )
    return {"ok": True, "id": run_id}


@router.get("/investment/screener/runs", dependencies=[Depends(verify_api_key)])
async def screener_runs_list():
    from api.database import screener_run_list
    items = await screener_run_list()
    return {"ok": True, "items": items}


@router.get("/investment/screener/runs/{run_id}", dependencies=[Depends(verify_api_key)])
async def screener_runs_get(run_id: int):
    from api.database import screener_run_get
    data = await screener_run_get(run_id)
    if not data:
        raise HTTPException(status_code=404, detail="保存済み結果が見つかりません")
    return {"ok": True, "data": data}


@router.delete("/investment/screener/runs/{run_id}", dependencies=[Depends(verify_api_key)])
async def screener_runs_delete(run_id: int):
    from api.database import screener_run_delete
    ok = await screener_run_delete(run_id)
    return {"ok": ok}


# =========================================================
# Gemini モデル設定 (機能ごとに Flash / Pro)
# =========================================================

# 機能カテゴリ定義: (key, ラベル, 説明, デフォルトモデル)
GEMINI_FEATURE_CATALOG = [
    ("screener_qualitative", "スクリーナー質的分析", "スクリーニング結果のPhase B/C質的分析", "flash"),
    ("investment_snapshot", "銘柄スナップショット", "銘柄の現状把握分析", "pro"),
    ("investment_audit", "憲法審査", "投資憲法に基づく銘柄審査", "pro"),
    ("investment_peer", "同業比較", "同業他社との比較分析", "pro"),
    ("investment_news", "ニュースセンチメント", "個別銘柄のニュース分析", "pro"),
    ("investment_earnings", "決算分析", "決算予定・資料・CEO検証", "pro"),
    ("investment_dividend", "配当分析", "配当スケジュール調査", "pro"),
    ("investment_sentiment", "地合い分析", "市場全体のセンチメント", "pro"),
    ("investment_journal", "投資日記分析", "投資日記の癖分析", "pro"),
    ("investment_review", "憲法レビュー", "投資憲法の定期レビュー", "pro"),
    ("investment_risk", "リスク評価", "ポートフォリオのリスク評価", "pro"),
    ("partner_chat", "マネージャー会話", "PWAチャットでの応答", "pro"),
    ("routines", "自動ルーチン", "朝MIT・週次レビュー・Gmail要約・取扱説明書", "flash"),
    ("memo_image", "メモ画像OCR", "撮影メモ・複数画像メモの構造化", "pro"),
    ("task_breakdown", "タスク細分化", "タスクをサブタスクに分割", "pro"),
    ("task_organize", "タスク整理", "タスク一覧の優先度/グルーピング提案", "flash"),
    ("book_prompt", "読書プロンプト生成", "書籍ごとの深掘り質問生成", "pro"),
    ("zt_themes", "ZTテーマ生成", "ゼロから100までの詳細テーマ生成", "pro"),
    ("zt_deep_dive", "ZT深掘り", "メモから追加テーマ5件を生成", "pro"),
    ("daily_review", "今日の振り返り", "活動サマリー＆明日への提案", "flash"),
    ("daily_summary", "デイリーサマリー", "1日のチャット/活動を統合し質問抽出", "pro"),
    ("receipt_ocr", "レシート分析", "レシート画像から店名・金額抽出", "flash"),
    ("meal_image", "食事画像分析", "料理名・カロリー・PFC推定", "flash"),
    ("meal_advice", "食事アドバイス", "1日の食事から栄養バランス助言", "flash"),
    ("english_translate", "英訳", "ENモード・フレーズ帳の日本語→英語変換", "flash"),
]


def _setting_key(feature_key: str) -> str:
    return f"gemini_model.{feature_key}"


@router.get("/settings/gemini_models", dependencies=[Depends(verify_api_key)])
async def settings_gemini_models_get():
    from api.database import get_app_setting
    from services.gemini_model_resolver import is_valid_choice
    items = []
    for key, label, desc, default in GEMINI_FEATURE_CATALOG:
        val = await get_app_setting(_setting_key(key), default)
        items.append({
            "key": key,
            "label": label,
            "description": desc,
            "default": default,
            "value": val if is_valid_choice(val) else default,
        })
    return {"ok": True, "items": items}


class SettingsGeminiModelRequest(BaseModel):
    # {feature_key: <model alias or "gemini-..." model id>, ...}
    values: dict


@router.post("/settings/gemini_models", dependencies=[Depends(verify_api_key)])
async def settings_gemini_models_post(req: SettingsGeminiModelRequest):
    from api.database import set_app_setting
    from services.gemini_model_resolver import is_valid_choice
    valid_keys = {k for k, *_ in GEMINI_FEATURE_CATALOG}
    saved = 0
    for k, v in (req.values or {}).items():
        if k not in valid_keys:
            continue
        if not is_valid_choice(v):
            continue
        await set_app_setting(_setting_key(k), v)
        saved += 1
    return {"ok": True, "saved": saved}


# =========================================================
# マネージャー連絡スケジュール / 自動同期 (ON/OFFのみ・時刻固定)
# =========================================================

def _schedule_setting_key(task_key: str, field: str) -> str:
    return f"schedule.{task_key}.{field}"


_DOW_LABEL = {
    "daily": "毎日", "monday": "月", "tuesday": "火", "wednesday": "水",
    "thursday": "木", "friday": "金", "saturday": "土", "sunday": "日",
}


@router.get("/settings/schedules", dependencies=[Depends(verify_api_key)])
async def settings_schedules_get():
    """カタログを 2 グループ（manager / auto）に分けて返す。時刻は固定で読取専用。"""
    from api.database import get_app_setting
    from services.schedule_resolver import SCHEDULE_CATALOG
    manager = []
    auto = []
    for row in SCHEDULE_CATALOG:
        enabled = await get_app_setting(_schedule_setting_key(row["key"], "enabled"), "1")
        entry = {
            "key": row["key"],
            "label": row["label"],
            "description": row["description"],
            "time": row["time"],
            "dow": row["dow"],
            "dow_label": _DOW_LABEL.get(row["dow"], row["dow"]),
            "category": row["category"],
            "enabled": enabled == "1",
        }
        if row["category"] == "manager":
            manager.append(entry)
        else:
            auto.append(entry)
    return {"ok": True, "manager": manager, "auto": auto}


class SettingsSchedulesRequest(BaseModel):
    values: dict  # {task_key: {"enabled": bool}}


@router.post("/settings/schedules", dependencies=[Depends(verify_api_key)])
async def settings_schedules_post(req: SettingsSchedulesRequest):
    """ON/OFF のみ受け付ける。時刻・曜日はカタログ固定で変更不可。"""
    from api.database import set_app_setting
    from services.schedule_resolver import SCHEDULE_CATALOG
    valid_keys = {row["key"] for row in SCHEDULE_CATALOG}
    saved = 0
    for k, v in (req.values or {}).items():
        if k not in valid_keys or not isinstance(v, dict):
            continue
        if "enabled" in v:
            await set_app_setting(_schedule_setting_key(k, "enabled"), "1" if v["enabled"] else "0")
            saved += 1
    return {"ok": True, "saved": saved}


# ==========================================================
# マネージャー通知ログ（長文の自動通知をチャットから分離）
# ==========================================================

@router.get("/manager/notices", dependencies=[Depends(verify_api_key)])
async def manager_notices_get(limit: int = 30):
    from api.database import list_manager_notices
    try:
        items = await list_manager_notices(limit=max(1, min(int(limit), 100)))
    except Exception as e:
        logging.error(f"manager_notices fetch error: {e}")
        items = []
    unread = sum(1 for it in items if not it.get("is_read"))
    return {"ok": True, "items": items, "unread": unread}


class ManagerNoticeReadRequest(BaseModel):
    is_read: bool = True


@router.post("/manager/notices/{nid}/read", dependencies=[Depends(verify_api_key)])
async def manager_notice_set_read(nid: int, req: ManagerNoticeReadRequest):
    from api.database import set_manager_notice_read
    ok = await set_manager_notice_read(nid, req.is_read)
    if not ok:
        raise HTTPException(status_code=404, detail="通知が見つかりません")
    return {"ok": True}


@router.delete("/manager/notices/{nid}", dependencies=[Depends(verify_api_key)])
async def manager_notice_delete(nid: int):
    from api.database import delete_manager_notice
    ok = await delete_manager_notice(nid)
    if not ok:
        raise HTTPException(status_code=404, detail="通知が見つかりません")
    return {"ok": True}


# ==========================================================
# Gemini Gem URL（設定画面で管理する外部 Gem の URL）
# ==========================================================

GEM_URL_CATALOG = [
    ("investment_screener", "スクリーナー質的分析"),
]


@router.get("/settings/gem_urls", dependencies=[Depends(verify_api_key)])
async def settings_gem_urls_get():
    from api.database import get_app_setting
    items = []
    for key, label in GEM_URL_CATALOG:
        url = await get_app_setting(f"gem_url.{key}", "")
        items.append({"key": key, "label": label, "url": url})
    return {"ok": True, "items": items}


class SettingsGemUrlsRequest(BaseModel):
    values: dict  # {key: url, ...}


@router.post("/settings/gem_urls", dependencies=[Depends(verify_api_key)])
async def settings_gem_urls_post(req: SettingsGemUrlsRequest):
    from api.database import set_app_setting
    valid_keys = {k for k, _ in GEM_URL_CATALOG}
    saved = 0
    for k, v in (req.values or {}).items():
        if k not in valid_keys:
            continue
        url = (v or "").strip()
        if url and not re.match(r"^https?://", url, flags=re.IGNORECASE):
            continue
        await set_app_setting(f"gem_url.{k}", url)
        saved += 1
    return {"ok": True, "saved": saved}


# ==========================================================
# ドラム上達ロードマップ（静的JSON + マイルストーン別YouTubeリンク）
# ==========================================================

from pathlib import Path as _Path

DRUM_ROADMAP_FILE_PATH = _Path(__file__).parent.parent / "data" / "drum_roadmap.json"
DRUM_ROADMAP_LINKS_FILE = "drum_roadmap_links.json"


def _extract_youtube_video_id(url: str) -> Optional[str]:
    """youtu.be/<id>, youtube.com/watch?v=<id>, /shorts/<id> から video_id を取り出す。"""
    if not url:
        return None
    from urllib.parse import urlparse, parse_qs
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return parsed.path.lstrip("/").split("/")[0] or None
    if "youtube.com" in host or "m.youtube.com" in host:
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/shorts/")[1].split("/")[0] or None
        qs = parse_qs(parsed.query or "")
        return qs.get("v", [None])[0]
    return None


def _load_drum_roadmap_static() -> dict:
    """data/drum_roadmap.json をローカル読込（git管理の固定ロードマップ）。"""
    try:
        with open(DRUM_ROADMAP_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"drum_roadmap.json 読込失敗: {e}")
        return {"instrument": "drum", "phases": []}


async def _load_drum_roadmap_links() -> dict:
    """Drive 上の drum_roadmap_links.json を読み込む。{milestone_id: [link, ...]}"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return {}
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return {}
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return {}
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, DRUM_ROADMAP_LINKS_FILE)
    if not f_id:
        return {}
    try:
        raw = await drive.read_text_file(service, f_id)
        return json.loads(raw) or {}
    except Exception as e:
        logging.warning(f"drum_roadmap_links.json 読込失敗: {e}")
        return {}


async def _save_drum_roadmap_links(data: dict) -> None:
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, DRUM_ROADMAP_LINKS_FILE)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    if f_id:
        await drive.update_text(service, f_id, content)
    else:
        await drive.upload_text(service, b_folder, DRUM_ROADMAP_LINKS_FILE, content)


@router.get("/drum_roadmap/links", dependencies=[Depends(verify_api_key)])
async def get_drum_roadmap_links():
    """マイルストーン別に紐付けられた YouTube リンクのみを返す。
    ロードマップ本体はクライアント側で静的に持つため、こちらはリンクの差分情報のみ。"""
    links_map = await _load_drum_roadmap_links()
    return {"ok": True, "links": links_map}


@router.get("/drum_roadmap", dependencies=[Depends(verify_api_key)])
async def get_drum_roadmap():
    """静的ロードマップ + Drive 上の動画リンクをマージして返す。"""
    roadmap = _load_drum_roadmap_static()
    links_map = await _load_drum_roadmap_links()
    phases = []
    for ph in roadmap.get("phases", []):
        milestones = []
        for m in ph.get("milestones", []):
            mid = m.get("id")
            milestones.append({
                "id": mid,
                "label": m.get("label", ""),
                "criteria": m.get("criteria", ""),
                "est_hours": m.get("est_hours"),
                "videos": list(links_map.get(mid, [])),
            })
        phases.append({
            "id": ph.get("id"),
            "label": ph.get("label"),
            "description": ph.get("description", ""),
            "milestones": milestones,
        })
    return {"ok": True, "instrument": roadmap.get("instrument", "drum"), "phases": phases}


class DrumRoadmapLinkAddRequest(BaseModel):
    milestone_id: str
    url: str


@router.post("/drum_roadmap/links/add", dependencies=[Depends(verify_api_key)])
async def add_drum_roadmap_link(req: DrumRoadmapLinkAddRequest):
    """マイルストーンに YouTube リンクを追加。WebClipService の oEmbed でタイトル/著者を取得。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"ok": False, "error": "Bot 未初期化"}
    video_id = _extract_youtube_video_id(req.url)
    if not video_id:
        return {"ok": False, "error": "YouTube URL を解釈できませんでした"}
    # oEmbed でメタ取得（WebClipService 流用）
    from services.webclip_service import WebClipService
    title = ""
    author = ""
    try:
        wc = WebClipService(getattr(bot, "drive_service", None), getattr(bot, "gemini_client", None))
        info = await wc.get_youtube_info(req.url)
        if info:
            title = info.get("title") or ""
            author = info.get("author_name") or ""
    except Exception as e:
        logging.warning(f"YouTube oEmbed 取得失敗: {e}")
    # マイルストーン存在チェック
    roadmap = _load_drum_roadmap_static()
    valid_ids = {m["id"] for ph in roadmap.get("phases", []) for m in ph.get("milestones", [])}
    if req.milestone_id not in valid_ids:
        return {"ok": False, "error": "未知の milestone_id です"}

    links = await _load_drum_roadmap_links()
    arr = links.setdefault(req.milestone_id, [])
    # 同一video_idは置き換え
    arr = [x for x in arr if x.get("video_id") != video_id]
    arr.append({
        "video_id": video_id,
        "url": req.url,
        "title": title or req.url,
        "author": author,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "added_at": datetime.datetime.now(JST).isoformat(),
    })
    links[req.milestone_id] = arr
    await _save_drum_roadmap_links(links)
    return {"ok": True, "videos": arr}


class DrumRoadmapLinkDeleteRequest(BaseModel):
    milestone_id: str
    video_id: str


@router.post("/drum_roadmap/links/delete", dependencies=[Depends(verify_api_key)])
async def delete_drum_roadmap_link(req: DrumRoadmapLinkDeleteRequest):
    links = await _load_drum_roadmap_links()
    arr = links.get(req.milestone_id) or []
    after = [x for x in arr if x.get("video_id") != req.video_id]
    if len(after) == len(arr):
        return {"ok": False, "error": "該当する動画が見つかりません"}
    links[req.milestone_id] = after
    await _save_drum_roadmap_links(links)
    return {"ok": True}


# ==========================================================
# 勉強（学習目標 + 教材/質問 + NotebookLM + セッション時間計測）
# ==========================================================

STUDY_DATA_FILE = "study_data.json"


def _empty_study_data() -> dict:
    return {"goals": [], "items": []}


async def _load_study_data() -> dict:
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return _empty_study_data()
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return _empty_study_data()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return _empty_study_data()
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, STUDY_DATA_FILE)
    if not f_id:
        return _empty_study_data()
    try:
        raw = await drive.read_text_file(service, f_id)
        data = json.loads(raw) or {}
        data.setdefault("goals", [])
        data.setdefault("items", [])
        return data
    except Exception as e:
        logging.warning(f"study_data.json 読込失敗: {e}")
        return _empty_study_data()


async def _save_study_data(data: dict) -> None:
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return
    drive = bot.drive_service
    service = drive.get_service()
    if not service:
        return
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return
    from config import BOT_FOLDER
    b_folder = await drive.find_file(service, folder_id, BOT_FOLDER)
    if not b_folder:
        b_folder = await drive.create_folder(service, folder_id, BOT_FOLDER)
    f_id = await drive.find_file(service, b_folder, STUDY_DATA_FILE)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    if f_id:
        await drive.update_text(service, f_id, content)
    else:
        await drive.upload_text(service, b_folder, STUDY_DATA_FILE, content)


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{datetime.datetime.now(JST).strftime('%Y%m%d%H%M%S%f')}"


@router.get("/study", dependencies=[Depends(verify_api_key)])
async def study_get():
    data = await _load_study_data()
    return {"ok": True, **data}


class StudyGoalSaveRequest(BaseModel):
    id: Optional[str] = None
    title: str
    due_date: Optional[str] = ""
    memo: Optional[str] = ""


@router.post("/study/goal/save", dependencies=[Depends(verify_api_key)])
async def study_goal_save(req: StudyGoalSaveRequest):
    title = (req.title or "").strip()
    if not title:
        return {"ok": False, "error": "タイトルを入力してください"}
    data = await _load_study_data()
    goals = data.setdefault("goals", [])
    if req.id:
        target = next((g for g in goals if g.get("id") == req.id), None)
        if not target:
            return {"ok": False, "error": "対象の目標が見つかりません"}
        target["title"] = title
        target["due_date"] = (req.due_date or "").strip()
        target["memo"] = (req.memo or "").strip()
        goal = target
    else:
        goal = {
            "id": _gen_id("g"),
            "title": title,
            "due_date": (req.due_date or "").strip(),
            "memo": (req.memo or "").strip(),
            "created_at": datetime.datetime.now(JST).isoformat(),
        }
        goals.append(goal)
    await _save_study_data(data)
    return {"ok": True, "goal": goal}


class StudyIdRequest(BaseModel):
    id: str


@router.post("/study/goal/delete", dependencies=[Depends(verify_api_key)])
async def study_goal_delete(req: StudyIdRequest):
    data = await _load_study_data()
    goals = data.setdefault("goals", [])
    after = [g for g in goals if g.get("id") != req.id]
    if len(after) == len(goals):
        return {"ok": False, "error": "対象の目標が見つかりません"}
    data["goals"] = after
    # 配下のアイテムは「目標なし」に移す（削除しない）
    for it in data.setdefault("items", []):
        if it.get("goal_id") == req.id:
            it["goal_id"] = None
    await _save_study_data(data)
    return {"ok": True}


class StudyItemSaveRequest(BaseModel):
    id: Optional[str] = None
    goal_id: Optional[str] = None
    type: str  # "material" | "question"
    title: str
    note_url: Optional[str] = ""
    memo: Optional[str] = ""


@router.post("/study/item/save", dependencies=[Depends(verify_api_key)])
async def study_item_save(req: StudyItemSaveRequest):
    title = (req.title or "").strip()
    if not title:
        return {"ok": False, "error": "タイトルを入力してください"}
    itype = req.type if req.type in ("material", "question") else "material"
    data = await _load_study_data()
    items = data.setdefault("items", [])
    if req.id:
        target = next((it for it in items if it.get("id") == req.id), None)
        if not target:
            return {"ok": False, "error": "対象の項目が見つかりません"}
        target["goal_id"] = req.goal_id or None
        target["type"] = itype
        target["title"] = title
        target["note_url"] = (req.note_url or "").strip()
        target["memo"] = (req.memo or "").strip()
        item = target
    else:
        item = {
            "id": _gen_id("i"),
            "goal_id": req.goal_id or None,
            "type": itype,
            "title": title,
            "note_url": (req.note_url or "").strip(),
            "memo": (req.memo or "").strip(),
            "created_at": datetime.datetime.now(JST).isoformat(),
        }
        items.append(item)
    await _save_study_data(data)
    return {"ok": True, "item": item}


@router.post("/study/item/delete", dependencies=[Depends(verify_api_key)])
async def study_item_delete(req: StudyIdRequest):
    data = await _load_study_data()
    items = data.setdefault("items", [])
    after = [it for it in items if it.get("id") != req.id]
    if len(after) == len(items):
        return {"ok": False, "error": "対象の項目が見つかりません"}
    data["items"] = after
    await _save_study_data(data)
    return {"ok": True}


class StudySessionRequest(BaseModel):
    item_id: str
    status: str  # "start" | "end"


@router.post("/study/session", dependencies=[Depends(verify_api_key)])
async def study_session(req: StudySessionRequest):
    """教材の学習セッションを開始/終了。lifelog_activity と同じフォーマットで Obsidian に記録。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        return {"ok": False, "error": "Bot 未初期化"}
    partner = bot.get_cog("PartnerCog")
    if not partner:
        return {"ok": False, "error": "PartnerCog 未ロード"}
    status = (req.status or "").lower().strip()
    if status not in ("start", "end"):
        return {"ok": False, "error": "status は start か end"}
    data = await _load_study_data()
    item = next((it for it in data.get("items", []) if it.get("id") == req.item_id), None)
    if not item:
        return {"ok": False, "error": "対象の項目が見つかりません"}
    activity_name = f"勉強：{item.get('title', '')}"
    try:
        msg = await partner._log_life_activity_to_obsidian(activity_name, status)
        return {"ok": True, "message": msg, "activity_name": activity_name}
    except Exception as e:
        logging.exception(f"study_session error: {e}")
        return {"ok": False, "error": "ライフログ記録に失敗"}


# ==========================================================
# EDINET API（金融庁 公式API）— 決算関連書類の検索 + PDF 取得
# ==========================================================

class EdinetFindRequest(BaseModel):
    ticker: str
    days: int = 400
    only_earnings: bool = True


class EdinetDownloadRequest(BaseModel):
    doc_id: str
    sec_code: Optional[str] = None
    submit_date: Optional[str] = None
    doc_type_label: Optional[str] = None


@router.post("/edinet/find", dependencies=[Depends(verify_api_key)])
async def edinet_find(req: EdinetFindRequest):
    """指定証券コードの過去 days 日分の EDINET 提出書類を検索する。
    only_earnings=True なら有報・四半期・半期報告書のみに絞る。"""
    from services import edinet_service
    if not edinet_service.get_api_key():
        return {"ok": False, "error": "サーバ側に EDINET_API_KEY が未設定です。.env に追加して bot を再起動してください。"}
    try:
        result = await edinet_service.find_documents_for_security_code(
            req.ticker, days=req.days, only_earnings=req.only_earnings
        )
    except Exception as e:
        logging.exception(f"edinet_find error: {e}")
        return {"ok": False, "error": f"EDINET 検索に失敗: {e}"}
    return result


@router.post("/edinet/download", dependencies=[Depends(verify_api_key)])
async def edinet_download(req: EdinetDownloadRequest):
    """EDINET から書類 PDF を取得し、Drive 上 Investment/EarningsDocs/EDINET/ に保存する。"""
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "drive_service", None):
        return {"ok": False, "error": "Drive サービス未初期化"}

    from services import edinet_service
    if not edinet_service.get_api_key():
        return {"ok": False, "error": "サーバ側に EDINET_API_KEY が未設定です"}

    data = await edinet_service.download_document(req.doc_id, doc_type=2)
    if not data:
        return {"ok": False, "error": "EDINET から PDF を取得できませんでした（PDF未提供か API キー無効）"}

    drive = bot.drive_service
    service = drive.get_service()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not service or not folder_id:
        return {"ok": False, "error": "Drive 認証/フォルダ未設定"}

    # Investment/EarningsDocs/EDINET/ を確保
    inv_folder = await drive.find_file(service, folder_id, "Investment")
    if not inv_folder:
        inv_folder = await drive.create_folder(service, folder_id, "Investment")
    docs_folder = await drive.find_file(service, inv_folder, "EarningsDocs")
    if not docs_folder:
        docs_folder = await drive.create_folder(service, inv_folder, "EarningsDocs")
    edinet_folder = await drive.find_file(service, docs_folder, "EDINET")
    if not edinet_folder:
        edinet_folder = await drive.create_folder(service, docs_folder, "EDINET")

    # ファイル名生成
    sec = (req.sec_code or "")[:4] or "unknown"
    submit_day = (req.submit_date or datetime.datetime.now(JST).strftime("%Y-%m-%d"))[:10]
    label = req.doc_type_label or ""
    # ファイル名で使えない文字を除去
    safe_label = re.sub(r"[\\/:*?\"<>|]", "_", label)[:24]
    filename = f"EDINET_{sec}_{submit_day}_{safe_label}_{req.doc_id}.pdf".replace("__", "_")

    # tempfile に書き出して drive_service.upload_file で保存
    import tempfile
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        # 既存同名はリプレース（古いを消す方が単純）
        existing = await drive.find_file(service, edinet_folder, filename)
        if existing:
            try:
                await asyncio.to_thread(lambda: service.files().delete(fileId=existing).execute())
            except Exception as e:
                logging.warning(f"EDINET 旧ファイル削除失敗: {e}")
        file_id = await drive.upload_file(
            service, edinet_folder, filename, tmp_path, mime_type="application/pdf"
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return {
        "ok": True,
        "file_id": file_id,
        "filename": filename,
        "drive_path": f"Investment/EarningsDocs/EDINET/{filename}",
        "bytes": len(data),
    }