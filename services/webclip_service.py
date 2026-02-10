import os
import datetime
import re
import asyncio
import logging
import zoneinfo
import aiohttp
import json
from google import genai
from web_parser import parse_url_with_readability

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class WebClipService:
    def __init__(self, drive_service, gemini_api_key):
        self.drive_service = drive_service
        self.gemini_client = None
        if gemini_api_key:
            self.gemini_client = genai.Client(api_key=gemini_api_key)

    async def get_youtube_info(self, url):
        """YouTubeのoembed APIからタイトルとチャンネル名を取得"""
        # URLから動画IDらしきものを抽出する簡易正規表現（oembedに投げるのでURLそのままでも動くことが多いが、念のため）
        # oembedは動画URLをパラメータとして受け取るため、URLそのままでリクエストします
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(oembed_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "title": data.get("title"),
                            "author_name": data.get("author_name")
                        }
        except Exception as e:
            logging.error(f"YouTube Info Fetch Error: {e}")
        
        return None

    async def process_url(self, url, message_content, trigger_message_obj):
        """
        URLを処理し、Driveに保存し、結果の概要を返す
        """
        is_youtube = "youtube.com" in url or "youtu.be" in url
        content_type = "YouTube" if is_youtube else "WebClip"
        
        title = "Untitled"
        raw_text = ""
        author_name = ""

        # 1. 情報取得（YouTubeとその他で分岐）
        if is_youtube:
            yt_info = await self.get_youtube_info(url)
            if yt_info:
                title = yt_info.get("title", "Untitled")
                author_name = yt_info.get("author_name", "")
            else:
                # oembed失敗時のフォールバック
                try:
                    title, _ = await parse_url_with_readability(url)
                except:
                    title = "YouTube Video"
        else:
            try:
                title, raw_text = await parse_url_with_readability(url)
                if not title or title == "No Title Found":
                    title = "Untitled"
            except Exception as e:
                logging.error(f"WebClip: Parse Error: {e}")
                title = "Untitled"

        # 2. ファイル名とコンテンツの作成
        now = datetime.datetime.now(JST)
        timestamp = now.strftime('%Y%m%d%H%M%S')
        daily_note_date = now.strftime('%Y-%m-%d')
        
        # ファイル名に使えない文字を除去
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        if not safe_title: safe_title = "Untitled"
        
        filename = f"{timestamp}-{safe_title}.md"
        filename_no_ext = f"{timestamp}-{safe_title}"
        
        final_content = ""
        summary_text = ""
        
        # --- 本文作成（# タイトル を削除しました）---
        if is_youtube:
            # YouTubeの場合
            final_content = (
                f"- **URL:** {url}\n"
                f"- **Channel:** {author_name}\n"
                f"- **Saved at:** {now}\n\n"
                f"## Note\n{message_content}\n\n"
                f"---\n"
                f"[[{daily_note_date}]]"
            )
            summary_text = f"YouTube動画のメモを保存しました: {title}"
        else:
            # Web記事の場合
            if len(raw_text) < 10:
                logging.warning(f"WebClip Warning: Content might be empty. URL: {url}")

            final_content = (
                f"- **Source:** <{url}>\n"
                f"- **Saved at:** {now}\n\n"
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{raw_text}"
            )
            summary_text = f"Web記事を保存しました: {title}"

        # 3. Driveへ保存
        service = self.drive_service.get_service()
        if not service:
            await trigger_message_obj.add_reaction('❌')
            return None

        folder_name = "YouTube" if is_youtube else "WebClips"
        section_header = f"## {folder_name}"

        try:
            # フォルダ取得・作成
            folder_id = await self.drive_service.find_file(service, self.drive_service.folder_id, folder_name)
            if not folder_id:
                folder_id = await self.drive_service.create_folder(service, self.drive_service.folder_id, folder_name)
            
            # ファイルアップロード
            await self.drive_service.upload_text(service, folder_id, filename, final_content)

            # 日記へリンク
            link_str = f"- [[{folder_name}/{filename_no_ext}|{title}]]"
            
            await self.drive_service.update_daily_note(service, daily_note_date, link_str, section_header)

            await trigger_message_obj.reply(f"✅ {content_type}を保存しました。\n📂 `{folder_name}/{filename}`")
            
            return {
                "title": title,
                "summary": summary_text,
                "type": content_type
            }

        except Exception as e:
            logging.error(f"WebClip: Save Error: {e}")
            await trigger_message_obj.add_reaction('❌')
            return None