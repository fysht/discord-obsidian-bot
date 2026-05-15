"""投資憲法 (Investment_Constitution.md) のパーサー。

v2.0 形式（共通セクション + ## スタイル: <name> の繰り返し）と
v1.0 形式（単一セクション）の両方を扱う。
"""
from __future__ import annotations

import re
from typing import Optional


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_STYLE_HEADING_RE = re.compile(r"^##\s*スタイル[:：]\s*(\S+)\s*(.*)$", re.MULTILINE)
_SEPARATOR_RE = re.compile(r"^---+\s*$", re.MULTILINE)


def parse_constitution(text: str) -> dict:
    """投資憲法を共通セクションとスタイル別セクションに分解する。

    Returns:
        {
            "version": "2.0" / "1.0",
            "frontmatter": "<原文 frontmatter (なければ空)>",
            "common": "<共通セクション markdown>",
            "styles": {
                "trend_follow": {"title": "順張り...", "body": "<該当 markdown>"},
                "value": {...},
                ...
            }
        }

    v1.0（スタイル見出しなし）の場合は styles = {"value": {"title": "...", "body": "<全文>"}}
    として後方互換扱いにする。
    """
    if not text:
        return {"version": "1.0", "frontmatter": "", "common": "", "styles": {}}

    body = text
    frontmatter_text = ""
    m = _FRONTMATTER_RE.match(text)
    version = "1.0"
    if m:
        frontmatter_text = m.group(1)
        body = text[m.end():]
        v_match = re.search(r"^version\s*:\s*([\d.]+)", frontmatter_text, re.MULTILINE)
        if v_match:
            version = v_match.group(1).strip()

    style_matches = list(_STYLE_HEADING_RE.finditer(body))
    if not style_matches:
        # v1.0 互換: 全文を value スタイルとして扱う
        return {
            "version": version,
            "frontmatter": frontmatter_text,
            "common": body.strip(),
            "styles": {
                "value": {"title": "v1.0 互換（全文）", "body": body.strip()}
            },
        }

    common = body[:style_matches[0].start()].strip()
    styles: dict[str, dict] = {}
    for i, sm in enumerate(style_matches):
        style_name = sm.group(1).strip()
        title_extra = (sm.group(2) or "").strip()
        section_start = sm.end()
        section_end = style_matches[i + 1].start() if i + 1 < len(style_matches) else len(body)
        section_body = body[section_start:section_end]
        # 末尾の --- 区切りを除去
        section_body = _SEPARATOR_RE.split(section_body)[0].strip()
        styles[style_name] = {
            "title": title_extra or style_name,
            "body": section_body,
        }

    return {
        "version": version,
        "frontmatter": frontmatter_text,
        "common": common,
        "styles": styles,
    }


def get_style_section(text: str, style: str) -> Optional[str]:
    """憲法本文から指定スタイルのセクション（共通+スタイル別）を返す。

    存在しなければ None。
    """
    parsed = parse_constitution(text)
    styles = parsed.get("styles") or {}
    if style in styles:
        common = parsed.get("common") or ""
        body = styles[style].get("body") or ""
        return f"{common}\n\n{body}".strip()
    return None
