# --- 定数定義 ---
# デイリーノートの見出しの順序をここで一元管理します
SECTION_ORDER = [
    "## Health Metrics",
    "## Location Logs",
    "## Daily Summary",
    "## WebClips",
    "## YouTube Summaries",
    "## Zero-Second Thinking",
    "## Memo"
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

    # セクションが既に存在するか確認
    try:
        header_index = lines.index(section_header)
        # 既存セクションの場合、内容を追加するロジック
        insert_index = header_index + 1
        while insert_index < len(lines) and (lines[insert_index].strip().startswith('- ') or not lines[insert_index].strip()):
            insert_index += 1
        lines.insert(insert_index, text_to_add)
        return "\n".join(lines)
    except ValueError:
        # セクションが存在しない場合、正しい位置に新規作成
        new_section_with_header = f"\n{section_header}\n{text_to_add}"
        
        existing_sections = {line.strip(): i for i, line in enumerate(lines) if line.strip() in SECTION_ORDER}
        
        try:
            new_section_order_index = SECTION_ORDER.index(section_header)
        except ValueError: # SECTION_ORDERにないヘッダーは末尾に追加
             return current_content.strip() + "\n" + new_section_with_header

        # 挿入位置を決定
        # 1. 自身の前にあるべきセクションを探す
        insert_after_index = -1
        for i in range(new_section_order_index - 1, -1, -1):
            preceding_header = SECTION_ORDER[i]
            if preceding_header in existing_sections:
                header_line_index = existing_sections[preceding_header]
                insert_after_index = header_line_index + 1
                while insert_after_index < len(lines) and not lines[insert_after_index].strip().startswith('## '):
                    insert_after_index += 1
                break
        
        if insert_after_index != -1:
            lines.insert(insert_after_index, new_section_with_header)
            return "\n".join(lines).strip()

        # 2. 自身の後にあるべきセクションを探す
        insert_before_index = -1
        for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
            following_header = SECTION_ORDER[i]
            if following_header in existing_sections:
                insert_before_index = existing_sections[following_header]
                break
        
        if insert_before_index != -1:
            lines.insert(insert_before_index, new_section_with_header + "\n")
            return "\n".join(lines).strip()

        # 3. どの既存セクションとも順序関係がない場合、ノートの末尾に追加
        if current_content.strip():
             lines.append("")
        lines.append(section_header)
        lines.append(text_to_add)
        return "\n".join(lines)