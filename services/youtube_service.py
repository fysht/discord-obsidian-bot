"""YouTube ラッパー：登録チャンネル一覧の取得（Data API）と新着動画の取得（RSS）。

設計（docs: ダラ見対策 / 登録チャンネル新着通知）:
- 認証は Drive と同じ creds を流用（OAuth スコープ `youtube.readonly` を追加済み）。
- 登録チャンネル一覧だけ Data API（`subscriptions.list`）で取得し、登録の入れ替えに追従する。
  1 日 1 回しか呼ばないのでクォータ消費は誤差。
- 新着動画の検知は各チャンネルの公式 RSS（`feeds/videos.xml?channel_id=...`）で行う。
  API クォータを消費せず、`feedparser` でパースできる。
"""
import asyncio
import logging

import aiohttp
import feedparser
from googleapiclient.discovery import build

from config import TIMEOUT_HTTP_DEFAULT

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# 字幕取得で優先する言語（日本語→英語の順で探す）
_TRANSCRIPT_LANGS = ["ja", "ja-JP", "en", "en-US", "en-GB"]
# 要約に渡す字幕の最大文字数。コスト暴発を防ぐため上限を設ける（約8000字≒数千トークン）。
_TRANSCRIPT_MAX_CHARS = 8000


async def fetch_transcript_text(video_id: str) -> str:
    """動画の字幕を1本のテキストにして返す。取得できなければ空文字。
    youtube-transcript-api を使い、API クォータは消費しない。"""
    def _fetch() -> str:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=_TRANSCRIPT_LANGS)
            parts = [seg.get("text", "") for seg in fetched.to_raw_data()]
            text = " ".join(p.strip() for p in parts if p and p.strip())
            return text
        except Exception as e:
            logging.debug(f"transcript fetch failed ({video_id}): {e}")
            return ""

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        logging.debug(f"transcript thread failed ({video_id}): {e}")
        return ""


_SUMMARY_RULES = (
    "次のYouTube動画の内容を、見るかどうかを判断できるように日本語で要約してください。\n"
    "【ルール】\n"
    "- 冒頭に1行で『どんな動画か』を要約\n"
    "- 続けて要点を箇条書きで3〜5個\n"
    "- 専門用語は避け、やさしい言葉で。誇張や憶測はしない\n"
    "- 全体で短めに（長文にしない）\n"
)


async def _summarize_from_text(gemini_client, model, title, text, source_label) -> str:
    prompt = (
        f"{_SUMMARY_RULES}\n"
        f"# タイトル\n{title}\n\n"
        f"# 内容（{source_label}）\n{text}\n"
    )
    resp = await gemini_client.aio.models.generate_content(model=model, contents=prompt)
    return (resp.text or "").strip()


async def _summarize_from_video_url(gemini_client, model, title, url) -> str:
    """GeminiにYouTube URLを直接渡して動画から要約する（字幕が取れない時のフォールバック）。
    字幕取得がサーバーIPブロック等で全滅する環境でも動くが、動画トークン分コストは高め。"""
    from google.genai import types
    prompt = f"{_SUMMARY_RULES}\n# タイトル\n{title}\n"
    video_part = types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*"))
    text_part = types.Part.from_text(text=prompt)
    content = types.Content(role="user", parts=[video_part, text_part])
    resp = await gemini_client.aio.models.generate_content(model=model, contents=[content])
    return (resp.text or "").strip()


async def summarize_video(gemini_client, model: str, video_id: str,
                          title: str = "", url: str = "", description: str = "") -> dict:
    """動画を要約する。字幕→（ダメなら）動画解析→概要欄の順で試す。
    返り値: {ok, summary, source}。source は 'transcript' / 'video' / 'description' / ''。"""
    video_url = url or f"https://www.youtube.com/watch?v={video_id}"
    errors: list[str] = []

    # 1) 字幕（最も安い。クォータ消費なし）
    transcript = await fetch_transcript_text(video_id)
    if transcript:
        try:
            summary = await _summarize_from_text(
                gemini_client, model, title, transcript[:_TRANSCRIPT_MAX_CHARS], "字幕")
            if summary:
                return {"ok": True, "summary": summary, "source": "transcript"}
        except Exception as e:
            logging.error(f"summarize_video(transcript) error ({video_id}): {e}")
            errors.append(f"字幕要約失敗: {e}")
    else:
        errors.append("字幕を取得できませんでした（サーバーがブロックされている可能性）")

    # 2) 字幕が取れない/失敗 → GeminiにYouTube URLを直接渡して動画から要約（堅牢・コスト高め）
    try:
        summary = await _summarize_from_video_url(gemini_client, model, title, video_url)
        if summary:
            return {"ok": True, "summary": summary, "source": "video"}
        errors.append("動画解析の応答が空でした")
    except Exception as e:
        logging.error(f"summarize_video(video) error ({video_id}): {e}")
        errors.append(f"動画解析失敗: {e}")

    # 3) 最後の手段：概要欄テキスト
    desc = (description or "").strip()
    if desc:
        try:
            summary = await _summarize_from_text(gemini_client, model, title, desc[:_TRANSCRIPT_MAX_CHARS], "概要欄")
            if summary:
                return {"ok": True, "summary": summary, "source": "description"}
        except Exception as e:
            logging.error(f"summarize_video(description) error ({video_id}): {e}")
            errors.append(f"概要欄要約失敗: {e}")

    return {"ok": False, "summary": "", "source": "",
            "error": " / ".join(errors) or "要約を生成できませんでした"}


class YouTubeService:
    def __init__(self, creds):
        self.creds = creds

    def get_service(self):
        if not self.creds:
            return None
        return build("youtube", "v3", credentials=self.creds)

    async def list_subscriptions(self) -> list[dict]:
        """ログインユーザーの登録チャンネル一覧を返す（[{channel_id, title}]）。

        `subscriptions.list(mine=True)` を 50 件ずつページネーションして全件集める。
        1 リクエスト 1 ユニット・1 日 1 回想定なのでクォータは軽微。"""
        service = self.get_service()
        if not service:
            return []
        out: list[dict] = []
        page_token = None
        try:
            while True:
                res = await asyncio.to_thread(
                    lambda pt=page_token: service.subscriptions().list(
                        part="snippet", mine=True, maxResults=50, pageToken=pt,
                        order="alphabetical",
                    ).execute()
                )
                for item in res.get("items", []) or []:
                    sn = item.get("snippet", {}) or {}
                    ch_id = (sn.get("resourceId", {}) or {}).get("channelId")
                    if not ch_id:
                        continue
                    out.append({"channel_id": ch_id, "title": (sn.get("title") or "").strip()})
                page_token = res.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            logging.error(f"YouTube list_subscriptions error: {e}")
        return out

    async def fetch_channel_feed(self, channel_id: str) -> list[dict]:
        """チャンネルの RSS から最新動画（最大15件）を返す。

        [{video_id, title, url, published_at, channel_title}] 。
        ネットワーク/パース失敗時は空リスト（その回はスキップ）。"""
        url = RSS_URL.format(channel_id=channel_id)
        text = None
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_HTTP_DEFAULT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return []
                    text = await resp.text()
        except Exception as e:
            logging.debug(f"YouTube RSS fetch failed ({channel_id}): {e}")
            return []
        if not text:
            return []
        try:
            feed = await asyncio.to_thread(feedparser.parse, text)
        except Exception as e:
            logging.debug(f"YouTube RSS parse failed ({channel_id}): {e}")
            return []
        channel_title = ((feed.get("feed") or {}).get("title") or "").strip()
        out: list[dict] = []
        for e in (feed.get("entries") or []):
            # feedparser は yt:videoId を yt_videoid に正規化する。無ければ id から拾う。
            vid = e.get("yt_videoid") or ""
            if not vid:
                raw_id = e.get("id") or ""
                if "yt:video:" in raw_id:
                    vid = raw_id.split("yt:video:", 1)[1].strip()
            if not vid:
                continue
            out.append({
                "video_id": vid,
                "title": (e.get("title") or "").strip(),
                "url": e.get("link") or f"https://www.youtube.com/watch?v={vid}",
                "published_at": e.get("published") or "",
                "channel_title": channel_title,
            })
        return out
