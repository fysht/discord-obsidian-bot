import datetime
import re
import asyncio
import logging
import aiohttp

from config import JST
from web_parser import parse_url_with_readability, fetch_maps_info
from utils.obsidian_utils import update_section  # デイリーノート更新用


class WebClipService:
    def __init__(self, drive_service, gemini_client):
        self.drive_service = drive_service
        self.gemini_client = gemini_client

    async def get_youtube_info(self, url):
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(oembed_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "title": data.get("title"),
                            "author_name": data.get("author_name"),
                        }
        except Exception as e:
            logging.error(f"YouTube Info Fetch Error: {e}")
        return None

    def _is_recipe(self, title, url, text=""):
        recipe_domains = [
            "cookpad.com",
            "kurashiru.com",
            "delishkitchen.tv",
            "macaro-ni.jp",
            "orangepage.net",
            "lettuceclub.net",
            "erecipe.woman.excite.co.jp",
            "kyounoryouri.jp",
            "ajinomoto.co.jp",
        ]
        if any(d in url for d in recipe_domains):
            return True

        keywords = [
            "レシピ",
            "作り方",
            "献立",
            "Recipe",
            "Cooking",
            "材料",
            "下ごしらえ",
        ]
        if any(k in title for k in keywords):
            return True
        if text and "材料" in text and "作り方" in text:
            return True
        return False

    # ★ 追加: Googleマップかどうかの判定メソッド
    def _is_google_maps(self, url):
        map_domains = [
            "google.com/maps",
            "goo.gl/maps",
            "maps.app.goo.gl",
            "http://googleusercontent.com/maps.google.com",
        ]
        return any(domain in url for domain in map_domains)

    async def _get_fallback_title(self, url):
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                async with session.get(url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        html = await response.text()
                        match = re.search(
                            r"<title[^>]*>(.*?)</title>",
                            html,
                            re.IGNORECASE | re.DOTALL,
                        )
                        if match:
                            return match.group(1).strip()
        except Exception as e:
            logging.error(f"Fallback Title Fetch Error: {e}")
        return "Untitled"

    async def process_url(self, url, message_content, trigger_message_obj):
        is_youtube = "youtube.com" in url or "youtu.be" in url
        is_map = self._is_google_maps(url)  # ★ マップ判定
        title = "Untitled"
        raw_text = ""
        author_name = ""
        map_desc = ""

        if is_youtube:
            yt_info = await self.get_youtube_info(url)
            if yt_info:
                title = yt_info.get("title", "Untitled")
                author_name = yt_info.get("author_name", "")
            else:
                try:
                    title, _ = await parse_url_with_readability(url)
                except Exception:
                    title = "YouTube Video"
        elif is_map:
            # Playwrightを使ってメタ情報を取得
            title, map_desc = await fetch_maps_info(url)
            if not title or title == "Untitled":
                title = "Google Maps Location"
        else:
            try:
                title, raw_text = await asyncio.wait_for(
                    parse_url_with_readability(url), timeout=60.0
                )
                if not title or title == "No Title Found":
                    title = await self._get_fallback_title(url)
            except asyncio.TimeoutError:
                logging.warning(f"WebClip: Parse Timeout for URL: {url}")
                title = await self._get_fallback_title(url)
                raw_text = "※ページの読み込みに時間がかかったため、本文は取得できませんでした。\n"
            except Exception as e:
                logging.error(f"WebClip: Parse Error: {e}")
                title = await self._get_fallback_title(url)
                raw_text = "※ページの解析中にエラーが発生しました。\n"

        check_text = raw_text if not is_youtube else (title + " " + message_content)
        is_recipe = self._is_recipe(title, url, check_text)

        # ★ 修正: 保存先フォルダとラベルの分岐にマップを追加
        if is_youtube:
            folder_name = "YouTube"
            content_type_label = "📺 YouTube"
            section_header = "## 📺 YouTube"
        elif is_map:
            folder_name = "Places"
            content_type_label = "📍 場所（マップ）"
            section_header = "## 🔗 WebClips"  # デイリーノート上ではWebClipsにまとめる
        elif is_recipe:
            folder_name = "Recipes"
            content_type_label = "🍳 レシピ"
            section_header = "## 🍳 Recipes"
        else:
            folder_name = "WebClips"
            content_type_label = "🔗 WebClip"
            section_header = "## 🔗 WebClips"

        now = datetime.datetime.now(JST)
        timestamp = now.strftime("%Y%m%d%H%M%S")
        daily_note_date = now.strftime("%Y-%m-%d")
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        if not safe_title:
            safe_title = "Untitled"

        filename = f"{timestamp}-{safe_title}.md"
        filename_no_ext = f"{timestamp}-{safe_title}"
        user_comment = message_content.replace(url, "").strip()
        note_section = f"## 💬 Note\n{user_comment}\n\n" if user_comment else ""

        final_content = ""

        # ★ 保存するノート内容の分岐
        if is_youtube:
            final_content = (
                f"- **URL:** {url}\n- **Channel:** {author_name}\n- **Saved at:** {now}\n\n"
                f"{note_section}---\n[[{daily_note_date}]]"
            )
        elif is_map:
            final_content = (
                f"- **Google Maps:** <{url}>\n"
                f"- **Place:** {title}\n"
                f"- **Info:** {map_desc}\n"
                f"- **Saved at:** {now}\n\n"
                f"{note_section}---\n[[{daily_note_date}]]"
            )
        else:
            if len(raw_text) < 10:
                raw_text = "※本文の自動取得ができなかったページです。\n"
            final_content = (
                f"- **Source:** <{url}>\n- **Saved at:** {now}\n\n"
                f"{note_section}---\n\n[[{daily_note_date}]]\n\n{raw_text}"
            )

        service = self.drive_service.get_service()
        if not service:
            await trigger_message_obj.add_reaction("❌")
            return None

        try:
            folder_id = await self.drive_service.find_file(
                service, self.drive_service.folder_id, folder_name
            )
            if not folder_id:
                folder_id = await self.drive_service.create_folder(
                    service, self.drive_service.folder_id, folder_name
                )

            # クリップ本体を保存
            await self.drive_service.upload_text(
                service, folder_id, filename, final_content
            )

            # ---- デイリーノートへのリンク追記処理 ----
            link_str = f"- [[{folder_name}/{filename_no_ext}|{title}]]"

            daily_folder_id = await self.drive_service.find_file(
                service, self.drive_service.folder_id, "DailyNotes"
            )
            if not daily_folder_id:
                daily_folder_id = await self.drive_service.create_folder(
                    service, self.drive_service.folder_id, "DailyNotes"
                )

            daily_file_name = f"{daily_note_date}.md"
            daily_file_id = await self.drive_service.find_file(
                service, daily_folder_id, daily_file_name
            )

            current_note_content = (
                f"---\ndate: {daily_note_date}\n---\n\n# Daily Note {daily_note_date}\n"
            )
            if daily_file_id:
                current_note_content = await self.drive_service.read_text_file(
                    service, daily_file_id
                )

            updated_note_content = update_section(
                current_note_content, link_str, section_header
            )

            if daily_file_id:
                await self.drive_service.update_text(
                    service, daily_file_id, updated_note_content
                )
            else:
                await self.drive_service.upload_text(
                    service, daily_folder_id, daily_file_name, updated_note_content
                )
            # ---- 修正ここまで ----

            # ★ 修正: 直接返信するのではなく、パートナーAIに渡すための辞書データを返す
            return {
                "success": True,
                "type": content_type_label,
                "title": title,
                "folder": folder_name,
                "file": filename,
            }

        except Exception as e:
            logging.error(f"WebClip: Save Error: {e}")
            await trigger_message_obj.add_reaction("❌")
            return None
