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


async def summarize_video(gemini_client, model: str, video_id: str,
                          title: str = "", description: str = "") -> dict:
    """字幕（なければ概要欄）を Gemini Flash で要約する。
    返り値: {ok, summary, source}。source は 'transcript' / 'description' / ''。"""
    transcript = await fetch_transcript_text(video_id)
    source = "transcript"
    base_text = transcript
    if not base_text:
        # 字幕が無い動画は概要欄テキストで代用（精度は落ちるがコストは同等）
        base_text = (description or "").strip()
        source = "description" if base_text else ""
    if not base_text:
        return {"ok": False, "summary": "", "source": "",
                "error": "字幕も概要も取得できませんでした"}

    base_text = base_text[:_TRANSCRIPT_MAX_CHARS]
    prompt = (
        "次のYouTube動画の内容を、見るかどうかを判断できるように日本語で要約してください。\n"
        "【ルール】\n"
        "- 冒頭に1行で『どんな動画か』を要約\n"
        "- 続けて要点を箇条書きで3〜5個\n"
        "- 専門用語は避け、やさしい言葉で。誇張や憶測はしない\n"
        "- 全体で短めに（長文にしない）\n\n"
        f"# タイトル\n{title}\n\n"
        f"# 内容（{'字幕' if source == 'transcript' else '概要欄'}）\n{base_text}\n"
    )
    try:
        resp = await gemini_client.aio.models.generate_content(
            model=model, contents=prompt,
        )
        summary = (resp.text or "").strip()
        if not summary:
            return {"ok": False, "summary": "", "source": source,
                    "error": "要約生成に失敗しました"}
        return {"ok": True, "summary": summary, "source": source}
    except Exception as e:
        logging.error(f"summarize_video error ({video_id}): {e}")
        return {"ok": False, "summary": "", "source": source, "error": str(e)}


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
