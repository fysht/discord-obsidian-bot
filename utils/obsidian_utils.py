import logging

# --- 定数定義 ---
# デイリーノートの見出しの順序をここで一元管理します
# (全cogsをスキャンし、使用されるすべてのヘッダーを網羅・整理)
SECTION_ORDER = [
    # --- 1. メディアクリップ ---
    "## WebClips",
    "## YouTube Summaries",
    "## Recipes", # ★ recipe_cog.py / youtube_cog.py(レシピ) 用
    "## Reading Notes", # ★ book_cog.py 用
    
    # --- 2. メモ・思考 ---
    "## Memo", # sync_worker (テキスト), voice_memo_cog
    "## Handwritten Memos", # handwritten_memo_cog
    "## Zero-Second Thinking", # zero-second_thinking_cog
    
    # --- 3. 学習・日誌 ---
    "## Journal", # ★ journal_cog.py 用
    "## English Learning Logs", # ★ english_learning_cog.py 用
    "## Sakubun Logs", # ★ english_learning_cog.py 用
    "## Task Log", # (将来用・または他Cogで使用)
    "## Make Time Note", # (将来用・または他Cogで使用)
    
    # --- 4. 健康・ログ ---
    "## Health Metrics", # fitbit_cog
    "## Location Logs", # location_log_cog
    "## Life Logs", # lifelog_cog
    
    # --- 5. サマリー ---
    "## Daily Summary" # (summary_cog.py 用 - ※現在は未使用)
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

    # セクションが既に存在するか確認 (大文字小文字/空白を無視)
    try:
        header_index = -1
        # ターゲットヘッダーを正規化
        normalized_target_header = section_header.strip().lstrip('#').strip().lower()
        
        for i, line in enumerate(lines):
            # 行のヘッダーも正規化して比較
            normalized_line_header = line.strip().lstrip('#').strip().lower()
            if line.strip().startswith('##') and normalized_line_header == normalized_target_header:
                header_index = i
                break
        
        if header_index == -1:
            raise ValueError

        # 既存セクションの場合、内容を追加するロジック
        insert_index = header_index + 1
        # 見出しの直後から、次の見出しまたはファイルの終わりまでを探索
        while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
            insert_index += 1
        
        # 挿入するテキストの前に空行がない場合、追加する
        if insert_index > 0 and lines[insert_index-1].strip() != "":
            lines.insert(insert_index, "")
            insert_index += 1
            
        lines.insert(insert_index, text_to_add)
        return "\n".join(lines)

    except ValueError:
        # セクションが存在しない場合、正しい位置に新規作成
        new_section_with_header = f"\n\n{section_header}\n{text_to_add}"
        
        # 既存のセクションがファイル内のどこにあるかマッピング
        existing_sections_map = {}
        for i, line in enumerate(original_lines):
            line_strip = line.strip()
            # SECTION_ORDERにあるヘッダーかどうか
            if line_strip in SECTION_ORDER:
                existing_sections_map[line_strip] = i
        
        try:
            new_section_order_index = SECTION_ORDER.index(section_header)
        except ValueError: # SECTION_ORDERにないヘッダーは末尾に追加
             logging.warning(f"utils: '{section_header}' はSECTION_ORDERに定義されていません。ノートの末尾に追加します。")
             return current_content.strip() + new_section_with_header

        # --- 挿入位置を決定 ---
        
        # 1. 自身の「前」にあるべきセクションのうち、ファイル内に存在する最後のものを探す
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
            # 適切なセクションの後ろに挿入
            original_lines.insert(insert_after_index, new_section_with_header)
            return "\n".join(original_lines).strip()

        # 2. 自身の「後」にあるべきセクションのうち、ファイル内に存在する最初のものを探す
        insert_before_index = -1
        for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
            following_header = SECTION_ORDER[i]
            if following_header in existing_sections_map:
                insert_before_index = existing_sections_map[following_header]
                break
        
        if insert_before_index != -1:
            # 適切なセクションの前に挿入
            original_lines.insert(insert_before_index, new_section_with_header)
            return "\n".join(original_lines).strip()

        # 3. どの既存セクションとも順序関係がない場合（＝ファイルが空か、他のセクションが一つもない）、末尾に追加
        if current_content.strip():
             return current_content.strip() + new_section_with_header
        else:
            # コンテンツが空の場合は、先頭の不要な改行は削除
            # headerが改行で始まらないように lstrip()
            return f"{section_header.lstrip()} \n{text_to_add}"