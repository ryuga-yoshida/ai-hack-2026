"""Confluence アダプタ（空実装）。本ハッカソンでは自作 Wiki で代替する。"""
from datetime import datetime

from app.models import Event


class ConfluenceAdapter:
    """Atlassian REST API v2 互換のアダプタ。

    GET /wiki/api/v2/pages/{id}/versions
    → {"results": [{"number": int, "createdAt": str, "authorId": str}]}
    GET /wiki/api/v2/pages/{id}?version={n}&body-format=storage
    → {"id": str, "title": str, "body": {"storage": {"value": str}}}

    版間の本文差分を Event(kind="artifact_change", source="wiki") に変換する。
    自作 Wiki アダプタと同一の Event を生成するため、
    本アダプタへの差し替えは ADAPTERS の1行を書き換えるだけで済む。
    読み取り専用スコープ（read:page:confluence）のみを要求する。
    """
    name = "confluence"

    def __init__(self, base_url: str = "", token: str = "", space_id: str = ""):
        self.base_url, self.token, self.space_id = base_url, token, space_id

    def fetch(self, since: datetime | None) -> list[Event]:
        raise NotImplementedError("本ハッカソンでは自作Wikiで代替")
