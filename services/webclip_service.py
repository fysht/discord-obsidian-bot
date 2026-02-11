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

    def _is_recipe(self, title, url, text=""):
        """タイトル、URL、本文からレシピかどうかを判定する"""
        # 1. ドメイン判定 (代表的なレシピサイト)
        recipe_domains = [
            'cookpad.com', 'kurashiru.com', 'delishkitchen.tv', 
            'macaro-ni.jp', 'orangepage.net', 'lettuceclub.net', 
            'erecipe.woman.excite.co.jp', 'kyounoryouri.jp', 'ajinomoto.co.jp'
        ]
        if any(d in url for d in recipe_domains):
            return True

        # 2. キーワード判定 (タイトル)
        keywords = ['レシピ', '作り方', '献立', 'Recipe', 'Cooking', '材料', '下ごしらえ']
        if any(k in title for k in keywords):
            return True
            
        # 3. 本文判定 (Web記事の場合)
        if text and '材料' in text and '作り方' in text:
            return True

        return False

    async def _get_fallback_title(self, url):
        """Playwrightでの取得が失敗した際に、軽量なHTTP通信でタイトルのみを取得する"""
        try:
            async with aiohttp.ClientSession() as session:
                # 一般的なブラウザからのアクセスに見せかける
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                async with session.get(url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        html = await response.text()
                        # 正規表現で <title> タグの中身だけを抽出
                        match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
                        if match:
                            return match.group(1).strip()
        except Exception as e:
            logging.error(f"Fallback Title Fetch Error: {e}")
        
        return "Untitled"

    async def process_url(self, url, message_content, trigger_message_obj):
        """
        URLを処理し、Driveに保存し、結果の概要を返す
        """
        is_youtube = "youtube.com" in url or "youtu.be" in url
        source_type = "YouTube" if is_youtube else "WebClip"
        
        title = "Untitled"
        raw_text = ""
        author_name = ""

        # 1. 情報取得
        if is_youtube:
            yt_info = await self.get_youtube_info(url)
            if yt_info:
                title = yt_info.get("title", "Untitled")
                author_name = yt_info.get("author_name", "")
            else:
                try:
                    title, _ = await parse_url_with_readability(url)
                except:
                    title = "YouTube Video"
        else:
            try:
                # 35秒でタイムアウトするように設定
                title, raw_text = await asyncio.wait_for(parse_url_with_readability(url), timeout=35.0)
                
                # タイトルが正常に取れなかった場合は予備メソッドで取得
                if not title or title == "No Title Found":
                    title = await self._get_fallback_title(url)
                    
            except asyncio.TimeoutError:
                logging.warning(f"WebClip: Parse Timeout for URL: {url}")
                # タイムアウト時は予備メソッドでタイトルだけ取得し、本文は固定メッセージにする
                title = await self._get_fallback_title(url)
                raw_text = "※ページの読み込みに時間がかかったため、本文は取得できませんでした。\n"
                
            except Exception as e:
                logging.error(f"WebClip: Parse Error: {e}")
                # その他のエラー時もタイトルだけ取得する
                title = await self._get_fallback_title(url)
                raw_text = f"※ページの解析中にエラーが発生しました。\n"

        # 2. レシピ判定
        check_text = raw_text if not is_youtube else (title + " " + message_content)
        is_recipe = self._is_recipe(title, url, check_text)

        # 3. 保存先フォルダとセクションの決定
        if is_recipe:
            folder_name = "Recipes"
            content_type_label = "Recipe"
        elif is_youtube:
            folder_name = "YouTube"
            content_type_label = "YouTube"
        else:
            folder_name = "WebClips"
            content_type_label = "WebClip"

        section_header = f"## {folder_name}"

        # 4. ファイル名とコンテンツの作成
        now = datetime.datetime.now(JST)
        timestamp = now.strftime('%Y%m%d%H%M%S')
        daily_note_date = now.strftime('%Y-%m-%d')
        
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        if not safe_title: safe_title = "Untitled"
        
        filename = f"{timestamp}-{safe_title}.md"
        filename_no_ext = f"{timestamp}-{safe_title}"
        
        # ユーザーのメモを抽出（メッセージからURLを取り除いた部分）
        user_comment = message_content.replace(url, "").strip()
        note_section = f"## Note\n{user_comment}\n\n" if user_comment else ""

        final_content = ""
        summary_text = ""
        
        if is_youtube:
            # YouTube形式
            final_content = (
                f"- **URL:** {url}\n"
                f"- **Channel:** {author_name}\n"
                f"- **Saved at:** {now}\n\n"
                f"{note_section}"
                f"---\n"
                f"[[{daily_note_date}]]"
            )
            summary_text = f"YouTube動画を保存しました: {title}"
        else:
            # Web記事・レシピ形式
            if len(raw_text) < 10:
                logging.warning(f"WebClip Warning: Content might be empty. URL: {url}")
                raw_text = "※本文の自動取得ができなかったページです。\n"

            final_content = (
                f"- **Source:** <{url}>\n"
                f"- **Saved at:** {now}\n\n"
                f"{note_section}"
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{raw_text}"
            )
            summary_text = f"Web記事を保存しました: {title}"
            
        if is_recipe:
            summary_text = f"レシピを保存しました: {title}"

        # 5. Driveへ保存
        service = self.drive_service.get_service()
        if not service:
            await trigger_message_obj.add_reaction('❌')
            return None

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

            await trigger_message_obj.reply(f"✅ {content_type_label}を保存しました。\n📂 `{folder_name}/{filename}`")
            
            return {
                "title": title,
                "summary": summary_text,
                "type": content_type_label
            }

        except Exception as e:
            logging.error(f"WebClip: Save Error: {e}")
            await trigger_message_obj.add_reaction('❌')
            return None