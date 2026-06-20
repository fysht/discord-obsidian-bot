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
