import asyncio
import datetime
import json
import logging
import random

from discord.ext import commands, tasks

from config import JST

# 朝のMITとして登録する質問テキスト（インライン回答欄のラベルに使われる）
_MIT_QUESTION_TEXT = "今朝のMIT候補。回答欄で編集（1行に1つ）して回答すると今日のMITに確定するよ。"


class MorningMitCog(commands.Cog):
    """毎朝 6:30 JST に、その日のカレンダー予定と昨日の MIT 進捗からマネージャーが
    MIT 候補 3 件を提案する。ユーザーは候補を編集して確定できる。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_run_date = None
        self.morning_mit_loop.start()

    def cog_unload(self):
        self.morning_mit_loop.cancel()

    @tasks.loop(minutes=1)
    async def morning_mit_loop(self):
        from services.schedule_resolver import is_enabled
        now = datetime.datetime.now(JST)
        today = now.date()
        # 1日1回。既に実行済みならスキップ
        if self._last_run_date == today:
            return
        # 06:30〜10:00 の時間窓で未実行なら実行する。
        # 厳密に「06:30 ちょうど」ではなく窓で判定することで、
        # Bot が 06:30 に停止していて少し後に起動しても取りこぼさない。
        now_minutes = now.hour * 60 + now.minute
        if not (6 * 60 + 30 <= now_minutes < 10 * 60):
            return
        if not await is_enabled("morning_mit"):
            return
        self._last_run_date = today
        # 一斉スパムにならないよう 0〜90 秒のジッタ
        await asyncio.sleep(random.randint(0, 90))
        await self._run()

    @morning_mit_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    async def _run(self):
        try:
            from api.database import add_daily_question, get_questions_by_date
            from services import cost_meter_service
        except Exception as e:
            logging.error(f"MorningMitCog import error: {e}")
            return

        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")

        # 既に今日の朝の MIT 質問が登録済みなら何もしない
        existing = await get_questions_by_date(today_str, scope='morning_mit')
        if existing:
            logging.info("MorningMitCog: 朝の MIT 候補は既に登録済み")
            return

        # 既に今日の MIT が DailyNote に設定済みなら、候補は出さない
        if await self._mit_already_set(today_str):
            logging.info("MorningMitCog: 今日の MIT は設定済みのため候補提案をスキップ")
            return

        # コスト閾値を超過している場合は AI 呼び出しをスキップ（頻度調整）
        throttled = False
        try:
            throttled = await cost_meter_service.should_throttle_heavy_tasks()
        except Exception as e:
            logging.debug(f"MorningMitCog: throttle check failed: {e}")

        if throttled:
            logging.info("MorningMitCog: 月額API閾値を超過しているため当日の AI 候補生成をスキップ")
            # カレンダー予定のみの簡易候補にフォールバック（無ければ空のまま）
            candidates = await self._build_calendar_only_candidates(today_str)
        else:
            # カレンダー予定が無ければ無理に候補を作らず、空のままユーザーに回答させる
            candidates = await self._build_mit_candidates(today_str)

        # 候補リストを context フィールドに JSON で格納（フロントが取り出して編集用に表示）
        context_payload = json.dumps(
            {"candidates": candidates, "throttled": throttled}, ensure_ascii=False
        )
        try:
            await add_daily_question(today_str, _MIT_QUESTION_TEXT, scope='morning_mit', context=context_payload)
        except Exception as e:
            logging.error(f"MorningMitCog: DB保存エラー: {e}")
            return

        # 夜の振り返りと同じく、チャットへ回答欄付きメッセージを送る
        await self._announce_in_chat(today_str, candidates, throttled=throttled)

    async def _mit_already_set(self, date_str: str) -> bool:
        """指定日の DailyNote の MIT セクションに、既にタスク行が書かれているか判定する。"""
        try:
            section = await self._read_mit_section(date_str)
        except Exception:
            return False
        if not section:
            return False
        # `- [ ] 実際のタスク文` のように、チェックボックス＋本文がある行が
        # 1つでもあれば設定済みとみなす（空テンプレ行 `- [ ]` のみは未設定扱い）。
        import re as _re
        for line in section.splitlines():
            if _re.match(r"^\s*[-*]\s*\[[ xX]\]\s*\S", line):
                return True
        return False

    async def _announce_in_chat(self, date_str: str, candidates: list[str], throttled: bool):
        """今朝のMIT候補をチャットへ送信する。末尾の [QUESTIONS:...] マーカーにより
        フロント側でインライン回答欄（夜の振り返りと同じUI）が描画される。"""
        try:
            from api.notification_service import save_message_and_notify
        except Exception as e:
            logging.error(f"MorningMitCog: import error: {e}")
            return
        if candidates:
            if throttled:
                head = "☀️ おはよう、ゆうすけ！今朝のMIT候補だよ（コスト節約モードでカレンダーから自動抽出したよ）。"
            else:
                head = "☀️ おはよう、ゆうすけ！今朝のMIT候補を考えてみたよ。"
            lines = [head]
            for i, c in enumerate(candidates, 1):
                lines.append(f"{i}. {c}")
            lines.append("")
            lines.append("下の回答欄で自由に編集（1行に1つ）して『回答』を押してね。そのまま今日のMITとして確定するよ💪")
        else:
            # カレンダーに予定がない等で候補なし。ユーザー自身に書いてもらう
            lines = [
                "☀️ おはよう、ゆうすけ！今日のMITを決めよう。",
                "今日は特に提案できる候補がなかったよ。下の回答欄に今日のMITを書いて（1行に1つ）『回答』を押してね💪",
            ]
        lines.append(f"[QUESTIONS:morning_mit:{date_str}]")
        try:
            await save_message_and_notify(
                "assistant", "\n".join(lines), title="☀️ 今朝のMIT候補", proactive=True,
            )
        except Exception as e:
            logging.error(f"MorningMitCog: chat送信エラー: {e}")

    async def _build_calendar_only_candidates(self, date_str: str) -> list[str]:
        """AI を使わず、カレンダー予定のタイトルだけから MIT 候補 3 件を作る（節約モード用）。"""
        cal_service = getattr(self.bot, "calendar_service", None)
        if not cal_service or not hasattr(cal_service, "get_raw_events_for_date"):
            return []
        try:
            events = await cal_service.get_raw_events_for_date(date_str)
        except Exception:
            return []
        cands = []
        for ev in events or []:
            title = (ev.get("summary") or "").strip()
            if not title or title == "(タイトルなし)":
                continue
            cands.append(title[:60])
            if len(cands) >= 3:
                break
        return cands

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
        # カレンダーに予定が無ければ無理に候補を作らず、ユーザー自身に
        # MIT を回答してもらう（AI 呼び出しもスキップ）。
        if not cal_lines:
            logging.info("MorningMitCog: 今日のカレンダー予定が無いため候補生成をスキップ")
            return []
        cal_text = "\n".join(cal_lines)

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
            from services.gemini_model_resolver import resolve_gemini_model
            _m = await resolve_gemini_model("routines", default_pro=False)
            response = await gemini_client.aio.models.generate_content(
                model=_m,
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
