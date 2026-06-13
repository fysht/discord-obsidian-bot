# 銘柄スクリーナー 設計メモ：2層モデル・メソッド・出口層

様々な投資本のスタイルを少しずつ足して「方向性を見失った」状態を整理するための設計地図。
実装は `services/screener_engine.py`（決定論的・pandas/numpy のみ・Gemini非依存）。

## 1. 2層モデル（整理の核）

スクリーナーは性質の違う2階層で構成する。

```
上層：投資メソッド（著者の名前付き手法）＝ 下層ファクター軸の組み合わせ
        ↑ 参照
下層：共通ファクター軸（FACTOR_AXES）＝ 各メソッドが見る評価の軸
```

- **メソッド = 軸の組み合わせ**。だから本を足しても、増えるのは共通軸 数個＋メソッドのプリセットだけ。
- **重複** = 複数メソッドが同じ軸を見ている状態（悪ではない。可視化して把握する）。
- **掛け合わせ** = 軸の和集合（`apply_secondary_style` のセカンダリ再評価＝軸を足してAND）。

### 共通ファクター軸（`FACTOR_AXES`）

| key | 軸 |
|---|---|
| growth | 成長性（売上・利益の伸び） |
| quality | 収益性（営業利益率・ROE） |
| value_earnings | 割安・利益面（PER・PSR） |
| value_asset | 割安・資産面（PBR・自己資本・配当） |
| safety | 財務安全性（自己資本比率・営業CF・FCF） |
| trend | トレンド（52週高値圏・移動平均） |
| pattern | チャート型（カップ/VCP/ボックス） |
| small_cap | 小型（時価総額の小ささ＝情報の非効率） |
| event | イベント（決算サプライズ・モメンタム） |
| cyclical | 景気循環（谷で買い・黒字転換） |
| catalyst | カタリスト（大株主買い増し・物言う株主・TOB/MBO期待） |

`STRATEGY_AXES` がメソッド→軸の単一の地図。`list_strategies()` が各メソッドに `axes`/`axis_labels`
を付与し、UI（method フィルタパネルの軸バッジ）と API（`list_styles` の `axes` カタログ）で可視化する。

## 2. メソッド一覧（上層）と軸

| メソッド (style_name) | 表示名 | 出典 | category | 軸 |
|---|---|---|---|---|
| new_high_breakout | 新高値ブレイク（DUKE） | DUKE『新高値ブレイク投資術』 | hybrid | trend, pattern, growth, quality |
| excel_stock | Excel株投資（森口亮） | 森口亮『Excel株投資』(40%ルール/PSR) | fundamental | growth, quality, value_earnings |
| earnings_momentum | 決算モメンタム（kenmo） | kenmo『5年で1億』 | technical | event, trend |
| small_cap_growth | 中長期・小型成長（片山晃/kenmo） | 片山晃『勝つ投資』/kenmo | fundamental | small_cap, growth, quality, value_earnings |
| asset_value | 資産バリュー（たーちゃん） | たーちゃん『50万円を50億円に』 | fundamental | value_asset, safety |
| cyclical_value | シクリカルバリュー（たーちゃん） | たーちゃん『50万円を50億円に』 | hybrid | cyclical, value_earnings, trend |
| creeping_breakout | 新高値ブレイク（じわじわ・低ボラ） | ユーザーのオリジナル | technical | trend |
| fundamental_gate | ファンダ総合（村上茂久） | 村上茂久『決算分析の地図』 | fundamental | growth, quality, value_earnings |
| breakout_patterns *(hidden)* | 新高値ブレイク（チャート型） | 新高値ブレイクのテクニカル単体 | technical | trend, pattern |
| aggressive_growth *(hidden)* | 強気グロース（高成長） | 強気業績ゲート単体 | fundamental | growth, quality |

※ `value`(バリュー)・`growth`(グロース) はユーザー指示で除外（2026-06）。

軸マップで重複が見える（成長性=5・収益性=5・割安利益=4メソッドが共有）。
hidden は他メソッドの内部部品（一覧から隠す）。

### 決算モメンタムの近似（重要）

yfinance に四半期サプライズが無いため、**決算ギャップ（窓開け急騰＋出来高急増）を好決算
サプライズの価格代理**にし、増益（`earnings_growth`/`earnings_quarterly_growth`）で裏付ける。
`TechnicalSignals.detect_earnings_gap` → `evaluate_earnings_momentum`。OHLCV中心で決定論的。

### ヒストリカルPER（単一銘柄deep-dive層）

一律のPER水準ではなく「対自分株価」で割安/割高を見る（片山/kenmo）。過去EPS時系列が要るため
バッチではなく単一銘柄診断側に置く。`provider.get_per_history`（年次EPS×年末株価）→
`evaluate_historical_per` → `analyze_projection` の `historical_per`。

## 3. 出口層（入口と分離した損切り・資金管理）

「選ぶ」機能（スクリーニング/診断）と分離した出口の単一ソース（`services/screener_engine.py`）。

| 関数 | 役割 |
|---|---|
| `build_tranche_plan` | 5分割の打診買い→買い増し計画（DUKE 6章/kenmo）。projection も委譲＝単一ソース |
| `compute_position_size` | 資金×リスク%とストップ幅から建玉数を逆算（1トレードの損失を資金の一定%に抑える） |
| `evaluate_exit_signals` | -8%(kenmo)/-10%(DUKE) ハード損切り＋トレイリング（シャンデリア）＋MA割れの統一判定 |

`advise_portfolio(capital=, hard_stop_pct=)` に配線：保有はexit判定（ストップ抵触でSELL昇格）、
新規候補はposition_size（資金指定時）。UI は一括診断カードに「🚪出口」「🧮建玉」、💰資金設定で
`localStorage 'screener_capital'` をセット。

### たーちゃんの価値株3分類（asset_value / cyclical_value / 収益バリュー）

たーちゃん『50万円を50億円に』は価値株を3つに分ける：
- **資産バリュー**＝`asset_value`（PBR≤0.5・含み資産・TOB/MBOカタリスト）→ value_asset。
- **シクリカルバリュー**＝`cyclical_value`（景気の谷で買い・赤字→黒字転換・低PSR）→ cyclical 新軸。
- **収益バリュー**（営業利益率10%/PER≤10/PBR≤1.5/ROA≥7%/時価総額≤300億）＝既存の
  `growth`/`fundamental_gate`/`small_cap_growth` と実質重複のため新メソッド化はせず（軸が同じ）。

## 3.5. 目標配分レイヤー（ポートフォリオ上位・目安表示＋ドリフト警告）

ボトムアップの銘柄点数化の上に、ポート全体の配分目標を持つ層（`advise_portfolio` が返す `allocation`）。
強制リバランスはせず、現状 vs 目標とドリフトを表示し、入替提案を目標に寄せる（ソフト誘導）。

- 目標：**最高値型 : 待ち型 = 4:1**、**日本株 : 米国株 = 1:1**（ユーザー確定）。
- `classify_portfolio_bucket(res)`：perfect_order or 52週高値5%以内＝momentum（最高値型）、他＝wait（待ち型）。
  価格アクションで分類＝「最高値更新に乗る／動かず待ち」というユーザーの区分に忠実。
- `build_allocation_plan(positions, …)`：共通通貨の時価で現状％・ドリフトpt・警告（±10ptで warning）を返す純粋関数。
- `advise_portfolio`：`_get_usdjpy()` で米国株時価を円換算（取れねば150円概算）→ `allocation`。入替(`rotations`)は
  同一市場内 value-matched（売却代金÷買い候補株価をlot丸め＝「何枚売って何枚買うか」）。過配分バケットを
  売り・過少を買う入替を `toward_target` 判定して先頭にソート。
- UI：advise モーダルに「⚖️ 目標配分」ブロック＋入替カードに数量と🎯。

日次ワークフロー（①チャート→②ファンダ→③Geminiディープリサーチ→④点数＋入替）の設計合意と
未着手分（毎日自動実行・Gemini深掘り）はメモリ project_screener_workflow を参照。

## 3.6. プロセス改善（利益最大化・過剰回転の抑制）

「毎日入れ替えて利益拡大」を健全化する決定論的ガード群（engine 純粋関数＋`advise_portfolio`/`analyze_projection` 配線）。

| # | 関数 | 効果 |
|---|---|---|
| ① 税/手数料 | `compute_rotation_friction` | 入替の足切りを `required_gap = 10 + 摩擦%` に。含み益大の勝ち株は実力差が大きくないと入替提案しない＝オーバートレード抑制 |
| ② 学習→建玉 | `hit_rate_risk_multiplier` / `learning_adjustment` | 事後検証の的中率で新規候補の建玉を増減（60%↑→×1.3／40%↓→×0.5）。**トレンド状態別×メソッド別の2レンズを `learning_adjustment` で統合**（件数加重で統合的中率＋倍率平均、いずれかが45%未満なら BUY→WATCH 格下げ）。メソッド別が効くよう売買記録に保有の `preferred_method` を style として保存（by_style が初めて機能） |
| ③ 地合い | `assess_market_regime` | 指数200日線の上下＋傾きで risk_on/off。リスクオフは新規買いを WATCH に格下げ（上昇相場でのみ攻める） |
| ④ 買い増し | `build_pyramid_plan` | 含み益＋perfect_order の保有に買い増し＋損切りを建値へ引き上げ（勝ちを伸ばし守る） |
| ⑤ 流動性 | `assess_liquidity` | 薄商い銘柄の入替枚数を日次売買代金10%上限にキャップ |
| ⑥ シグナル検証 | `backtest_entry_signal` | エントリー（新高値/PO）の過去 forward リターン vs buy&hold の優位性（銘柄単位の裏取り） |

**ポート単位バックテスト**（`backtest_portfolio_rotation`）：定期リバランスでモメンタム上位を等加重保有 vs 等加重 buy&hold を
ポイントインタイム・回転コスト込みで比較。service `backtest_rotation`（与えた銘柄群を**JP/US 市場別に分離**して個別検証＝
営業日カレンダー差の近似を排除→1:1 合成 `combined`）／`backtest_universe`（ユニバース構成員で本格検証・現在構成員のみ＝
生存者バイアス注記）。API `POST /screener/backtest`・`/screener/backtest_universe`。UI は一括診断の「📊 戦略バックテスト」
（市場別＋合成表示・ユニバース全体select）。**回転コストを織り込むと回転が買い持ちに負けるケースが普通に出る**＝
「厳選入替＋勝ち株を伸ばす」方針の数値的裏付け。

設計思想：エントリー精度より**勝ち逃げ/損切りの非対称性**と**回転コストの抑制**が損益を支配する。
事後検証ループ（`decision_review_report`）の「握り続けた方が得だった」傾向（over_trading_caution）と整合。

## 3.7. 抽出精度の強化（2段階スクリーニング・フェーズ1）

ファンダ系メソッドの主データ yfinance `.info` は日本の小型株で欠損/古い/不正確になりがち。
これを **EDINET(JP)/EDGAR(US) の有報実績で2段目確認**して精度を上げる（`run_screening(refine=True)`、
単一スタイル時のみ＝EDINET走査が重いため）。

- `merge_fundamentals(yf, financials)`：EDINETで確実に取れる **ROE・売上高・自己資本比率・純資産・営業CF・FCF**
  を上書き/追加（市場データ=時価総額/PER/PBR/配当は yfinance のまま）。例：yfinanceのROE 0.18→有報実績 0.09 に補正。
- `assess_quality(fundamentals, df, market)`：**薄商い**（売買代金）・**債務超過**（純資産マイナス）・**営業CFマイナス**を判定。
- `_refine_candidates`（service）：1段目通過候補を有報実績で**再評価**し、基準未達(`refined_out`)・債務超過は**除外**、薄商いはフラグ。
  各候補に `data_confidence`（EDINET確認済/要確認/yfinanceのみ）・`financials_source`・`quality` を付与。
- UI：スクリーナーに「🔬 EDINET/EDGAR有報で精査」チェック、候補行に信頼度＋品質バッジ。
- 日次の多スタイル自動タスクには載せない（スタイル数ぶん EDINET 走査が重くなるため）。

**フェーズ2（時系列＋相対評価）も精査(refine)に追加：**
- **連続増収増益**：`edinet_financials._select_series`（有報「主要な経営指標等の推移」5年の売上/純利益を CurrentYear/PriorNYear コンテキストから時系列化・連結優先）＋`_consecutive_growth`。`consecutive_revenue_growth`/`consecutive_profit_growth` を refine 候補に `growth_streak` で付与＝一時的でなく**実績が連続して伸びているか**を確認。
- **ヒストリカルPER（対自分株価）**：refine の各候補に `get_per_history`→`evaluate_historical_per` を併用（top の少数のみ）。絶対PERでなく**自社の過去レンジ比で割安か**を相対評価。
- UI：候補バッジに「連続増収N期/増益M期」「ヒストリカルPER 割安/割高（現在倍/中央倍）」。
- **的中率での閾値自動調整（実装済）**：`learning_adjustment`（engine純粋関数）がトレンド状態別×メソッド別の的中率レンズを統合し、`advise_portfolio` で新規候補の建玉倍率と BUY→WATCH 格下げを自動調整。メソッド別レンズを機能させるため売買スナップショット（`record_trade_decision`）に保有の `preferred_method` を style として記録（従来は空で by_style が無効だった）。UI：advise カードの 🧠学習 に「統合的中率／状態・手法の内訳／建玉倍率／格下げ理由」。
- **セクター中央値の相対評価（実装済）**：`compute_sector_medians`（走査したユニバース**全体**を不偏標本にセクター別 PER/PBR/PSR の中央値を算出・正値のみ・各指標 n≥5）＋`evaluate_relative_valuation`（候補の per/pbr/psr を同業中央値と比較し vs%・平均で cheap/fair/rich）。`run_screening` で**refine 有無に関わらず**ファンダ取得スタイルの候補へ `relative_valuation` を付与（標本が上位少数でなくユニバース全体なので偏らない＝以前見送った理由を解消）。UI：候補に「📊 同業比 割安/割高（PER±%/PBR±%/PSR±%・n）」バッジ。ヒストリカルPER（対自分株価）と相互補完＝**自社の過去比＋同業他社比**の二面で割安度を見る。
- **cyclical用の外部景気指標（実装済）**：`assess_cyclical_regime`（engine純粋関数）＋`_cyclical_phase`（プロキシ1本を200日線の上下×短期の向き=50日線上＋3ヶ月モメンタムで 回復初動/拡張/後退/ピークアウト の4象限に分類）。service `assess_cyclical_macro` が銅(HG=F)・原油(CL=F)・半導体(SOXX)を `provider.get_ohlcv` で取得し集約。`run_screening` で `style=="cyclical_value"` のとき結果に `cyclical_regime`（supportive=谷から反転で買い向き）を付与。UI：スクリーニング結果上部に「🌐 景気指標：回復初動/後退…（買い向き/反転待ち）」バナー＋Markdownレポートにも1行。シクリカルの谷判定（低営業利益率・低PSR・60日MA上の反転初動）を**外部マクロが谷→反転を支持しているか**で裏取りする。
- **指標別チューニング（実装済）**：`signal_lens`（engine純粋関数）が銘柄のいま立てている指標（パーフェクトオーダー/25日線上/75日線上）ごとの過去的中率（`by_signal`）を件数加重で1レンズに集約。`advise_portfolio` の学習を **状態×メソッド×指標の3レンズ統合**に拡張（`learning_adjustment` に第3レンズ追加）。UI：🧠学習に「指標 N%(件)」を追加。
- **cyclical 景気指標の advise 反映（実装済）**：`advise_portfolio` で保有・候補にシクリカル（style==cyclical_value か景気循環セクター）が含まれるときだけ `assess_cyclical_macro` を取得し、該当 r に `cyclical_regime` を付与。BUY 候補は景気指標が「後退（谷継続）」なら BUY→WATCH（ウォッチ（景気））に格下げ＝反転を待つ。UI：advise カードに「🌐 景気指標」行。

## 4. 取捨選択の根拠（ユーザー確定）

- **除外**: 株主優待現金給付（kenmo）、小松原氏（機関投資家）の手法。
- **実装済（catalyst 軸）**: 木原直哉/エミン『確率思考』の大株主・アクティビスト・TOB/MBO期待の
  カタリスト手法。`services/edinet_large_holdings.py` が EDINET 大量保有報告書（docTypeCode 350/360）を
  走査し、`secCode`=対象企業／`filerName`=保有者で拾う。保有割合は直近数件の CSV(type=5) を best-effort
  パース（`HoldingRatioOfShareCertificatesEtc` と直前報告書値）。`evaluate_catalyst`（engine・純粋関数）が
  物言う株主・買い増し・複数報告・高保有率を点数化。`analyze_projection` に `catalyst` として配線（JP銘柄のみ・
  EDINET_API_KEY 必須・無ければ ok:False・180日走査で重いので単一銘柄 deep-dive 層）。フロントは projection
  モーダルに「🎯 カタリスト」ブロック。アクティビスト名は `_ACTIVIST_HINTS` の部分一致（村上系/オアシス/
  エフィッシモ等）。
- **見送り（churn）**: metric→check のボイラープレートを汎用ビルダーに寄せる dedup。閾値・ラベル・
  フォーマットがメソッドごとに意図的に異なり、寄せると可読性が下がる割に効果が小さい＝churn。
  整理の本質は2層モデルの可視化（軸マップ＋UIバッジ）で達成済み。

## 5. 関連

- 事後検証ループ（売買判断→20/60営業日後に答え合わせ→トレンド別的中率を学習）は
  `record_trade_decision`/`verify_due_decisions`/`decision_review_report`。出口層と相補。
- 定性分析（Phase B/C, Gemini）は目標株価/値動き予測/確率を出さない制約付き。
