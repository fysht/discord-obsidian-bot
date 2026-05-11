"""Gmail API ラッパー。受信トレイ未読の取得・要約・既読化・ゴミ箱移動を行う。

設計:
- 認証は Drive と同じ creds を流用（OAuth スコープ `gmail.modify` を追加済み）。
- 受信ごとに DB に保存し、AI 要約 + 重要度判定の結果をキャッシュ。
- 重要度 high のみプッシュ通知を送る（low/medium は静かに DB 保存）。
"""
import asyncio
import base64
import logging
from typing import Optional

from googleapiclient.discovery import build


def _decode_b64_url(data: str) -> str:
    try:
        # Gmail API は URL-safe base64 (パディングなし) を返す
        padded = data + "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(padded)
        # 文字コード推定: UTF-8 優先、失敗時 ISO
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("iso-2022-jp", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """MIME ツリーからテキスト本文を抽出する。text/plain 優先、なければ text/html を素朴に整形。"""
    if not payload:
        return ""
    mime_type = payload.get("mimeType", "")
    body_data = (payload.get("body") or {}).get("data")
    if mime_type == "text/plain" and body_data:
        return _decode_b64_url(body_data)
    parts = payload.get("parts") or []
    # text/plain を最優先
    for p in parts:
        if p.get("mimeType") == "text/plain":
            data = (p.get("body") or {}).get("data")
            if data:
                return _decode_b64_url(data)
    # 再帰探索
    for p in parts:
        nested = _extract_body(p)
        if nested:
            return nested
    # text/html フォールバック（タグを雑に除去）
    if mime_type == "text/html" and body_data:
        import re
        html = _decode_b64_url(body_data)
        return re.sub(r"<[^>]+>", " ", html)
    for p in parts:
        if p.get("mimeType") == "text/html":
            data = (p.get("body") or {}).get("data")
            if data:
                import re
                return re.sub(r"<[^>]+>", " ", _decode_b64_url(data))
    return ""


class GmailService:
    def __init__(self, creds):
        self.creds = creds

    def get_service(self):
        if not self.creds:
            return None
        return build("gmail", "v1", credentials=self.creds)

    async def list_unread(self, max_results: int = 20, newer_than_days: int = 1) -> list[dict]:
        """未読メールの最小限のメタ情報を返す（id 一覧のみ）。"""
        service = self.get_service()
        if not service:
            return []
        try:
            query = f"is:unread newer_than:{newer_than_days}d -category:promotions -category:social"
            res = await asyncio.to_thread(
                lambda: service.users().messages().list(
                    userId="me", q=query, maxResults=max_results
                ).execute()
            )
            return res.get("messages", []) or []
        except Exception as e:
            logging.error(f"Gmail list_unread error: {e}")
            return []

    async def get_message(self, message_id: str) -> Optional[dict]:
        """メッセージ全体（ヘッダ + 本文）を取得して dict で返す。"""
        service = self.get_service()
        if not service:
            return None
        try:
            msg = await asyncio.to_thread(
                lambda: service.users().messages().get(
                    userId="me", id=message_id, format="full"
                ).execute()
            )
            headers = {h["name"].lower(): h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
            return {
                "id": msg.get("id"),
                "thread_id": msg.get("threadId"),
                "snippet": msg.get("snippet", ""),
                "label_ids": msg.get("labelIds", []),
                "internal_date": msg.get("internalDate"),
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "date": headers.get("date", ""),
                "body": _extract_body(msg.get("payload", {})),
            }
        except Exception as e:
            logging.error(f"Gmail get_message error ({message_id}): {e}")
            return None

    async def mark_as_read(self, message_id: str) -> bool:
        service = self.get_service()
        if not service:
            return False
        try:
            await asyncio.to_thread(
                lambda: service.users().messages().modify(
                    userId="me", id=message_id,
                    body={"removeLabelIds": ["UNREAD"]},
                ).execute()
            )
            return True
        except Exception as e:
            logging.error(f"Gmail mark_as_read error: {e}")
            return False

    async def trash(self, message_id: str) -> bool:
        """ゴミ箱に移動（完全削除ではない）。"""
        service = self.get_service()
        if not service:
            return False
        try:
            await asyncio.to_thread(
                lambda: service.users().messages().trash(userId="me", id=message_id).execute()
            )
            return True
        except Exception as e:
            logging.error(f"Gmail trash error: {e}")
            return False
