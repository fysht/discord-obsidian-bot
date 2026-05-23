"""Gemini モデル設定（機能カテゴリごとに Flash / Pro を選択）。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="/settings/gemini_models", tags=["settings"])


# 機能カテゴリ定義: (key, ラベル, 説明, デフォルトモデル)
GEMINI_FEATURE_CATALOG = [
    ("screener_qualitative", "スクリーナー質的分析", "スクリーニング結果のPhase B/C質的分析", "flash"),
    ("investment_snapshot", "銘柄スナップショット", "銘柄の現状把握分析", "pro"),
    ("investment_audit", "憲法審査", "投資憲法に基づく銘柄審査", "pro"),
    ("investment_peer", "同業比較", "同業他社との比較分析", "pro"),
    ("investment_news", "ニュースセンチメント", "個別銘柄のニュース分析", "pro"),
    ("investment_earnings", "決算分析", "決算予定・資料・CEO検証", "pro"),
    ("investment_dividend", "配当分析", "配当スケジュール調査", "pro"),
    ("investment_sentiment", "地合い分析", "市場全体のセンチメント", "pro"),
    ("investment_journal", "投資日記分析", "投資日記の癖分析", "pro"),
    ("investment_review", "憲法レビュー", "投資憲法の定期レビュー", "pro"),
    ("investment_risk", "リスク評価", "ポートフォリオのリスク評価", "pro"),
    ("partner_chat", "マネージャー会話", "PWAチャットでの応答", "pro"),
    ("routines", "自動ルーチン", "朝MIT・週次レビュー・Gmail要約・取扱説明書", "flash"),
    ("memo_image", "メモ画像OCR", "撮影メモ・複数画像メモの構造化", "pro"),
    ("task_breakdown", "タスク細分化", "タスクをサブタスクに分割", "pro"),
    ("task_organize", "タスク整理", "タスク一覧の優先度/グルーピング提案", "flash"),
    ("book_prompt", "読書プロンプト生成", "書籍ごとの深掘り質問生成", "pro"),
    ("zt_themes", "ZTテーマ生成", "ゼロから100までの詳細テーマ生成", "pro"),
    ("zt_deep_dive", "ZT深掘り", "メモから追加テーマ5件を生成", "pro"),
    ("daily_review", "今日の振り返り", "活動サマリー＆明日への提案", "flash"),
    ("daily_summary", "デイリーサマリー", "1日のチャット/活動を統合し質問抽出", "pro"),
    ("receipt_ocr", "レシート分析", "レシート画像から店名・金額抽出", "flash"),
    ("meal_image", "食事画像分析", "料理名・カロリー・PFC推定", "flash"),
    ("meal_advice", "食事アドバイス", "1日の食事から栄養バランス助言", "flash"),
    ("english_translate", "英訳", "ENモード・フレーズ帳の日本語→英語変換", "flash"),
]


def _setting_key(feature_key: str) -> str:
    return f"gemini_model.{feature_key}"


class SettingsGeminiModelRequest(BaseModel):
    # {feature_key: <model alias or "gemini-..." model id>, ...}
    values: dict


@router.get("", dependencies=[Depends(verify_api_key)])
async def settings_gemini_models_get():
    from api.database import get_app_setting
    from services.gemini_model_resolver import is_valid_choice
    items = []
    for key, label, desc, default in GEMINI_FEATURE_CATALOG:
        val = await get_app_setting(_setting_key(key), default)
        items.append({
            "key": key,
            "label": label,
            "description": desc,
            "default": default,
            "value": val if is_valid_choice(val) else default,
        })
    return {"ok": True, "items": items}


@router.post("", dependencies=[Depends(verify_api_key)])
async def settings_gemini_models_post(req: SettingsGeminiModelRequest):
    from api.database import set_app_setting
    from services.gemini_model_resolver import is_valid_choice
    valid_keys = {k for k, *_ in GEMINI_FEATURE_CATALOG}
    saved = 0
    for k, v in (req.values or {}).items():
        if k not in valid_keys:
            continue
        if not is_valid_choice(v):
            continue
        await set_app_setting(_setting_key(k), v)
        saved += 1
    return {"ok": True, "saved": saved}
