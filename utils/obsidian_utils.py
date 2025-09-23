# --- 定数定義 ---
# デイリーノートの見出しの順序をここで一元管理します
SECTION_ORDER = [
    "## WebClips",
    "## YouTube Summaries",
    "## Memo",
    "## Handwritten Memos",
    "## Zero-Second Thinking",
    "## Task Log",
    "## Health Metrics",
    "## Location Logs",
    "## Daily Summary",
    "## Make Time Note",
]

def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
    """
    Obsidianのデイリーノート内で、定義された順序に基づいてセクションの内容を更新または新規追加する共通関数

    Args:
        current_content (str): 現在のノートの全内容
        text_to_add (str): 追加または更新するテキスト。リンクやリストなど
        section_header (str): 対象となるセクションの見出し (例: "## WebClips")

    Returns:
        str: 更新後のノートの全内容
    """
    lines = current_content.split('\n')
    original_lines = list(lines) # 元のリストをコピー

    # セクションが既に存在するか確認
    try:
        # 空白行などを除外して完全一致で検索
        header_index = -1
        for i, line in enumerate(lines):
            if line.strip() == section_header:
                header_index = i
                break
        
        if header_index == -1:
            raise ValueError

        # 既存セクションの場合、内容を追加するロジック
        insert_index = header_index + 1
        # 見出しの直後から、次の見出しまたはファイルの終わりまでを探索
        while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
            insert_index += 1
        
        # 挿入するテキストの前後に空行がない場合、追加する
        if insert_index > 0 and lines[insert_index-1].strip() != "":
            lines.insert(insert_index, "")
        lines.insert(insert_index, text_to_add)
        return "\n".join(lines)

    except ValueError:
        # セクションが存在しない場合、正しい位置に新規作成
        new_section_with_header = f"\n\n{section_header}\n{text_to_add}"
        
        existing_sections = {line.strip(): i for i, line in enumerate(original_lines) if line.strip() in SECTION_ORDER}
        
        try:
            new_section_order_index = SECTION_ORDER.index(section_header)
        except ValueError: # SECTION_ORDERにないヘッダーは末尾に追加
             return current_content.strip() + new_section_with_header

        # 挿入位置を決定
        # 1. 自身の前にあるべきセクションを探す
        insert_after_index = -1
        for i in range(new_section_order_index - 1, -1, -1):
            preceding_header = SECTION_ORDER[i]
            if preceding_header in existing_sections:
                header_line_index = existing_sections[preceding_header]
                # そのセクションの末尾を探す
                section_end_index = header_line_index + 1
                while section_end_index < len(original_lines) and not original_lines[section_end_index].strip().startswith('## '):
                    section_end_index += 1
                insert_after_index = section_end_index
                break
        
        if insert_after_index != -1:
            original_lines.insert(insert_after_index, new_section_with_header)
            return "\n".join(original_lines).strip()

        # 2. 自身の後にあるべきセクションを探す
        insert_before_index = -1
        for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
            following_header = SECTION_ORDER[i]
            if following_header in existing_sections:
                insert_before_index = existing_sections[following_header]
                break
        
        if insert_before_index != -1:
            original_lines.insert(insert_before_index, new_section_with_header)
            return "\n".join(original_lines).strip()

        # 3. どの既存セクションとも順序関係がない場合、ノートの末尾に追加
        if current_content.strip():
             return current_content.strip() + new_section_with_header
        else:
            # コンテンツが空の場合は、先頭の不要な改行や線は削除
            return f"{section_header}\n{text_to_add}"