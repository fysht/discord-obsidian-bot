import logging

# --- 定数定義 ---
# デイリーノートの見出しの順序をここで一元管理します
SECTION_ORDER = [
    # --- 1. メディアクリップ ---
    "## WebClips",
    "## YouTube Summaries",
    "## Recipes", 
    "## Reading Notes", 
    
    # --- 2. メモ・思考 ---
    "## Memo",
    "## Handwritten Memos", 
    "## Zero-Second Thinking", 
    
    # --- 3. 学習・日誌 ---
    "## Journal",
    "## English Learning Logs", 
    "## Sakubun Logs", 
    "## Task Log", 
    "## Make Time Note", 
    
    # --- 4. 健康・ログ ---
    "## Health Metrics", 
    "## Location Logs", 
    "## Life Logs", 
    
    # --- 5. サマリー ---
    "## Life Logs Summary",
    "## Daily Summary"
]

def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
    """
    Obsidianのデイリーノート内で、定義された順序に基づいてセクションの内容を更新または新規追加する共通関数
    空白行を極力排除する仕様に変更。

    Args:
        current_content (str): 現在のノートの全内容
        text_to_add (str): 追加または更新するテキスト。
        section_header (str): 対象となるセクションの見出し (例: "## WebClips")

    Returns:
        str: 更新後のノートの全内容
    """
    lines = current_content.split('\n')
    original_lines = list(lines) # 元のリストを保持

    # 1. ターゲットのセクションが既に存在するか確認 (大文字小文字/空白を無視して検索)
    header_index = -1
    normalized_target_header = section_header.strip().lstrip('#').strip().lower()
    
    for i, line in enumerate(lines):
        normalized_line_header = line.strip().lstrip('#').strip().lower()
        if line.strip().startswith('##') and normalized_line_header == normalized_target_header:
            header_index = i
            break
    
    # --- ケースA: セクションが既に存在する場合 ---
    if header_index != -1:
        insert_index = header_index + 1
        # 見出しの直後から、次の見出しまたはファイルの終わりまでを探索して末尾を見つける
        while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
            insert_index += 1
        
        # リストが連続するように、そのまま挿入（空行なし）
        lines.insert(insert_index, text_to_add)
        return "\n".join(lines)

    # --- ケースB: セクションが存在しない場合（新規作成） ---
    else:
        # 挿入するブロックを作成（ヘッダー + 内容）
        new_section_block = f"{section_header}\n{text_to_add}"
        
        # 既存のセクションがファイル内のどこにあるかマッピング
        existing_sections_map = {}
        for i, line in enumerate(original_lines):
            line_strip = line.strip()
            if line_strip in SECTION_ORDER:
                existing_sections_map[line_strip] = i
        
        # 新しいセクションの理想的な順序インデックスを取得
        try:
            new_section_order_index = SECTION_ORDER.index(section_header)
        except ValueError:
             logging.warning(f"utils: '{section_header}' はSECTION_ORDERに未定義です。末尾に追加します。")
             return current_content.strip() + f"\n{new_section_block}"

        # 挿入位置の決定ロジック
        
        # B-1. 自身の「前」にあるべきセクションのうち、ファイル内に存在する最後のものを探す
        insert_after_index = -1
        for i in range(new_section_order_index - 1, -1, -1):
            preceding_header = SECTION_ORDER[i]
            if preceding_header in existing_sections_map:
                header_line_index = existing_sections_map[preceding_header]
                # そのセクションの末尾を探す
                section_end_index = header_line_index + 1
                while section_end_index < len(original_lines) and not original_lines[section_end_index].strip().startswith('## '):
                    section_end_index += 1
                insert_after_index = section_end_index
                break
        
        if insert_after_index != -1:
            # 前のセクションの直後に挿入（空行なし）
            original_lines.insert(insert_after_index, new_section_block)
            return "\n".join(original_lines)

        # B-2. 自身の「後」にあるべきセクションのうち、ファイル内に存在する最初のものを探す
        insert_before_index = -1
        for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
            following_header = SECTION_ORDER[i]
            if following_header in existing_sections_map:
                insert_before_index = existing_sections_map[following_header]
                break
        
        if insert_before_index != -1:
            # 後のセクションの直前に挿入（空行なし）
            original_lines.insert(insert_before_index, new_section_block)
            return "\n".join(original_lines)

        # B-3. 既存セクションが何もない場合
        if current_content.strip():
             return f"{current_content.strip()}\n{new_section_block}"
        else:
            return new_section_block