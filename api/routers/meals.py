"""食事ログ（Meals）関連エンドポイント。Vision 解析 / 保存 / 一覧 / 編集 / 削除 / アドバイス / 提案。"""

import base64
import datetime
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/meals", tags=["meals"])

# 食事区分ごとの代表時刻。time 未指定で保存された場合に、メッセージ送信時刻ではなく
# 食事区分の標準時刻で記録するために使う。これにより「夜にまとめて朝食を入力」しても
# 朝食は朝の時刻で残る（区分は JP/EN どちらのキーでも引ける）。
MEAL_TYPE_TIMES = {
    "朝食": "08:30", "breakfast": "08:30",
    "昼食": "12:45", "lunch": "12:45",
    "夕食": "20:00", "dinner": "20:00",
    "間食": "15:30", "snack": "15:30",
}

# 食事区分の正規化（JP/EN どちらの入力でも内部キーを揃える）と、ノート表示用の日本語ラベル。
_MEAL_TYPE_NORM = {
    "朝食": "breakfast", "breakfast": "breakfast",
    "昼食": "lunch", "lunch": "lunch",
    "夕食": "dinner", "dinner": "dinner",
    "間食": "snack", "snack": "snack",
}
_MEAL_TYPE_LABEL_JA = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食", "snack": "間食"}


def _norm_meal_type(s: str) -> str:
    return _MEAL_TYPE_NORM.get((s or "").strip(), (s or "").strip())


class MealAnalyzeRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    hint: str = ""


class MealSaveRequest(BaseModel):
    name: str
    time: Optional[str] = None
    date: Optional[str] = None
    meal_type: str = ""
    calories: int = 0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carbs_g: float = 0.0
    memo: str = ""
    image_drive_id: str = ""
    # 外食関連（任意）
    restaurant: str = ""        # 店名
    ordered_items: str = ""     # 注文内容（複数行可）
    price: int = 0              # 金額（円）
    source: str = ""            # 自炊 / 外食 / デリバリー / 中食 / その他
    companions: str = ""        # 同席者（家族・友人・一人 等）
    rating: int = 0             # 満足度 1〜5（0=未評価）
    restaurant_url: str = ""    # Google Maps 等の店舗 URL
    expense_id: int = 0         # 先に登録済みの支出と紐付ける（レシート→食事連携。二重計上を防ぐ）


class MealPatchRequest(BaseModel):
    date: Optional[str] = None
    time: Optional[str] = None
    meal_type: Optional[str] = None
    name: Optional[str] = None
    calories: Optional[int] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carbs_g: Optional[float] = None
    memo: Optional[str] = None
    restaurant: Optional[str] = None
    ordered_items: Optional[str] = None
    price: Optional[int] = None
    source: Optional[str] = None
    companions: Optional[str] = None
    rating: Optional[int] = None
    restaurant_url: Optional[str] = None


@router.post("/analyze", dependencies=[Depends(verify_api_key)])
async def meals_analyze(req: MealAnalyzeRequest):
    """食事の写真から料理名・推定カロリー・PFC を抽出（保存はしない）。"""
    from google.genai import types as _gt
    from api import app

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    hint_text = f"\n補足: {req.hint}" if req.hint else ""
    prompt = (
        "この食事の写真から栄養情報を推定し、必ず以下の JSON 形式だけを返してください。\n"
        f"前置きや説明は禁止。{hint_text}\n\n"
        "{\n"
        '  "name": "料理名（複数なら『+』でつなぐ。例: 唐揚げ定食 + 味噌汁）",\n'
        '  "meal_type": "breakfast / lunch / dinner / snack のいずれか（時間帯がわからなければ best effort）",\n'
        '  "calories": 推定カロリー(kcal, int),\n'
        '  "protein_g": タンパク質(g, number),\n'
        '  "fat_g": 脂質(g, number),\n'
        '  "carbs_g": 炭水化物(g, number),\n'
        '  "confidence": "high / medium / low",\n'
        '  "memo": "気づいたこと（量が多い・野菜が少ない 等）を1〜2行"\n'
        "}\n"
        "推定根拠が乏しいときは confidence='low' とし、数値は控えめに。"
    )

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = _gt.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = _gt.Part.from_text(text=prompt)
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("meal_image", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[image_part, text_part]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        return {"ok": True, "result": json.loads(response.text)}
    except Exception as e:
        logging.error(f"meals_analyze error: {e}")
        raise HTTPException(status_code=500, detail=f"解析失敗: {str(e)}")


async def analyze_meal_text(text: str) -> dict:
    """テキスト（例:「コーンフレーク」「唐揚げ定食」）から栄養情報を推定して dict を返す。
    画像版 meals_analyze と同じ JSON 契約を流用し、画像 Part をテキスト Part に差し替えただけ。
    失敗時は name だけ埋めた最小 dict を返す。"""
    from google.genai import types as _gt
    from api import app

    fallback = {"name": (text or "").strip()[:40] or "食事", "meal_type": "",
                "calories": 0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0,
                "confidence": "low", "memo": ""}
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        return fallback

    now_hour = datetime.datetime.now(JST).hour
    prompt = (
        "次の『食べたもの』の説明から栄養情報を推定し、必ず以下の JSON 形式だけを返してください。\n"
        f"前置きや説明は禁止。現在の時刻は約{now_hour}時。\n\n"
        f"食べたもの: {text}\n\n"
        "{\n"
        '  "name": "料理名（複数なら『+』でつなぐ）",\n'
        '  "meal_type": "breakfast / lunch / dinner / snack のいずれか（時間帯から best effort）",\n'
        '  "calories": 推定カロリー(kcal, int),\n'
        '  "protein_g": タンパク質(g, number),\n'
        '  "fat_g": 脂質(g, number),\n'
        '  "carbs_g": 炭水化物(g, number),\n'
        '  "confidence": "high / medium / low",\n'
        '  "memo": "気づいたこと（量が多い・野菜が少ない 等）を1行（任意）"\n'
        "}\n"
        "情報が乏しいときは confidence='low' とし、数値は控えめに。"
    )
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("meal_image", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[_gt.Part.from_text(text=prompt)]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text)
        if not (data.get("name") or "").strip():
            data["name"] = fallback["name"]
        return data
    except Exception as e:
        logging.error(f"analyze_meal_text error: {e}")
        return fallback


class MealTextAnalyzeRequest(BaseModel):
    text: str


@router.post("/analyze_text", dependencies=[Depends(verify_api_key)])
async def meals_analyze_text(req: MealTextAnalyzeRequest):
    """料理名・説明文からカロリー/PFC を推定して返す（保存はしない）。手入力時の自動見積用。"""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text が空です")
    result = await analyze_meal_text(text)
    return {"ok": True, "result": result}


async def _sync_meal_expense(meal_id, date, name, restaurant, price, existing_expense_id):
    """外食の金額を支出メモへ反映する。meal と expense を expense_id で1対1に紐付け、
    金額の新規/変更は作成・更新、金額が 0 になったら連携支出を削除する（二重計上を防ぐ）。"""
    from api.database import add_expense, update_expense, delete_expense, set_meal_expense_id

    price = int(price or 0)
    eid = existing_expense_id or None

    if price > 0:
        from api.routers.expenses import _get_large_threshold
        nm = (name or "食事").strip() or "食事"
        rest = (restaurant or "").strip()
        vendor = rest or nm
        memo = f"🍽 外食: {nm}" + (f"（{rest}）" if rest else "")
        threshold = await _get_large_threshold()
        is_large = price >= threshold
        if eid:
            await update_expense(eid, {
                "date": date, "amount": price, "vendor": vendor,
                "memo": memo, "category": "食費", "is_large": is_large,
            })
            return eid
        new_id = await add_expense(
            date=date, amount=price, category="食費", vendor=vendor,
            payment_method="", memo=memo, is_large=is_large,
        )
        await set_meal_expense_id(meal_id, new_id)
        return new_id

    # 金額が無くなった → 連携していた支出があれば削除して紐付けも解除
    if eid:
        await delete_expense(eid)
        await set_meal_expense_id(meal_id, None)
    return None


@router.post("", dependencies=[Depends(verify_api_key)])
async def meals_save(req: MealSaveRequest):
    """食事ログを保存。Obsidian の `## 🍽 Meals` セクションにも `- HH:MM 🍽 ...` で時刻順に追記する。"""
    from api import app
    from api.database import add_meal

    now = datetime.datetime.now(JST)
    date = req.date or now.strftime("%Y-%m-%d")
    mtype = (req.meal_type or "").strip()
    # time 未指定なら食事区分の代表時刻を採用（夜にまとめて回答しても朝食=朝の時刻で記録）。
    # 区分も未指定なら最後の手段として現在時刻を使う。
    time = req.time or MEAL_TYPE_TIMES.get(mtype) or now.strftime("%H:%M")
    name = (req.name or "").strip() or "食事"

    # カロリー未入力（0）かつ料理名がある場合は AI で自動推定して補完する。
    # ユーザーはカロリーが分からなくても保存でき、必要なら後で手動修正できる。
    calories = int(req.calories or 0)
    protein_g, fat_g, carbs_g = req.protein_g, req.fat_g, req.carbs_g
    if calories <= 0 and name and name != "食事":
        try:
            nutri = await analyze_meal_text(name)
            calories = int(nutri.get("calories") or 0)
            if not protein_g:
                protein_g = float(nutri.get("protein_g") or 0)
            if not fat_g:
                fat_g = float(nutri.get("fat_g") or 0)
            if not carbs_g:
                carbs_g = float(nutri.get("carbs_g") or 0)
        except Exception as e:
            logging.debug(f"meals_save calorie auto-estimate failed: {e}")

    meal_id = await add_meal(
        date=date, time=time, name=name,
        meal_type=(req.meal_type or "").strip(),
        calories=calories,
        protein_g=protein_g, fat_g=fat_g, carbs_g=carbs_g,
        memo=req.memo or "",
        image_drive_id=req.image_drive_id or "",
        restaurant=(req.restaurant or "").strip(),
        ordered_items=(req.ordered_items or "").strip(),
        price=int(req.price or 0),
        source=(req.source or "").strip(),
        companions=(req.companions or "").strip(),
        rating=int(req.rating or 0),
        restaurant_url=(req.restaurant_url or "").strip(),
    )

    # Obsidian の対象日 DailyNote の独立セクション `## 🍽 Meals` に時刻順で追記する。
    # 外食時は店名・注文・金額・★を併記して見返しやすくする。
    try:
        from api.routers._obsidian_helpers import append_lifelog_line
        # 先頭は食事区分ラベル（朝食/昼食/夕食/間食）を主にする。実際に入力された時刻が
        # あるときだけ HH:MM を併記する（未入力時の代表時刻は“偽の時刻”でノイズになるため出さない）。
        # 並び順は update_section 側が区分ラベルを代表時刻として扱うため、時刻を出さなくても区分順に整列する。
        norm_mt = _norm_meal_type(mtype)
        label_ja = _MEAL_TYPE_LABEL_JA.get(norm_mt, "")
        lead = "- " + (f"{time} " if (req.time or "").strip() else "") + "🍽"
        if label_ja:
            lead += f"【{label_ja}】"
        parts = [lead]
        if req.restaurant:
            parts.append(f"@{req.restaurant.strip()}")
        parts.append(name)
        extras = []
        if calories:
            extras.append(f"推定{calories}kcal")
        if req.price:
            extras.append(f"¥{int(req.price):,}")
        if req.rating and req.rating > 0:
            extras.append("★" * max(1, min(5, int(req.rating))))
        if req.companions:
            extras.append(f"with {req.companions.strip()}")
        head_line = " ".join(parts) + (f"（{' / '.join(extras)}）" if extras else "")
        # 注文内容は箇条書きでサブ行に
        sub_lines = []
        if req.ordered_items:
            for it in (req.ordered_items or "").splitlines():
                it = it.strip().lstrip("-・*").strip()
                if it:
                    sub_lines.append(f"    - {it}")
        full_line = head_line + ("\n" + "\n".join(sub_lines) if sub_lines else "")
        await append_lifelog_line(date, full_line, heading="## 🍽 Meals", sort_by_time=True)
    except Exception as e:
        logging.debug(f"meals_save lifelog append failed: {e}")

    # 外食などで金額が入っていれば支出メモにも反映する（食費カテゴリで自動登録）。
    # レシートから先に支出を登録済みの場合（expense_id 指定）は、新規作成せず
    # その支出を update して 1 件に紐付ける（二重計上を防ぐ）。
    expense_id = None
    try:
        # 金額が無いのに既存支出ID（レシート連携）を渡された場合は、その独立した支出を
        # 消さないよう連携自体をスキップする（_sync_meal_expense は price=0 で連携支出を
        # 削除する仕様のため）。金額があるときだけ既存支出に紐付けて update する。
        existing_eid = (int(req.expense_id or 0) or None) if int(req.price or 0) > 0 else None
        expense_id = await _sync_meal_expense(
            meal_id, date, name, req.restaurant, req.price, existing_eid
        )
        if existing_eid and expense_id:
            from api.database import set_meal_expense_id
            await set_meal_expense_id(meal_id, expense_id)
    except Exception as e:
        logging.debug(f"meals_save expense sync failed: {e}")

    # この食事ログで、同日の未解決「食事」質問があれば自動的に削除する。
    # 食事ログ画面から登録したら、チャット／「今日の記録」に残る同じ食事の質問は
    # （✓ で残さず）まるごと消してボードをすっきりさせる（ユーザー方針）。
    closed_qids: list[int] = []
    try:
        from api.database import get_questions_by_date, delete_daily_question
        pending = [
            q for q in (await get_questions_by_date(date, scope="meal") or [])
            if q.get("status") != "resolved"
        ]
        norm_mt = _norm_meal_type(mtype)
        targets = []
        for q in pending:
            try:
                q_mt = _norm_meal_type((json.loads(q.get("context") or "{}") or {}).get("meal_type") or "")
            except Exception:
                q_mt = ""
            if norm_mt and q_mt and q_mt == norm_mt:
                targets.append(q)          # 区分が一致
            elif not q_mt:
                targets.append(q)          # 区分指定のない質問はどの食事でも閉じてよい
        # 区分が判定できず該当なしでも、未解決の食事質問が1件だけなら閉じる（取りこぼし防止）。
        if not targets and not norm_mt and len(pending) == 1:
            targets = pending
        for q in targets:
            if await delete_daily_question(q["id"]):
                closed_qids.append(q["id"])
    except Exception as e:
        logging.debug(f"meals_save question auto-delete failed: {e}")

    return {
        "ok": True, "id": meal_id, "date": date, "time": time,
        "expense_id": expense_id, "closed_question_ids": closed_qids,
    }


@router.get("", dependencies=[Depends(verify_api_key)])
async def meals_list(date: str = ""):
    from api.database import get_meals_by_date
    if not date:
        date = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    rows = await get_meals_by_date(date)
    total = {
        "calories": sum(r["calories"] or 0 for r in rows),
        "protein_g": round(sum(r["protein_g"] or 0 for r in rows), 1),
        "fat_g": round(sum(r["fat_g"] or 0 for r in rows), 1),
        "carbs_g": round(sum(r["carbs_g"] or 0 for r in rows), 1),
    }
    return {"date": date, "meals": rows, "total": total}


@router.patch("/{meal_id}", dependencies=[Depends(verify_api_key)])
async def meals_patch(meal_id: int, req: MealPatchRequest):
    from api.database import update_meal, get_meal
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    ok = await update_meal(meal_id, fields)
    if not ok:
        raise HTTPException(status_code=404, detail="食事が見つかりません")
    # 金額を後から追記/変更した場合も支出メモへ反映（連携支出を作成・更新・削除）。
    try:
        m = await get_meal(meal_id)
        if m:
            await _sync_meal_expense(
                meal_id, m.get("date"), m.get("name"), m.get("restaurant"),
                m.get("price"), m.get("expense_id") or None,
            )
    except Exception as e:
        logging.debug(f"meals_patch expense sync failed: {e}")
    return {"ok": True}


@router.delete("/{meal_id}", dependencies=[Depends(verify_api_key)])
async def meals_delete(meal_id: int):
    from api.database import delete_meal, get_meal, delete_expense
    # 連携している自動作成の支出も一緒に削除（手動の支出には触れない）
    linked = await get_meal(meal_id)
    ok = await delete_meal(meal_id)
    if not ok:
        raise HTTPException(status_code=404, detail="食事が見つかりません")
    if linked and linked.get("expense_id"):
        try:
            await delete_expense(linked["expense_id"])
        except Exception as e:
            logging.debug(f"meals_delete linked expense delete failed: {e}")
    return {"ok": True}


@router.post("/advice", dependencies=[Depends(verify_api_key)])
async def meals_advice(date: str = ""):
    """指定日（既定: 今日）の食事ログを Gemini に渡し、栄養観点でのマネージャーアドバイスを返す。"""
    from api import app
    from api.database import get_meals_by_date
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")
    if not date:
        date = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    meals = await get_meals_by_date(date)
    if not meals:
        return {"ok": False, "error": "食事の記録がまだありません"}
    lines = []
    total_kcal = total_p = total_f = total_c = 0
    for m in meals:
        lines.append(
            f"- {m['time']} {m['name']}: {m['calories']}kcal "
            f"(P{m['protein_g']}/F{m['fat_g']}/C{m['carbs_g']})"
            + (f" — {m['memo']}" if m["memo"] else "")
        )
        total_kcal += m["calories"] or 0
        total_p += m["protein_g"] or 0
        total_f += m["fat_g"] or 0
        total_c += m["carbs_g"] or 0
    body = "\n".join(lines)
    prompt = (
        "あなたはユーザー専属のマネージャー兼栄養アドバイザーです。\n"
        f"今日（{date}）の食事ログから、栄養バランスの観点で短くアドバイスしてください。\n\n"
        f"## 食事ログ\n{body}\n\n"
        f"## 当日合計\nカロリー: {total_kcal}kcal / P: {total_p:.0f}g / F: {total_f:.0f}g / C: {total_c:.0f}g\n\n"
        "【ルール】\n"
        "- 文体はマネージャーらしいタメ口で 3〜5 行\n"
        "- 「足りない/多い」を 1 つだけ具体的に指摘し、明日に向けた小さな提案を 1 つ\n"
        "- カロリー数値だけでなくPFCバランスにも触れる\n"
        "- 否定的な強い言葉は使わず、励まし基調で\n"
    )
    try:
        from google.genai import types as _gt
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("meal_advice", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=prompt,
            config=_gt.GenerateContentConfig(),
        )
        advice = (response.text or "").strip()
        return {"ok": True, "advice": advice, "total": {
            "calories": total_kcal,
            "protein_g": round(total_p, 1),
            "fat_g": round(total_f, 1),
            "carbs_g": round(total_c, 1),
        }}
    except Exception as e:
        logging.error(f"meals_advice error: {e}")
        return {"ok": False, "error": "アドバイス生成に失敗しました"}


@router.post("/suggest", dependencies=[Depends(verify_api_key)])
async def meals_suggest():
    """過去の食事履歴と「最後に食べてからの空白期間」を踏まえて献立を提案する。"""
    from api import app
    from api.database import get_meals_by_range
    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    today = datetime.datetime.now(JST).date()
    start = (today - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    rows = await get_meals_by_range(start, end)
    if not rows:
        return {"ok": False, "error": "過去の食事記録がまだありません"}

    history: dict[str, dict] = {}
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        d = r.get("date") or ""
        h = history.setdefault(name, {"last": d, "count": 0})
        h["count"] += 1
        if d > h["last"]:
            h["last"] = d

    items = []
    for name, h in history.items():
        try:
            last_date = datetime.datetime.strptime(h["last"], "%Y-%m-%d").date()
            days_ago = (today - last_date).days
        except Exception:
            days_ago = 0
        items.append((days_ago, name, h["count"]))
    items.sort(key=lambda x: x[0], reverse=True)
    hist_lines = "\n".join(
        f"- {name}: 最後に食べたのは{days}日前（過去60日で{cnt}回）"
        for days, name, cnt in items[:40]
    )

    prompt = (
        "あなたはユーザー専属のマネージャーです。ユーザーが「今日のごはん何にしよう」と迷っています。\n"
        "下の『過去の食事履歴』を見て、次の食事の献立を提案してください。\n\n"
        f"## 過去の食事履歴（最後に食べてからの日数つき）\n{hist_lines}\n\n"
        "【ルール】\n"
        "- 提案は3〜4品。最近食べていない（空白期間が長い）メニューを優先的に挙げる\n"
        "- 各提案に「最後に食べたのは○日前だからそろそろどう？」のように空白期間に触れた一言の理由を書く\n"
        "- 履歴にある定番だけでなく、履歴の傾向から外れた新しいメニュー案を1つ混ぜてもよい\n"
        "- 文体はマネージャーらしいタメ口で、全体で短めに\n"
    )
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("meal_suggest", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m, contents=prompt,
        )
        suggestion = (response.text or "").strip()
        return {"ok": True, "suggestion": suggestion}
    except Exception as e:
        logging.error(f"meals_suggest error: {e}")
        return {"ok": False, "error": "提案の生成に失敗しました"}
