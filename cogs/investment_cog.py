"""投資サポート機能を提供するCog（PWA専用）。

PWAから REST 経由でのみ呼び出されるサービス層。Discordコマンドは持たない。

サービスメソッド:
- run_market_sentiment() / run_stock_snapshot() / run_stock_audit()
- run_earnings_schedule() / run_earnings_documents() / run_ceo_crosscheck()
- run_peer_comparison() / run_news_sentiment() / run_dividend_schedule()
- run_constitution_review() / run_risk_assessment()
- run_get_constitution() / run_init_constitution() / run_update_constitution()
- portfolio_list() / portfolio_add() / portfolio_remove() / portfolio_transactions()
- journal_list() / journal_add() / journal_analyze_pattern()
- alerts_list() / alerts_add() / alerts_remove() / alerts_toggle() / alerts_check_now()
- list_history(category, limit) / read_history_item(category, file_id)

データ保存先 (Googleドライブ):
- Investment/
  - Investment_Constitution.md
  - Snapshots/{date}_{ticker}.md
  - Audits/{date}_{ticker}.md
  - Sentiment/{date}.md
  - EarningsDocs/{date}_{ticker}.md
  - CEOChecks/{date}_{ticker}.md
  - Comparisons/{date}_{ticker}.md
  - NewsSentiment/{date}_{ticker}.md
  - Dividends/{date}_{ticker}.md
  - RiskReports/{date}.md
  - ConstitutionReviews/{date}.md
  - Portfolio/holdings.json
  - Portfolio/transactions.jsonl
  - Journal/journal_index.json + 個別エントリ
  - Alerts/rules.json + alert_log.jsonl
"""
import os
import re
import json
import asyncio
import logging
import datetime
import random

from discord.ext import commands, tasks
from google.genai import types

from config import JST
from prompts import (
    PROMPT_MARKET_SENTIMENT,
    PROMPT_STOCK_SNAPSHOT,
    PROMPT_STOCK_AUDIT,
    PROMPT_EARNINGS_SCHEDULE,
    PROMPT_EARNINGS_DOCUMENTS,
    PROMPT_CEO_CROSSCHECK,
    PROMPT_PEER_COMPARISON,
    PROMPT_NEWS_SENTIMENT,
    PROMPT_JOURNAL_PATTERN,
    PROMPT_CONSTITUTION_REVIEW,
    PROMPT_DIVIDEND_SCHEDULE,
    PROMPT_RISK_ASSESSMENT,
)


INVESTMENT_FOLDER = "Investment"
STOCKS_FOLDER = "Stocks"
SNAPSHOTS_FOLDER = "Snapshots"
AUDITS_FOLDER = "Audits"
SENTIMENT_FOLDER = "Sentiment"
EARNINGS_DOCS_FOLDER = "EarningsDocs"
CEO_CHECKS_FOLDER = "CEOChecks"
COMPARISONS_FOLDER = "Comparisons"
NEWS_SENTIMENT_FOLDER = "NewsSentiment"
DIVIDENDS_FOLDER = "Dividends"
RISK_REPORTS_FOLDER = "RiskReports"
CONSTITUTION_REVIEWS_FOLDER = "ConstitutionReviews"
PORTFOLIO_FOLDER = "Portfolio"
JOURNAL_FOLDER = "Journal"
ALERTS_FOLDER = "Alerts"

CONSTITUTION_FILE = "Investment_Constitution.md"
HOLDINGS_FILE = "holdings.json"
TRANSACTIONS_FILE = "transactions.jsonl"
JOURNAL_INDEX_FILE = "journal_index.json"
ALERT_RULES_FILE = "rules.json"
ALERT_LOG_FILE = "alert_log.jsonl"

GEMINI_MODEL = "gemini-2.5-pro"
GEMINI_FLASH_MODEL = "gemini-2.5-flash"


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
        # 自動実行ループ群（環境変数 INVESTMENT_AUTO_DISABLE=1 で全停止）
        if os.getenv("INVESTMENT_AUTO_DISABLE", "0") != "1":
            for task in (
                self.auto_market_sentiment_task,
                self.auto_alerts_and_earnings_task,
                self.auto_news_sentiment_task,
            ):
                try:
                    task.start()
                except RuntimeError:
                    pass

    def cog_unload(self):
        for task in (
            self.auto_market_sentiment_task,
            self.auto_alerts_and_earnings_task,
            self.auto_news_sentiment_task,
        ):
            try:
                task.cancel()
            except Exception:
                pass

    # ==========================================================
    # 自動実行ヘルパー
    # ==========================================================

    async def _notify_routine(self, body: str):
        """投資レポートをユーザーへ通知する。PartnerCog経由でDiscord/PWAに届ける。"""
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog or not body:
            return
        instruction = (
            "次の投資レポートをユーザーへ優しいタメ口で届けてください。"
            "前置きや改変はせず、本文をそのまま送ってください。\n\n" + body
        )
        try:
            await partner_cog.generate_and_send_routine_message("", instruction)
        except Exception as e:
            logging.error(f"InvestmentCog notify error: {e}")

    @tasks.loop(time=datetime.time(hour=6, minute=30, tzinfo=JST))
    async def auto_market_sentiment_task(self):
        """毎朝 06:30 (JST) に米国市場クローズ後の地合いを取得して通知する。"""
        await asyncio.sleep(random.randint(0, 180))
        try:
            result = await self.run_market_sentiment()
        except Exception:
            logging.exception("auto_market_sentiment_task failed")
            return
        if not result.get("ok"):
            return
        report = (result.get("report") or "").strip()
        if not report:
            return
        header = "🌅 今朝の市場の地合い"
        await self._notify_routine(f"{header}\n\n{report}")

    @auto_market_sentiment_task.before_loop
    async def _before_auto_market_sentiment(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=7, minute=0, tzinfo=JST))
    async def auto_alerts_and_earnings_task(self):
        """毎朝 07:00 (JST) に価格アラートをチェックし、保有銘柄の当日決算予定も通知する。"""
        await asyncio.sleep(random.randint(0, 180))
        lines = []

        # 1) 価格アラート
        try:
            alerts_result = await self.alerts_check_now()
            hits = alerts_result.get("hits") or []
            if hits:
                lines.append("🔔 価格アラート")
                for h in hits:
                    msg = h.get("message") or "(詳細不明)"
                    lines.append(f"- {h.get('ticker', '?')}: {msg}")
        except Exception:
            logging.exception("auto alerts_check failed")

        # 2) 保有銘柄の当日決算
        try:
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            holdings = await self._read_json_file(
                PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
            )
            today_earnings = []
            for h in holdings:
                code = h.get("code")
                if not code:
                    continue
                try:
                    res = await self.run_earnings_schedule(code, register_calendar=False)
                except Exception:
                    logging.exception(f"earnings_schedule failed for {code}")
                    continue
                if not res.get("ok"):
                    continue
                ed = (res.get("data") or {}).get("next_earnings_date") or res.get("earnings_date")
                if ed and ed.startswith(today):
                    company = (res.get("data") or {}).get("company_name") or code
                    today_earnings.append(f"- {company} ({code})")
                await asyncio.sleep(2)
            if today_earnings:
                lines.append("📊 本日の決算予定（保有銘柄）")
                lines.extend(today_earnings)
        except Exception:
            logging.exception("auto today earnings failed")

        if lines:
            await self._notify_routine("\n".join(lines))

    @auto_alerts_and_earnings_task.before_loop
    async def _before_auto_alerts_and_earnings(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=8, minute=30, tzinfo=JST))
    async def auto_news_sentiment_task(self):
        """毎朝 08:30 (JST) に保有銘柄のニュースセンチメントを順次取得して通知する。"""
        await asyncio.sleep(random.randint(0, 180))
        try:
            holdings = await self._read_json_file(
                PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
            )
        except Exception:
            logging.exception("auto news_sentiment: holdings read failed")
            return
        if not holdings:
            return

        sections = []
        for h in holdings:
            code = h.get("code")
            if not code:
                continue
            try:
                res = await self.run_news_sentiment(code)
            except Exception:
                logging.exception(f"run_news_sentiment failed for {code}")
                continue
            if not res.get("ok"):
                continue
            report = (res.get("report") or "").strip()
            if not report:
                continue
            head = (res.get("name") or h.get("name") or code)
            snippet = report.splitlines()
            short = "\n".join(snippet[:8])  # 長すぎ防止に先頭8行
            sections.append(f"📰 {head} ({code})\n{short}")
            await asyncio.sleep(2)

        if sections:
            body = "📰 保有銘柄ニュースセンチメント（朝刊）\n\n" + "\n\n".join(sections)
            await self._notify_routine(body)

    @auto_news_sentiment_task.before_loop
    async def _before_auto_news_sentiment(self):
        await self.bot.wait_until_ready()

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
            # URL末尾から file_format を確実に判定（プロンプトの返却が不正な場合のフォールバック）
            def _format_of(url: str, declared: str) -> str:
                if declared in ("pdf", "html"):
                    return declared
                return "pdf" if isinstance(url, str) and url.lower().split("?")[0].endswith(".pdf") else "html"

            # PDF を先頭に並び替え
            def _doc_sort_key(d):
                fmt = _format_of(d.get("url", ""), d.get("file_format", ""))
                # PDF を 0、HTML を 1 にして PDF を先頭に
                return (0 if fmt == "pdf" else 1, d.get("published_date", ""))

            documents = sorted(documents, key=_doc_sort_key)
            for doc in documents:
                title = doc.get("title", "(無題)")
                doc_type = doc.get("type", "")
                period = doc.get("fiscal_period", "")
                pub = doc.get("published_date", "")
                url = doc.get("url", "")
                lang = doc.get("language", "")
                fmt = _format_of(url, doc.get("file_format", ""))
                badge = "📄 PDF" if fmt == "pdf" else "🔗 HTML"
                lines.append(f"### {badge} {title}")
                lines.append(
                    f"- 種別: {doc_type} / 形式: {fmt} / 会計期間: {period} / 公表日: {pub} / 言語: {lang}"
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

    HISTORY_FOLDER_MAP = {
        "snapshot": SNAPSHOTS_FOLDER,
        "audit": AUDITS_FOLDER,
        "sentiment": SENTIMENT_FOLDER,
        "earnings_docs": EARNINGS_DOCS_FOLDER,
        "ceo_check": CEO_CHECKS_FOLDER,
        "comparison": COMPARISONS_FOLDER,
        "news_sentiment": NEWS_SENTIMENT_FOLDER,
        "dividend": DIVIDENDS_FOLDER,
        "risk_report": RISK_REPORTS_FOLDER,
        "constitution_review": CONSTITUTION_REVIEWS_FOLDER,
        "journal": JOURNAL_FOLDER,
    }

    async def list_history(self, category: str, limit: int = 20) -> dict:
        """履歴一覧を取得する。"""
        sub = self.HISTORY_FOLDER_MAP.get(category)
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
        if category not in self.HISTORY_FOLDER_MAP:
            return {"ok": False, "error": f"未知のカテゴリ: {category}"}
        content = await self._read_subfolder_file(
            self.HISTORY_FOLDER_MAP[category], file_id
        )
        if not content:
            return {"ok": False, "error": "ファイルが空または取得失敗"}
        return {"ok": True, "content": content}

    # ==========================================================
    # JSONストレージヘルパー (ポートフォリオ・日記・アラート用)
    # ==========================================================

    async def _read_json_file(self, subfolder: str, filename: str, default):
        """Investment/{subfolder}/{filename} をJSONとして読む。無ければdefault。"""
        if not self.drive_service:
            return default
        service = self.drive_service.get_service()
        if not service:
            return default
        inv_id = await self._get_investment_folder(service)
        sub_id = await self._get_or_create_folder(service, inv_id, subfolder)
        f_id = await self.drive_service.find_file(service, sub_id, filename)
        if not f_id:
            return default
        try:
            text = await self.drive_service.read_text_file(service, f_id)
            return json.loads(text) if text else default
        except Exception as e:
            logging.error(f"InvestmentCog: JSON read error ({filename}): {e}")
            return default

    async def _write_json_file(self, subfolder: str, filename: str, data):
        """Investment/{subfolder}/{filename} にJSONを書き込む。"""
        if not self.drive_service:
            return False
        service = self.drive_service.get_service()
        if not service:
            return False
        inv_id = await self._get_investment_folder(service)
        sub_id = await self._get_or_create_folder(service, inv_id, subfolder)
        body = json.dumps(data, ensure_ascii=False, indent=2)
        existing = await self.drive_service.find_file(service, sub_id, filename)
        if existing:
            await self.drive_service.update_text(
                service, existing, body, mime_type="application/json"
            )
        else:
            await self.drive_service.upload_text(
                service, sub_id, filename, body, mime_type="application/json"
            )
        return True

    async def _append_jsonl(self, subfolder: str, filename: str, entry: dict):
        """append-only JSONL ファイルに1行追加する。"""
        if not self.drive_service:
            return False
        service = self.drive_service.get_service()
        if not service:
            return False
        inv_id = await self._get_investment_folder(service)
        sub_id = await self._get_or_create_folder(service, inv_id, subfolder)
        existing_id = await self.drive_service.find_file(service, sub_id, filename)
        prev = ""
        if existing_id:
            try:
                prev = await self.drive_service.read_text_file(service, existing_id)
            except Exception:
                prev = ""
        new_line = json.dumps(entry, ensure_ascii=False)
        new_text = (prev or "") + new_line + "\n"
        if existing_id:
            await self.drive_service.update_text(
                service, existing_id, new_text, mime_type="application/x-ndjson"
            )
        else:
            await self.drive_service.upload_text(
                service, sub_id, filename, new_text, mime_type="application/x-ndjson"
            )
        return True

    async def _read_jsonl(self, subfolder: str, filename: str):
        """JSONLファイルの全行をパースしてリストで返す。"""
        if not self.drive_service:
            return []
        service = self.drive_service.get_service()
        if not service:
            return []
        inv_id = await self._get_investment_folder(service)
        sub_id = await self.drive_service.find_file(service, inv_id, subfolder)
        if not sub_id:
            return []
        f_id = await self.drive_service.find_file(service, sub_id, filename)
        if not f_id:
            return []
        try:
            text = await self.drive_service.read_text_file(service, f_id)
        except Exception:
            return []
        out = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ==========================================================
    # ポートフォリオ管理
    # ==========================================================

    async def portfolio_list(self) -> dict:
        holdings = await self._read_json_file(
            PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
        )
        return {"ok": True, "holdings": holdings}

    async def portfolio_add(self, holding: dict) -> dict:
        ticker = (holding.get("ticker") or "").strip()
        if not ticker:
            return {"ok": False, "error": "tickerが必要です"}
        market, code = _resolve_market(ticker)
        try:
            shares = float(holding.get("shares", 0))
            avg_cost = float(holding.get("avg_cost", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "shares/avg_cost は数値を指定してください"}
        if shares <= 0 or avg_cost <= 0:
            return {"ok": False, "error": "shares と avg_cost は正の値が必要です"}

        holdings = await self._read_json_file(
            PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
        )
        # 既存があれば加重平均で更新、なければ追加
        idx = next(
            (i for i, h in enumerate(holdings) if h.get("code") == code),
            None,
        )
        now_iso = datetime.datetime.now(JST).isoformat()
        if idx is not None:
            existing = holdings[idx]
            old_shares = float(existing.get("shares", 0))
            old_cost = float(existing.get("avg_cost", 0))
            total_shares = old_shares + shares
            new_avg = (
                (old_shares * old_cost + shares * avg_cost) / total_shares
                if total_shares > 0
                else avg_cost
            )
            existing["shares"] = total_shares
            existing["avg_cost"] = round(new_avg, 4)
            existing["updated_at"] = now_iso
            if holding.get("name"):
                existing["name"] = holding["name"]
            if holding.get("sector"):
                existing["sector"] = holding["sector"]
            if holding.get("notes"):
                existing["notes"] = holding["notes"]
        else:
            holdings.append(
                {
                    "code": code,
                    "ticker": ticker,
                    "market": market,
                    "name": holding.get("name") or code,
                    "sector": holding.get("sector") or "",
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "currency": holding.get("currency") or ("JPY" if market == "JP" else "USD"),
                    "opened_at": holding.get("opened_at") or now_iso,
                    "updated_at": now_iso,
                    "notes": holding.get("notes") or "",
                }
            )
        await self._write_json_file(PORTFOLIO_FOLDER, HOLDINGS_FILE, holdings)
        # 取引履歴にも追記
        await self._append_jsonl(
            PORTFOLIO_FOLDER,
            TRANSACTIONS_FILE,
            {
                "ts": now_iso,
                "action": "buy",
                "code": code,
                "shares": shares,
                "price": avg_cost,
                "notes": holding.get("notes") or "",
            },
        )
        return {"ok": True, "holdings": holdings}

    async def portfolio_remove(self, code: str, shares: float = None) -> dict:
        """sharesがNoneなら全数売却。指定なら部分売却。"""
        holdings = await self._read_json_file(
            PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
        )
        idx = next(
            (i for i, h in enumerate(holdings) if h.get("code") == code),
            None,
        )
        if idx is None:
            return {"ok": False, "error": f"{code} は保有していません"}

        existing = holdings[idx]
        now_iso = datetime.datetime.now(JST).isoformat()
        sold_shares = float(existing.get("shares", 0)) if shares is None else float(shares)
        if sold_shares <= 0:
            return {"ok": False, "error": "売却数量が不正"}
        if sold_shares >= float(existing.get("shares", 0)):
            holdings.pop(idx)
        else:
            existing["shares"] = float(existing["shares"]) - sold_shares
            existing["updated_at"] = now_iso
        await self._write_json_file(PORTFOLIO_FOLDER, HOLDINGS_FILE, holdings)
        await self._append_jsonl(
            PORTFOLIO_FOLDER,
            TRANSACTIONS_FILE,
            {
                "ts": now_iso,
                "action": "sell",
                "code": code,
                "shares": sold_shares,
                "price": existing.get("avg_cost"),
            },
        )
        return {"ok": True, "holdings": holdings}

    async def portfolio_update(self, code: str, **fields) -> dict:
        """既存保有銘柄を直接編集する（株数・平均取得単価などを上書き）。"""
        holdings = await self._read_json_file(
            PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
        )
        idx = next(
            (i for i, h in enumerate(holdings) if h.get("code") == code),
            None,
        )
        if idx is None:
            return {"ok": False, "error": f"{code} は保有していません"}

        existing = holdings[idx]
        now_iso = datetime.datetime.now(JST).isoformat()

        if "shares" in fields and fields["shares"] is not None:
            try:
                new_shares = float(fields["shares"])
            except (TypeError, ValueError):
                return {"ok": False, "error": "shares は数値を指定してください"}
            if new_shares <= 0:
                return {"ok": False, "error": "shares は正の値が必要です"}
            existing["shares"] = new_shares

        if "avg_cost" in fields and fields["avg_cost"] is not None:
            try:
                new_cost = float(fields["avg_cost"])
            except (TypeError, ValueError):
                return {"ok": False, "error": "avg_cost は数値を指定してください"}
            if new_cost <= 0:
                return {"ok": False, "error": "avg_cost は正の値が必要です"}
            existing["avg_cost"] = round(new_cost, 4)

        for key in ("name", "sector", "currency", "notes"):
            if key in fields and fields[key] is not None:
                existing[key] = fields[key]

        existing["updated_at"] = now_iso
        await self._write_json_file(PORTFOLIO_FOLDER, HOLDINGS_FILE, holdings)
        await self._append_jsonl(
            PORTFOLIO_FOLDER,
            TRANSACTIONS_FILE,
            {
                "ts": now_iso,
                "action": "update",
                "code": code,
                "shares": existing.get("shares"),
                "price": existing.get("avg_cost"),
                "notes": fields.get("notes") or "",
            },
        )
        return {"ok": True, "holdings": holdings}

    async def portfolio_transactions(self, limit: int = 100) -> dict:
        items = await self._read_jsonl(PORTFOLIO_FOLDER, TRANSACTIONS_FILE)
        items = items[-limit:][::-1]
        return {"ok": True, "transactions": items}

    # ==========================================================
    # 投資日記
    # ==========================================================

    async def journal_add(self, entry: dict) -> dict:
        title = (entry.get("title") or "").strip() or "(無題)"
        content = (entry.get("content") or "").strip()
        if not content:
            return {"ok": False, "error": "本文が空です"}
        ticker = (entry.get("ticker") or "").strip().upper()
        action = (entry.get("action") or "").strip()  # buy / sell / hold / observe
        emotion = (entry.get("emotion") or "").strip()
        now = datetime.datetime.now(JST)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        slug = _safe_filename(ticker or title)[:40]
        filename = f"{date_str}_{now.strftime('%H%M%S')}_{slug}.md"

        body_lines = [
            f"---",
            f"title: {title}",
            f"date: {date_str}",
            f"ticker: {ticker}",
            f"action: {action}",
            f"emotion: {emotion}",
            f"tags: [investment, journal]",
            f"---",
            "",
            f"# {title}",
            "",
            f"- 日時: {date_str} {time_str}",
            f"- 銘柄: {ticker or '(なし)'}",
            f"- アクション: {action or '(なし)'}",
            f"- 感情: {emotion or '(なし)'}",
            "",
            "## 内容",
            "",
            content,
        ]
        body = "\n".join(body_lines)
        await self._save_dated_note(JOURNAL_FOLDER, filename, body)

        # インデックスを更新
        index = await self._read_json_file(
            JOURNAL_FOLDER, JOURNAL_INDEX_FILE, default=[]
        )
        index.append(
            {
                "filename": filename,
                "date": date_str,
                "time": time_str,
                "title": title,
                "ticker": ticker,
                "action": action,
                "emotion": emotion,
            }
        )
        await self._write_json_file(JOURNAL_FOLDER, JOURNAL_INDEX_FILE, index)
        return {"ok": True, "filename": filename}

    async def journal_list(self, limit: int = 50) -> dict:
        index = await self._read_json_file(
            JOURNAL_FOLDER, JOURNAL_INDEX_FILE, default=[]
        )
        # 新しい順
        items = list(reversed(index))[:limit]
        return {"ok": True, "items": items}

    async def journal_analyze_pattern(self, limit: int = 30) -> dict:
        index = await self._read_json_file(
            JOURNAL_FOLDER, JOURNAL_INDEX_FILE, default=[]
        )
        if not index:
            return {"ok": False, "error": "投資日記がまだありません"}
        # 直近N件を読み込む
        recent = list(reversed(index))[:limit]
        chunks = []
        service = self.drive_service.get_service() if self.drive_service else None
        if not service:
            return {"ok": False, "error": "Drive接続失敗"}
        inv_id = await self._get_investment_folder(service)
        sub_id = await self.drive_service.find_file(service, inv_id, JOURNAL_FOLDER)
        if not sub_id:
            return {"ok": False, "error": "Journalフォルダがありません"}
        for it in recent:
            f_id = await self.drive_service.find_file(
                service, sub_id, it.get("filename") or ""
            )
            if not f_id:
                continue
            content = await self.drive_service.read_text_file(service, f_id)
            if content:
                chunks.append(content)
        if not chunks:
            return {"ok": False, "error": "日記本文の読み込みに失敗"}
        joined = "\n\n---\n\n".join(chunks)
        if len(joined) > 80000:
            joined = joined[:80000] + "\n\n（以下省略）"
        prompt = PROMPT_JOURNAL_PATTERN.format(entries=joined)
        result = await self._gemini_plain(prompt)
        if not result:
            return {"ok": False, "error": "Geminiでの解析に失敗"}
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        await self._save_dated_note(
            JOURNAL_FOLDER, f"_pattern_{date_str}.md", result
        )
        return {"ok": True, "report": result}

    # ==========================================================
    # アラート
    # ==========================================================

    async def alerts_list(self) -> dict:
        rules = await self._read_json_file(
            ALERTS_FOLDER, ALERT_RULES_FILE, default=[]
        )
        return {"ok": True, "rules": rules}

    async def alerts_add(self, rule: dict) -> dict:
        ticker = (rule.get("ticker") or "").strip()
        rtype = (rule.get("type") or "").strip()
        valid_types = {
            "per_below", "per_above",
            "price_below", "price_above",
            "drop_pct", "rise_pct",
            "earnings_within_days",
        }
        if rtype not in valid_types:
            return {
                "ok": False,
                "error": f"未知のアラート種別: {rtype}. 有効値: {sorted(valid_types)}",
            }
        if not ticker and rtype != "earnings_within_days":
            return {"ok": False, "error": "tickerが必要です"}
        try:
            threshold = float(rule.get("threshold", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "threshold は数値を指定してください"}

        rules = await self._read_json_file(
            ALERTS_FOLDER, ALERT_RULES_FILE, default=[]
        )
        market, code = _resolve_market(ticker) if ticker else ("", "")
        new_rule = {
            "id": int(datetime.datetime.now(JST).timestamp() * 1000),
            "ticker": ticker,
            "code": code,
            "market": market,
            "type": rtype,
            "threshold": threshold,
            "enabled": rule.get("enabled", True),
            "memo": rule.get("memo") or "",
            "created_at": datetime.datetime.now(JST).isoformat(),
        }
        rules.append(new_rule)
        await self._write_json_file(ALERTS_FOLDER, ALERT_RULES_FILE, rules)
        return {"ok": True, "rule": new_rule, "rules": rules}

    async def alerts_remove(self, rule_id: int) -> dict:
        rules = await self._read_json_file(
            ALERTS_FOLDER, ALERT_RULES_FILE, default=[]
        )
        new_rules = [r for r in rules if r.get("id") != int(rule_id)]
        if len(new_rules) == len(rules):
            return {"ok": False, "error": f"rule_id={rule_id} が見つかりません"}
        await self._write_json_file(ALERTS_FOLDER, ALERT_RULES_FILE, new_rules)
        return {"ok": True, "rules": new_rules}

    async def alerts_toggle(self, rule_id: int, enabled: bool) -> dict:
        rules = await self._read_json_file(
            ALERTS_FOLDER, ALERT_RULES_FILE, default=[]
        )
        for r in rules:
            if r.get("id") == int(rule_id):
                r["enabled"] = bool(enabled)
                await self._write_json_file(ALERTS_FOLDER, ALERT_RULES_FILE, rules)
                return {"ok": True, "rules": rules}
        return {"ok": False, "error": f"rule_id={rule_id} が見つかりません"}

    async def alerts_check_now(self) -> dict:
        """現在のアラートルールをすべて評価し、ヒットしたものを返す。"""
        rules = await self._read_json_file(
            ALERTS_FOLDER, ALERT_RULES_FILE, default=[]
        )
        active = [r for r in rules if r.get("enabled")]
        if not active:
            return {"ok": True, "hits": [], "checked": 0}
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        # すべてのルールをまとめてGeminiに渡し、現在の市場データで判定させる
        rules_str = json.dumps(active, ensure_ascii=False, indent=2)
        today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        prompt = (
            f"以下の投資アラートルールを、Google検索とGoogleファイナンスから取得"
            f"できる現在の最新データで評価してください。今日の日付は {today} です。\n\n"
            f"【ルール】\n{rules_str}\n\n"
            f"各ルールを評価し、ヒットしたものを報告してください。\n"
            f"出力は以下の JSON のみで、前置き不要:\n"
            f"{{\n"
            f'  "hits": [\n'
            f'    {{"id": 123, "ticker": "...", "type": "...", "threshold": 0,'
            f'      "current_value": 0, "message": "1行で日本語"}}\n'
            f"  ],\n"
            f'  "checked_count": 0,\n'
            f'  "as_of": "YYYY-MM-DD HH:MM"\n'
            f"}}\n"
            f"ルール種別の意味:\n"
            f"- per_below/per_above: PERが threshold を下回った/上回った\n"
            f"- price_below/price_above: 株価が threshold を下回った/上回った\n"
            f"- drop_pct/rise_pct: 直近1ヶ月で threshold% 以上下落/上昇\n"
            f"- earnings_within_days: 次回決算が threshold 日以内に迫っている"
        )
        raw = await self._gemini_with_search(prompt)
        data = self._extract_json(raw) or {}
        hits = data.get("hits") or []
        # ログ追記
        for h in hits:
            await self._append_jsonl(
                ALERTS_FOLDER,
                ALERT_LOG_FILE,
                {"ts": datetime.datetime.now(JST).isoformat(), **h},
            )
        return {
            "ok": True,
            "hits": hits,
            "checked": data.get("checked_count", len(active)),
            "as_of": data.get("as_of") or today,
            "raw": raw if not data else None,
        }

    # ==========================================================
    # 同業他社比較
    # ==========================================================

    async def run_peer_comparison(self, ticker: str) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        market, code = _resolve_market(ticker)
        prompt = PROMPT_PEER_COMPARISON.format(ticker=code, market=market)
        result = await self._gemini_with_search(prompt)
        if not result:
            return {"ok": False, "error": "比較レポートの生成に失敗"}
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filename = f"{date_str}_{_safe_filename(code)}.md"
        body = (
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"tags: [investment, comparison]\n---\n\n{result}"
        )
        await self._save_dated_note(COMPARISONS_FOLDER, filename, body)
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "report": result,
            "saved_as": filename,
        }

    # ==========================================================
    # ニュースセンチメント
    # ==========================================================

    async def run_news_sentiment(self, ticker: str) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        market, code = _resolve_market(ticker)
        prompt = PROMPT_NEWS_SENTIMENT.format(ticker=code, market=market)
        result = await self._gemini_with_search(prompt)
        if not result:
            return {"ok": False, "error": "ニュース取得に失敗"}
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        filename = f"{date_str}_{_safe_filename(code)}.md"
        body = (
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"tags: [investment, news_sentiment]\n---\n\n{result}"
        )
        await self._save_dated_note(NEWS_SENTIMENT_FOLDER, filename, body)
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "report": result,
            "saved_as": filename,
        }

    # ==========================================================
    # 配当カレンダー
    # ==========================================================

    async def run_dividend_schedule(
        self, ticker: str, register_calendar: bool = True
    ) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        market, code = _resolve_market(ticker)
        prompt = PROMPT_DIVIDEND_SCHEDULE.format(ticker=code, market=market)
        raw = await self._gemini_with_search(prompt)
        if not raw:
            return {"ok": False, "error": "配当情報の取得に失敗"}
        data = self._extract_json(raw)
        if not data:
            return {
                "ok": False,
                "error": "配当情報のパースに失敗",
                "raw": raw[:1500],
            }
        company = data.get("company_name") or code
        events = data.get("events") or []
        currency = data.get("currency") or ("JPY" if market == "JP" else "USD")

        # Markdownレポート
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        lines = [
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"tags: [investment, dividend]\n---\n",
            f"# 💴 {company} ({code}) 配当カレンダー",
            "",
            f"- 年間配当: {data.get('annual_dividend_per_share', 'N/A')} {currency}/株",
            f"- 配当利回り: {data.get('yield_pct', 'N/A')}%",
            f"- 信頼度: {data.get('confidence', 'N/A')}",
            f"- 出典: {data.get('source', 'N/A')}",
            "",
            "## 🗓 イベント",
            "",
        ]
        if not events:
            lines.append("（取得できる配当イベントがありませんでした）")
        else:
            for ev in events:
                lines.append(
                    f"- **{ev.get('date', '?')}** [{ev.get('type', '?')}] "
                    f"{ev.get('amount_per_share', '?')} {currency}/株 "
                    f"({ev.get('fiscal_period', '?')})"
                )
        body = "\n".join(lines)

        registered = []
        if register_calendar and self.calendar_service and events:
            for ev in events:
                ev_date = ev.get("date")
                ev_type = ev.get("type")
                if not (ev_date and ev_type):
                    continue
                type_label_map = {
                    "ex_dividend": "配当落ち日",
                    "record_date": "権利確定日",
                    "payment_date": "配当支払日",
                    "forecast": "配当予想",
                }
                summary = (
                    f"💴 {company} {type_label_map.get(ev_type, ev_type)}"
                )
                desc = (
                    f"配当: {ev.get('amount_per_share', '?')} {currency}/株\n"
                    f"対象期: {ev.get('fiscal_period', '?')}\n"
                    f"出典: {data.get('source', 'N/A')}"
                )
                try:
                    r = await self.calendar_service.create_event(
                        summary=summary,
                        start_time=ev_date,
                        end_time=ev_date,
                        description=desc,
                    )
                    registered.append({"summary": summary, "date": ev_date, "result": r})
                except Exception as e:
                    registered.append({"error": str(e), "summary": summary})
        await self._save_dated_note(
            DIVIDENDS_FOLDER, f"{date_str}_{_safe_filename(code)}.md", body
        )
        return {
            "ok": True,
            "ticker": code,
            "market": market,
            "data": data,
            "report": body,
            "registered": registered,
        }

    # ==========================================================
    # 投資憲法レビュー
    # ==========================================================

    async def run_constitution_review(self, lookback_days: int = 180) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        constitution = await self._read_constitution()
        if not constitution:
            return {"ok": False, "error": "投資憲法が存在しません"}

        cutoff = datetime.datetime.now(JST) - datetime.timedelta(days=lookback_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        # 過去Nヶ月の審査履歴を集約
        audit_files = await self._list_subfolder_files(AUDITS_FOLDER, limit=50)
        audit_chunks = []
        service = self.drive_service.get_service() if self.drive_service else None
        if service:
            for f in audit_files:
                name = f.get("name") or ""
                if name[:10] >= cutoff_str:
                    content = await self.drive_service.read_text_file(
                        service, f.get("id")
                    )
                    if content:
                        audit_chunks.append(f"### {name}\n{content[:3000]}")
        audits_str = "\n\n".join(audit_chunks)[:40000] or "(履歴なし)"

        # 投資日記の最近を抜粋
        index = await self._read_json_file(
            JOURNAL_FOLDER, JOURNAL_INDEX_FILE, default=[]
        )
        recent_journal = [
            it for it in index if (it.get("date", "") >= cutoff_str)
        ]
        journal_summary_lines = []
        for it in recent_journal[-30:]:
            journal_summary_lines.append(
                f"- {it.get('date')} [{it.get('ticker', '')}/{it.get('action', '')}] "
                f"{it.get('title')} (感情: {it.get('emotion', '')})"
            )
        journal_str = "\n".join(journal_summary_lines) or "(日記なし)"

        # 保有銘柄
        holdings = await self._read_json_file(
            PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
        )
        holdings_str = json.dumps(holdings, ensure_ascii=False, indent=2)

        prompt = PROMPT_CONSTITUTION_REVIEW.format(
            constitution=constitution,
            audits=audits_str,
            journal=journal_str,
            holdings=holdings_str,
        )
        result = await self._gemini_plain(prompt)
        if not result:
            return {"ok": False, "error": "レビュー生成に失敗"}
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        await self._save_dated_note(
            CONSTITUTION_REVIEWS_FOLDER, f"{date_str}.md", result
        )
        return {"ok": True, "report": result, "lookback_days": lookback_days}

    # ==========================================================
    # リスク評価
    # ==========================================================

    async def run_risk_assessment(self) -> dict:
        if not self.gemini_client:
            return {"ok": False, "error": "Geminiクライアント未設定"}
        constitution = await self._read_constitution() or ""
        # ポジション管理セクションを抜粋
        pos_section = ""
        if constitution:
            m = re.search(
                r"(##\s*💰?\s*ポジション管理.*?)(?=\n##\s|\Z)",
                constitution,
                re.DOTALL,
            )
            if m:
                pos_section = m.group(1).strip()
        if not pos_section:
            pos_section = "(投資憲法のポジション管理セクションが見つかりません)"

        holdings = await self._read_json_file(
            PORTFOLIO_FOLDER, HOLDINGS_FILE, default=[]
        )
        if not holdings:
            return {"ok": False, "error": "保有銘柄がありません"}

        # 各銘柄の現在価格をGeminiで一気に取得
        codes = [h.get("code") for h in holdings if h.get("code")]
        market_data_prompt = (
            "次の銘柄について Google検索/Googleファイナンスから現在株価とPERを取得し、"
            "JSONのみで返してください。前置き不要。\n\n"
            f"銘柄リスト: {codes}\n\n"
            "形式:\n"
            '{"data": [{"code": "...", "price": 0, "currency": "JPY/USD", "per": 0, "change_pct_1m": 0}]}\n'
        )
        raw = await self._gemini_with_search(market_data_prompt)
        market_obj = self._extract_json(raw) or {"data": []}
        market_data_str = json.dumps(market_obj, ensure_ascii=False, indent=2)

        prompt = PROMPT_RISK_ASSESSMENT.format(
            constitution_position_rules=pos_section,
            holdings_json=json.dumps(holdings, ensure_ascii=False, indent=2),
            market_data=market_data_str,
        )
        report = await self._gemini_plain(prompt)
        if not report:
            return {"ok": False, "error": "リスク評価の生成に失敗"}
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        await self._save_dated_note(RISK_REPORTS_FOLDER, f"{date_str}.md", report)
        return {
            "ok": True,
            "report": report,
            "market_data": market_obj.get("data", []),
        }

async def setup(bot: commands.Bot):
    await bot.add_cog(InvestmentCog(bot))
