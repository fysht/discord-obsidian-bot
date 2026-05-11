"""Gmail を 7 分間隔でポーリングし、未読メールを AI 要約 + 重要度判定して DB に蓄積する。

設計:
- `is:unread newer_than:1d` で 1 日以内の未読のみ対象。
- 既に DB に存在する ID はスキップ（再要約しない）。
- 重要度 high のみプッシュ通知。low / medium は静かに DB に保存。
- 朝の 6:00 〜 23:00 のみ動作（深夜は通知ノイズを避ける）。
- コスト閾値超過時は AI 要約をスキップして件名のみで保存（簡易フォールバック）。
"""
import asyncio
import datetime
import json
import logging

from discord.ext import commands, tasks

from config import JST


GMAIL_SUMMARY_MODEL = "gemini-2.5-flash"


class GmailWatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.gmail_loop.start()

    def cog_unload(self):
        self.gmail_loop.cancel()

    @tasks.loop(minutes=7)
    async def gmail_loop(self):
        await self._run()

    @gmail_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        # 起動直後の負荷を避けるため 30 秒待ち
        await asyncio.sleep(30)

    async def _run(self):
        try:
            now = datetime.datetime.now(JST)
            # 深夜帯はスキップ
            if now.hour < 6 or now.hour >= 23:
                return

            gmail = getattr(self.bot, "gmail_service", None)
            if not gmail or not gmail.creds:
                return

            from api.database import gmail_get, gmail_upsert, gmail_update
            from api.notification_service import send_push
            from services import cost_meter_service

            unread_meta = await gmail.list_unread(max_results=20, newer_than_days=1)
            if not unread_meta:
                return

            throttle = await cost_meter_service.should_throttle_heavy_tasks()

            for entry in unread_meta:
                mid = entry.get("id")
                if not mid:
                    continue
                if await gmail_get(mid):
                    continue  # 既に DB にあるので再要約しない

                full = await gmail.get_message(mid)
                if not full:
                    continue

                received_iso = ""
                try:
                    ts_ms = int(full.get("internal_date") or 0)
                    if ts_ms:
                        received_iso = datetime.datetime.fromtimestamp(
                            ts_ms / 1000, tz=JST
                        ).isoformat()
                except (TypeError, ValueError):
                    pass

                base_record = {
                    "id": mid,
                    "thread_id": full.get("thread_id", ""),
                    "subject": full.get("subject", "(件名なし)"),
                    "from_addr": full.get("from", ""),
                    "received_at": received_iso or datetime.datetime.now(JST).isoformat(),
                    "snippet": (full.get("snippet") or "")[:300],
                    "summary": "",
                    "importance": "medium",
                }

                # AI 要約 + 重要度判定（コスト閾値超過時はスキップ）
                summary = ""
                importance = "medium"
                if not throttle:
                    try:
                        summary, importance = await self._summarize(full)
                    except Exception as e:
                        logging.debug(f"GmailWatchCog summarize fail: {e}")

                base_record["summary"] = summary or (full.get("snippet") or "")[:120]
                base_record["importance"] = importance or "medium"

                await gmail_upsert(base_record)

                # high のみプッシュ通知
                if base_record["importance"] == "high":
                    try:
                        from_short = (base_record["from_addr"] or "").split("<")[0].strip()[:30]
                        await send_push(
                            title=f"📧 {base_record['subject'][:40]}",
                            body=(
                                f"{from_short} ・ "
                                f"{(base_record['summary'] or '')[:80]}"
                            ),
                            url="/?openInbox=1",
                        )
                        await gmail_update(mid, notified=1)
                    except Exception as e:
                        logging.debug(f"GmailWatchCog push fail: {e}")
        except Exception as e:
            logging.error(f"GmailWatchCog run error: {e}", exc_info=True)

    async def _summarize(self, msg: dict) -> tuple[str, str]:
        """件名 + 本文先頭からマネージャー口調の 2〜3 行要約 + 重要度を返す。"""
        gemini = getattr(self.bot, "gemini_client", None)
        if not gemini:
            return "", "medium"
        body_excerpt = (msg.get("body") or "")[:1500]
        prompt = (
            "あなたはユーザー専属のマネージャー（AI秘書）です。\n"
            "次の Gmail を 2〜3 行で要約し、ユーザーが今すぐ対応する必要があるかを判定してください。\n"
            "「件名」「差出人」「本文の先頭」を見て、出力は必ず以下の JSON だけ。\n\n"
            "{\n"
            '  "summary": "2〜3行（マネージャー口調・タメ口OK・改行は \\n）",\n'
            '  "importance": "high / medium / low",\n'
            '  "reason": "高/低と判断した理由を1行"\n'
            "}\n\n"
            "判定基準:\n"
            "- high: 期日のある依頼・支払い・面接/予約・金銭の動き・本人宛の重要連絡\n"
            "- medium: 普通の案内・予約確認・通常の業務連絡\n"
            "- low: メルマガ・キャンペーン・自動送信・通知のみ\n\n"
            f"件名: {msg.get('subject', '')}\n"
            f"差出人: {msg.get('from', '')}\n"
            f"日時: {msg.get('date', '')}\n"
            f"本文（先頭 1500 文字）:\n{body_excerpt}"
        )
        try:
            from google.genai import types as _gt
            response = await gemini.aio.models.generate_content(
                model=GMAIL_SUMMARY_MODEL,
                contents=prompt,
                config=_gt.GenerateContentConfig(response_mime_type="application/json"),
            )
            data = json.loads(response.text or "{}")
            summary = (data.get("summary") or "").strip()
            importance = (data.get("importance") or "medium").strip().lower()
            if importance not in ("high", "medium", "low"):
                importance = "medium"
            return summary, importance
        except Exception as e:
            logging.error(f"Gmail summarize error: {e}")
            return "", "medium"


async def setup(bot: commands.Bot):
    await bot.add_cog(GmailWatchCog(bot))
