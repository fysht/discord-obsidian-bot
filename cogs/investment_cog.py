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
from typing import Optional

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
    PROMPT_NEWS_DIGEST,
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
SCREENINGS_FOLDER = "Screenings"

CONSTITUTION_FILE = "Investment_Constitution.md"
HOLDINGS_FILE = "holdings.json"
TRANSACTIONS_FILE = "transactions.jsonl"
JOURNAL_INDEX_FILE = "journal_index.json"
ALERT_RULES_FILE = "rules.json"
ALERT_LOG_FILE = "alert_log.jsonl"

GEMINI_MODEL = "gemini-2.5-pro"
GEMINI_FLASH_MODEL = "gemini-2.5-flash"


# 投資憲法サンプル (初回起動時にDriveに作成される)
# v2.0: 共通セクション + スタイル別セクション
INVESTMENT_CONSTITUTION_SAMPLE = """---
title: 投資憲法
version: 2.0
last_updated: {date}
styles: [trend_follow, creeping_up, low_vol_breakout, value, growth]
tags: [investment, constitution]
---

# 投資憲法（Investment Constitution）

> 共通セクションは全スタイルに適用、スタイル別セクションはスクリーナー実行時に該当条件として使う。

## 🎯 投資の目的（共通）
- 経済的自由の獲得
- 長期複利による資産形成（年率 7% 以上を目標）

## 💰 ポジション管理（共通）
- 1 銘柄あたりの最大保有額: ポートフォリオの 20%
- 同一セクター集中: 40% 以下
- 現金比率: 相場過熱時は 30% 以上を維持

## 🚪 売却ルール（共通）
1. **下落ストップ**: 取得価格から -20% で機械的に損切り
2. **業績悪化**: 2 期連続で営業利益が前年割れしたら撤退
3. **投資仮説の崩壊**: 当初の成長ストーリーが破綻したら即売却

## 🧠 行動規律（共通）
- 決算翌日の急騰急落で決断しない（24時間ルール）
- SNS / 株掲示板の意見では売買しない
- 1 日 1 回以上は株価チャートを開かない（チャートチェック病の回避）
- 自分の投資憲法に書かれていない理由で買わない

---

## スタイル: trend_follow 順張り（52週高値ブレイク）

### 銘柄選定基準
- [ ] 52週高値からの乖離が 1% 以内
- [ ] 出来高が 20 日平均の 1.5 倍以上
- [ ] 200日移動平均線より上

### 除外条件
- [ ] 直近の決算で大幅な下方修正
- [ ] PER が業界平均の 3 倍超

### ポジションサイズ
- 5%（順張りはロスカット幅が広い分、ポジションを抑える）

### 売却ルール（スタイル固有）
- 取得価格から -8% で機械的に損切り
- 25日移動平均線割れで部分利確

---

## スタイル: creeping_up じわじわ上昇（注目集まり前）

### 銘柄選定基準
- [ ] 直近 5 営業日で 3 日以上、+0.5%〜+3% の上昇
- [ ] 出来高が 5日/20日 比で 1.1 倍以上に増加
- [ ] ATR/Close < 4%（過熱でない）
- [ ] 時価総額 300 億円以上

### 除外条件
- [ ] 過去 1 年で同様パターン後に -20% 以上の調整あり

### ポジションサイズ
- 3%

---

## スタイル: low_vol_breakout 待てば上がる（低ボラ収束）

### 銘柄選定基準
- [ ] BB 幅が直近 100 日で下位 10%
- [ ] 過去 60 日のリターン絶対値が ±15% 以内
- [ ] 自己資本比率 40% 以上

### ポジションサイズ
- 5%（時間軸が長いため厚めに取れる）

---

## スタイル: value バリュー（割安+配当）

### 必須条件
- [ ] 時価総額: 1000億円以上
- [ ] 直近3期連続で営業黒字
- [ ] PER: 業界平均以下、PEG < 1.5
- [ ] PBR: 1.5 倍以下
- [ ] 配当利回り: 3% 以上

### ポジションサイズ
- 10%

---

## スタイル: growth グロース（成長性重視）

### 必須条件
- [ ] 売上高成長率（3年CAGR） > 15%
- [ ] 営業利益成長率 > 15%
- [ ] ROE > 12%
- [ ] フリーキャッシュフロー黒字

### ポジションサイズ
- 7%

---

## 📝 改訂履歴
- 2.0 ({date}): スタイル別セクション化（trend_follow / creeping_up / low_vol_breakout / value / growth）
- 1.0: 初版作成
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


def _normalize_news_line(line: str) -> str:
    """ニュース行の比較用正規化キー。
    - 行頭の箇条書き記号・番号・空白を除去
    - 日付（YYYY-MM-DD, M/D 等）と数字単位を緩く除去して、同一トピックを同一視
    """
    s = line.strip()
    s = re.sub(r"^[-*・●◯○■□▶▷>]+\s*", "", s)
    s = re.sub(r"^\d+[\.\)、]\s*", "", s)
    s = re.sub(r"\(\s*出典[^\)]*\)", "", s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", "", s)
    s = re.sub(r"\d{1,2}/\d{1,2}", "", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _extract_news_diff(prev_report: str, curr_report: str) -> list[str]:
    """前回レポートに無い「新規行」だけを抜き出す。
    比較は正規化キー（記号/日付/URLを除去した素のテキスト）で行う。
    見出し行は新規・既存に関わらず文脈用に残す（直後に新規行があるときだけ）。
    """
    if not curr_report:
        return []
    prev_keys: set[str] = set()
    for ln in (prev_report or "").splitlines():
        k = _normalize_news_line(ln)
        if k:
            prev_keys.add(k)

    out: list[str] = []
    pending_header: str | None = None
    for raw in curr_report.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        is_header = line.lstrip().startswith("#") or line.lstrip().startswith("##")
        if is_header:
            pending_header = line
            continue
        key = _normalize_news_line(line)
        if not key:
            continue
        if key in prev_keys:
            continue
        if pending_header is not None:
            out.append(pending_header)
            pending_header = None
        out.append(line)
    return out


def ext_map_lookup_mime(filename: str) -> str:
    """ファイル拡張子から代表的な MIME タイプを推測する。"""
    fname = (filename or "").lower()
    if fname.endswith(".pdf"):
        return "application/pdf"
    if fname.endswith((".html", ".htm")):
        return "text/html"
    if fname.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if fname.endswith(".xls"):
        return "application/vnd.ms-excel"
    if fname.endswith(".zip"):
        return "application/zip"
    return ""


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
                self.auto_holdings_noon_review_task,
                self.auto_decision_review_verify_task,
                self.auto_daily_screening_task,
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
            self.auto_holdings_noon_review_task,
            self.auto_decision_review_verify_task,
            self.auto_daily_screening_task,
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

    async def _notify_long(self, category: str, title: str, body: str, advice_job_id: str = ""):
        """長文レポートを「マネージャーからのお知らせ」に保存し、チャットには短いリードのみ送る。

        category 例: market_sentiment / news_sentiment / alerts / weekend_stocks など
        advice_job_id を渡すと、リードに「一括診断の結果を開く」ボタン（カード表示・チャート・注目追加）も付ける。
        """
        if not body:
            return
        # 1) DB に全文を保存
        try:
            from api.database import add_manager_notice
            await add_manager_notice(category, title, body)
        except Exception as e:
            logging.error(f"manager_notices save error: {e}")

        # 2) チャットには短いリードのみ。末尾の [ACTION:open_notices] により
        #    フロント側で「マネージャーからのお知らせを開く」ボタンが描画される。
        #    AI を通すとアクションタグが欠落するため、リードは直接送信する。
        #    advice_job_id があれば、テキストではなく対話的なカード表示を開くボタンも添える。
        advice_btn = f"\n[ACTION:open_advice_result:job={advice_job_id}]" if advice_job_id else ""
        lead = (
            f"📨 {title} を更新したよ。詳しくは下のボタンからお知らせを見てね。\n"
            "[ACTION:open_notices]" + advice_btn
        )
        try:
            from api.notification_service import save_message_and_notify
            await save_message_and_notify(
                "assistant", lead, title=f"📨 {title}", proactive=True,
            )
        except Exception as e:
            logging.error(f"InvestmentCog notify_long lead error: {e}")

    @tasks.loop(time=datetime.time(hour=6, minute=45, tzinfo=JST))
    async def auto_market_sentiment_task(self):
        """平日朝 06:45 (JST) に米国市場クローズ後の地合いを取得して通知する。"""
        # 土日は実行しない
        if datetime.datetime.now(JST).weekday() >= 5:
            return
        from services.schedule_resolver import is_enabled
        if not await is_enabled("auto_market_sentiment"):
            return
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
        await self._notify_long("market_sentiment", header, report)

    @auto_market_sentiment_task.before_loop
    async def _before_auto_market_sentiment(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=7, minute=15, tzinfo=JST))
    async def auto_alerts_and_earnings_task(self):
        """毎朝 07:15 (JST) に価格アラートをチェックし、保有銘柄の当日決算予定も通知する。"""
        from services.schedule_resolver import is_enabled
        if not await is_enabled("auto_alerts_earnings"):
            return
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
            joined = "\n".join(lines)
            # 価格アラート＋決算予定が3行を超えるなら通知ログへ、短い場合はチャットのまま
            if len(lines) > 3:
                await self._notify_long("alerts_earnings", "価格アラート・決算予定", joined)
            else:
                await self._notify_routine(joined)

    @auto_alerts_and_earnings_task.before_loop
    async def _before_auto_alerts_and_earnings(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=8, minute=30, tzinfo=JST))
    async def auto_news_sentiment_task(self):
        """毎朝 08:30 (JST) に保有銘柄のニュースを取得し、
        前回配信からの「差分（新規行）」のみを銘柄ごとにまとめて通知する。

        - 銘柄ごとに直近レポートを app_settings に保存し、次回はそれと比較する
        - 行（箇条書きやヘッダなど）単位で前回に無いものだけ抽出
        - 全銘柄で新規行ゼロなら通知自体をスキップ（朝刊は出さない）
        """
        from services.schedule_resolver import is_enabled
        if not await is_enabled("auto_news_sentiment"):
            return
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

        from api.database import get_app_setting, set_app_setting
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
            setting_key = f"news_sentiment.last.{code}"
            try:
                prev_report = await get_app_setting(setting_key, "")
            except Exception:
                prev_report = ""

            new_lines = _extract_news_diff(prev_report, report)
            # 今回の本文は次回比較用に常に保存（変化があってもなくても）
            try:
                await set_app_setting(setting_key, report)
            except Exception:
                pass

            if new_lines:
                diff_body = "\n".join(new_lines)
                sections.append(f"## {head} ({code})\n{diff_body}")
            await asyncio.sleep(2)

        if sections:
            raw_body = "\n\n---\n\n".join(sections)
            yesterday = (datetime.datetime.now(JST).date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            digest = ""
            try:
                digest_prompt = PROMPT_NEWS_DIGEST.format(yesterday=yesterday, reports=raw_body)
                digest = await self._gemini_plain(digest_prompt, feature_key="news_sentiment")
            except Exception:
                logging.exception("auto_news_sentiment: digest 生成失敗。生データで通知します。")
            body = (digest or "").strip() or raw_body
            await self._notify_long(
                "news_sentiment", "保有銘柄ニュース（前日分）", body
            )
        else:
            logging.info("auto_news_sentiment: 全銘柄で新規ニュース無し、通知スキップ")

    @auto_news_sentiment_task.before_loop
    async def _before_auto_news_sentiment(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=JST))
    async def auto_holdings_noon_review_task(self):
        """平日 12:00 (JST) に保有銘柄の継続/縮小/売却をテクニカル×ファンダで診断し通知。
        12:30 の売買判断の参考にする。決定論的・Gemini非依存・無料。"""
        # 土日は実行しない
        if datetime.datetime.now(JST).weekday() >= 5:
            return
        from services.schedule_resolver import is_enabled
        if not await is_enabled("holdings_noon_review"):
            return
        try:
            result = await self.run_holdings_review()
        except Exception:
            logging.exception("auto_holdings_noon_review_task failed")
            return
        if not result.get("ok") or not result.get("report"):
            return
        await self._notify_long("holdings_review", "🕛 保有銘柄の昼チェック", result["report"],
                                advice_job_id=result.get("advice_job_id") or "")

    @auto_holdings_noon_review_task.before_loop
    async def _before_auto_holdings_noon_review(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=15, minute=45, tzinfo=JST))
    async def auto_decision_review_verify_task(self):
        """平日 15:45 (JST) 市場クローズ後に、過去の売買判断を答え合わせ（20/60営業日後）。
        新たに判定が確定した分があるときだけ、的中率を通知ログに届ける。"""
        if datetime.datetime.now(JST).weekday() >= 5:
            return
        from services.schedule_resolver import is_enabled
        if not await is_enabled("decision_review_verify"):
            return
        screener = self.bot.get_cog("ScreenerCog")
        if not screener:
            return
        try:
            verified = await screener.verify_due_decisions()
        except Exception:
            logging.exception("auto_decision_review_verify_task failed")
            return
        if not verified.get("ok") or not verified.get("updated"):
            return  # 新たに確定した判定が無ければ通知しない

        try:
            report = await screener.decision_review_report(horizon="d60")
        except Exception:
            report = {}

        results = verified.get("results") or []
        labels = {"buy": "🟢買い", "sell": "🔴売り"}
        lines = [f"🔎 売買判断の答え合わせ（新たに{verified['updated']}件確定）", ""]
        for r in results[:12]:
            cps = r.get("checkpoints") or {}
            cp = cps.get("d60") or cps.get("d20") or {}
            horizon = "60営業日" if "d60" in cps else "20営業日"
            ex = cp.get("excess_pct")
            ex_s = f"（市場比 {ex:+.1f}%）" if ex is not None else ""
            lines.append(
                f"- {r.get('code')} {r.get('name', '')}: "
                f"{labels.get(r.get('trade_action'), r.get('trade_action', ''))} → "
                f"{cp.get('outcome', '—')}{ex_s} [{horizon}]"
            )
        if report.get("summary"):
            lines += ["", report["summary"]]
        tv = (report or {}).get("trading_value_add") or {}
        if tv.get("message"):
            lines += ["", f"📌 {tv['message']}"]
        lines += ["", "※ 上昇/下落は市場全体に対する『超過』で採点（相場の地合いに翻弄されない判定）。"]
        await self._notify_long("decision_review", "🔎 売買判断の答え合わせ", "\n".join(lines))

    @auto_decision_review_verify_task.before_loop
    async def _before_auto_decision_review_verify(self):
        await self.bot.wait_until_ready()

    async def run_holdings_review(self) -> dict:
        """保有銘柄をテクニカル×ファンダで診断し、売買判断用の短いレポートを作る。
        ScreenerCog の advise_portfolio（決定論的・高速・無料）を使う。"""
        screener = self.bot.get_cog("ScreenerCog")
        if not screener:
            return {"ok": False, "error": "ScreenerCog 未ロード"}
        advice = await screener.advise_portfolio(candidates=None, with_financials=False)
        if not advice.get("ok"):
            return advice
        holds = [h for h in (advice.get("holdings") or []) if h.get("ok")]
        if not holds:
            return {"ok": True, "report": ""}  # 保有なし→通知なし

        # 構造化結果を done ジョブとして保存し、通知ボタンから『毎日ここから』のカード表示
        # （保有のチャート確認など）で開けるようにする。
        advice_job_id = await screener.save_advice_as_job(advice)

        labels = {"SELL": "🔴 売却・撤退", "TRIM": "🟠 一部利確・縮小",
                  "HOLD_WATCH": "🟡 保有（警戒）", "HOLD": "🟢 継続保有"}
        order = {"SELL": 0, "TRIM": 1, "HOLD_WATCH": 2, "HOLD": 3}
        holds.sort(key=lambda r: order.get(r["verdict"]["action"], 9))

        def _line(r):
            v = r["verdict"]
            t = r.get("trend") or {}
            pnl = r.get("pnl") or {}
            pnl_s = f" / 含み{pnl['pnl_pct']:+.1f}%" if pnl.get("pnl_pct") is not None else ""
            stop = t.get("trailing_stop")
            stop_s = f" / トレイル{stop:g}円" if stop is not None else ""
            return (f"- {r['code']} {r.get('name', '')}: "
                    f"{labels.get(v['action'], v['action_label'])}（{r.get('score')}点）"
                    f"{stop_s}{pnl_s}\n  {v.get('note', '')}")

        action_items = [r for r in holds if r["verdict"]["action"] in ("SELL", "TRIM")]
        keep_items = [r for r in holds if r["verdict"]["action"] in ("HOLD", "HOLD_WATCH")]

        today = datetime.datetime.now(JST).strftime("%-m/%-d") if os.name != "nt" else datetime.datetime.now(JST).strftime("%m/%d")
        parts = [f"🕛 保有銘柄の昼チェック（{today}）", advice.get("summary", ""), ""]
        if action_items:
            parts.append("【要対応：売却/縮小の検討】")
            parts.extend(_line(r) for r in action_items)
            parts.append("")
        if keep_items:
            parts.append("【継続保有】")
            parts.extend(_line(r) for r in keep_items)
            parts.append("")
        for rot in (advice.get("rotations") or [])[:3]:
            parts.append(f"🔁 {rot.get('reason', '')}")
        parts.append("")
        parts.append("※ テクニカル(トレンド)×ファンダの決定論的診断です。12:30 の売買判断の参考に。")
        return {"ok": True, "report": "\n".join(p for p in parts if p is not None),
                "advice_job_id": advice_job_id}

    # 旧: 「じわじわ高値ブレイク×一括診断」(run_breakout_advise) と平日16:00の自動通知は、
    # 16:15 の全手法版（auto_daily_screening / 毎日ここから）に内包される部分集合だったため撤去した。

    @tasks.loop(time=datetime.time(hour=16, minute=15, tzinfo=JST))
    async def auto_daily_screening_task(self):
        """平日 16:15 (JST) 大引け後に、全メソッドで日本株＋米国株を横断抽出（どの投資手法が拾ったかの
        ラベル付き）し、保有＋候補を一括診断（目標配分のドリフト・入替数量つき）してお知らせへ。
        日次ワークフロー①の自動化（日米1:1配分のため両市場から候補抽出）。"""
        if datetime.datetime.now(JST).weekday() >= 5:
            return
        from services.schedule_resolver import is_enabled
        if not await is_enabled("auto_daily_screening"):
            return
        try:
            result = await self.run_daily_screening()
        except Exception:
            logging.exception("auto_daily_screening_task failed")
            return
        if not result.get("ok") or not result.get("report"):
            return
        await self._notify_long("daily_screening", "🔎 今日の注目銘柄×手法", result["report"],
                                advice_job_id=result.get("advice_job_id") or "")

    @auto_daily_screening_task.before_loop
    async def _before_auto_daily_screening(self):
        await self.bot.wait_until_ready()

    async def gather_daily_candidates(
        self, universes: Optional[list] = None,
        styles: Optional[list] = None, top_n: int = 3, max_per_market: int = 10,
    ) -> dict:
        """全メソッドで日米ユニバースを横断し、買い候補（手法ラベル付き union）を抽出する。
        自動の日次スクリーニング（run_daily_screening）と、PWA「毎日ここから一括診断」の
        auto_screen の双方が共有する“候補生成”部。各候補がどの手法で拾われたかを matched_by_code に残す。
        戻り値: {"ok", "candidates": [{code,name,sector}], "matched_by_code": {code: [styles]}}"""
        screener = self.bot.get_cog("ScreenerCog")
        if not screener:
            return {"ok": False, "error": "ScreenerCog 未ロード", "candidates": [], "matched_by_code": {}}

        if styles is None:
            from services.screener_engine import list_strategies
            styles = [s["name"] for s in list_strategies()]

        # JP/US 各ユニバースを全メソッドで union 抽出（各候補に matched_styles＝どの手法が拾ったか）
        matched_by_code: dict[str, list] = {}
        cand_input: list[dict] = []
        any_ok = False

        async def _screen(universe: str):
            nonlocal any_ok
            if not universe:
                return
            try:
                scr = await screener.run_multi_screening(
                    styles=styles, top_n=int(top_n), universe_name=universe, combine_mode="any",
                )
            except Exception as e:
                logging.warning(f"gather_daily_candidates screening失敗({universe}): {e}")
                return
            if not scr.get("ok"):
                return
            any_ok = True
            cands = sorted((scr.get("candidates") or []),
                           key=lambda c: c.get("score") or 0, reverse=True)[:max_per_market]
            for c in cands:
                code = c.get("code")
                if not code or code in matched_by_code:
                    continue
                matched_by_code[code] = c.get("matched_styles") or []
                cand_input.append({"code": code, "name": c.get("name"), "sector": c.get("sector")})

        # 日本株 topix500 ＋ 米国株 sp500/mega を横断（重複コードは先勝ちでスキップ）。
        for u in (universes or ["topix500", "us_sp500", "us_mega"]):
            await _screen(u)
        return {"ok": any_ok, "candidates": cand_input, "matched_by_code": matched_by_code}

    async def run_daily_screening(
        self, universes: Optional[list] = None,
        styles: Optional[list] = None, top_n: int = 3, max_per_market: int = 10,
        deep_research_top_n: int = 2,
    ) -> dict:
        """全メソッドで日本株＋米国株を横断抽出（手法ラベル付き union）し、保有＋候補を一括診断する。
        run_multi_screening(combine_mode="any") を JP/US 各ユニバースで実行 → advise_portfolio を連結。
        日次ワークフロー①「平日チャート分析→候補を手法ラベル付きでピックアップ＋保有分析」。
        日米1:1 配分のため候補も両市場から拾う（決定論的・無料）。"""
        screener = self.bot.get_cog("ScreenerCog")
        if not screener:
            return {"ok": False, "error": "ScreenerCog 未ロード"}

        # 1) 候補生成（全メソッド・JP/US 横断）は gather_daily_candidates に集約。
        gathered = await self.gather_daily_candidates(
            universes=universes, styles=styles, top_n=top_n, max_per_market=max_per_market)
        matched_by_code = gathered.get("matched_by_code") or {}
        cand_input = gathered.get("candidates") or []
        any_ok = bool(gathered.get("ok"))

        # 2) 保有＋候補を一括診断（目標配分・入替数量込み）
        advice = await screener.advise_portfolio(candidates=cand_input, with_financials=False)
        if not advice.get("ok"):
            return advice
        holds = [h for h in (advice.get("holdings") or []) if h.get("ok")]
        cands_out = [c for c in (advice.get("candidates") or []) if c.get("ok")]
        if not holds and not cands_out:
            return {"ok": True, "report": ""}

        # 候補に手法ラベル（どの手法が拾ったか）を付け、構造化結果を done ジョブとして保存する。
        # 通知のボタンから『毎日ここから』のカード表示（チャート・注目追加）で開けるようにするため。
        for c in (advice.get("candidates") or []):
            ms = matched_by_code.get(str(c.get("code") or ""))
            if ms:
                c["matched_styles"] = [screener._style_display(s) for s in ms]
        advice_job_id = await screener.save_advice_as_job(advice)

        today = (datetime.datetime.now(JST).strftime("%-m/%-d")
                 if os.name != "nt" else datetime.datetime.now(JST).strftime("%m/%d"))
        parts = [f"🔎 今日の注目銘柄 × 手法（{today} 大引け後）", advice.get("summary", ""), ""]

        # 地合い（レジーム）：上昇相場でのみ攻める。リスクオフは新規買いを抑制。
        reg = advice.get("regime") or {}
        reg_bits = [f"{('日本株' if mk == 'JP' else '米国株')}: {(reg.get(mk) or {}).get('label', '不明')}"
                    for mk in ("JP", "US") if mk in reg]
        if reg_bits:
            parts.append("🌐 地合い: " + " ／ ".join(reg_bits))
            parts.append("")

        # 目標配分（最高値型:待ち型=4:1／日本株:米国株=1:1）のドリフト
        alloc = advice.get("allocation")
        if alloc and alloc.get("ok"):
            ba, ma = alloc["bucket_axis"], alloc["market_axis"]
            parts.append("⚖️ 目標配分（時価ベース・目安）")
            parts.append(f"- 最高値型:待ち型 = {ba['a']['pct']:g}%:{ba['b']['pct']:g}%"
                         f"（目標{ba['a']['target_pct']:g}:{ba['b']['target_pct']:g}・ズレ{ba['drift_pct']:+g}pt）")
            parts.append(f"- 日本株:米国株 = {ma['a']['pct']:g}%:{ma['b']['pct']:g}%"
                         f"（目標{ma['a']['target_pct']:g}:{ma['b']['target_pct']:g}・ズレ{ma['drift_pct']:+g}pt）")
            for w in alloc.get("warnings", []):
                parts.append(f"  ⚠️ {w}")
            parts.append("")

        # 今日の候補（手法ラベル付き・日本株/米国株で分けて表示）
        def _cand_line(r):
            v = r["verdict"]
            ms = matched_by_code.get(r["code"], [])
            ms_disp = "・".join(screener._style_display(s) for s in ms) or "—"
            flag = "🔵" if v["action"] == "BUY" else "・"
            return (f"{flag} {r['code']} {r.get('name', '')}: "
                    f"{v.get('action_label', v['action'])}（{r.get('score')}点）／ 手法: {ms_disp}")

        diagnosed = sorted(cands_out, key=lambda r: r.get("score") or 0, reverse=True)
        for mkt, mkt_label in (("JP", "日本株"), ("US", "米国株")):
            rows = [r for r in diagnosed if (r.get("market") or "JP") == mkt]
            if rows:
                parts.append(f"【今日の候補・{mkt_label}（手法ラベル付き）】")
                parts.extend(_cand_line(r) for r in rows)
                parts.append("")

        # 保有：売却/縮小の検討
        sells = [r for r in holds if r["verdict"]["action"] in ("SELL", "TRIM")]
        if sells:
            parts.append("【保有：売却/縮小の検討】")
            for r in sells:
                v = r["verdict"]
                parts.append(f"- {r['code']} {r.get('name', '')}: {v.get('action_label', v['action'])}"
                             f"（{r.get('score')}点）　{v.get('note', '')}")
            parts.append("")

        # 勝ち株の買い増し（ピラミッディング）：含み益＋トレンド継続中の保有
        pyramids = [r for r in holds if r.get("pyramid")]
        if pyramids:
            parts.append("【勝ち株の買い増し（利を伸ばす）】")
            for r in pyramids:
                parts.append(f"📈 {r['code']} {r.get('name', '')}: {r['pyramid'].get('note', '')}")
            parts.append("")

        caution = advice.get("over_trading_caution")
        if caution:
            parts += [caution, ""]

        # 入替（数量つき・摩擦/流動性考慮・目標に寄せる入替を先頭）
        rotations = (advice.get("rotations") or [])[:3]
        if rotations:
            parts.append("【入替の検討（目標配分に寄せる）】")
            parts.extend(f"🔁 {rot.get('reason', '')}" for rot in rotations)
            parts.append("")

        # ③ ディープリサーチの自動実行：最強の新規買い候補 上位 deep_research_top_n 件だけ
        # Gemini で網羅的に深掘り（7日キャッシュでコスト上限固定・点数はエンジン据え置き）。
        if deep_research_top_n and getattr(self, "gemini_client", None):
            buys = [r for r in diagnosed if r["verdict"]["action"] == "BUY"][:int(deep_research_top_n)]
            dr_parts = []
            for r in buys:
                try:
                    dr = await screener.deep_research(r["code"], r.get("name", ""), r.get("sector", ""))
                except Exception as e:
                    logging.debug(f"daily deep_research エラー {r['code']}: {e}")
                    continue
                if dr.get("ok") and dr.get("report"):
                    tag = "💾キャッシュ" if dr.get("cached") else "🆕新規"
                    warn = (" ⚠️" + "・".join(dr.get("warnings") or [])) if dr.get("warnings") else ""
                    dr_parts.append(f"### 🔬 {r['code']} {r.get('name', '')} ディープリサーチ（{tag}{warn}）\n"
                                    f"{dr['report']}")
            if dr_parts:
                parts.append("──────────")
                parts.append(f"🔬 特に強い新規買い候補のディープリサーチ（上位{len(dr_parts)}件・出典URL付き）")
                parts.append("")
                parts.extend(dr_parts)
                parts.append("")

        if not any_ok:
            parts.append("※ スクリーニングは実行できませんでした（保有のみ診断）。")
            parts.append("")
        parts.append("※ 当日確定の日足×ファンダ（米国株は時間差あり）の決定論的診断です。"
                     "手法ラベルは『どのメソッドが拾ったか』。日米1:1配分のため両市場から抽出。最終判断は自己責任で。")
        return {"ok": True, "report": "\n".join(p for p in parts if p is not None),
                "advice_job_id": advice_job_id}

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

    async def _resolve_model(self, model: Optional[str], feature_key: Optional[str]) -> str:
        """model > feature_key設定 > デフォルト の優先で Gemini モデル名を決定。"""
        if model:
            return model
        if feature_key:
            try:
                from services.gemini_model_resolver import resolve_gemini_model
                return await resolve_gemini_model(feature_key, default_pro=True)
            except Exception as e:
                logging.debug(f"resolve_gemini_model failed for {feature_key}: {e}")
        return GEMINI_MODEL

    async def _gemini_with_search(
        self, prompt: str, model: Optional[str] = None, feature_key: Optional[str] = None
    ) -> str:
        if not self.gemini_client:
            return ""
        resolved = await self._resolve_model(model, feature_key)
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model=resolved,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            return (response.text or "").strip()
        except Exception as e:
            logging.error(f"InvestmentCog: Gemini(search) error: {e}", exc_info=True)
            return ""

    async def _gemini_plain(
        self, prompt: str, model: Optional[str] = None, feature_key: Optional[str] = None
    ) -> str:
        if not self.gemini_client:
            return ""
        resolved = await self._resolve_model(model, feature_key)
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model=resolved,
                contents=prompt,
            )
            return (response.text or "").strip()
        except Exception as e:
            logging.error(f"InvestmentCog: Gemini(plain) error: {e}", exc_info=True)
            return ""

    async def _gemini_with_video(
        self, prompt: str, video_url: str, model: Optional[str] = None, feature_key: Optional[str] = None
    ) -> str:
        """Gemini multimodalにYouTube URLを直接渡して解析させる。"""
        if not self.gemini_client:
            return ""
        resolved = await self._resolve_model(model, feature_key)
        try:
            video_part = types.Part(
                file_data=types.FileData(file_uri=video_url, mime_type="video/*")
            )
            text_part = types.Part.from_text(text=prompt)
            content = types.Content(role="user", parts=[video_part, text_part])
            response = await self.gemini_client.aio.models.generate_content(
                model=resolved,
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
        result = await self._gemini_with_search(prompt, feature_key="investment_sentiment")
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
        result = await self._gemini_with_search(prompt, feature_key="investment_snapshot")
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
        snapshot = await self._gemini_with_search(snap_prompt, feature_key="investment_snapshot")
        if not snapshot:
            return {"ok": False, "error": "銘柄データの取得に失敗"}

        audit_prompt = PROMPT_STOCK_AUDIT.format(
            constitution=constitution,
            snapshot=snapshot,
            ticker=code,
        )
        audit = await self._gemini_plain(audit_prompt, feature_key="investment_audit")
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
        raw = await self._gemini_with_search(prompt, feature_key="investment_earnings")
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
        raw = await self._gemini_with_search(prompt, feature_key="investment_earnings")
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

        ir_page_url = (data.get("ir_page_url") or "").strip()
        ir_docs_url = (data.get("ir_documents_page_url") or "").strip()
        edinet_url = (data.get("edinet_search_url") or "").strip()
        edgar_url = (data.get("edgar_search_url") or "").strip()

        # Markdown形式に整形
        lines = [
            f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
            f"tags: [investment, earnings_docs]\n---\n",
            f"# 📑 {company} ({code}) 決算関連資料",
            "",
            f"- 情報源品質: {data.get('source_quality', 'N/A')}",
            f"- 取得日: {date_str}",
            "",
            "## 🌟 確実に飛べる公式ページ",
            "下の個別資料リンクは時々リンク切れになることがあります。資料が開けない場合は以下から手動で探してください。",
            "",
        ]
        if ir_page_url:
            lines.append(f"- 🏢 **公式IRトップ**: [{ir_page_url}]({ir_page_url})")
        if ir_docs_url and ir_docs_url != ir_page_url:
            lines.append(f"- 📚 **決算資料一覧ページ**: [{ir_docs_url}]({ir_docs_url})")
        if edinet_url:
            lines.append(f"- 🗃 **EDINET 検索**: [{edinet_url}]({edinet_url})")
        if edgar_url:
            lines.append(f"- 🗃 **SEC EDGAR 検索**: [{edgar_url}]({edgar_url})")
        if not (ir_page_url or ir_docs_url or edinet_url or edgar_url):
            lines.append("（公式IRページのURLが取得できませんでした。`{code}` で企業名を検索してください）".replace("{code}", code))
        lines.append("")
        lines.append("## 📋 個別資料一覧（直リンクは切れていることがあります）")
        lines.append("")
        documents = data.get("documents") or []
        if not documents:
            lines.append("（個別資料の直リンクは取得できませんでした。上の公式ページから手動でご確認ください）")
        else:
            # URL末尾から file_format を確実に判定（プロンプトの返却が不正な場合のフォールバック）
            def _format_of(url: str, declared: str) -> str:
                if declared in ("pdf", "html", "ir_page"):
                    return declared
                return "pdf" if isinstance(url, str) and url.lower().split("?")[0].endswith(".pdf") else "html"

            # PDF を先頭に並び替え（次に html、最後に ir_page）
            _order = {"pdf": 0, "html": 1, "ir_page": 2}
            def _doc_sort_key(d):
                fmt = _format_of(d.get("url", ""), d.get("file_format", ""))
                return (_order.get(fmt, 3), d.get("published_date", ""))

            documents = sorted(documents, key=_doc_sort_key)
            for doc in documents:
                title = doc.get("title", "(無題)")
                doc_type = doc.get("type", "")
                period = doc.get("fiscal_period", "")
                pub = doc.get("published_date", "")
                url = doc.get("url", "")
                lang = doc.get("language", "")
                fmt = _format_of(url, doc.get("file_format", ""))
                badge = {"pdf": "📄 PDF", "html": "🔗 HTML", "ir_page": "📚 IRページ"}.get(fmt, "🔗 HTML")
                lines.append(f"### {badge} {title}")
                lines.append(
                    f"- 種別: {doc_type} / 形式: {fmt} / 会計期間: {period} / 公表日: {pub} / 言語: {lang}"
                )
                if url:
                    lines.append(f"- [{url}]({url})")
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

    async def save_earnings_document_from_url(
        self, ticker: str, url: str, label: str = ""
    ) -> dict:
        """企業サイトの PDF/HTML を Drive の Investment/EarningsDocs/<ticker>/ に保存。"""
        import aiohttp
        from urllib.parse import urlparse, unquote

        if not self.drive_service:
            return {"ok": False, "error": "Drive 未接続"}
        if not url or not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "有効な URL を指定してください"}
        market, code = _resolve_market(ticker)
        safe_code = _safe_filename(code) or "UNKNOWN"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=60), allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        return {
                            "ok": False,
                            "error": f"取得失敗 (HTTP {resp.status})",
                        }
                    content = await resp.read()
                    if len(content) == 0:
                        return {"ok": False, "error": "空のレスポンス"}
                    if len(content) > 50 * 1024 * 1024:
                        return {"ok": False, "error": "50MBを超えるファイルは保存できません"}
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    cdisp = resp.headers.get("Content-Disposition", "")
                    final_url = str(resp.url)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "タイムアウトしました"}
        except Exception as e:
            return {"ok": False, "error": f"取得エラー: {e}"}

        # ファイル名決定
        fname = ""
        m = re.search(r'filename\*?=(?:UTF-8\'\')?\"?([^;\"]+)\"?', cdisp, re.IGNORECASE)
        if m:
            fname = unquote(m.group(1)).strip()
        if not fname:
            path = urlparse(final_url).path
            fname = unquote(path.rsplit("/", 1)[-1]) if path else ""
        if not fname or fname == "/":
            fname = "document"
        fname = _safe_filename(fname) or "document"

        # 拡張子
        ext_map = {
            "application/pdf": ".pdf",
            "text/html": ".html",
            "application/xhtml+xml": ".html",
            "application/zip": ".zip",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-excel": ".xls",
        }
        if "." not in fname:
            fname += ext_map.get(ctype, ".bin")

        mime = ctype or ext_map_lookup_mime(fname)
        if not mime:
            mime = "application/octet-stream"

        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        final_filename = f"{date_str}_{fname}"

        # アップロード: Investment/EarningsDocs/<code>/<filename>
        try:
            service = self.drive_service.get_service()
            inv_id = await self._get_investment_folder(service)
            edocs_id = await self._get_or_create_folder(service, inv_id, EARNINGS_DOCS_FOLDER)
            ticker_id = await self._get_or_create_folder(service, edocs_id, safe_code)

            # 同名があれば末尾に連番を追加
            existing = await self.drive_service.find_file(service, ticker_id, final_filename)
            if existing:
                stem, _, ext = final_filename.rpartition(".")
                ts = datetime.datetime.now(JST).strftime("%H%M%S")
                final_filename = f"{stem}_{ts}.{ext}" if ext else f"{final_filename}_{ts}"

            from googleapiclient.http import MediaIoBaseUpload
            import io as _io

            media = MediaIoBaseUpload(
                _io.BytesIO(content), mimetype=mime, resumable=True
            )
            file_metadata = {
                "name": final_filename,
                "parents": [ticker_id],
                "mimeType": mime,
            }
            created = await asyncio.to_thread(
                lambda: service.files()
                .create(body=file_metadata, media_body=media, fields="id, webViewLink")
                .execute()
            )
            file_id = created.get("id")
            view_link = created.get("webViewLink", "")
        except Exception as e:
            logging.error(f"決算資料ファイルアップロード失敗: {e}", exc_info=True)
            return {"ok": False, "error": f"Drive 保存失敗: {e}"}

        # インデックス Markdown へ追記
        index_filename = f"_index_{safe_code}.md"
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        entry = (
            f"\n### 📄 {label or fname}\n"
            f"- 保存日時: {now_str}\n"
            f"- 形式: {mime}\n"
            f"- サイズ: {len(content):,} bytes\n"
            f"- 元URL: [{final_url}]({final_url})\n"
            f"- Drive: [{final_filename}]({view_link})\n"
        )
        try:
            service2 = self.drive_service.get_service()
            existing_idx = await self.drive_service.find_file(service2, ticker_id, index_filename)
            if existing_idx:
                cur = await self.drive_service.read_text_file(service2, existing_idx)
                await self.drive_service.update_text(service2, existing_idx, (cur or "") + entry)
            else:
                header = (
                    f"---\nticker: {safe_code}\ntags: [investment, earnings_docs, manual]\n---\n\n"
                    f"# 📥 {safe_code} 決算資料（手動保存ログ）\n"
                )
                await self.drive_service.upload_text(
                    service2, ticker_id, index_filename, header + entry
                )
        except Exception as e:
            logging.error(f"決算資料インデックス更新失敗: {e}")

        return {
            "ok": True,
            "ticker": safe_code,
            "filename": final_filename,
            "size": len(content),
            "mime": mime,
            "drive_link": view_link,
            "folder": f"Investment/{EARNINGS_DOCS_FOLDER}/{safe_code}",
        }

    async def save_earnings_document_from_bytes(
        self, ticker: str, content: bytes, filename: str, label: str = "", mime: str = ""
    ) -> dict:
        """ローカルアップロード版（フロントから multipart で送られたバイト列を保存）。"""
        if not self.drive_service:
            return {"ok": False, "error": "Drive 未接続"}
        if not content:
            return {"ok": False, "error": "空のファイルです"}
        if len(content) > 50 * 1024 * 1024:
            return {"ok": False, "error": "50MBを超えるファイルは保存できません"}
        market, code = _resolve_market(ticker)
        safe_code = _safe_filename(code) or "UNKNOWN"
        fname = _safe_filename(filename or "document") or "document"
        if not mime:
            mime = ext_map_lookup_mime(fname) or "application/octet-stream"
        date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        final_filename = f"{date_str}_{fname}"
        try:
            service = self.drive_service.get_service()
            inv_id = await self._get_investment_folder(service)
            edocs_id = await self._get_or_create_folder(service, inv_id, EARNINGS_DOCS_FOLDER)
            ticker_id = await self._get_or_create_folder(service, edocs_id, safe_code)
            existing = await self.drive_service.find_file(service, ticker_id, final_filename)
            if existing:
                stem, _, ext = final_filename.rpartition(".")
                ts = datetime.datetime.now(JST).strftime("%H%M%S")
                final_filename = f"{stem}_{ts}.{ext}" if ext else f"{final_filename}_{ts}"
            from googleapiclient.http import MediaIoBaseUpload
            import io as _io
            media = MediaIoBaseUpload(_io.BytesIO(content), mimetype=mime, resumable=True)
            file_metadata = {"name": final_filename, "parents": [ticker_id], "mimeType": mime}
            created = await asyncio.to_thread(
                lambda: service.files()
                .create(body=file_metadata, media_body=media, fields="id, webViewLink")
                .execute()
            )
            view_link = created.get("webViewLink", "")
        except Exception as e:
            logging.error(f"決算資料ローカル保存失敗: {e}", exc_info=True)
            return {"ok": False, "error": f"Drive 保存失敗: {e}"}

        # インデックス
        index_filename = f"_index_{safe_code}.md"
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        entry = (
            f"\n### 📄 {label or fname}\n"
            f"- 保存日時: {now_str}\n"
            f"- 形式: {mime} (ローカルアップロード)\n"
            f"- サイズ: {len(content):,} bytes\n"
            f"- Drive: [{final_filename}]({view_link})\n"
        )
        try:
            service2 = self.drive_service.get_service()
            existing_idx = await self.drive_service.find_file(service2, ticker_id, index_filename)
            if existing_idx:
                cur = await self.drive_service.read_text_file(service2, existing_idx)
                await self.drive_service.update_text(service2, existing_idx, (cur or "") + entry)
            else:
                header = (
                    f"---\nticker: {safe_code}\ntags: [investment, earnings_docs, manual]\n---\n\n"
                    f"# 📥 {safe_code} 決算資料（手動保存ログ）\n"
                )
                await self.drive_service.upload_text(
                    service2, ticker_id, index_filename, header + entry
                )
        except Exception as e:
            logging.error(f"決算資料インデックス更新失敗(local): {e}")

        return {
            "ok": True,
            "ticker": safe_code,
            "filename": final_filename,
            "size": len(content),
            "mime": mime,
            "drive_link": view_link,
            "folder": f"Investment/{EARNINGS_DOCS_FOLDER}/{safe_code}",
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
        snapshot = await self._gemini_with_search(snap_prompt, feature_key="investment_snapshot")

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

        analysis = await self._gemini_with_video(cross_prompt, video_url, feature_key="investment_earnings")
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
        "screening": SCREENINGS_FOLDER,
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

        # 口座区分：NISA（非課税）なら入替の摩擦計算で譲渡益税を0にする。既定は特定/一般（課税）。
        acc_raw = str(holding.get("account") or "").strip().lower()
        account = "nisa" if acc_raw in ("nisa", "非課税", "tax_free") else "taxable"

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
            if holding.get("account"):
                existing["account"] = account
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
                    "account": account,
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
        # 事後検証用に「買い」判断のスナップショットを記録（バックグラウンド）
        self._snapshot_trade_decision(
            code=code, name=holding.get("name") or code, market=market,
            trade_action="buy", price=avg_cost,
            style=holding.get("preferred_method") or "",
        )
        return {"ok": True, "holdings": holdings}

    async def portfolio_remove(self, code: str, shares: float = None, price: float = None) -> dict:
        """sharesがNoneなら全数売却。指定なら部分売却。
        price に実際の売却単価を渡すと、実現損益((売却単価-平均取得単価)×株数)を記録する。"""
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

        # 実際の売却単価（指定が無ければ平均取得単価で代用＝損益0扱い）
        try:
            avg_cost = float(existing.get("avg_cost") or 0)
        except (TypeError, ValueError):
            avg_cost = 0.0
        sell_price = None
        if price is not None:
            try:
                sell_price = float(price)
            except (TypeError, ValueError):
                sell_price = None
            if sell_price is not None and sell_price <= 0:
                return {"ok": False, "error": "売却単価は正の値が必要です"}
        # 実現損益（売却単価が分かるときだけ算出）
        realized_pnl = None
        realized_pnl_pct = None
        if sell_price is not None and avg_cost > 0:
            realized_pnl = round((sell_price - avg_cost) * sold_shares, 2)
            realized_pnl_pct = round((sell_price - avg_cost) / avg_cost * 100, 2)

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
                "name": existing.get("name") or code,
                "shares": sold_shares,
                "price": sell_price if sell_price is not None else avg_cost,
                "avg_cost": avg_cost,
                "realized_pnl": realized_pnl,
                "realized_pnl_pct": realized_pnl_pct,
            },
        )
        # 事後検証用に「売り」判断のスナップショットを記録（バックグラウンド）。
        # 実売却単価が分かればそれを基準にし、無ければ診断時の現値を使う。
        self._snapshot_trade_decision(
            code=code, name=existing.get("name") or code,
            market=existing.get("market") or ("JP" if str(code).isdigit() else "US"),
            trade_action="sell", price=sell_price,
            style=existing.get("preferred_method") or "",
        )
        return {
            "ok": True, "holdings": holdings,
            "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
        }

    async def portfolio_realized_summary(self) -> dict:
        """取引履歴から、売却で確定した実現損益を集計する。
        「自分の売買がどれだけ利益を生んだか」を可視化する（実現損益ベース）。"""
        txns = await self._read_jsonl(PORTFOLIO_FOLDER, TRANSACTIONS_FILE)
        sells = [t for t in txns if t.get("action") == "sell"]
        realized = [t for t in sells if t.get("realized_pnl") is not None]

        total = round(sum(float(t.get("realized_pnl") or 0) for t in realized), 2)
        wins = [t for t in realized if float(t.get("realized_pnl") or 0) > 0]
        loses = [t for t in realized if float(t.get("realized_pnl") or 0) < 0]
        win_rate = round(len(wins) / len(realized) * 100, 1) if realized else None

        # 銘柄別の実現損益
        by_code: dict[str, dict] = {}
        for t in realized:
            c = str(t.get("code") or "")
            b = by_code.setdefault(c, {"code": c, "name": t.get("name") or c,
                                       "realized_pnl": 0.0, "sell_count": 0})
            b["realized_pnl"] = round(b["realized_pnl"] + float(t.get("realized_pnl") or 0), 2)
            b["sell_count"] += 1
        by_code_list = sorted(by_code.values(), key=lambda x: x["realized_pnl"], reverse=True)

        # 直近の売却（新しい順）
        recent = [{
            "ts": t.get("ts"), "code": t.get("code"), "name": t.get("name"),
            "shares": t.get("shares"), "price": t.get("price"), "avg_cost": t.get("avg_cost"),
            "realized_pnl": t.get("realized_pnl"), "realized_pnl_pct": t.get("realized_pnl_pct"),
        } for t in realized[-30:][::-1]]

        if realized:
            summary = (f"確定済み売却{len(realized)}件：実現損益 合計 {total:+,.0f}。"
                       f"勝ち{len(wins)}・負け{len(loses)}（勝率 {win_rate}%）。")
        else:
            untracked = len(sells)
            summary = ("実現損益はまだ記録されていません。"
                       + (f"（売却{untracked}件は売却単価が未入力のため損益未計算）"
                          if untracked else "売却履歴がありません。"))

        return {
            "ok": True,
            "summary": summary,
            "total_realized_pnl": total,
            "realized_trades": len(realized),
            "win_count": len(wins),
            "lose_count": len(loses),
            "win_rate": win_rate,
            "by_code": by_code_list,
            "recent": recent,
        }

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

        for key in ("name", "sector", "currency", "notes", "preferred_method"):
            if key in fields and fields[key] is not None:
                existing[key] = fields[key]

        # 口座区分（NISA=非課税／特定=課税）。入替の摩擦(税)計算に使う。
        if fields.get("account") is not None:
            acc = str(fields["account"]).strip().lower()
            existing["account"] = "nisa" if acc in ("nisa", "非課税", "tax_free") else "taxable"

        # 購入日(opened_at)の補正。YYYY-MM-DD を ISO 日時に正規化して保存。
        if fields.get("opened_at"):
            raw = str(fields["opened_at"]).strip()
            try:
                d = datetime.date.fromisoformat(raw[:10])
                existing["opened_at"] = datetime.datetime(
                    d.year, d.month, d.day, tzinfo=JST
                ).isoformat()
            except ValueError:
                return {"ok": False, "error": "購入日は YYYY-MM-DD 形式で指定してください"}

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

    def _snapshot_trade_decision(
        self, code: str, name: str, market: str, trade_action: str, price=None, style: str = ""
    ) -> None:
        """売買成立時に、その瞬間の診断（テクニカル状態・推奨・利確目安）を事後検証用に
        スナップショット記録する。ScreenerCog に委譲し、バックグラウンドで実行するので
        売買処理の応答を遅らせない。失敗しても売買自体は止めない。"""
        cog = self.bot.get_cog("ScreenerCog")
        if not cog:
            return

        async def _run():
            try:
                await cog.record_trade_decision(
                    code=code, name=name or code, market=market,
                    trade_action=trade_action, price=price, style=style,
                )
            except Exception as e:
                logging.debug(f"判断スナップショット記録に失敗 {code}: {e}")

        try:
            asyncio.create_task(_run())
        except RuntimeError:
            # イベントループ外（テスト等）では握りつぶす
            pass

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

    async def journal_get(self, filename: str) -> dict:
        """指定ファイル名の日記エントリを取得（インデックス + 本文）。"""
        if not filename:
            return {"ok": False, "error": "filename が空です"}
        index = await self._read_json_file(
            JOURNAL_FOLDER, JOURNAL_INDEX_FILE, default=[]
        )
        meta = next((it for it in index if it.get("filename") == filename), None)
        if not meta:
            return {"ok": False, "error": "日記が見つかりません"}
        # 本文取得
        if not self.drive_service:
            return {"ok": False, "error": "Drive未接続"}
        service = self.drive_service.get_service()
        if not service:
            return {"ok": False, "error": "Drive接続失敗"}
        inv_id = await self._get_investment_folder(service)
        sub_id = await self.drive_service.find_file(service, inv_id, JOURNAL_FOLDER)
        if not sub_id:
            return {"ok": False, "error": "Journalフォルダがありません"}
        f_id = await self.drive_service.find_file(service, sub_id, filename)
        if not f_id:
            return {"ok": False, "error": "ファイルが見つかりません"}
        raw = await self.drive_service.read_text_file(service, f_id)
        # 本文（## 内容 以降）を抽出
        content = ""
        if raw:
            marker = "## 内容"
            if marker in raw:
                content = raw.split(marker, 1)[1].lstrip("\n")
            else:
                content = raw
        return {
            "ok": True,
            "filename": filename,
            "title": meta.get("title", ""),
            "ticker": meta.get("ticker", ""),
            "action": meta.get("action", ""),
            "emotion": meta.get("emotion", ""),
            "date": meta.get("date", ""),
            "time": meta.get("time", ""),
            "content": content.strip(),
        }

    async def journal_edit(self, filename: str, entry: dict) -> dict:
        """既存日記を上書き編集する。filename と作成日時は維持し、本文・メタを更新。"""
        if not filename:
            return {"ok": False, "error": "filename が空です"}
        index = await self._read_json_file(
            JOURNAL_FOLDER, JOURNAL_INDEX_FILE, default=[]
        )
        meta = next((it for it in index if it.get("filename") == filename), None)
        if not meta:
            return {"ok": False, "error": "日記が見つかりません"}

        title = (entry.get("title") or "").strip() or "(無題)"
        content = (entry.get("content") or "").strip()
        if not content:
            return {"ok": False, "error": "本文が空です"}
        ticker = (entry.get("ticker") or "").strip().upper()
        action = (entry.get("action") or "").strip()
        emotion = (entry.get("emotion") or "").strip()
        date_str = meta.get("date") or datetime.datetime.now(JST).strftime("%Y-%m-%d")
        time_str = meta.get("time") or datetime.datetime.now(JST).strftime("%H:%M")
        now_iso = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")

        body_lines = [
            "---",
            f"title: {title}",
            f"date: {date_str}",
            f"ticker: {ticker}",
            f"action: {action}",
            f"emotion: {emotion}",
            f"updated_at: {now_iso}",
            "tags: [investment, journal]",
            "---",
            "",
            f"# {title}",
            "",
            f"- 日時: {date_str} {time_str}",
            f"- 銘柄: {ticker or '(なし)'}",
            f"- アクション: {action or '(なし)'}",
            f"- 感情: {emotion or '(なし)'}",
            f"- 更新: {now_iso}",
            "",
            "## 内容",
            "",
            content,
        ]
        body = "\n".join(body_lines)
        # _save_dated_note は既存ファイルがあれば上書きする
        await self._save_dated_note(JOURNAL_FOLDER, filename, body)

        # インデックスを更新
        meta["title"] = title
        meta["ticker"] = ticker
        meta["action"] = action
        meta["emotion"] = emotion
        meta["updated_at"] = now_iso
        await self._write_json_file(JOURNAL_FOLDER, JOURNAL_INDEX_FILE, index)
        return {"ok": True, "filename": filename}

    async def journal_delete(self, filename: str) -> dict:
        """日記を削除する。Driveファイルをゴミ箱に移動し、インデックスからも除去。"""
        if not filename:
            return {"ok": False, "error": "filename が空です"}
        index = await self._read_json_file(
            JOURNAL_FOLDER, JOURNAL_INDEX_FILE, default=[]
        )
        meta = next((it for it in index if it.get("filename") == filename), None)
        if not meta:
            return {"ok": False, "error": "日記が見つかりません"}

        # Driveファイル削除（ゴミ箱）
        if self.drive_service:
            service = self.drive_service.get_service()
            if service:
                inv_id = await self._get_investment_folder(service)
                sub_id = await self.drive_service.find_file(
                    service, inv_id, JOURNAL_FOLDER
                )
                if sub_id:
                    f_id = await self.drive_service.find_file(
                        service, sub_id, filename
                    )
                    if f_id:
                        try:
                            await self.drive_service.delete_file(service, f_id)
                        except Exception as e:
                            logging.warning(f"journal_delete: drive削除失敗 {filename}: {e}")

        # インデックスから除去
        new_index = [it for it in index if it.get("filename") != filename]
        await self._write_json_file(JOURNAL_FOLDER, JOURNAL_INDEX_FILE, new_index)
        return {"ok": True}

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
        result = await self._gemini_plain(prompt, feature_key="investment_journal")
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
        raw = await self._gemini_with_search(prompt, feature_key="investment_review")
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
        result = await self._gemini_with_search(prompt, feature_key="investment_peer")
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
        yesterday = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        prompt = PROMPT_NEWS_SENTIMENT.format(ticker=code, market=market, yesterday=yesterday)
        result = await self._gemini_with_search(prompt, feature_key="investment_news")
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
        raw = await self._gemini_with_search(prompt, feature_key="investment_dividend")
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
        result = await self._gemini_plain(prompt, feature_key="investment_review")
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
        raw = await self._gemini_with_search(market_data_prompt, feature_key="investment_risk")
        market_obj = self._extract_json(raw) or {"data": []}
        market_data_str = json.dumps(market_obj, ensure_ascii=False, indent=2)

        prompt = PROMPT_RISK_ASSESSMENT.format(
            constitution_position_rules=pos_section,
            holdings_json=json.dumps(holdings, ensure_ascii=False, indent=2),
            market_data=market_data_str,
        )
        report = await self._gemini_plain(prompt, feature_key="investment_risk")
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
