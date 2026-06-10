"""ログ質問フレームワークの scope レジストリ。

マネージャーの「質問 → 回答 → 記録」を全ログ種別で共通化するための単一の定義源。
各 scope が「回答欄の出し方（選択チップ等）」と、将来的には「回答が来たら何をするか
（reflect ハンドラ）」を持つ。新しいログ種別を増やす時は、ここに 1 エントリ足すだけ。

Phase 1（このファイルの現状）ではメタデータ（回答タイプ・チップ・アイコン）と
チップ解決のみを提供する。reflect ハンドラ（meal/expense 等の保存処理）と
AI 一文抽出は Phase 2 以降で追加する。設計は docs/log_question_framework.md を参照。
"""

# answer_type:
#   "text"   … 自由記載（textarea のみ）
#   "choice" … 選択チップ中心（1 タップ回答を想定。自由記載も併用可）
#   "extract"… AI が一文から複数フィールドへ分解（Phase 2 で実装）
#
# 任意キー:
#   "label"    … 表示名／掘り下げプロンプトに渡す種類名。
#   "self_log" … True なら「マネージャーの質問を待たず、自分から先に記録できる」入口を出す。
#                記録は質問への回答と同じ reflect パイプを通る（質問起点と自発で挙動を揃える）。
#   "followup" … "ai" なら回答後に AI が「掘り下げる価値あり」と判断したときだけ追質問を生成し、
#                未回答インボックスに静かに積む（push 通知なし・深さ上限つき・スキップ可）。
LOG_QUESTION_SCOPES: dict[str, dict] = {
    # --- 既存スコープ（挙動はそのまま。ここではメタデータのみ集約） ---
    "summary": {"answer_type": "text", "icon": "📝", "chips": None},
    "morning_mit": {"answer_type": "text", "icon": "🎯", "chips": None},
    "nightly_reflection": {"answer_type": "text", "icon": "🌙", "chips": None},
    # --- 新スコープ（選択式・1 問 1 答）。Phase 2 で reflect を接続 ---
    # 気分：3択だと「まあまあ」に寄りがちなので、中庸を分散させた5段階にする。
    # 回答後は理由を一言うながす追質問を出す（_reflect_mood_answer 側）。
    "mood": {
        "answer_type": "choice",
        "icon": "😀",
        "chips": ["絶好調 🤩", "良い 🙂", "ふつう 😐", "もやもや 😕", "しんどい 😫"],
    },
    "condition": {
        "answer_type": "choice",
        "icon": "🩺",
        "chips": ["絶好調", "普通", "疲れ気味", "不調"],
    },
    # 昼の振り返り（午後の調子。選択式・1問1答）
    "afternoon": {
        "answer_type": "choice",
        "icon": "🌤",
        "chips": ["順調 👍", "ぼちぼち 😐", "疲れてきた 😪", "集中できてない"],
    },
    # 今日の出来事（自由記載・自発記録OK・AI掘り下げ対象）
    "event": {
        "answer_type": "text", "icon": "📌", "label": "出来事",
        "chips": None, "self_log": True, "followup": "ai",
    },
    # 今日学んだこと・気づき（自由記載・自発記録OK・AI掘り下げ対象）
    "learning": {
        "answer_type": "text", "icon": "💡", "label": "学び",
        "chips": None, "self_log": True, "followup": "ai",
    },
    # 今日良かったこと・感謝（自由記載・自発記録OK・AI掘り下げ対象）
    "gratitude": {
        "answer_type": "text", "icon": "🙏", "label": "良かったこと",
        "chips": None, "self_log": True, "followup": "ai",
    },
    # MIT（今日の最重要タスク・自発記録OK。掘り下げはしない＝計画なので）。
    # 記録は append ではなく1日1セットの set（Obsidian の ## 🎯 MIT を置き換え）。
    "mit": {
        "answer_type": "text", "icon": "🎯", "label": "MIT",
        "chips": None, "self_log": True,
    },
    # 読書メモ（多項目→AI抽出で書名＋学びに分解）
    "reading": {"answer_type": "extract", "icon": "📖", "chips": None},
    # 英単語/フレーズのクイズ（学習。選択肢は context.chips に都度格納）
    "english_quiz": {"answer_type": "choice", "icon": "🗣", "chips": None},
}

DEFAULT_SCOPE: dict = {"answer_type": "text", "icon": "💬", "chips": None}

# 回答欄に並べるチップの上限（増えすぎ防止）
MAX_CHIPS = 8


def get_scope_config(scope: str) -> dict:
    """scope の設定を返す（未登録ならデフォルト）。"""
    return LOG_QUESTION_SCOPES.get(scope or "", DEFAULT_SCOPE)


def should_followup(scope: str) -> bool:
    """この scope の回答後に AI 掘り下げ質問を生成してよいか。"""
    return get_scope_config(scope).get("followup") == "ai"


def resolve_chips(scope: str, context: dict | None) -> list[str]:
    """回答欄に出す選択チップを決める。

    優先順位:
      1. context.chips … 質問生成時に埋めた候補（履歴集計や AI 生成の結果。Phase 2 で活用）
      2. レジストリの静的チップ … scope ごとの既定候補

    （Phase 2 では、ここで食事履歴の頻出項目集計や AI 生成チップをマージする）
    """
    if context and isinstance(context.get("chips"), list) and context["chips"]:
        return [str(c) for c in context["chips"] if str(c).strip()][:MAX_CHIPS]
    cfg = get_scope_config(scope)
    chips = cfg.get("chips")
    return list(chips)[:MAX_CHIPS] if chips else []
