"""全ソース共通のアダプタインターフェース。呼び出し側はソースの種類を知らない。"""
from datetime import datetime
from typing import Protocol

from app.models import Event


class SourceAdapter(Protocol):
    name: str

    def fetch(self, since: datetime | None) -> list[Event]:
        """since 以降に発生した出来事を Event として返す"""
        ...
