import asyncio
import datetime
import json
import logging
import random

from discord.ext import commands, tasks

from config import JST


class MorningMitCog(commands.Cog):
    """毎朝 6:30 JST に、その日のカレンダー予定と昨日の MIT 進捗からマネージャーが
    MIT 候補 3 件を提案する。ユーザーは候補を編集して確定できる。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.morning_mit_loop.start()

    def cog_unload(self):
        self.morning_mit_loop.cancel()

    @tasks.loop(time=datetime.time(hour=6, minute=30, tzinfo=JST))
    async def morning_mit_loop(self):
        # 一斉スパムにならないよう 0〜90 秒のジッタ
        await asyncio.sleep(random.randint(0, 90))
        await self._run()

    @morning_mit_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    async def _run(self):
        try:
            from api.database import add_daily_question, get_questions_by_date
            from api.notification_service import send_push
        except Exception as e:
            logging.error(f"MorningMitCog import error: {e}")
            return

        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")

        # 既に今日の朝の MIT 質問が登録済みなら何もしない
        existing = await get_questions_by_date(today_str, scope='morning_mit')
        if existing:
            logging.info("MorningMitCog: 朝の MIT 候補は既に登録済み")
            return

        candidates = await self._build_mit_candidates(today_str)
        if not candidates:
            logging.info("MorningMitCog: MIT 候補を生成できなかったためスキップ")
            return

        # 候補リストを context フィールドに JSON で格納（フロントが取り出して編集用に表示）
        question_text = "今朝のMIT候補を 3 つ用意したよ。アプリで編集・確定してね！"
        context_payload = json.dumps({"candidates": candidates}, ensure_ascii=False)
        try:
            await add_daily_question(today_str, question_text, scope='morning_mit', context=context_payload)
        except Exception as e:
            logging.error(f"MorningMitCog: DB保存エラー: {e}")
            return

        # Web プッシュで通知（タップで /?openMorningMit=1 のような遷移を期待）
        try:
            await send_push(
                title="☀️ 今朝のMIT候補",
                body="マネージャーから今日のMIT候補が届いたよ。タップして編集・確定。",
                url="/?openMorningMit=1",
            )
        except Exception as e:
            logging.error(f"MorningMitCog: push送信エラー: {e}")

    async def _build_mit_candidates(self, date_str: str) -> list[str]:
        """Google Calendar・前日 MIT 進捗・直近会話を元に Gemini に MIT 候補 3 件を提案させる。"""
        # 1) 今日のカレンダー予定
        cal_lines = []
        cal_service = getattr(self.bot, "calendar_service", None)
        if cal_service and hasattr(cal_service, "get_raw_events_for_date"):
            try:
                events = await cal_service.get_raw_events_for_date(date_str)
                for ev in events or []:
                    t = ev.get("time", "終日")
                    s = ev.get("summary", "(無題)")
                    cal_lines.append(f"- {t} {s}")
            except Exception as e:
                logging.debug(f"MorningMitCog: calendar error: {e}")
        cal_text = "\n".join(cal_lines) if cal_lines else "（予定なし）"

        # 2) 昨日の MIT 進捗（Daily Note の `## 🎯 MIT` セクション）
        yesterday = (datetime.datetime.strptime(date_str, "%Y-%m-%d") - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_mit = await self._read_mit_section(yesterday)

        # 3) Gemini にプロンプト
        gemini_client = getattr(self.bot, "gemini_client", None)
        if not gemini_client:
            # AI 非接続の場合: カレンダーをそのまま MIT 候補にフォールバック
            fallback = [line[2:].strip() for line in cal_lines[:3]] if cal_lines else []
            return fallback

        prompt = (
            "あなたはユーザー専属のマネージャーです。今日達成すべき MIT（Most Important Task）の候補を **3 件** 提案してください。\n\n"
            f"## 今日（{date_str}）のカレンダー予定\n{cal_text}\n\n"
            f"## 昨日（{yesterday}）の MIT 進捗\n{yesterday_mit or '（記録なし）'}\n\n"
            "【ルール】\n"
            "- MIT 1 件あたり 30 文字以内、行動が明確な動詞句で書く\n"
            "- カレンダー予定の中で「重要そうな会議」「準備が必要なタスク」を優先\n"
            "- 昨日未達のものは候補に含める（- [ ] のままだったタスクが優先）\n"
            "- 抽象的すぎる表現（『頑張る』『集中する』など）は避ける\n"
            "- 出力は JSON のみ：{\"mit\": [\"候補1\", \"候補2\", \"候補3\"]} 形式\n"
        )

        try:
            from google.genai import types as _gt
            response = await gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=_gt.GenerateContentConfig(response_mime_type="application/json"),
            )
            data = json.loads(response.text)
            items = data.get("mit") or []
            return [str(s).strip()[:60] for s in items if str(s).strip()][:3]
        except Exception as e:
            logging.error(f"MorningMitCog: Gemini error: {e}")
            return []

    async def _read_mit_section(self, date_str: str) -> str:
        """指定日の Daily Note から `## 🎯 MIT` セクション本文を読み取って返す。"""
        try:
            from api import app
            chat_service = getattr(app.state, "chat_service", None)
            if not chat_service or not chat_service.drive_service:
                return ""
            service = chat_service.drive_service.get_service()
            folder_id = await chat_service.drive_service.find_file(
                service, chat_service.drive_folder_id, "DailyNotes"
            )
            if not folder_id:
                return ""
            f_id = await chat_service.drive_service.find_file(service, folder_id, f"{date_str}.md")
            if not f_id:
                return ""
            content = await chat_service.drive_service.read_text_file(service, f_id)
            import re as _re
            m = _re.search(r"## 🎯 MIT\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
            if m:
                return m.group(1).strip()
        except Exception as e:
            logging.debug(f"MorningMitCog: read MIT error: {e}")
        return ""


async def setup(bot: commands.Bot):
    await bot.add_cog(MorningMitCog(bot))
