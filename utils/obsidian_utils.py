import datetime
import re

# 日本語の曜日（月=0 .. 日=6）。ノートのタイトル/プロパティに添えて見返しやすくする。
_WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _weekday_ja(date_str: str) -> str:
    """YYYY-MM-DD から日本語の曜日 1 文字を返す。失敗時は空文字。"""
    m = _DATE_RE.search(date_str or "")
    if not m:
        return ""
    try:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return _WEEKDAYS_JA[d.weekday()]
    except Exception:
        return ""


def _ensure_frontmatter(frontmatter: str, date_str: str, wd: str) -> str:
    """フロントマター（Obsidian のプロパティ）に date / weekday / tags を補完する。
    既存のキーや値は尊重し、欠けているものだけを足す。"""
    if not frontmatter:
        lines = ["---", f"date: {date_str}"]
        if wd:
            lines.append(f"weekday: {wd}")
        lines += ["tags:", "  - daily", "---"]
        return "\n".join(lines)
    body = frontmatter
    if not re.search(r"(?m)^date:", body):
        body = body.replace("---", f"---\ndate: {date_str}", 1)
    if wd and not re.search(r"(?m)^weekday:", body):
        body = re.sub(r"\n---\s*$", f"\nweekday: {wd}\n---", body)
    return body


# --- 定数定義 ---
# デイリーノートの見出し順序定義
# Botは項目を新規作成する際、この順序に従って適切な位置に挿入します。
# 「やること → 実行 → 振り返り → インプット → 自動記録」の流れに統一。
SECTION_ORDER = [
    # --- 1. Plan (今日やること) ---
    "## 🎯 MIT",  # 今日の最重要タスク (PartnerCog)
    "## 📋 Daily Log",  # 客観データを時系列で並べたビュー (DailyOrganizeCog) ※旧名: Daily Timeline
    # --- 2. Execution (リアルタイム記録) ---
    "## 🪟 Lifelog",   # 行動記録 (PartnerCog / PWA)
    "## 🍽 Meals",      # 食事ログ (meals / PWA) ※時刻順にソート
    "## 😀 Mood",       # 今日の気分 (PartnerCog / ログ質問)
    "## 🩺 Condition",  # 今日の体調 (PartnerCog / ログ質問)
    "## 🌤 Afternoon Check",  # 午後の調子 (ログ質問)
    "## 💡 Learnings",  # 今日の学び (ログ質問)
    "## 🙏 Gratitude",  # 感謝・良かったこと (ログ質問)
    "## 🎯 Tasks",     # LLR風タスク＆時間記録 (PartnerCog)
    "## 💬 Chat Log",  # 日常のつぶやき・メモ (PartnerCog) ※旧名: Timeline
    "## 🤔 Thought Reflection",  # 壁打ち (PartnerCog)
    # --- 3. Reflection (1日の振り返り)
    # 流れ: 当日データ → 主観の日記 → 客観総括 → マネージャー Q&A → メタ観察 → 派生分析 → 明日のアクション
    # アプリのログタブの並び（デイリーノート → 今日の振り返り → マネージャーの気づき）と一致
    "## 📊 今日のデータ",  # 天気・Fitbit・食事など当日の客観データ一覧 (routes.py / DailySummaryCog)
    "## 📔 Daily Journal",  # 主観の日記 (DailyOrganizeCog)
    "## 📅 Daily Summary",  # 客観総括 (routes.py)
    "## 🤝 Manager Q&A",   # マネージャー質問への回答 (DailySummaryCog)
    "## 🪞 Alter Log",     # 忖度ゼロのメタ観察 (DailyOrganizeCog)
    "## 🔎 Insights",  # 派生分析 (DailyOrganizeCog) ※旧名: Insights & Thoughts
    "## 🚀 Next Actions",  # 明日のアクション (DailyOrganizeCog)
    # --- 4. Input & Information (インプット・情報収集) ---
    "## 📖 Reading Log",  # 読書メモ (PartnerCog / BookCog → デイリーノートにもリンク)
    "## 📝 Study Log",  # 勉強ログ (StudyCog → デイリーノートにリンク)
    "## 🍳 Recipes",  # レシピクリップ (WebClipService)
    "## 📺 YouTube",  # YouTube動画リンク (WebClipService)
    "## 🗺 Places",   # Google Maps の場所情報 (旧 WebClips から分離)
    "## 🔗 WebClips",  # Web記事クリップ (WebClipService)
    "## ✉️ Emails",   # 保存したメールノートへのリンク (gmail)
    # --- 5. Logs & Records (自動記録・活動データ) ---
    "## 📷 Media",  # 撮影画像（写真／書類）への索引リンク (media)
    "## 📊 Health Metrics",  # 健康データ (FitbitCog)
    "## 📍 Location History",  # 位置情報ログ (LocationLogCog)
    "## 🗒️ Logs",  # 一般ログ (PartnerCog)
    "## 📝 Memo",  # メモ (sync_worker) ※旧名: Memo（絵文字なし）
]


# 旧セクション名 → 新セクション名のマイグレーションマップ
SECTION_RENAMES = {
    "## ⏱ Daily Timeline": "## 📋 Daily Log",
    "## 💬 Timeline": "## 💬 Chat Log",
    # 見出しを英語に統一（2026-05-31）。既存ノートの日本語見出しを英語へ移行。
    "## 🍽 食事": "## 🍽 Meals",
    "## 😀 気分": "## 😀 Mood",
    "## 🩺 体調": "## 🩺 Condition",
    "## 🌤 昼の振り返り": "## 🌤 Afternoon Check",
    "## 💡 学び・気づき": "## 💡 Learnings",
    "## 🙏 良かったこと": "## 🙏 Gratitude",
    "## 💡 Insights & Thoughts": "## 🔎 Insights",
    "## Memo": "## 📝 Memo",
}


def migrate_legacy_sections(content: str) -> str:
    """既存ノートの旧セクション見出しを新名に書き換える（1パス置換）。"""
    if not content:
        return content
    for old, new in SECTION_RENAMES.items():
        content = re.sub(
            r"^" + re.escape(old) + r"\s*$",
            new,
            content,
            flags=re.MULTILINE,
        )
    return content


def _sort_section_by_time(section_text: str) -> str:
    """セクション本文を、各エントリ先頭の `- HH:MM` で時刻昇順に並べ替える。

    1 エントリ＝先頭のトップレベル箇条書き行（`- ...`）＋それに続くインデント行
    （`    - 注文内容` など）。時刻を持たないエントリは末尾へ回す（安定ソート）。
    食事ログのように「入力順ではなく時刻順」で見たいセクション向け。
    """
    lines = section_text.split("\n")
    entries: list[list[str]] = []
    cur: list[str] | None = None
    for line in lines:
        # インデントの無いトップレベル箇条書きを新しいエントリの開始とみなす
        if re.match(r"^[-*]\s+", line):
            cur = [line]
            entries.append(cur)
        else:
            if cur is None:
                cur = [line]
                entries.append(cur)
            else:
                cur.append(line)

    time_re = re.compile(r"^[-*]\s*(\d{1,2}):(\d{2})\b")

    def _key(block: list[str]):
        m = time_re.match(block[0])
        if m:
            return (0, int(m.group(1)) * 60 + int(m.group(2)))
        return (1, 0)

    entries.sort(key=_key)  # list.sort は安定ソート
    return "\n".join("\n".join(b) for b in entries)


def update_section(
    current_content: str,
    text_to_add: str,
    section_header: str,
    sort_by_time: bool = False,
) -> str:
    """
    Obsidianのデイリーノート内で、定義された順序に基づいてセクションの内容を更新または新規追加する共通関数。
    ノート全体をパースして再構築することで、セクション間の空白行を統一し、項目内の不要な空白行を削除します。

    sort_by_time=True のとき、対象セクションの中身を `- HH:MM` の時刻順に並べ替える
    （食事ログなど、入力順ではなく時系列で見たいセクション向け）。
    """
    # 0. 旧セクション名のマイグレーション（既存ノート互換）
    current_content = migrate_legacy_sections(current_content)

    # 1. フロントマターと本文を分離
    frontmatter = ""
    body = current_content
    match = re.search(r"^(---\n.*?\n---)(.*)", current_content, re.DOTALL)
    if match:
        frontmatter = match.group(1).strip()
        body = match.group(2)

    # 2. 本文をセクション（見出し）ごとにパース
    lines = body.split("\n")
    preamble = []
    sections = {}
    current_section = None

    for line in lines:
        if line.startswith("## "):
            current_section = line.strip()
            if current_section not in sections:
                sections[current_section] = []
        else:
            if current_section:
                sections[current_section].append(line)
            else:
                preamble.append(line)

    # 3. 指定されたセクションにテキストを追加
    if section_header not in sections:
        sections[section_header] = []

    # 追加するテキスト自体に含まれる連続する空白行も事前に圧縮
    clean_text_to_add = re.sub(r"\n\s*\n", "\n", text_to_add.strip())
    if clean_text_to_add:
        sections[section_header].append(clean_text_to_add)

    # 対象セクションを時刻順に並べ替える（食事ログなど）
    if sort_by_time and sections.get(section_header):
        joined = "\n".join(sections[section_header])
        sections[section_header] = [_sort_section_by_time(joined)]

    # 3.5 見た目の正規化：日付（frontmatter か preamble のタイトルから推定）を元に
    #     タイトルを「# 📓 YYYY-MM-DD（曜）」へ統一し、プロパティを補完する。
    #     後で見返したときに「いつの・何曜日のノートか」が一目で分かるようにする。
    note_date = ""
    fm_date = re.search(r"(?m)^date:\s*(\d{4}-\d{2}-\d{2})", frontmatter)
    if fm_date:
        note_date = fm_date.group(1)
    else:
        pm = _DATE_RE.search("\n".join(preamble))
        if pm:
            note_date = pm.group(0)
    if note_date:
        wd = _weekday_ja(note_date)
        title = f"# 📓 {note_date}" + (f"（{wd}）" if wd else "")
        # 既存の H1 タイトル行は除去して統一タイトルに置き換える
        preamble = [ln for ln in preamble if not ln.lstrip().startswith("# ")]
        preamble.insert(0, title)
        frontmatter = _ensure_frontmatter(frontmatter, note_date, wd)

    # 4. ノート全体を美しいフォーマットで再構築
    output_blocks = []

    # フロントマターがあれば追加
    if frontmatter:
        output_blocks.append(frontmatter)

    # 見出し前のテキスト（# タイトル など）があれば追加
    preamble_text = "\n".join(preamble).strip()
    # 連続する空白行を圧縮
    preamble_text = re.sub(r"\n\s*\n", "\n", preamble_text)
    if preamble_text:
        output_blocks.append(preamble_text)

    # 定義された順序（SECTION_ORDER）に従ってセクションを配置。
    # 中身が空のセクション（見出しだけ）は出力せず、雑然とした空見出しを残さない。
    added_sections = set()
    for header in SECTION_ORDER:
        if header in sections:
            # セクション内の行を結合し、連続する空白行を1つに圧縮（項目内の空白行をなくす）
            raw_content = "\n".join(sections[header]).strip()
            clean_content = re.sub(r"\n\s*\n", "\n", raw_content)

            # 中身がある見出しだけを出力する
            if clean_content:
                output_blocks.append(f"{header}\n{clean_content}")
            added_sections.add(header)

    # SECTION_ORDERに未定義の未知のセクションがあれば末尾に配置（空見出しは出さない）
    for header, content_lines in sections.items():
        if header not in added_sections:
            raw_content = "\n".join(content_lines).strip()
            clean_content = re.sub(r"\n\s*\n", "\n", raw_content)

            if clean_content:
                output_blocks.append(f"{header}\n{clean_content}")

    # 各ブロック（フロントマター、タイトル、各見出しセクション）を「必ず1つの空白行（\n\n）」で繋いで出力
    return "\n\n".join(output_blocks) + "\n"


def update_frontmatter(content: str, updates: dict) -> str:
    """
    ObsidianのYAMLフロントマター(Properties)を更新または新規作成する関数。
    """
    # ★ 修正: 安全装置として、値が空の辞書 {} だった場合はアップデート対象から除外する
    clean_updates = {}
    for k, v in updates.items():
        if isinstance(v, dict) and not v:  # 値が空の辞書の場合
            continue
        clean_updates[k] = v

    match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)

    new_lines = []

    if match:
        frontmatter_raw = match.group(1)
        body = content[match.end() :]

        current_lines = frontmatter_raw.split("\n")
        skip_mode = False

        for line in current_lines:
            if skip_mode:
                if line.strip().startswith("-") or (
                    line.startswith(" ") and ":" not in line
                ):
                    continue
                else:
                    skip_mode = False

            key_match = re.match(r"^([^:\s]+):", line)
            if key_match:
                key = key_match.group(1).strip()
                if key in clean_updates:
                    skip_mode = True
                    continue

            new_lines.append(line)

        for k, v in clean_updates.items():
            if isinstance(v, list):
                new_lines.append(f"{k}:")
                for item in v:
                    new_lines.append(f"  - {item}")
            else:
                new_lines.append(f"{k}: {v}")

        return "---\n" + "\n".join(new_lines) + "\n---" + body

    else:
        new_lines.append("---")
        for k, v in clean_updates.items():
            if isinstance(v, list):
                new_lines.append(f"{k}:")
                for item in v:
                    new_lines.append(f"  - {item}")
            else:
                new_lines.append(f"{k}: {v}")
        new_lines.append("---\n")
        return "\n".join(new_lines) + content
