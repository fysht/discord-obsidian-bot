"""支出ログ（Expenses）関連エンドポイント。レシートVision解析、保存、月次集計、編集、削除、閾値設定。"""

import base64
import calendar as _cal
import datetime
import json
import logging
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import notification_service
from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="/expenses", tags=["expenses"])

EXPENSE_CATEGORIES = ["食費", "交通費", "娯楽", "衣服", "家電", "医療", "教育", "通信", "光熱費", "投資", "その他"]
SETTING_EXPENSE_LARGE_THRESHOLD = "expense_large_threshold_jpy"
DEFAULT_LARGE_THRESHOLD_JPY = 5000


async def _get_large_threshold() -> int:
    from api.database import get_app_setting
    raw = await get_app_setting(SETTING_EXPENSE_LARGE_THRESHOLD, str(DEFAULT_LARGE_THRESHOLD_JPY))
    try:
        v = int(float(raw))
        return v if v > 0 else DEFAULT_LARGE_THRESHOLD_JPY
    except (TypeError, ValueError):
        return DEFAULT_LARGE_THRESHOLD_JPY


class ReceiptAnalyzeRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"


class ReceiptUploadRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    date: Optional[str] = None  # ファイル名用 (YYYY-MM)


class ExpenseSaveRequest(BaseModel):
    amount: int
    date: Optional[str] = None
    category: str = "その他"
    vendor: str = ""
    payment_method: str = ""
    memo: str = ""
    receipt_drive_id: str = ""
    breakdown: str = ""


class ExpensePatchRequest(BaseModel):
    date: Optional[str] = None
    amount: Optional[int] = None
    category: Optional[str] = None
    vendor: Optional[str] = None
    payment_method: Optional[str] = None
    memo: Optional[str] = None
    breakdown: Optional[str] = None


class ExpenseThresholdRequest(BaseModel):
    threshold_jpy: int


@router.post("/analyze", dependencies=[Depends(verify_api_key)])
async def expenses_analyze(req: ReceiptAnalyzeRequest):
    """レシート写真から日付・店名・合計金額・支払方法を Gemini Vision で抽出（保存はしない）。"""
    from google.genai import types as _gt
    from api import app

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    prompt = (
        "この画像（レシート、または通販サイトの購入履歴・注文履歴のスクリーンショット）を読み取り、"
        "必ず以下の JSON 形式だけを返してください。前置きや説明は禁止。\n\n"
        "{\n"
        '  "date": "YYYY-MM-DD（読み取れなければ空文字）",\n'
        '  "vendor": "店名・サイト名（読み取れた文字を最大40文字。空可）",\n'
        '  "amount": 合計金額(int, 円。税込み合計を優先),\n'
        '  "category": "食費 / 交通費 / 娯楽 / 衣服 / 家電 / 医療 / 教育 / 通信 / 光熱費 / 投資 / その他 のいずれか",\n'
        '  "payment_method": "現金 / クレジット / 電子マネー / QR / 不明 のいずれか",\n'
        '  "memo": "備考（空可）",\n'
        '  "items": [{"name": "商品名・注文名", "amount": 金額int}],\n'
        '  "confidence": "high / medium / low"\n'
        "}\n"
        "items には、画像から読み取れる個々の商品・注文を「何にいくら使ったか」が分かるよう列挙する"
        "（複数の注文・商品が写っていれば全て。読み取れなければ空配列）。\n"
        "amount は items の合計または画像中の合計金額。数字のみ。"
        "支出に関係ない画像なら confidence='low' で空相当の値を入れる。"
    )

    try:
        image_bytes = base64.b64decode(req.image_base64)
        image_part = _gt.Part.from_bytes(data=image_bytes, mime_type=req.mime_type)
        text_part = _gt.Part.from_text(text=prompt)
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("receipt_ocr", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m,
            contents=_gt.Content(role="user", parts=[image_part, text_part]),
            config=_gt.GenerateContentConfig(response_mime_type="application/json"),
        )
        return {"ok": True, "result": json.loads(response.text)}
    except Exception as e:
        logging.error(f"expenses_analyze error: {e}")
        raise HTTPException(status_code=500, detail=f"解析失敗: {str(e)}")


@router.post("/receipt_upload", dependencies=[Depends(verify_api_key)])
async def expenses_receipt_upload(req: ReceiptUploadRequest):
    """レシート画像を Google Drive (`/Expenses/YYYY-MM/`) に保存して file_id を返す。"""
    from api import app

    chat_service = getattr(app.state, "chat_service", None)
    if not chat_service or not chat_service.drive_service:
        return {"ok": False, "error": "Drive未接続"}

    date_str = req.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    month_str = date_str[:7] if len(date_str) >= 7 else datetime.datetime.now(JST).strftime("%Y-%m")
    try:
        service = chat_service.drive_service.get_service()
        if not service:
            return {"ok": False, "error": "Drive未接続"}
        root = chat_service.drive_folder_id
        expenses_folder = await chat_service.drive_service.find_file(service, root, "Expenses")
        if not expenses_folder:
            expenses_folder = await chat_service.drive_service.create_folder(service, root, "Expenses")
        month_folder = await chat_service.drive_service.find_file(service, expenses_folder, month_str)
        if not month_folder:
            month_folder = await chat_service.drive_service.create_folder(service, expenses_folder, month_str)

        suffix = ".jpg" if "jpeg" in (req.mime_type or "") or "jpg" in (req.mime_type or "") else ".png"
        timestamp = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            tf.write(base64.b64decode(req.image_base64))
            tmp_path = tf.name
        try:
            file_id = await chat_service.drive_service.upload_file(
                service, month_folder, f"receipt_{timestamp}{suffix}", tmp_path, mime_type=req.mime_type or "image/jpeg"
            )
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return {"ok": True, "drive_id": file_id}
    except Exception as e:
        logging.error(f"expenses_receipt_upload error: {e}")
        return {"ok": False, "error": "アップロード失敗"}


@router.post("", dependencies=[Depends(verify_api_key)])
async def expenses_save(req: ExpenseSaveRequest):
    """支出を保存。閾値超過なら is_large=1 を立て、Lifelog 追記と通知を実行。"""
    from api import app
    from api.database import add_expense

    now = datetime.datetime.now(JST)
    date = req.date or now.strftime("%Y-%m-%d")
    threshold = await _get_large_threshold()
    is_large = req.amount >= threshold

    expense_id = await add_expense(
        date=date, amount=req.amount, category=req.category, vendor=req.vendor,
        payment_method=req.payment_method, memo=req.memo,
        receipt_drive_id=req.receipt_drive_id, is_large=is_large,
        breakdown=req.breakdown,
    )

    # 支出はすべて Obsidian の対象日 DailyNote の `## 🪟 Lifelog` に
    # 統一フォーマット `- HH:MM 💴 名前 ¥金額（メモ）` で追記する。
    # 大きな支出には `💴⚠️` を付けて視認性を上げる。
    try:
        from api.routers._obsidian_helpers import append_lifelog_line
        time_str = now.strftime("%H:%M")
        vendor_str = req.vendor or req.category or "支出"
        icon = "💴⚠️" if is_large else "💴"
        line = f"- {time_str} {icon} {vendor_str} ¥{req.amount:,}"
        if req.memo:
            line += f"（{req.memo}）"
        await append_lifelog_line(date, line)
    except Exception as e:
        logging.debug(f"expenses_save lifelog append failed: {e}")

    if is_large:
        try:
            await notification_service.send_push(
                title="💴 大きな支出を記録",
                body=f"¥{req.amount:,}（{req.vendor or req.category}）。閾値 ¥{threshold:,} を超えました。",
                url="/?openExpenses=1",
            )
        except Exception:
            pass

    return {"ok": True, "id": expense_id, "is_large": is_large, "threshold": threshold}


@router.get("", dependencies=[Depends(verify_api_key)])
async def expenses_list(year: Optional[int] = None, month: Optional[int] = None):
    """指定月（既定: 今月）の支出一覧と集計を返す。"""
    from api.database import get_expenses_by_range
    now = datetime.datetime.now(JST)
    y = year or now.year
    m = month or now.month
    days_in_month = _cal.monthrange(y, m)[1]
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y:04d}-{m:02d}-{days_in_month:02d}"
    rows = await get_expenses_by_range(start, end)

    total = sum(r["amount"] for r in rows)
    by_category: dict[str, int] = {}
    for r in rows:
        c = r["category"] or "その他"
        by_category[c] = by_category.get(c, 0) + r["amount"]
    by_category_list = sorted(
        [{"category": k, "amount": v} for k, v in by_category.items()],
        key=lambda x: x["amount"], reverse=True,
    )
    threshold = await _get_large_threshold()
    return {
        "year": y, "month": m,
        "start": start, "end": end,
        "expenses": rows,
        "total": total,
        "by_category": by_category_list,
        "large_threshold": threshold,
        "categories": EXPENSE_CATEGORIES,
    }


@router.patch("/{expense_id}", dependencies=[Depends(verify_api_key)])
async def expenses_patch(expense_id: int, req: ExpensePatchRequest):
    from api.database import update_expense
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "amount" in fields:
        threshold = await _get_large_threshold()
        fields["is_large"] = fields["amount"] >= threshold
    ok = await update_expense(expense_id, fields)
    if not ok:
        raise HTTPException(status_code=404, detail="支出が見つかりません")
    return {"ok": True}


@router.delete("/{expense_id}", dependencies=[Depends(verify_api_key)])
async def expenses_delete(expense_id: int):
    from api.database import delete_expense
    ok = await delete_expense(expense_id)
    if not ok:
        raise HTTPException(status_code=404, detail="支出が見つかりません")
    return {"ok": True}


@router.get("/threshold", dependencies=[Depends(verify_api_key)])
async def expenses_threshold_get():
    return {"threshold_jpy": await _get_large_threshold()}


@router.post("/threshold", dependencies=[Depends(verify_api_key)])
async def expenses_threshold_set(req: ExpenseThresholdRequest):
    from api.database import set_app_setting
    v = max(0, int(req.threshold_jpy or 0))
    await set_app_setting(SETTING_EXPENSE_LARGE_THRESHOLD, str(v))
    return {"ok": True, "threshold_jpy": v}
