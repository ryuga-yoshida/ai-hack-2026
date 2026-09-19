"""SharePoint Online アダプタ（空実装）。本ハッカソンでは fixtures/excel/ の版ファイルで代替する。"""
from datetime import datetime

from app.models import Event


class SharePointAdapter:
    """Microsoft Graph API 互換のアダプタ。

    GET /sites/{site-id}/drive/items/{item-id}/versions
    → {"value": [{"id": "2.0", "lastModifiedDateTime": str,
                  "lastModifiedBy": {"user": {"displayName": str}}, "size": int}]}
    GET /sites/{site-id}/drive/items/{item-id}/versions/{version-id}/content
    → .xlsx のバイナリ

    連続する2版をダウンロードして app.excel_diff.diff_workbooks に渡し、
    ExcelAdapter と同一の Event(kind="artifact_change", source="excel") を生成する。
    ref は "<ファイル名>:<シート>!<セル>"、meta["url"] に SPO の URL を持つ。
    要求スコープは Files.Read.All / Sites.Read.All の読み取りのみ。
    対象は設定で明示したサイト・フォルダに限る。
    """
    name = "sharepoint"

    def __init__(self, tenant: str = "", site_id: str = "", token: str = ""):
        self.tenant, self.site_id, self.token = tenant, site_id, token

    def fetch(self, since: datetime | None) -> list[Event]:
        raise NotImplementedError("本ハッカソンでは fixtures/excel の版ファイルで代替")
