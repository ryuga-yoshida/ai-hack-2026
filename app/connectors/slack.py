"""Slack アダプタ（空実装）。本ハッカソンでは自作チャットで代替する。"""
from datetime import datetime

from app.models import Event


class SlackAdapter:
    """Slack Web API 互換のアダプタ。

    GET conversations.history?channel={id}&oldest={ts}
    → {"ok": true, "messages": [{"type": "message", "user": str, "text": str, "ts": str}]}

    各 message を Event(kind="utterance", source="chat") に変換し、
    ref は "chat:<channel>#<ts>" とする。自作チャットアダプタと同一の Event を
    生成するため、差し替えは ADAPTERS の1行で済む。
    要求スコープは channels:history / channels:read の読み取りのみ。
    対象チャンネルは設定で明示したものに限り、全社検索は行わない。
    """
    name = "slack"

    def __init__(self, token: str = "", channels: list[str] | None = None):
        self.token, self.channels = token, channels or []

    def fetch(self, since: datetime | None) -> list[Event]:
        raise NotImplementedError("本ハッカソンでは自作チャットで代替")
