# ログ質問フレームワーク 設計メモ

マネージャーとの「質問 → 回答 → 記録」を、食事に限らず**全ログ種別で自然・スムーズに**行うための統一設計。
既存の `[QUESTIONS:scope:date]` インライン回答欄と `[ACTION:...]` ボタンの仕組みを土台に一般化する。

## 目的
- マネージャーが質問 → ユーザーが回答欄（自由記載／選択チップ）に答える → 該当ログに記録され、結果カードへのリンクが返る、という往復をアプリ全域で共通化する。
- 負担なくログを残す。よくある回答は1タップ、複雑なログも一文で。

## 確定した設計判断
| 論点 | 決定 |
|---|---|
| 質問の発火者 | **両方**：会話中はAIが Function Calling で、定期/位置はルーティンがサーバ側で |
| 選択チップの候補 | **両方ミックス**：履歴集計を基本に、不足分はAI生成で補完し `context.chips` に格納 |
| 多項目ログ（支出・読書等） | **AIが一文から抽出**：保存前に確認（編集可能なプレフィル）ステップを挟む |
| 既存の独自レール | **最初から全部統合**：english_phrases `/answer`・habit・lifelog・thought_reflection を `daily_questions` レールへ寄せる |

## データモデル：scope レジストリ方式（スキーマ変更なし）
`daily_questions` テーブル（`scope` / `question` / `answer` / `status` / `context` JSON）をそのまま流用。
反映ルールを単一の「scope レジストリ」に集約する。

```python
# services/log_question_registry.py（新規）
LOG_QUESTION_SCOPES = {
  # 1問1答：回答→直接保存→リンクバック
  "meal":   {"answer": "single", "parse": "nutrition", "reflect": meal_save,
             "chips": history_meals, "linkback": "open_link", "icon": "🍽"},
  "mood":   {"answer": "single", "parse": None, "reflect": lifelog_append,
             "chips": ["😀","😐","😫","1","2","3","4","5"]},
  "lifelog":{"answer": "single", "reflect": lifelog_activity},  # 既存ACTION吸収
  # 多項目：回答→AI抽出→確認(プレフィル)→保存
  "expense":{"answer": "extract", "fields": ["amount","name","category"],
             "reflect": expense_save, "confirm": True, "linkback": "open_link"},
  "reading":{"answer": "extract", "fields": ["book","page","takeaway"],
             "reflect": reading_log_append, "confirm": True},
  # 既存をrail化（挙動不変）
  "summary":     {...既存…},
  "morning_mit": {...既存…},
  # クイズ（ログではない）：UIレールのみ共通化、テーブル/データは english_phrases のまま
  "english_quiz":{"answer": "choice", "chips": quiz_options,
                  "reflect": record_quiz_attempt},
}
```

新ログ種別の追加＝この表に1エントリ追加するだけ。

## 記録先は3タイプに収束
`reflect` ハンドラの実体は3種類しかない：
1. **DB insert** — 食事 / 支出 / 習慣 / 英語 / リンク / 投資ジャーナル
2. **Obsidian section append** — ライフログ / 振り返り / ジャーナル / 次アクション / 読書 / 気分
3. **Google サービス** — 予定 / タスク

## 2つの回答フロー

### A. 1問1答（meal, mood, lifelog, habit…）
```
1. 発火：AIが ask_log_question(scope, question) → add_daily_question + 返信に [QUESTIONS:scope:date]
2. 描画：renderInlineQuestionForm が context.chips でチップ＋textarea を表示
3. 回答：チップ1タップ or 自由記載 → POST /daily_questions/{id}/answer
4. 反映：registry[scope].reflect が保存 → resolved化
5. 返信：確定文 + [ACTION:open_link:id=...] を save_message_and_notify
```

### B. 多項目（expense, reading, journal…）— AI抽出＋確認
```
1〜3. 同上（回答は「ラーメン 980円 駅前」のような一文でOK）
4. 抽出：回答テキストをAIが fields に分解（meals/expenses の /analyze の JSON 契約を流用、
   画像Part→テキストPartに差し替え。confidence も取得）
5. 分岐：**confidence=high なら即保存**、low なら [ACTION:<scope>_confirm:...] を返し
   プレフィル済みモーダルで確認・編集（A/Bを confidence で自動切替）
6. 保存：reflect → リンクバック
```
※ 確認モーダルは既存の log_meal / propose_note のプレフィル方式を踏襲。

## 全ログ対象 → 反映先 対応表
| 領域 | 記録先 | 回答 | フロー |
|---|---|---|---|
| 🍽 食事 | meals(DB) | 自由＋栄養推定 | A |
| 💰 支出 | expenses(DB) | 一文抽出 | B |
| ✅ 習慣 | habit_logs(DB) | 選択 | A |
| 😀 気分/体調 | DailyNote `## Lifelog` | 選択 | A（新規） |
| 🔥 ライフログ | DailyNote `## Lifelog` | start/end | A |
| 💭 振り返り | DailyNote `## Thought Reflection` | 自由 | A |
| 📔 ジャーナル | DailyNote `## Daily Journal` | 自由 | A |
| 🚀 次アクション | DailyNote `## Next Actions` | 複数行 | A |
| 📖 読書 | Obsidian Reading Log | 一文抽出 | B |
| 🗣 英語学習（クイズ） | english_phrases(DB) | 選択 | A（UIレールのみ共通化／別ハンドラ） |
| 🎯 MIT | DailyNote | 複数行 | A（既存） |
| 📅 予定/タスク | Google | 一文抽出 | B |
| 💹 投資ジャーナル | journal(DB) | 一文抽出 | B |

## チャット ↔ カード連携
- **チャット→カード**：反映後に結果カードへのディープリンク（`[ACTION:open_link]` / `open_*`）を返信に挿す。
- **カード→チャット**：各タブのカードに「マネージャーに聞く」ボタン → カード引用付きでチャットを開く（`msg-quote` 既存）。
- **未回答バッジ**：`/api/daily_questions/pending` 件数をチャットタブ／カードにバッジ表示。「スキップ」で後回しも可。

## 触るファイル
| ファイル | 変更 |
|---|---|
| `services/log_question_registry.py` | 新規・scope→ハンドラ dispatch |
| `api/routers/daily_summary.py:177` | 回答ハンドラを registry dispatch 化（既存scopeも吸収） |
| `api/chat_service.py:169` | `ask_log_question` ツール追加 |
| `api/routers/meals.py:64` | テキスト入力での栄養推定経路を追加 |
| `api/routers/expenses.py` / `english_phrases.py` 等 | reflect ハンドラとして接続・統合 |
| `cogs/partner_routine_cog.py` | 定期/位置トリガーで質問生成 |
| `static/js/app_v12.js:888` | 回答欄にチップ行＋多項目確認の接続 |
| `prompts.py` | ask_log_question 使用ルールを明記 |
| `api/database.py` | スキーマ変更なし（context 流用） |

## 段階的ロールアウト
- **Phase 1**：registry 化リファクタ（既存 summary/morning_mit/habit/lifelog/thought を載せ替え・挙動不変）＋チップ描画。
- **Phase 2**：`meal`（フローA）と `expense`（フローB）を1本ずつ通す。AB両パターンの初の体感。
- **Phase 3**：mood/reading/journal/english を量産統合＋ルーティン自動投下＋カード↔チャット双方向。

## 詰めた結論（旧・未決事項）
- **栄養推定／支出抽出のテキスト経路**：meals/expenses の `/analyze` は Vision だが JSON 契約が明快。
  画像Part→テキストPartに差し替えるだけで同じ JSON が得られ、保存処理・モーダルのプレフィルは不変。
  両者とも `confidence` を返すので、**confidence=low のときだけ確認ステップを強制**（high は即保存）＝
  フローA/Bを confidence で自動切替できる。
- **外食位置トリガー**：location_log_cog は Google Maps Timeline の JSON を**バッチ後処理**で滞在地分類
  （自宅/勤務先/店名 via Places API）。リアルタイム geofence ではない。トリガーは「処理後、食事時間帯に
  自宅・勤務先以外の滞在を検知 → 店名を context に入れた `meal` 質問を作る」＝**事後の振り返り質問**。
  誤検知は“質問”なのでスキップで無害。
- **english_phrases は統合しない（補正）**：これは正解/不正解を記録する**間隔反復クイズ**で、ログとは意味が違う。
  テーブル統合・データ移行はしない。共通化するのは**回答UIのレールだけ**（選択肢を context.chips に、
  `scope=english_quiz` / `reflect=record_quiz_attempt`）。ログとは別ハンドラに保つ。
- **チップ履歴**：食事＝meals を name で集計し直近N日の**頻度上位3〜5件**を**質問生成時**に context.chips へ格納
  （回答時は集計ゼロ＝無遅延）。支出の費目チップは analyze プロンプトの固定 enum を流用。

## なお要検証（実装フェーズで確認）
- テキスト入力時の栄養／支出抽出の実際の精度（confidence しきい値の調整）
- location バッチ処理の頻度と「事後質問」のタイミング（夜まとめて出すか、処理直後か）
- チップ履歴の N（対象日数）の最適値
