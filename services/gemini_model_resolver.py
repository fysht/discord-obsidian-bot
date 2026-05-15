"""機能キー -> Gemini モデル名 への解決ヘルパー。

設定画面 (PWA) で各機能ごとに Flash / Pro を選べるようにするための薄いラッパ。
- 設定値は app_settings テーブルに `gemini_model.<feature_key>` として保存される。
- 値は "flash" or "pro" の文字列で、ここで具体的なモデルIDに変換する。
- 設定が無い場合、または不正値の場合は default モデルへフォールバック。

呼び出し例:
    model = await resolve_gemini_model("investment_snapshot", default_pro=True)
"""
from __future__ import annotations

import logging
from typing import Optional


GEMINI_FLASH_MODEL = "gemini-2.5-flash"
GEMINI_PRO_MODEL = "gemini-2.5-pro"

# 設定 UI のドロップダウンキー → 実際の Gemini モデル ID へのエイリアス
KNOWN_MODELS = {
    "flash": "gemini-2.5-flash",                # 既存互換（デフォルトFlash）
    "pro": "gemini-2.5-pro",                    # 既存互換（デフォルトPro）
    "flash-lite": "gemini-2.5-flash-lite",
    "flash-preview": "gemini-3-flash-preview",
    "flash-lite-3": "gemini-3.1-flash-lite",
    "pro-preview": "gemini-3.1-pro-preview",
}


def _from_choice(choice: str, default_pro: bool) -> str:
    if not choice:
        return GEMINI_PRO_MODEL if default_pro else GEMINI_FLASH_MODEL
    if choice in KNOWN_MODELS:
        return KNOWN_MODELS[choice]
    # 任意のモデルIDを直接受け付ける（"gemini-" で始まる文字列）
    if choice.startswith("gemini-"):
        return choice
    return GEMINI_PRO_MODEL if default_pro else GEMINI_FLASH_MODEL


def is_valid_choice(choice: str) -> bool:
    """設定 UI から保存される値の妥当性チェック。"""
    if not choice:
        return False
    if choice in KNOWN_MODELS:
        return True
    return choice.startswith("gemini-") and len(choice) <= 100


async def resolve_gemini_model(feature_key: str, default_pro: bool = True) -> str:
    """機能キーから Gemini モデル名を返す。

    Args:
        feature_key: routes.py の GEMINI_FEATURE_CATALOG のキーと一致するもの
        default_pro: 設定が無い・不正な場合のフォールバック (True=Pro)

    Returns:
        "gemini-2.5-flash" または "gemini-2.5-pro"
    """
    try:
        from api.database import get_app_setting
        val = await get_app_setting(f"gemini_model.{feature_key}", "")
    except Exception as e:
        logging.debug(f"resolve_gemini_model: settings read failed for {feature_key}: {e}")
        val = ""
    return _from_choice(val, default_pro)
