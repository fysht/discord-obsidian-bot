"""投資サポート機能を提供するCog。

提供コマンド (Discord):
- !地合い                 : 日米市場の現在の環境を分析
- !銘柄スナップ <ticker>   : 指定銘柄の最新の財務指標スナップショットを取得
- !銘柄審査 <ticker>       : 投資憲法に照らして銘柄を審査
- !決算予定 <ticker>       : 次回決算発表日を調査してGoogle Calendarに登録
- !決算資料 <ticker>       : 決算関連資料のリンク一覧を取得して保存
- !CEO検証 <ticker> <url>  : YouTube動画のCEO発言と財務情報を照合
- !投資憲法               : 現在の投資憲法を表示
- !投資憲法初期化          : 投資憲法をサンプルで再生成（既存があれば上書きしない）

サービスメソッド (PWA APIから呼ばれる):
- run_market_sentiment() / run_stock_snapshot() / run_stock_audit()
- run_earnings_schedule() / run_earnings_documents() / run_ceo_crosscheck()
- run_get_constitution() / run_init_constitution() / run_update_constitution()
- list_history(category, limit)

データ保存先 (Googleドライブ):
- Investment/
  - Investment_Constitution.md
  - Stocks/{code}_{企業名}.md       … 銘柄個別ノート（既存StockCogと共通）
  - Snapshots/{date}_{ticker}.md    … 銘柄スナップショット履歴
  - Audits/{date}_{ticker}.md       … 銘柄審査結果履歴
  - Sentiment/{date}.md             … 地合い分析履歴
  - EarningsDocs/{date}_{ticker}.md … 決算関連資料一覧
  - CEOChecks/{date}_{ticker}.md    … CEO発言クロスチェック結果
"""
import os
import re
import json
import asyncio
import logging
import datetime

from discord.ext import commands
from google.genai import types

from config import JST
from prompts import (
    PROMPT_MARKET_SENTIMENT,
    PROMPT_STOCK_SNAPSHOT,
    PROMPT_STOCK_AUDIT,
    PROMPT_EARNINGS_SCHEDULE,
    PROMPT_EARNINGS_DOCUMENTS,
    PROMPT_CEO_CROSSCHECK,
)


INVESTMENT_FOLDER = "Investment"
STOCKS_FOLDER = "Stocks"
SNAPSHOTS_FOLDER = "Snapshots"
AUDITS_FOLDER = "Audits"
SENTIMENT_FOLDER = "Sentiment"
EARNINGS_DOCS_FOLDER = "EarningsDocs"
CEO_CHECKS_FOLDER = "CEOChecks"
CONSTITUTION_FILE = "Investment_Constitution.md"

GEMINI_MODEL = "gemini-2.5-pro"
GEMINI_FLASH_MODEL = "gemini-2.5-flash"

DISCORD_CHUNK_SIZE = 1900


# 投資憲法サンプル (初回起動時にDriveに作成される)
INVESTMENT_CONSTITUTION_SAMPLE = """---
title: 投資憲法
version: 1.0
last_updated: {date}
tags: [investment, constitution]
---

# 投資憲法（Investment Constitution）

> 自分の投資判断を貫くための個人の憲法。
> 銘柄審査時はこのドキュメントの基準に厳密に照らして合否を判定する。

## 🎯 投資の目的
- 経済的自由の獲得
- 長期複利による資産形成（年率 7% 以上を目標）
- 市場平均（インデックス）に対するアルファの追求

## 📐 投資哲学（Philosophy）
1. **長期視点**: 3〜10 年単位で企業価値の成長に投資する
2. **質>量**: 分散しすぎず、自分が深く理解できる10銘柄程度に集中
3. **逆張りより順張り**: 下落トレンドの銘柄に手を出さない
4. **わからないものには投資しない**: ビジネスモデルを3行で説明できなければ買わない

## ✅ 銘柄選定基準（Selection Criteria）

### 必須条件（すべて満たすこと）
- [ ] 時価総額: 1000億円以上（または 10B USD 以上）
- [ ] 直近3期連続で営業黒字
- [ ] 売上高成長率: 年平均 5% 以上
- [ ] 営業利益率: 8% 以上
- [ ] 自己資本比率: 40% 以上

### 評価指標（4項目以上で合格）
- [ ] PER: 業界平均以下、または成長率を考慮して妥当（PEG < 1.5）
- [ ] PBR: 3倍以下（成長株は柔軟に判断）
- [ ] ROE: 10% 以上
- [ ] フリーキャッシュフロー黒字
- [ ] 配当利回り: 2% 以上（インカム重視時のみ）
- [ ] ビジネスに堀（moat）がある（ブランド/ネットワーク効果/スイッチングコスト）

### 除外条件（1つでも該当したら買わない）
- [ ] 直近の決算で大幅な下方修正
- [ ] 監査法人からの継続疑義注記
- [ ] CEO の過去発言と財務実態に明らかな矛盾
- [ ] ガバナンス問題（不祥事・粉飾疑惑）
- [ ] 主要事業が法規制リスクに直撃される構造

## 💰 ポジション管理（Position Sizing）
- 1 銘柄あたりの最大保有額: ポートフォリオの 20%
- 同一セクター集中: 40% 以下
- 現金比率: 相場過熱時は 30% 以上を維持

## 🚪 売却ルール（Exit Rules）
1. **下落ストップ**: 取得価格から -20% で機械的に損切り
2. **割高判定**: PER が買い時の 2 倍以上に到達したら部分利確
3. **業績悪化**: 2 期連続で営業利益が前年割れしたら撤退
4. **投資仮説の崩壊**: 当初の成長ストーリーが破綻したら即売却

## 🧠 行動規律（Behavioral Discipline）
- 決算翌日の急騰急落で決断しない（24時間ルール）
- SNS / 株掲示板の意見では売買しない
- 1 日 1 回以上は株価チャートを開かない（チャートチェック病の回避）
- 自分の投資憲法に書かれていない理由で買わない

## 📝 改訂履歴
- 1.0 ({date}): 初版作成（自動生成サンプル）
"""


def _split_for_discord(text: str, chunk_size: int = DISCORD_CHUNK_SIZE):
    """Discord送信用にテキストを分割する。改行境界を尊重する。"""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > chunk_size:
        cut = remaining.rfind("\n", 0, chunk_size)
        if cut == -1:
            cut = chunk_size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _resolve_market(ticker: str):
    """ティッカーから市場を判別する。
    戻り値: (market: "JP" or "US", normalized_code: str)
    """
    t = (ticker or "").strip().upper()
    m = re.match(r"^(\d{4})(\.T)?$", t)
    if m:
        return "JP", m.group(1)
    return "US", t


def _safe_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", name).strip() or "untitled"


def _extract_youtube_id(url: str):
    """YouTube URLからvideo IDを抽出する。"""
    if not url:
        return None
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtube\.com/shorts/|youtu\.be/|youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/live/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    # 11文字IDが直接渡された場合
    if re.match(r"^[A-Za-z0-9_-]{11}$", url.strip()):
        return url.strip()
    return None


class InvestmentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_service = bot.drive_service
        self.calendar_service = bot.calendar_service
        self.gemini_client = bot.gemini_client
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    async def cog_load(self):
        asyncio.create_task(self._ensure_constitution_exists())

    # ==========================================================
    # Driveフォルダ・ファイル管理ヘルパー
    # ==========================================================

    async def _get_or_create_folder(self, service, parent_id: str, name: str):
        f_id = await self.drive_service.find_file(service, parent_id, name)
        if not f_id:
            f_id = await self.drive_service.create_folder(service, parent_id, name)
        return f_id

    async def _get_investment_folder(self, service):
        return await self._get_or_create_folder(
            service, self.drive_folder_id, INVESTMENT_FOLDER
        )

    async def _ensure_constitution_exists(self):
        """投資憲法ファイルがDriveに無ければサンプルを作成する。"""
        if not self.drive_service:
            return
        try:
            service = self.drive_service.get_service()
            if not service:
                return
            inv_folder_id = await self._get_investment_folder(service)
            existing = await self.drive_service.find_file(
                service, inv_folder_id, CONSTITUTION_FILE
            )
            if existing:
                return
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            content = INVESTMENT_CONSTITUTION_SAMPLE.format(date=today)
            await self.drive_service.upload_text(
                service, inv_folder_id, CONSTITUTION_FILE, content
            )
            logging.info(
                f"InvestmentCog: 投資憲法サンプルを作成しました ({CONSTITUTION_FILE})"
            )
        except Exception as e:
            logging.error(f"InvestmentCog: 投資憲法初期化エラー: {e}", exc_info=True)

    async def _read_constitution(self):
        if not self.drive_service:
            return None
        service = self.drive_service.get_service()
        if not service:
            return None
        inv_folder_id = await self._get_investment_folder(service)
        f_id = await self.drive_service.find_file(
            service, inv_folder_id, CONSTITUTION_FILE
        )
        if not f_id:
            return None
        return await self.drive_service.read_text_file(service, f_id)

    async def _save_dated_note(self, subfolder: str, filename: str, content: str):
        """Investment/{subfolder}/{filename} に保存。"""
        if not self.drive_service:
            return None
        service = self.drive_service.get_service()
        if not service:
            return None
        inv_id = await self._get_investment_folder(service)
        sub_id = await self._get_or_create_folder(service, inv_id, subfolder)
        existing = await self.drive_service.find_file(service, sub_id, filename)
        if existing:
            await self.drive_service.update_text(service, existing, content)
            return existing
        return await self.drive_service.upload_text(
            service, sub_id, filename, content
        )

    async def _list_subfolder_files(self, subfolder: str, limit: int = 30):
        """指定サブフォルダ内のMarkdownファイル一覧を取得する。"""
        if not self.drive_service:
            return []
        service = self.drive_service.get_service()
        if not service:
            return []
        inv_id = await self._get_investment_folder(service)
        sub_id = await self.drive_service.find_file(service, inv_id, subfolder)
        if not sub_id:
            return []
        query = (
            f"'{sub_id}' in parents and mimeType = 'text/markdown' and trashed = false"
        )
        try:
            results = await asyncio.to_thread(
                lambda: service.files()
                .list(
                    q=query,
                    fields="files(id, name, modifiedTime)",
                    orderBy="modifiedTime desc",
                    pageSize=limit,
                )
                .execute()
            )
            return results.get("files", [])
        except Exception as e:
            logging.error(f"InvestmentCog: list error ({subfolder}): {e}")
            return []

    async def _read_subfolder_file(self, subfolder: str, file_id: str):
        if not self.drive_service:
            return ""
        service = self.drive_service.get_service()
        if not service:
            return ""
        return await self.drive_service.read_text_file(service, file_id)

    # ==========================================================
    # Gemini呼び出しヘルパー
    # ==========================================================

    async def _gemini_with_search(self, prompt: str, model: str = GEMINI_MODEL) -> str:
        if not self.gemini_client:
            return ""
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            return (response.text or "").strip()
        except Exception as e:
            logging.error(f"InvestmentCog: Gemini(search) error: {e}", exc_info=True)
            return ""

    async def _gemini_plain(self, prompt: str, model: str = GEMINI_MODEL) -> str:
        if not self.gemini_client:
            return ""
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model=model,
                contents=prompt,
            )
            return (response.text or "").strip()
        except Exception as e:
            logging.error(f"InvestmentCog: Gemini(plain) error: {e}", exc_info=True)
            return ""

    async def _gemini_with_video(
        self, prompt: str, video_url: str, model: str = GEMINI_MODEL
    ) -> str:
        """Gemini multimodalにYouTube URLを直接渡して解析させる。"""
        if not self.gemini_client:
            return ""
        try:
            video_part = types.Part(
                file_data=types.FileData(file_uri=video_url, mime_type="video/*")
            )
            text_part = types.Part.from_text(text=prompt)
            content = types.Content(role="user", parts=[video_part, text_part])
            response = await self.gemini_client.aio.models.generate_content(
                model=model,
                contents=[content],
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            return (response.text or "").strip()
        except Exception as e:
            logging.error(f"InvestmentCog: Gemini(video) error: {e}", exc_info=True)
            return ""

    @staticmethod
    def _extract_json(text: str):
        if not text:
            return None
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    # ==========================================================
    # サービスメソッド (PWA APIから呼ばれる)
    # ==========================================================

    async def run_market_sentiment(self) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        today = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        prompt = PROMPT_MARKET_SENTIMENT.format(date=today)
        result = await self._gemini_with_search(prompt)
        if not result:
            return {"ok": False, "error": "地合い分析の取得に失敗"}

        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filename = f"{date_str}.md"
        body = (
            f"---\ndate: {date_str}\ntags: [market, sentiment]\n---\n\n{result}"
        )
        try:
            await self._save_dated_note(SENTIMENT_FOLDER, filename, body)
        except Exception as e:
            logging.error(f"地合いノート保存エラー: {e}")
        return {"ok": True, "report": result, "saved_as": filename}

    async def run_stock_snapshot(self, ticker: str) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        market, code = _resolve_market(ticker)
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        prompt = PROMPT_STOCK_SNAPSHOT.format(
            ticker=code, market=market, date=now_str
        )
        result = await self._gemini_with_search(prompt)
        if not result:
            return {"ok": False, "error": "スナップショットの取得に失敗"}
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filename = f"{date_str}_{_safe_filename(code)}.md"
        body = (
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"tags: [investment, snapshot]\n---\n\n{result}"
        )
        try:
            await self._save_dated_note(SNAPSHOTS_FOLDER, filename, body)
        except Exception as e:
            logging.error(f"スナップショット保存エラー: {e}")
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "report": result,
            "saved_as": filename,
        }

    async def run_stock_audit(self, ticker: str) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        constitution = await self._read_constitution()
        if not constitution:
            return {
                "ok": False,
                "error": "投資憲法が見つかりません。先に投資憲法を初期化してください。",
            }
        market, code = _resolve_market(ticker)
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")

        snap_prompt = PROMPT_STOCK_SNAPSHOT.format(
            ticker=code, market=market, date=now_str
        )
        snapshot = await self._gemini_with_search(snap_prompt)
        if not snapshot:
            return {"ok": False, "error": "銘柄データの取得に失敗"}

        audit_prompt = PROMPT_STOCK_AUDIT.format(
            constitution=constitution,
            snapshot=snapshot,
            ticker=code,
        )
        audit = await self._gemini_plain(audit_prompt)
        if not audit:
            return {"ok": False, "error": "審査結果の生成に失敗"}

        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filename = f"{date_str}_{_safe_filename(code)}.md"
        body = (
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"tags: [investment, audit]\n---\n\n{audit}\n\n---\n\n"
            f"## 📊 使用したスナップショット\n\n{snapshot}"
        )
        try:
            await self._save_dated_note(AUDITS_FOLDER, filename, body)
        except Exception as e:
            logging.error(f"審査結果保存エラー: {e}")
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "audit": audit,
            "snapshot": snapshot,
            "saved_as": filename,
        }

    async def run_earnings_schedule(self, ticker: str, register_calendar: bool = True) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        market, code = _resolve_market(ticker)
        prompt = PROMPT_EARNINGS_SCHEDULE.format(ticker=code, market=market)
        raw = await self._gemini_with_search(prompt)
        if not raw:
            return {"ok": False, "error": "決算情報の取得に失敗"}
        data = self._extract_json(raw)
        if not data:
            return {
                "ok": False,
                "error": "決算情報のパースに失敗",
                "raw": raw[:1500],
            }
        company = data.get("company_name") or code
        earnings_date = data.get("next_earnings_date")
        if not earnings_date:
            return {
                "ok": False,
                "error": f"{company} ({code}) の次回決算日が確認できませんでした",
                "data": data,
            }
        registered = []
        if register_calendar and self.calendar_service:
            summary = f"📊 {company} ({code}) 決算発表"
            description_lines = [
                f"会計期間: {data.get('fiscal_period', '不明')}",
                f"発表時間帯: {data.get('earnings_time', '不明')}",
                f"信頼度: {data.get('confidence', 'N/A')}",
                f"出典: {data.get('source', 'N/A')}",
            ]
            related = data.get("related_events") or []
            if related:
                description_lines.append("\n関連イベント:")
                for ev in related:
                    description_lines.append(
                        f"- {ev.get('title', '?')} : {ev.get('date', '?')}"
                    )
            description = "\n".join(description_lines)
            try:
                cal_result = await self.calendar_service.create_event(
                    summary=summary,
                    start_time=earnings_date,
                    end_time=earnings_date,
                    description=description,
                )
                registered.append(
                    {"summary": summary, "date": earnings_date, "result": cal_result}
                )
            except Exception as e:
                registered.append({"error": str(e), "summary": summary})
            for ev in related:
                ev_date = ev.get("date")
                ev_title = ev.get("title")
                if not (ev_date and ev_title):
                    continue
                try:
                    r = await self.calendar_service.create_event(
                        summary=f"📅 {company} {ev_title}",
                        start_time=ev_date,
                        end_time=ev_date,
                        description=f"出典: {data.get('source', 'N/A')}",
                    )
                    registered.append(
                        {"summary": ev_title, "date": ev_date, "result": r}
                    )
                except Exception as e:
                    registered.append({"error": str(e), "summary": ev_title})
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "data": data,
            "registered": registered,
        }

    async def run_earnings_documents(self, ticker: str) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        market, code = _resolve_market(ticker)
        prompt = PROMPT_EARNINGS_DOCUMENTS.format(ticker=code, market=market)
        raw = await self._gemini_with_search(prompt)
        if not raw:
            return {"ok": False, "error": "決算資料の取得に失敗"}
        data = self._extract_json(raw)
        if not data:
            return {
                "ok": False,
                "error": "決算資料情報のパースに失敗",
                "raw": raw[:1500],
            }
        company = data.get("company_name") or code
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filename = f"{date_str}_{_safe_filename(code)}.md"

        # Markdown形式に整形
        lines = [
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"tags: [investment, earnings_docs]\n---\n",
            f"# 📑 {company} ({code}) 決算関連資料",
            "",
            f"- 公式IR: {data.get('ir_page_url', 'N/A')}",
            f"- 情報源品質: {data.get('source_quality', 'N/A')}",
            f"- 取得日: {date_str}",
            "",
            "## 📋 資料一覧",
            "",
        ]
        documents = data.get("documents") or []
        if not documents:
            lines.append("（取得できる資料がありませんでした）")
        else:
            for doc in documents:
                title = doc.get("title", "(無題)")
                doc_type = doc.get("type", "")
                period = doc.get("fiscal_period", "")
                pub = doc.get("published_date", "")
                url = doc.get("url", "")
                lang = doc.get("language", "")
                lines.append(f"### {title}")
                lines.append(
                    f"- 種別: {doc_type} / 会計期間: {period} / 公表日: {pub} / 言語: {lang}"
                )
                lines.append(f"- URL: {url}")
                lines.append("")
        notes = data.get("notes")
        if notes:
            lines.append("## 📝 補足")
            lines.append(notes)
        body = "\n".join(lines)

        try:
            await self._save_dated_note(EARNINGS_DOCS_FOLDER, filename, body)
        except Exception as e:
            logging.error(f"決算資料保存エラー: {e}")
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "data": data,
            "saved_as": filename,
            "report": body,
        }

    async def run_ceo_crosscheck(
        self, ticker: str, video_url: str, video_title: str = ""
    ) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        if not video_url:
            return {"ok": False, "error": "動画URLが指定されていません"}
        if not _extract_youtube_id(video_url):
            return {
                "ok": False,
                "error": "YouTubeのURLとして認識できませんでした",
            }
        market, code = _resolve_market(ticker)

        # 1. 銘柄スナップショットを下調べとして取得
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        snap_prompt = PROMPT_STOCK_SNAPSHOT.format(
            ticker=code, market=market, date=now_str
        )
        snapshot = await self._gemini_with_search(snap_prompt)

        # 2. 動画解析プロンプトを構築
        background = (
            f"\n\n【参考: 直近の財務スナップショット】\n{snapshot}\n"
            if snapshot
            else ""
        )
        cross_prompt = PROMPT_CEO_CROSSCHECK.format(
            ticker=code,
            video_title=video_title or video_url,
        ) + background + (
            "\n\n【動画URL】\n" + video_url +
            "\n\n動画内のCEO発言を以下の観点で抽出・検証してください:\n"
            "1. 発言された具体的な数値（売上・利益・成長率・新工場/新店舗など）\n"
            "2. 発言と直近財務データとの整合性\n"
            "3. 計画スケジュール（新工場建設等）について Google 検索で衛星写真や"
            "関連報道を裏取りし、進捗が現実と一致しているかを判定\n"
            "4. CEOの非言語シグナル（自信のなさ・視線そらし・口ごもり）\n"
            "5. スライド/字幕の隅に書かれた小さな注釈や但し書き\n"
            "数値を引用するときは必ず時刻（mm:ss）も併記してください。"
        )

        analysis = await self._gemini_with_video(cross_prompt, video_url)
        if not analysis:
            return {"ok": False, "error": "動画解析に失敗しました"}

        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filename = f"{date_str}_{_safe_filename(code)}.md"
        body = (
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"video_url: {video_url}\ntags: [investment, ceo_check]\n---\n\n"
            f"{analysis}\n\n---\n\n"
            f"## 📊 使用したスナップショット\n\n{snapshot or '(取得失敗)'}"
        )
        try:
            await self._save_dated_note(CEO_CHECKS_FOLDER, filename, body)
        except Exception as e:
            logging.error(f"CEO検証保存エラー: {e}")
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "video_url": video_url,
            "analysis": analysis,
            "snapshot": snapshot,
            "saved_as": filename,
        }

    async def run_get_constitution(self) -> dict:
        constitution = await self._read_constitution()
        if not constitution:
            return {"ok": False, "error": "投資憲法が存在しません"}
        return {"ok": True, "content": constitution}

    async def run_init_constitution(self, force: bool = False) -> dict:
        if not self.drive_service:
            return {"ok": False, "error": "Driveサービス未設定"}
        existing = await self._read_constitution()
        if existing and not force:
            return {"ok": False, "error": "既存の投資憲法があるため上書きしません"}
        await self._ensure_constitution_exists()
        if force and existing is not None:
            # 強制上書き
            service = self.drive_service.get_service()
            inv_id = await self._get_investment_folder(service)
            f_id = await self.drive_service.find_file(
                service, inv_id, CONSTITUTION_FILE
            )
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            content = INVESTMENT_CONSTITUTION_SAMPLE.format(date=today)
            if f_id:
                await self.drive_service.update_text(service, f_id, content)
        result = await self._read_constitution()
        return {"ok": True, "content": result or ""}

    async def run_update_constitution(self, content: str) -> dict:
        if not self.drive_service:
            return {"ok": False, "error": "Driveサービス未設定"}
        if not content or not content.strip():
            return {"ok": False, "error": "内容が空です"}
        service = self.drive_service.get_service()
        if not service:
            return {"ok": False, "error": "Drive接続失敗"}
        inv_id = await self._get_investment_folder(service)
        f_id = await self.drive_service.find_file(service, inv_id, CONSTITUTION_FILE)
        if f_id:
            await self.drive_service.update_text(service, f_id, content)
        else:
            await self.drive_service.upload_text(
                service, inv_id, CONSTITUTION_FILE, content
            )
        return {"ok": True, "content": content}

    async def list_history(self, category: str, limit: int = 20) -> dict:
        """履歴一覧を取得する。category: snapshot/audit/sentiment/earnings_docs/ceo_check"""
        folder_map = {
            "snapshot": SNAPSHOTS_FOLDER,
            "audit": AUDITS_FOLDER,
            "sentiment": SENTIMENT_FOLDER,
            "earnings_docs": EARNINGS_DOCS_FOLDER,
            "ceo_check": CEO_CHECKS_FOLDER,
        }
        sub = folder_map.get(category)
        if not sub:
            return {"ok": False, "error": f"未知のカテゴリ: {category}"}
        files = await self._list_subfolder_files(sub, limit=limit)
        return {
            "ok": True,
            "category": category,
            "items": [
                {
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "modifiedTime": f.get("modifiedTime"),
                }
                for f in files
            ],
        }

    async def read_history_item(self, category: str, file_id: str) -> dict:
        folder_map = {
            "snapshot": SNAPSHOTS_FOLDER,
            "audit": AUDITS_FOLDER,
            "sentiment": SENTIMENT_FOLDER,
            "earnings_docs": EARNINGS_DOCS_FOLDER,
            "ceo_check": CEO_CHECKS_FOLDER,
        }
        if category not in folder_map:
            return {"ok": False, "error": f"未知のカテゴリ: {category}"}
        content = await self._read_subfolder_file(folder_map[category], file_id)
        if not content:
            return {"ok": False, "error": "ファイルが空または取得失敗"}
        return {"ok": True, "content": content}

    # ==========================================================
    # Discordユーティリティ
    # ==========================================================

    async def _send_long(self, ctx: commands.Context, text: str):
        for chunk in _split_for_discord(text):
            await ctx.send(chunk)

    # ==========================================================
    # Discordコマンド (薄いラッパー、サービスメソッドを呼ぶ)
    # ==========================================================

    @commands.command(name="地合い", aliases=["sentiment", "market"])
    async def market_sentiment(self, ctx: commands.Context):
        async with ctx.typing():
            res = await self.run_market_sentiment()
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        await self._send_long(ctx, res["report"])

    @commands.command(name="銘柄スナップ", aliases=["snapshot", "snap"])
    async def stock_snapshot(self, ctx: commands.Context, ticker: str = None):
        if not ticker:
            await ctx.send(
                "使い方: `!銘柄スナップ <ティッカー>` 例: `!銘柄スナップ 7203`"
            )
            return
        async with ctx.typing():
            res = await self.run_stock_snapshot(ticker)
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        await self._send_long(ctx, res["report"])

    @commands.command(name="銘柄審査", aliases=["audit"])
    async def stock_audit(self, ctx: commands.Context, ticker: str = None):
        if not ticker:
            await ctx.send("使い方: `!銘柄審査 <ティッカー>`")
            return
        async with ctx.typing():
            res = await self.run_stock_audit(ticker)
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        await self._send_long(ctx, res["audit"])

    @commands.command(name="決算予定", aliases=["earnings"])
    async def earnings_schedule(self, ctx: commands.Context, ticker: str = None):
        if not ticker:
            await ctx.send("使い方: `!決算予定 <ティッカー>`")
            return
        if not self.calendar_service:
            await ctx.send("Google Calendarが未設定のため使えません。")
            return
        async with ctx.typing():
            res = await self.run_earnings_schedule(ticker)
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        data = res.get("data") or {}
        registered = res.get("registered") or []
        msg_lines = [
            f"**{data.get('company_name', res['ticker'])} ({res['ticker']})**",
            f"次回決算: {data.get('next_earnings_date', '?')} ({data.get('earnings_time', '?')})",
        ]
        for r in registered:
            if "error" in r:
                msg_lines.append(f"❌ {r.get('summary')}: {r['error']}")
            else:
                msg_lines.append(f"✅ {r.get('summary')}: {r.get('result')}")
        await self._send_long(ctx, "\n".join(msg_lines))

    @commands.command(name="決算資料", aliases=["earnings_docs", "docs"])
    async def earnings_documents(self, ctx: commands.Context, ticker: str = None):
        if not ticker:
            await ctx.send("使い方: `!決算資料 <ティッカー>`")
            return
        async with ctx.typing():
            res = await self.run_earnings_documents(ticker)
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        await self._send_long(ctx, res["report"])

    @commands.command(name="CEO検証", aliases=["ceo_check", "ceo"])
    async def ceo_crosscheck(
        self,
        ctx: commands.Context,
        ticker: str = None,
        video_url: str = None,
        *,
        video_title: str = "",
    ):
        if not ticker or not video_url:
            await ctx.send(
                "使い方: `!CEO検証 <ティッカー> <YouTube URL> [動画タイトル]`"
            )
            return
        async with ctx.typing():
            res = await self.run_ceo_crosscheck(
                ticker, video_url, video_title=video_title
            )
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        await self._send_long(ctx, res["analysis"])

    @commands.command(name="投資憲法", aliases=["constitution"])
    async def show_constitution(self, ctx: commands.Context):
        res = await self.run_get_constitution()
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        await self._send_long(ctx, res["content"])

    @commands.command(name="投資憲法初期化", aliases=["init_constitution"])
    async def init_constitution(self, ctx: commands.Context):
        res = await self.run_init_constitution(force=False)
        if not res.get("ok"):
            await ctx.send(f"❌ {res.get('error')}")
            return
        await ctx.send(
            f"投資憲法サンプルを作成しました: `Investment/{CONSTITUTION_FILE}`"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(InvestmentCog(bot))
