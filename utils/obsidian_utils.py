import logging
import re

# --- 定数定義 ---
# デイリーノートの見出し順序定義
# Botは項目を新規作成する際、この順序に従って適切な位置に挿入します。
SECTION_ORDER = [
    # --- 1. Daily Context (朝・コンテキスト) ---
    "## Weather",           # 天気予報 (NewsCog) - 旧テキスト形式用(互換性のため維持)
    "## Habits",            # 習慣トラッカー (HabitCog) - 旧テキスト形式用

    # --- 2. Input & Information (インプット・情報収集) ---
    "## WebClips",          # Web記事クリップ (WebClipCog)
    "## YouTube",           # YouTube動画リンク (PartnerCog) <-- 追加
    "## YouTube Summaries", # 動画要約 (YouTubeCog)
    "## Reading Notes",     # 読書メモ (BookCog)
    "## Recipes",           # レシピ (RecipeCog)

    # --- 3. Output & Thoughts (アウトプット・思考・学習) ---
    "## Memo",              # テキストメモ (MemoCog)
    "## Handwritten Memos", # 手書きメモ画像 (HandwrittenMemoCog)
    "## Zero-Second Thinking", # 0秒思考 (ZeroSecondThinkingCog)
    "## Journal",           # 日記・ジャーナル (JournalCog)
    "## English Learning Logs", # 英語学習ログ (EnglishLearningCog)

    # --- 4. Logs & Records (ログ・記録・活動データ) ---
    "## Task Log",          # タスクログ (TodoCog)
    "## Completed Tasks",   # 完了タスク (TodoCog)
    "## Health Metrics",    # 健康データ (FitbitCog)
    "## Location Logs",     # 位置情報ログ (LocationLogCog)
    "## Life Logs"          # 生活ログ (LifeLogCog)
]

def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
    """
    Obsidianのデイリーノート内で、定義された順序に基づいてセクションの内容を更新または新規追加する共通関数。
    
    Args:
        current_content (str): 現在のノートの全内容
        text_to_add (str): 追加または更新するテキスト (見出しを含まない内容のみ)
        section_header (str): 対象となるセクションの見出し (例: "## Journal")

    Returns:
        str: 更新後のノートの全内容
    """
    lines = current_content.split('\n')
    original_lines = list(lines) # 参照用

    # 1. ターゲットのセクションが既に存在するか検索 (大文字小文字/空白無視)
    header_index = -1
    normalized_target_header = section_header.strip().lstrip('#').strip().lower()
    
    for i, line in enumerate(lines):
        # 行が "## " で始まり、かつ中身が一致するか確認
        if line.strip().startswith('##'):
            normalized_line_header = line.strip().lstrip('#').strip().lower()
            if normalized_line_header == normalized_target_header:
                header_index = i
                break
    
    # --- ケースA: セクションが既に存在する場合 -> 追記 ---
    if header_index != -1:
        # 見出しの次の行から探索し、次の見出し(##)の手前、またはファイル末尾に追加位置を決める
        insert_index = header_index + 1
        while insert_index < len(lines):
            line = lines[insert_index].strip()
            if line.startswith('## '):
                break
            insert_index += 1
        
        # 挿入 (直前が空行でなければ空行を入れて読みやすくする)
        if insert_index > 0 and lines[insert_index-1].strip() != "":
            lines.insert(insert_index, "")
            insert_index += 1
        
        lines.insert(insert_index, text_to_add)
        return "\n".join(lines)

    # --- ケースB: セクションが存在しない場合 -> 新規作成して挿入 ---
    else:
        # 新しいセクションブロックを作成（前後に空行を入れて視認性を確保）
        new_section_block = f"\n{section_header}\n{text_to_add}\n"
        
        # 現在のファイルに含まれる既存セクションの位置を特定
        existing_indices = {} # {"## Planning": 行番号, ...}
        for i, line in enumerate(original_lines):
            clean_line = line.strip()
            if clean_line in SECTION_ORDER:
                existing_indices[clean_line] = i
        
        # 新しいセクションの理想的な順序インデックスを取得
        try:
            target_order_idx = SECTION_ORDER.index(section_header)
        except ValueError:
             # 定義にない見出しの場合は、ログを出して末尾に追加
             logging.warning(f"utils: '{section_header}' はSECTION_ORDERに未定義です。末尾に追加します。")
             return current_content.strip() + f"\n\n{section_header}\n{text_to_add}"

        # 挿入位置の決定ロジック:
        # 「自分の本来の位置より『後ろ』にあるべきセクション」のうち、
        # 現在のファイル内に存在する『一番最初』のものを見つけ、その直前に割り込ませる。
        insert_before_line_index = -1
        
        for i in range(target_order_idx + 1, len(SECTION_ORDER)):
            next_header = SECTION_ORDER[i]
            if next_header in existing_indices:
                insert_before_line_index = existing_indices[next_header]
                break
        
        if insert_before_line_index != -1:
            # 見つかったセクションの前に挿入
            lines.insert(insert_before_line_index, new_section_block.strip())
            # 挿入箇所の前後に空行を確保
            if insert_before_line_index > 0 and lines[insert_before_line_index-1].strip() != "":
                 lines.insert(insert_before_line_index, "")
            
            # 挿入したブロックの後ろにも空行が必要なら追加（次の見出しとの間）
            return "\n".join(lines)
        
        else:
            # 後ろにあるべきセクションがファイル内に一つもない場合 -> 
            # 「自分の本来の位置より『前』にあるべきセクション」を探すまでもなく、
            # ファイルの末尾に追加すれば順序は守られる。
            return current_content.strip() + f"\n\n{section_header}\n{text_to_add}"

def update_frontmatter(content: str, updates: dict) -> str:
    """
    ObsidianのYAMLフロントマター(Properties)を更新または新規作成する関数。
    
    Args:
        content (str): ノートの全文
        updates (dict): 更新したいキーと値の辞書。
                        値がリストの場合はYAMLのリスト形式で出力します。
                        値がNoneの場合はキーを削除しません(上書き動作)。
    Returns:
        str: 更新後のノート全文
    """
    # フロントマターの検出 (^--- ... ---)
    match = re.search(r'^---\n(.*?)\n---', content, re.DOTALL)
    
    new_lines = []
    
    if match:
        # 既存フロントマターがある場合
        frontmatter_raw = match.group(1)
        body = content[match.end():] # ---の後ろ全て
        
        # 既存行を解析して更新対象は除外、それ以外は保持
        current_lines = frontmatter_raw.split('\n')
        
        # リストのパース状態管理
        skip_mode = False
        
        for line in current_lines:
            # 前の行で処理済みキーのリスト項目だった場合はスキップ
            if skip_mode:
                # リスト項目(インデント付きorハイフン)ならスキップ
                if line.strip().startswith('-') or (line.startswith(' ') and ':' not in line):
                    continue
                else:
                    skip_mode = False
            
            # キーの検出 (例: "weather: ...")
            # 行頭から始まり、コロンを含むものをキーとみなす
            key_match = re.match(r'^([^:\s]+):', line)
            if key_match:
                key = key_match.group(1).strip()
                if key in updates:
                    # 更新対象のキーなので、ここには出力せず、あとで新しい値を出力する
                    skip_mode = True # リストなどの場合、後続行をスキップさせる
                    continue
            
            new_lines.append(line)
        
        # 更新・新規キーの追加
        for k, v in updates.items():
            if isinstance(v, list):
                new_lines.append(f"{k}:")
                for item in v:
                    new_lines.append(f"  - {item}")
            else:
                new_lines.append(f"{k}: {v}")
        
        # 再構築
        return f"---\n" + "\n".join(new_lines) + "\n---" + body

    else:
        # フロントマターがない場合、新規作成
        new_lines.append("---")
        for k, v in updates.items():
            if isinstance(v, list):
                new_lines.append(f"{k}:")
                for item in v:
                    new_lines.append(f"  - {item}")
            else:
                new_lines.append(f"{k}: {v}")
        new_lines.append("---\n")
        return "\n".join(new_lines) + content