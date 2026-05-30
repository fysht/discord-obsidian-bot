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
    get_recent_errors,
    get_reading_plan, update_reading_plan,
)
from api import notification_service
from services.info_service import InfoService
# web_parser（playwright/readability 等の重い依存）は起動時の常駐メモリを抑えるため
# 使用箇所で遅延 import する。
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
    search_mode: bool = False  # 🔍 ON 時に Gemini の Google Search grounding を強制有効化
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
            from web_parser import fetch_maps_info
            place_name, _ = await fetch_maps_info(url)
            if place_name and place_name != "Google Maps Location":
                title = place_name
        except Exception as e:
            logging.debug(f"Maps情報取得失敗: {e}")
        return {"title": title, "type": link_type}

    if "amazon.co.jp" in url or "amzn.to" in url or "amazon.com" in url:
        link_type = "book"
        try:
            from web_parser import parse_url_with_readability
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

    # 注: mark_user_responded() は AI 応答を保存した後に呼ぶ。
    # 先に呼ぶと、ユーザーのメッセージより前に保留通知が表示されてしまい、流れが悪い。

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
                reply += f"\n[ACTION:open_link:id={link_id}]"
            # レシピを保存したら、食事ログへの登録も提案する
            if meta["type"] == "recipe":
                _meal_name = re.sub(r"[\[\]|\n]", "", meta["title"]).strip()[:40]
                if _meal_name:
                    reply += f"\n[ACTION:log_meal:name={_meal_name}]"
            user_id = await save_message("user", req.message, reply_to=req.reply_to_id)
            asst_id = await notification_service.save_message_and_notify("assistant", reply)
            try:
                await notification_service.mark_user_responded()
            except Exception as e:
                logging.debug(f"mark_user_responded failed: {e}")
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
        user_message + english_feedback_hint, history_messages,
        english_mode=req.english_mode, search_mode=req.search_mode,
    )

    # 「〇〇を食べた」系のメッセージを検出 → 食事ログ登録ボタンを添える
    try:
        if "[ACTION:log_meal" not in reply:
            meal_m = re.search(
                r"([^\s、。!?！？]{1,40}?)\s*を?\s*(?:食べた(?!い)|食べました|食った(?![らり])|いただきました)",
                req.message,
            )
            if meal_m:
                food = meal_m.group(1)
                # 「今日は」「昼に」などの前置きを助詞で切り落として料理名を取り出す
                for _sep in ("は", "に", "で"):
                    if _sep in food:
                        food = food.split(_sep)[-1]
                food = re.sub(r"[\[\]|\n]", "", food).strip("　 、。")
                if food and len(food) <= 30:
                    reply += f"\n[ACTION:log_meal:name={food}]"
    except Exception as e:
        logging.debug(f"meal action detection failed: {e}")

    asst_id = await notification_service.save_message_and_notify("assistant", reply)

    # AI 応答送信後に保留通知を放出（順序: ユーザー → AI → 保留通知）
    try:
        await notification_service.mark_user_responded()
    except Exception as e:
        logging.debug(f"mark_user_responded failed: {e}")

    # 夜の振り返り保留中なら、ユーザーのメッセージを回答として記録し Obsidian へ保存
    try:
        _today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        _pending_reflect = await get_questions_by_date(_today, scope='nightly_reflection')
        _unanswered = [q for q in _pending_reflect if q.get('status') == 'pending']
        if _unanswered:
            await answer_daily_question(_unanswered[0]['id'], req.message)
            safe_create_task(
                _save_nightly_reflection_to_obsidian(_today),
                name="nightly-reflection-save",
            )
    except Exception as e:
        logging.debug(f"nightly reflection capture failed: {e}")

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

# /history, /error_log, /permanent_notes/confirm は api/routers/core_misc.py へ移動済み

# /dashboard は api/routers/dashboard.py へ移動済み

# task_action は api/routers/tasks_ai.py へ移動済み

# /reset_history は api/routers/core_misc.py へ移動済み

# calendar_action は api/routers/calendar.py へ移動済み

# Google Tasks 関連 (action / move / get) は api/routers/google_tasks.py へ移動済み

# /sleep_trend は api/routers/fitbit.py へ移動済み

# /daily_report は api/routers/core_misc.py へ移動済み

# habit notes 関連ヘルパーは api/routers/habits.py へ移動済み
# /google_tasks (GET) も api/routers/google_tasks.py へ移動済み


# Habits 関連エンドポイント (9件) は api/routers/habits.py へ移動済み



# task_candidates / book_notes は api/routers/tasks_ai.py へ移動済み

# /links CRUD は api/routers/stocked_links.py へ移動済み

# ===== 手書きメモ読み取り・保存 =====

# /note_from_image は api/routers/notes.py へ移動済み

# /notes/list, /notes/search, /save_note は api/routers/notes.py へ移動済み

# ===== メッセージ操作 (削除 / star / 検索) =====

# /messages の delete/star/starred/search は api/routers/messages.py へ移動済み

# ===== 手書きメモ: 複数画像対応 =====

# /note_from_images は api/routers/notes.py へ移動済み

# ===== ストックリンク一括既読化 =====

# LinkBulkStatusRequest は api/routers/stocked_links.py へ移動済み


# ===== MIT (Most Important Tasks) =====

# MIT / daily_journal エンドポイントは api/routers/mit_journal.py へ移動済み


# ===== Web Push 通知 =====

# Push 関連エンドポイントは api/routers/push.py へ移動済み

# /links/bulk_status は api/routers/stocked_links.py へ移動済み

# task_breakdown 系は api/routers/tasks_ai.py へ移動済み

# Reading 関連エンドポイントは api/routers/reading.py へ移動済み

# 旧 Study (subjects/save) は api/routers/legacy_study.py へ移動済み

# Zerosec 関連エンドポイントは api/routers/zerosec.py へ移動済み

# task_triage は api/routers/tasks_ai.py へ移動済み

# Weather / location_log は api/routers/environment.py へ移動済み

# 英語フレーズ帳エンドポイント (7件) は api/routers/english_phrases.py へ移動済み

# /messages の label/collections/labeled は api/routers/messages.py へ移動済み

# /fitbit_all_data は api/routers/fitbit.py へ移動済み

# Briefing は api/routers/briefing.py へ移動済み

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


async def _save_nightly_reflection_to_obsidian(date_str: str) -> bool:
    """夜の振り返り（scope='nightly_reflection'）の Q&A を `## 🌙 Nightly Reflection` セクションへ保存する。"""
    import re as _re
    from api import app
    from utils.obsidian_utils import update_section
    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return False
    items = await get_questions_by_date(date_str, scope='nightly_reflection')
    answered = [q for q in items if (q.get("answer") or "").strip()]
    if not answered:
        return False
    lines = []
    for q in answered:
        question = (q.get("question") or "").strip()
        answer = (q.get("answer") or "").strip()
        if question:
            lines.append(f"- **Q:** {question}")
            ans_lines = answer.splitlines() or [answer]
            lines.append(f"  - **A:** {ans_lines[0]}")
            for extra in ans_lines[1:]:
                if extra.strip():
                    lines.append(f"    {extra}")
        else:
            lines.append(answer)
    body = "\n".join(lines)
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

        section_header = "## 🌙 Nightly Reflection"
        replacement = f"{section_header}\n{body}"
        pattern = _re.compile(rf"{_re.escape(section_header)}\n.*?(?=\n## |\Z)", _re.DOTALL)
        if pattern.search(content):
            new_content = pattern.sub(replacement, content, count=1)
        else:
            new_content = update_section(content, body, section_header)

        if f_id:
            await chat_service.drive_service.update_text(service, f_id, new_content)
        else:
            await chat_service.drive_service.upload_text(
                service, folder_id, f"{date_str}.md", new_content
            )
        # 保存後は質問を resolved に
        await resolve_questions(date_str, scope='nightly_reflection')
        return True
    except Exception as e:
        logging.error(f"_save_nightly_reflection_to_obsidian error: {e}")
        return False


# daily_summary / daily_questions エンドポイントは api/routers/daily_summary.py へ移動済み

# ============================================================
# コストメーター (API 利用料金の可視化)
# ============================================================

# Cost 関連エンドポイントは api/routers/cost.py へ移動済み

# Gmail 関連エンドポイント (5件) は api/routers/gmail.py へ移動済み

# Expenses 関連エンドポイント (8件) は api/routers/expenses.py へ移動済み

# Meals 関連エンドポイント (7件) は api/routers/meals.py へ移動済み

# Lifelog / thought_reflection / morning_mit エンドポイントは api/routers/lifelog.py へ移動済み

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


# Investment 分析エンドポイント (16件) は api/routers/investment_analysis.py へ移動済み

# Portfolio 関連エンドポイントは api/routers/investment_portfolio.py へ移動済み


# ----- 投資日記 -----

# Journal 関連エンドポイント (list/add/get/edit/delete/analyze/suggest_title) は
# api/routers/investment_journal.py へ移動済み（main.py で include_router される）


# Alerts 関連エンドポイントは api/routers/investment_alerts.py へ移動済み


# Screener / Watchlist / Screener Runs 関連エンドポイントは
# api/routers/investment_screener.py および investment_watchlist.py へ移動済み


# Gemini モデル設定エンドポイントは api/routers/gemini_settings.py へ移動済み

# Schedules / Gem URLs 設定エンドポイントは api/routers/settings_misc.py へ移動済み

# Drum roadmap エンドポイントは api/routers/drum_roadmap.py へ移動済み

# Study (goal/item/session) エンドポイントは api/routers/study.py へ移動済み

# ==========================================================
# EDINET API（金融庁 公式API）— 決算関連書類の検索 + PDF 取得
# ==========================================================

# EDINET 関連エンドポイントは api/routers/edinet.py へ移動済み

