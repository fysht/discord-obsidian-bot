import os
import datetime
import re
import asyncio
import logging
import zoneinfo
from google import genai
from web_parser import parse_url_with_readability

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class WebClipService:
    def __init__(self, drive_service, gemini_api_key):
        self.drive_service = drive_service
        self.gemini_client = None
        if gemini_api_key:
            self.gemini_client = genai.Client(api_key=gemini_api_key)

    async def process_url(self, url, message_content, trigger_message_obj):
        """
        URLを処理し、Driveに保存し、結果の概要を返す
        """
        # 1. URL解析（タイトルと本文取得）
        try:
            title, raw_text = await parse_url_with_readability(url)
            # タイトルが取得できなかった場合のフォールバック
            if not title or title == "No Title Found":
                title = "Untitled"
        except Exception as e:
            logging.error(f"WebClip: Parse Error: {e}")
            title = "Untitled"
            raw_text = ""

        is_youtube = "youtube.com" in url or "youtu.be" in url
        content_type = "YouTube" if is_youtube else "WebClip"
        
        # 2. ファイル名とコンテンツの作成
        now = datetime.datetime.now(JST)
        timestamp = now.strftime('%Y%m%d%H%M%S') # YYYYMMDDHHMMSS形式
        daily_note_date = now.strftime('%Y-%m-%d')
        
        # ファイル名に使えない文字を除去
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        if not safe_title: safe_title = "Untitled"
        
        # 拡張子付きファイル名
        filename = f"{timestamp}-{safe_title}.md"
        # リンク用ファイル名（拡張子なし）
        filename_no_ext = f"{timestamp}-{safe_title}"
        
        final_content = ""
        summary_text = ""
        
        if is_youtube:
            # --- YouTubeの場合: メッセージ内容をそのまま保存 ---
            final_content = (
                f"# {title}\n\n"
                f"- **URL:** {url}\n"
                f"- **Saved at:** {now}\n\n"
                f"## Note\n{message_content}\n\n"
                f"---\n"
                f"[[{daily_note_date}]]"
            )
            summary_text = f"YouTube動画のメモを保存しました: {title}"
        else:
            # --- Web記事の場合: 要約せずそのまま保存 ---
            if len(raw_text) < 10: # 極端に短い場合は警告しつつ保存は試みる
                logging.warning(f"WebClip Warning: Content might be empty. URL: {url}")

            final_content = (
                f"# {title}\n\n"
                f"- **Source:** <{url}>\n\n"
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{raw_text}"
            )
            
            # Botの返答用テキスト（要約はしないのでタイトルのみ）
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

            # 日記へリンク（ファイル名ベースのWikiLink形式）
            # ノート名が変わったため、リンク形式もそれに合わせます
            link_str = f"- [[{folder_name}/{filename_no_ext}|{title}]]"
            
            await self.drive_service.update_daily_note(service, daily_note_date, link_str, section_header)

            # 完了メッセージ
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