import os
import logging
import datetime
import json
import re

from discord.ext import commands, tasks
from google.genai import types

from config import JST
from utils.obsidian_utils import update_section, update_frontmatter
from prompts import PROMPT_DAILY_ORGANIZE
from services.info_service import InfoService
from api.database import get_history


class DailyOrganizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client
        self.tasks_service = getattr(bot, "tasks_service", None)
        self.info_service = getattr(bot, "info_service", InfoService())

        self.daily_organize_task.start()

    def cog_unload(self):
        self.daily_organize_task.cancel()

    @tasks.loop(time=datetime.time(hour=23, minute=55, tzinfo=JST))
    async def daily_organize_task(self):
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return

        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")

        current_tasks_text = "タスクAPIに接続されていません。"
        if self.tasks_service:
            current_tasks_text = await self.tasks_service.get_uncompleted_tasks()

        log_text = await partner_cog.fetch_todays_chat_log()

        weather_res = await self.info_service.get_weather()
        weather = weather_res.get("summary", "取得失敗")
        max_t = weather_res.get("max_temp", "N/A")
        min_t = weather_res.get("min_temp", "N/A")

        location_log_text = "（記録なし）"
        service = self.drive_service.get_service()
        if service:
            daily_folder = await self.drive_service.find_file(
                service, self.drive_folder_id, "DailyNotes"
            )
            if daily_folder:
                daily_file = await self.drive_service.find_file(
                    service, daily_folder, f"{today_str}.md"
                )
                if daily_file:
                    try:
                        raw_content = await self.drive_service.read_text_file(
                            service, daily_file
                        )
                        match = re.search(
                            r"## 📍 Location History\n(.*?)(?=\n## |\Z)",
                            raw_content,
                            re.DOTALL,
                        )
                        if match and match.group(1).strip():
                            location_log_text = match.group(1).strip()
                    except Exception as e:
                        logging.error(f"DailyOrganize: Location read error: {e}")

        result = {
            "journal": "",
            "insights": [],
            "next_actions": [],
            "message": "（今日の会話とデータをノートにまとめたよ🌙 おやすみ！）",
        }

        if log_text.strip():
            prompt = f"{PROMPT_DAILY_ORGANIZE}\n【現在の未完了タスク】\n{current_tasks_text}\n\n【今日の移動記録】\n{location_log_text}\n\n--- Chat Log ---\n{log_text}"
            try:
                if self.gemini_client:
                    from services.gemini_model_resolver import resolve_gemini_model
                    _m = await resolve_gemini_model("routines", default_pro=True)
                    response = await self.gemini_client.aio.models.generate_content(
                        model=_m,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json"
                        ),
                    )
                    res_data = json.loads(response.text)
                    result.update(res_data)
            except Exception as e:
                logging.error(f"DailyOrganize: JSON Error: {e}")

        result["meta"] = {
            "weather": f'"{weather}"' if weather != "取得失敗" else "取得失敗",
            "temp_max": f'"{max_t}"' if max_t != "N/A" else "N/A",
            "temp_min": f'"{min_t}"' if min_t != "N/A" else "N/A",
        }

        try:
            timeline_md = await self._build_integrated_timeline(today_str, location_log_text)
            if timeline_md:
                result["timeline"] = timeline_md
        except Exception as e:
            logging.error(f"DailyOrganize: timeline build error: {e}")

        await self._execute_organization(result, today_str)

        if result.get("next_actions") and self.tasks_service:
            for act_data in result["next_actions"]:
                if isinstance(act_data, str):
                    act_title = re.sub(r"^-\s*", "", act_data).strip()
                    list_name = None
                elif isinstance(act_data, dict):
                    act_title = act_data.get("title", "").strip()
                    list_name = act_data.get("list")
                else:
                    continue

                if act_title:
                    try:
                        await self.tasks_service.add_task(
                            title=act_title, list_name=list_name
                        )
                    except Exception as e:
                        logging.error(f"Google Tasks自動登録エラー: {e}")

        send_msg = result.get(
            "message",
            "（今日の会話とデータをノートにまとめたよ🌙 今日も一日お疲れ様、おやすみ！）",
        )
        try:
            from api.notification_service import save_message_and_notify as _save_msg
            await _save_msg("assistant", send_msg)
        except Exception:
            pass

    async def _execute_organization(self, data, date_str):
        service = self.drive_service.get_service()
        if not service:
            return

        daily_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, "DailyNotes"
        )
        if not daily_folder:
            daily_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, "DailyNotes"
            )

        f_id = await self.drive_service.find_file(
            service, daily_folder, f"{date_str}.md"
        )

        content = f"# Daily Note {date_str}\n"
        if f_id:
            try:
                raw_content = await self.drive_service.read_text_file(service, f_id)
                if raw_content:
                    content = raw_content
            except Exception:
                pass

        meta = data.get("meta", {})
        updates_fm = {"date": date_str}
        if meta.get("weather") != "N/A":
            updates_fm["weather"] = meta.get("weather")
        if meta.get("temp_max") != "N/A":
            updates_fm["temp_max"] = meta.get("temp_max")
        if meta.get("temp_min") != "N/A":
            updates_fm["temp_min"] = meta.get("temp_min")
        content = update_frontmatter(content, updates_fm)

        if data.get("timeline"):
            # Daily Log（旧 Daily Timeline）は毎晩再生成して全置換するため、既存セクションを除去してから挿入
            # 旧見出しが残っていた場合も合わせて掃除する
            content = re.sub(
                r"##\s*(?:⏱\s*Daily Timeline|📋\s*Daily Log)\n.*?(?=\n## |\Z)",
                "",
                content,
                flags=re.DOTALL,
            )
            content = update_section(content, data["timeline"], "## 📋 Daily Log")
        if data.get("journal"):
            content = update_section(content, data["journal"], "## 📔 Daily Journal")
        # events は Lifelog（ユーザーが log_life_activity で記録する事実）に集約。
        # AI による events 補完はプロンプトから外したため、ここでは書き込まない。
        if data.get("insights") and len(data["insights"]) > 0:
            content = update_section(
                content,
                "\n".join(data["insights"])
                if isinstance(data["insights"], list)
                else str(data["insights"]),
                "## 💡 Insights & Thoughts",
            )

        if data.get("next_actions") and len(data["next_actions"]) > 0:
            formatted_actions = []
            for act in data["next_actions"]:
                if isinstance(act, str):
                    formatted_actions.append(act if act.startswith("-") else f"- {act}")
                elif isinstance(act, dict):
                    title = act.get("title", "")
                    lst = act.get("list", "")
                    prefix = f"[{lst}] " if lst else ""
                    formatted_actions.append(f"- {prefix}{title}")
            content = update_section(
                content, "\n".join(formatted_actions), "## 🚀 Next Actions"
            )

        if f_id:
            await self.drive_service.update_text(service, f_id, content)
        else:
            await self.drive_service.upload_text(
                service, daily_folder, f"{date_str}.md", content
            )


    # 時間帯区分（HH:MM, 区分名）。終端は次の区分の開始時刻。
    _PERIOD_BUCKETS = [
        ("00:00", "06:00", "早朝"),
        ("06:00", "11:00", "午前"),
        ("11:00", "13:00", "昼"),
        ("13:00", "17:00", "午後"),
        ("17:00", "19:00", "夕方"),
        ("19:00", "24:00", "夜"),
    ]

    async def _summarize_user_messages_per_period(self, date_str: str) -> list:
        """ユーザー発言を時間帯ごとに集約して Gemini で1〜3行に要約する。
        会話原文は ## 💬 Chat Log セクションに保存されるためここでは要約のみ作る。
        戻り値: [(start_HHMM, end_HHMM, bullets_list), ...]
        bullets_list は「だ・である」調の短い箇条書き（時間帯ラベル不要）。
        """
        try:
            history = await get_history(limit=500)
        except Exception as e:
            logging.error(f"timeline: history fetch error: {e}")
            return []

        # 時間帯別にユーザー発言を集める
        buckets = {b[2]: [] for b in self._PERIOD_BUCKETS}
        for m in history:
            ts = m.get("timestamp") or ""
            if not ts.startswith(date_str):
                continue
            if m.get("role") != "user":
                continue
            content = (m.get("content") or "").strip()
            if not content:
                continue
            try:
                dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=JST)
                hhmm = dt.astimezone(JST).strftime("%H:%M")
            except Exception:
                continue
            for start, end, name in self._PERIOD_BUCKETS:
                if start <= hhmm < end:
                    buckets[name].append((hhmm, content))
                    break

        results = []
        for start, end, name in self._PERIOD_BUCKETS:
            msgs = buckets.get(name, [])
            if not msgs:
                continue
            joined = "\n".join(f"[{t}] {c}" for t, c in msgs)
            bullets = await self._summarize_period_messages(name, joined)
            if not bullets:
                continue
            results.append((start, end, bullets))
        return results

    async def _summarize_period_messages(self, period_name: str, joined: str) -> str:
        """時間帯のユーザー発言テキストから1〜3行の客観的トピック要約を「だ・である」調で作る。
        Gemini が使えない場合は最初の発言を短縮して返す。"""
        if not self.gemini_client:
            first = joined.splitlines()[0] if joined.splitlines() else joined
            first = first.split("] ", 1)[-1]
            return first[:60]
        prompt = (
            f"次は{period_name}の時間帯にユーザーが送信したチャット発言です。\n"
            "発言原文は別に保存されているので、ここでは要約だけ作ってください。\n"
            "次のルールで1〜3行のmarkdown箇条書き内に収まる短い行動・話題の要約を書きます。\n"
            "- 各行は「- 」で始め、30文字程度に収める\n"
            "- 文体は必ず「だ・である」調（『〜した』『〜だ』『〜である』など）。タメ口や敬語は禁止\n"
            "- 体言止め（『投資の相談』『夕食メニュー』など名詞だけで終わる形）は禁止\n"
            "- 朝/昼/夜などの時間帯ラベルを文中に含めない\n"
            "- 感情語や評価語を含めず、客観的事実のみ\n"
            "- 同種の話題はまとめて1行\n"
            "- 説明や前置きは書かず、箇条書きだけを返す\n\n"
            f"--- 発言ログ ---\n{joined}\n--- 終わり ---"
        )
        try:
            from services.gemini_model_resolver import resolve_gemini_model
            _m = await resolve_gemini_model("routines", default_pro=False)
            response = await self.gemini_client.aio.models.generate_content(
                model=_m,
                contents=prompt,
            )
            text = (response.text or "").strip()
        except Exception as e:
            logging.error(f"timeline: summary error ({period_name}): {e}")
            return ""
        # 最大3行に制限し、・/-/数字始まりの行のみ拾う
        bullets = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            stripped = line.lstrip("・-*0123456789.)） ").strip()
            if not stripped:
                continue
            bullets.append(stripped)
            if len(bullets) >= 3:
                break
        return bullets

    async def _build_integrated_timeline(self, date_str: str, location_log_text: str) -> str:
        """客観データ（睡眠・天気・カレンダー・ライフログ・ロケーション）と
        ユーザー発言の時間帯要約を統合し、時系列順のmarkdownを返す。
        会話原文は ## 💬 Chat Log セクションに別途保存されるためここには含めない。
        """
        events = []  # list[(time_str_HHMM, icon, line)]

        # --- 1) 睡眠データ（起床ポイント・活動量サマリ） ---
        try:
            fitbit_cog = self.bot.get_cog("FitbitCog")
            if fitbit_cog and getattr(fitbit_cog, "fitbit_service", None):
                try:
                    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                    stats = await fitbit_cog.fitbit_service.get_stats(target_date)
                except Exception:
                    stats = None
                if stats:
                    score = stats.get("sleep_score", "N/A")
                    total = stats.get("total_sleep_minutes", 0)
                    if total:
                        h, m = divmod(int(total), 60)
                        sleep_text = f"{h}h{m:02d}m"
                    else:
                        sleep_text = "N/A"
                    steps = stats.get("steps", 0)
                    rhr = stats.get("resting_heart_rate", "N/A")
                    events.append((
                        "06:00",
                        "🌙",
                        f"起床（昨夜の睡眠 スコア:{score} / {sleep_text}）",
                    ))
                    if steps:
                        events.append((
                            "23:50",
                            "👟",
                            f"本日の活動: {steps:,}歩 / 安静時心拍 {rhr}",
                        ))
        except Exception as e:
            logging.error(f"timeline: fitbit fetch error: {e}")

        # --- 2) 天気（朝・昼・夜の概況） ---
        try:
            if self.info_service:
                weather_res = await self.info_service.get_weather()
                summary = weather_res.get("summary")
                if summary and summary != "取得失敗":
                    max_t = weather_res.get("max_temp", "")
                    min_t = weather_res.get("min_temp", "")
                    detail = summary
                    if max_t and min_t:
                        detail = f"{summary} / 最高{max_t}・最低{min_t}"
                    events.append(("07:00", "🌤", detail))
        except Exception as e:
            logging.error(f"timeline: weather fetch error: {e}")

        # --- 3) カレンダー予定 ---
        try:
            cal_service = getattr(self.bot, "calendar_service", None)
            if cal_service and hasattr(cal_service, "get_raw_events_for_date"):
                cal_events = await cal_service.get_raw_events_for_date(date_str)
                for ev in cal_events or []:
                    t = ev.get("time", "終日")
                    summary = ev.get("summary", "(タイトルなし)")
                    sort_key = t if t and t != "終日" else "00:01"
                    events.append((sort_key, "📅", f"{t} {summary}"))
        except Exception as e:
            logging.error(f"timeline: calendar fetch error: {e}")

        # --- 4) ロケーションログ（行単位で時刻抽出） ---
        try:
            for line in (location_log_text or "").splitlines():
                line = line.strip().lstrip("-").strip()
                if not line:
                    continue
                m = re.search(r"(\d{1,2}):(\d{2})", line)
                if m:
                    hh = int(m.group(1)); mm = int(m.group(2))
                    hhmm = f"{hh:02d}:{mm:02d}"
                    events.append((hhmm, "📍", line))
        except Exception as e:
            logging.error(f"timeline: location parse error: {e}")

        # --- 5) ライフログ（既存 Lifelog / Tasks セクションから時刻付き行を取り込み） ---
        try:
            service = self.drive_service.get_service()
            if service:
                daily_folder = await self.drive_service.find_file(
                    service, self.drive_folder_id, "DailyNotes"
                )
                if daily_folder:
                    f_id = await self.drive_service.find_file(
                        service, daily_folder, f"{date_str}.md"
                    )
                    if f_id:
                        raw = await self.drive_service.read_text_file(service, f_id)
                        for sec_header, icon in [
                            ("## 🪟 Lifelog", "🪟"),
                            ("## 🎯 Tasks", "🎯"),
                        ]:
                            mm = re.search(
                                rf"{re.escape(sec_header)}\n(.*?)(?=\n## |\Z)",
                                raw,
                                re.DOTALL,
                            )
                            if not mm:
                                continue
                            for line in mm.group(1).splitlines():
                                line_s = line.strip().lstrip("-").strip()
                                if not line_s:
                                    continue
                                tm = re.search(r"(\d{1,2}):(\d{2})", line_s)
                                if not tm:
                                    continue
                                hh = int(tm.group(1)); mn = int(tm.group(2))
                                events.append((f"{hh:02d}:{mn:02d}", icon, line_s))
        except Exception as e:
            logging.error(f"timeline: lifelog re-import error: {e}")

        # --- 6) ユーザー発言の時間帯別要約（タイムラインの下に別ブロックで配置） ---
        period_bullets = []
        try:
            period_summaries = await self._summarize_user_messages_per_period(date_str)
            # 時系列順（早朝→夜）にフラット化。朝/昼/夜などのラベルは付けない
            for _start, _end, bullets in period_summaries:
                for b in bullets:
                    period_bullets.append(b)
        except Exception as e:
            logging.error(f"timeline: user summary error: {e}")

        if not events and not period_bullets:
            return ""

        events.sort(key=lambda x: x[0])
        lines = [f"- {t} {icon} {text}" for t, icon, text in events]
        if period_bullets:
            # タイムラインの直下に、時間帯ラベルを付けずに「だ・である」調の箇条書きを並べる
            lines.extend(f"- {b}" for b in period_bullets)
        return "\n".join(lines)

    @daily_organize_task.before_loop
    async def before_daily_organize(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyOrganizeCog(bot))
