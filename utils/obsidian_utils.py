import logging
import re

# --- 定数定義 ---
# デイリーノートの見出し順序定義
# Botは項目を新規作成する際、この順序に従って適切な位置に挿入します。
SECTION_ORDER = [
    # --- 1. Timeline (リアルタイムメモ) ---
    "## 💬 Timeline",           # 日常のつぶやき・メモ (PartnerCog)

    # --- 2. Daily Summary (1日の振り返り整理) ---
    "## 📔 Daily Journal",      # AIによる振り返り日記 (DailyOrganizeCog)
    "## 📝 Events & Actions",   # 出来事・行動記録 (DailyOrganizeCog)
    "## 💡 Insights & Thoughts",# 考えたこと・気づき (DailyOrganizeCog)
    "## ➡️ Next Actions",       # ネクストアクション (DailyOrganizeCog)

    # --- 3. Input & Information (インプット・情報収集) ---
    "## 🍳 Recipes",            # レシピクリップ (WebClipService)
    "## 📺 YouTube",            # YouTube動画リンク (WebClipService)
    "## 🔗 WebClips",           # Web記事クリップ (WebClipService)
    "## 📖 Reading Log",        # 読書メモ (PartnerCog)

    # --- 4. Logs & Records (自動記録・活動データ) ---
    "## 📍 Location History",   # 位置情報ログ (LocationLogCog)
    "## 📊 Health Metrics"      # 健康データ (FitbitCog)
]

def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
    """
    Obsidianのデイリーノート内で、定義された順序に基づいてセクションの内容を更新または新規追加する共通関数。
    ノート全体をパースして再構築することで、セクション間の空白行を統一し、項目内の不要な空白行を削除します。
    """
    # 1. フロントマターと本文を分離
    frontmatter = ""
    body = current_content
    match = re.search(r'^(---\n.*?\n---)(.*)', current_content, re.DOTALL)
    if match:
        frontmatter = match.group(1).strip()
        body = match.group(2)

    # 2. 本文をセクション（見出し）ごとにパース
    lines = body.split('\n')
    preamble = []
    sections = {}
    current_section = None

    for line in lines:
        if line.startswith('## '):
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
    clean_text_to_add = re.sub(r'\n\s*\n', '\n', text_to_add.strip())
    if clean_text_to_add:
        sections[section_header].append(clean_text_to_add)

    # 4. ノート全体を美しいフォーマットで再構築
    output_blocks = []
    
    # フロントマターがあれば追加
    if frontmatter:
        output_blocks.append(frontmatter)
        
    # 見出し前のテキスト（# タイトル など）があれば追加
    preamble_text = "\n".join(preamble).strip()
    # 連続する空白行を圧縮
    preamble_text = re.sub(r'\n\s*\n', '\n', preamble_text)
    if preamble_text:
        output_blocks.append(preamble_text)
        
    # 定義された順序（SECTION_ORDER）に従ってセクションを配置
    added_sections = set()
    for header in SECTION_ORDER:
        if header in sections:
            # セクション内の行を結合し、連続する空白行を1つに圧縮（項目内の空白行をなくす）
            raw_content = "\n".join(sections[header]).strip()
            clean_content = re.sub(r'\n\s*\n', '\n', raw_content)
            
            # 見出しと中身を結合したブロックを作成
            if clean_content:
                output_blocks.append(f"{header}\n{clean_content}")
            else:
                output_blocks.append(f"{header}")
            added_sections.add(header)
            
    # SECTION_ORDERに未定義の未知のセクションがあれば末尾に配置
    for header, content_lines in sections.items():
        if header not in added_sections:
            raw_content = "\n".join(content_lines).strip()
            clean_content = re.sub(r'\n\s*\n', '\n', raw_content)
            
            if clean_content:
                output_blocks.append(f"{header}\n{clean_content}")
            else:
                output_blocks.append(f"{header}")

    # 各ブロック（フロントマター、タイトル、各見出しセクション）を「必ず1つの空白行（\n\n）」で繋いで出力
    return "\n\n".join(output_blocks) + "\n"

def update_frontmatter(content: str, updates: dict) -> str:
    """
    ObsidianのYAMLフロントマター(Properties)を更新または新規作成する関数。
    """
    match = re.search(r'^---\n(.*?)\n---', content, re.DOTALL)
    
    new_lines = []
    
    if match:
        frontmatter_raw = match.group(1)
        body = content[match.end():] 
        
        current_lines = frontmatter_raw.split('\n')
        skip_mode = False
        
        for line in current_lines:
            if skip_mode:
                if line.strip().startswith('-') or (line.startswith(' ') and ':' not in line):
                    continue
                else:
                    skip_mode = False
            
            key_match = re.match(r'^([^:\s]+):', line)
            if key_match:
                key = key_match.group(1).strip()
                if key in updates:
                    skip_mode = True 
                    continue
            
            new_lines.append(line)
        
        for k, v in updates.items():
            if isinstance(v, list):
                new_lines.append(f"{k}:")
                for item in v:
                    new_lines.append(f"  - {item}")
            else:
                new_lines.append(f"{k}: {v}")
        
        return f"---\n" + "\n".join(new_lines) + "\n---" + body

    else:
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