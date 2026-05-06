"""投資サポート機能を提供するCog。

提供コマンド:
- !地合い              : 日米市場の現在の環境を分析
- !銘柄スナップ <ticker>: 指定銘柄の最新の財務指標スナップショットを取得
- !銘柄審査 <ticker>    : 投資憲法に照らして銘柄を審査
- !決算予定 <ticker>    : 次回決算発表日を調査してGoogle Calendarに登録
- !投資憲法            : 現在の投資憲法を表示
- !投資憲法初期化       : 投資憲法をサンプルで再生成（既存があれば上書きしない）

データ保存先（Googleドライブ）:
- Investment/
  - Investment_Constitution.md
  - Stocks/{code}_{企業名}.md       … 銘柄個別ノート（既存StockCogと共通）
  - Snapshots/{date}_{ticker}.md    … 銘柄スナップショット履歴
  - Audits/{date}_{ticker}.md       … 銘柄審査結果履歴
  - Sentiment/{date}.md             … 地合い分析履歴
"""
import os
import re
import json
import asyncio
import logging
import datetime

import discord
from discord.ext import commands
from google.genai import types

from config import JST
from prompts import (
    PROMPT_MARKET_SENTIMENT,
    PROMPT_STOCK_SNAPSHOT,
    PROMPT_STOCK_AUDIT,
    PROMPT_EARNINGS_SCHEDULE,
)


INVESTMENT_FOLDER = "Investment"
STOCKS_FOLDER = "Stocks"
SNAPSHOTS_FOLDER = "Snapshots"
AUDITS_FOLDER = "Audits"
SENTIMENT_FOLDER = "Sentiment"
CONSTITUTION_FILE = "Investment_Constitution.md"

GEMINI_MODEL = "gemini-2.5-pro"

DISCORD_CHUNK_SIZE = 1900  # Discordの2000文字制限から余裕を見た値


# 投資憲法サンプル（初回起動時にDriveに作成される）
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
    戻り値: ("JP" or "US", 正規化後のティッカー)
    """
    t = ticker.strip().upper()
    # 4桁数字 (+ オプションで .T) は日本株
    m = re.match(r"^(\d{4})(\.T)?$", t)
    if m:
        return "JP", m.group(1)
    return "US", t


class InvestmentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_service = bot.drive_service
        self.calendar_service = bot.calendar_service
        self.gemini_client = bot.gemini_client
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    async def cog_load(self):
        # サンプル投資憲法をバックグラウンドで作成
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
                return  # 既存はそのまま
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

    async def _save_dated_note(
        self, subfolder: str, filename: str, content: str
    ):
        """Investment/{subfolder}/{filename} に保存。"""
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

    # ==========================================================
    # Gemini呼び出しヘルパー
    # ==========================================================

    async def _gemini_with_search(self, prompt: str, model: str = GEMINI_MODEL) -> str:
        """Google検索Groundingを有効にしてGeminiに問い合わせる。"""
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
        """Groundingなしの純粋な推論。"""
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

    # ==========================================================
    # Discordユーティリティ
    # ==========================================================

    async def _send_long(self, ctx: commands.Context, text: str):
        for chunk in _split_for_discord(text):
            await ctx.send(chunk)

    # ==========================================================
    # コマンド: !地合い
    # ==========================================================

    @commands.command(name="地合い", aliases=["sentiment", "market"])
    async def market_sentiment(self, ctx: commands.Context):
        """日米市場の現在の地合いを分析する。"""
        if not self.gemini_client:
            await ctx.send("Geminiクライアントが未設定のため使えません。")
            return

        async with ctx.typing():
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
            prompt = PROMPT_MARKET_SENTIMENT.format(date=today)
            result = await self._gemini_with_search(prompt)

            if not result:
                await ctx.send("地合い分析の取得に失敗しました。")
                return

            # Drive保存
            date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            filename = f"{date_str}.md"
            note_body = (
                f"---\ndate: {date_str}\ntags: [market, sentiment]\n---\n\n"
                + result
            )
            try:
                await self._save_dated_note(SENTIMENT_FOLDER, filename, note_body)
            except Exception as e:
                logging.error(f"地合いノート保存エラー: {e}")

            await self._send_long(ctx, result)

    # ==========================================================
    # コマンド: !銘柄スナップ
    # ==========================================================

    @commands.command(name="銘柄スナップ", aliases=["snapshot", "snap"])
    async def stock_snapshot(self, ctx: commands.Context, ticker: str = None):
        """指定銘柄の最新財務指標を取得して保存する。"""
        if not ticker:
            await ctx.send("使い方: `!銘柄スナップ <ティッカー>` 例: `!銘柄スナップ 7203` / `!銘柄スナップ AAPL`")
            return
        if not self.gemini_client:
            await ctx.send("Geminiクライアントが未設定のため使えません。")
            return

        market, code = _resolve_market(ticker)
        async with ctx.typing():
            now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
            prompt = PROMPT_STOCK_SNAPSHOT.format(
                ticker=code, market=market, date=now_str
            )
            result = await self._gemini_with_search(prompt)
            if not result:
                await ctx.send("スナップショットの取得に失敗しました。")
                return

            # スナップショット履歴を保存
            date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            snapshot_filename = f"{date_str}_{code}.md"
            note_body = (
                f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
                f"tags: [investment, snapshot]\n---\n\n{result}"
            )
            try:
                await self._save_dated_note(
                    SNAPSHOTS_FOLDER, snapshot_filename, note_body
                )
            except Exception as e:
                logging.error(f"スナップショット保存エラー: {e}")

            await self._send_long(ctx, result)

    # ==========================================================
    # コマンド: !銘柄審査
    # ==========================================================

    @commands.command(name="銘柄審査", aliases=["audit"])
    async def stock_audit(self, ctx: commands.Context, ticker: str = None):
        """投資憲法に基づいて銘柄を審査する。"""
        if not ticker:
            await ctx.send("使い方: `!銘柄審査 <ティッカー>` 例: `!銘柄審査 7203`")
            return
        if not self.gemini_client:
            await ctx.send("Geminiクライアントが未設定のため使えません。")
            return

        market, code = _resolve_market(ticker)

        async with ctx.typing():
            # 1. 投資憲法を読み込み
            constitution = await self._read_constitution()
            if not constitution:
                await ctx.send(
                    "投資憲法が見つかりません。`!投資憲法初期化` でサンプルを作成してください。"
                )
                return

            # 2. スナップショットを取得（その場で生成）
            now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
            snap_prompt = PROMPT_STOCK_SNAPSHOT.format(
                ticker=code, market=market, date=now_str
            )
            snapshot = await self._gemini_with_search(snap_prompt)
            if not snapshot:
                await ctx.send("銘柄データの取得に失敗しました。")
                return

            # 3. 投資憲法 + スナップショットで審査
            audit_prompt = PROMPT_STOCK_AUDIT.format(
                constitution=constitution,
                snapshot=snapshot,
                ticker=code,
            )
            audit = await self._gemini_plain(audit_prompt)
            if not audit:
                await ctx.send("審査結果の生成に失敗しました。")
                return

            # 4. 結果を保存（スナップショット + 審査をまとめて記録）
            date_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            audit_filename = f"{date_str}_{code}.md"
            note_body = (
                f"---\nticker: {code}\nmarket: {market}\ndate: {date_str}\n"
                f"tags: [investment, audit]\n---\n\n"
                f"{audit}\n\n---\n\n## 📊 使用したスナップショット\n\n{snapshot}"
            )
            try:
                await self._save_dated_note(AUDITS_FOLDER, audit_filename, note_body)
            except Exception as e:
                logging.error(f"審査結果保存エラー: {e}")

            await self._send_long(ctx, audit)

    # ==========================================================
    # コマンド: !決算予定
    # ==========================================================

    @commands.command(name="決算予定", aliases=["earnings"])
    async def earnings_schedule(self, ctx: commands.Context, ticker: str = None):
        """次回決算日を調査してGoogle Calendarに登録する。"""
        if not ticker:
            await ctx.send("使い方: `!決算予定 <ティッカー>` 例: `!決算予定 AAPL`")
            return
        if not self.gemini_client:
            await ctx.send("Geminiクライアントが未設定のため使えません。")
            return
        if not self.calendar_service:
            await ctx.send("Google Calendarが未設定のため使えません。")
            return

        market, code = _resolve_market(ticker)

        async with ctx.typing():
            prompt = PROMPT_EARNINGS_SCHEDULE.format(ticker=code, market=market)
            raw = await self._gemini_with_search(prompt)
            if not raw:
                await ctx.send("決算情報の取得に失敗しました。")
                return

            data = self._extract_json(raw)
            if not data:
                await ctx.send(
                    f"決算情報のパースに失敗しました。生応答:\n```\n{raw[:1500]}\n```"
                )
                return

            company = data.get("company_name") or code
            earnings_date = data.get("next_earnings_date")
            if not earnings_date:
                await ctx.send(
                    f"{company} ({code}) の次回決算日が確認できませんでした。"
                )
                return

            # カレンダー登録（終日予定）
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

            cal_result = await self.calendar_service.create_event(
                summary=summary,
                start_time=earnings_date,
                end_time=earnings_date,
                description=description,
            )

            # 関連イベントもまとめて登録
            related_results = []
            for ev in related:
                ev_date = ev.get("date")
                ev_title = ev.get("title")
                if ev_date and ev_title:
                    r = await self.calendar_service.create_event(
                        summary=f"📅 {company} {ev_title}",
                        start_time=ev_date,
                        end_time=ev_date,
                        description=f"出典: {data.get('source', 'N/A')}",
                    )
                    related_results.append(f"- {ev_title} ({ev_date}): {r}")

            msg = (
                f"**{company} ({code})** の次回決算: **{earnings_date}** "
                f"({data.get('earnings_time', '時間帯不明')})\n"
                f"カレンダー登録結果: {cal_result}"
            )
            if related_results:
                msg += "\n" + "\n".join(related_results)
            await self._send_long(ctx, msg)

    @staticmethod
    def _extract_json(text: str):
        """LLM応答からJSONブロックを抽出する。"""
        if not text:
            return None
        # ```json ... ``` の中身を優先
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 最初の { から最後の } まで貪欲に
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    # ==========================================================
    # コマンド: !投資憲法
    # ==========================================================

    @commands.command(name="投資憲法", aliases=["constitution"])
    async def show_constitution(self, ctx: commands.Context):
        """現在の投資憲法を表示する。"""
        constitution = await self._read_constitution()
        if not constitution:
            await ctx.send(
                "投資憲法が見つかりません。`!投資憲法初期化` でサンプルを作成できます。"
            )
            return
        await self._send_long(ctx, constitution)

    @commands.command(name="投資憲法初期化", aliases=["init_constitution"])
    async def init_constitution(self, ctx: commands.Context):
        """投資憲法サンプルを作成する（既存があれば上書きしない）。"""
        if not self.drive_service:
            await ctx.send("Driveサービスが未設定です。")
            return
        existing = await self._read_constitution()
        if existing:
            await ctx.send(
                "投資憲法は既に存在します。上書きしません。編集はDrive上で直接行ってください。"
            )
            return
        await self._ensure_constitution_exists()
        await ctx.send(
            f"投資憲法サンプルを作成しました: `Investment/{CONSTITUTION_FILE}`"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(InvestmentCog(bot))
