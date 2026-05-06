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
                    response = await self.gemini_client.aio.models.generate_content(
                        model="gemini-2.5-pro",
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
            # タイムラインは毎晩再生成して全置換するため、既存セクションを除去してから挿入
            content = re.sub(
                r"## ⏱ Daily Timeline\n.*?(?=\n## |\Z)",
                "",
                content,
                flags=re.DOTALL,
            )
            content = update_section(content, data["timeline"], "## ⏱ Daily Timeline")
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


    async def _build_integrated_timeline(self, date_str: str, location_log_text: str) -> str:
        """カレンダー予定・チャット会話・睡眠・歩数・ロケーションログを統合し、
        時系列順のmarkdownを返す。既存の各セクション記述は維持される（このメソッドは追加生成のみ）。
        """
        events = []  # list[(time_str_HHMM, icon, line)]

        # --- 1) 睡眠データ（起床ポイント） ---
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
                    # 起床は時刻不明だが先頭に置きたいので 06:00 仮置き
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

        # --- 2) カレンダー予定 ---
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

        # --- 3) チャット会話（マネージャーとの会話） ---
        try:
            history = await get_history(limit=500)
            for m in history:
                ts = m.get("timestamp") or ""
                if not ts.startswith(date_str):
                    continue
                try:
                    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=JST)
                    hhmm = dt.astimezone(JST).strftime("%H:%M")
                except Exception:
                    hhmm = ts[11:16] if len(ts) >= 16 else "00:00"
                role = m.get("role")
                tag = "🧑" if role == "user" else "🤖"
                content = (m.get("content") or "").replace("\n", " ").strip()
                if len(content) > 80:
                    content = content[:77] + "…"
                if not content:
                    continue
                events.append((hhmm, tag, content))
        except Exception as e:
            logging.error(f"timeline: history fetch error: {e}")

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

        # --- 5) ライフログ（タスク開始/終了など Lifelog セクションがあれば取り込み） ---
        # 既存ノートの「## 🪟 Lifelog」内の時刻付き行を追加
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
                            ("## 💬 Timeline", "💬"),
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

        if not events:
            return ""

        events.sort(key=lambda x: x[0])
        lines = [f"- `{t}` {icon} {text}" for t, icon, text in events]
        return "\n".join(lines)

    @daily_organize_task.before_loop
    async def before_daily_organize(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyOrganizeCog(bot))
