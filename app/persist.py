"""Cloud Run 用の永続化: SQLite とライブラリ（fixtures/excel）を GCS に定期バックアップし、起動時に復元する。
GCS_BUCKET が未設定ならローカル運用（何もしない）。"""
import hashlib
import io
import logging
import os
import tarfile
import threading
import time

from app import config

log = logging.getLogger(__name__)
BUCKET = os.getenv("GCS_BUCKET", "")
OBJECT = os.getenv("GCS_OBJECT", "backup/state.tar.gz")
INTERVAL = int(os.getenv("GCS_SYNC_SEC", "120"))
_last_digest = ""


def _client():
    from google.cloud import storage
    return storage.Client()


def _paths() -> list:
    return [config.DB_PATH.parent, config.LIBRARY_DIR]


def _make_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in _paths():
            if p.exists():
                tar.add(str(p), arcname=str(p.relative_to(config.ROOT)),
                        filter=lambda ti: None if (ti.name.endswith(("-wal", "-shm")) or "/.trash" in ti.name) else ti)
    return buf.getvalue()


def restore() -> bool:
    """DB が無ければ GCS から復元。あれば何もしない（ローカルの状態を優先）"""
    if not BUCKET:
        return False
    if config.DB_PATH.exists():
        log.info("persist: ローカル DB があるため復元しない")
        return False
    try:
        blob = _client().bucket(BUCKET).blob(OBJECT)
        if not blob.exists():
            log.info("persist: バックアップ無し（初回）")
            return False
        data = blob.download_as_bytes()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(str(config.ROOT))
        log.info("persist: GCS から復元 (%d bytes)", len(data))
        return True
    except Exception as e:
        log.warning("persist: 復元失敗 %s", e)
        return False


def backup(force: bool = False) -> bool:
    global _last_digest
    if not BUCKET:
        return False
    try:
        # WAL の内容を本体に書き戻してから固める
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH); conn.execute("PRAGMA wal_checkpoint(TRUNCATE)"); conn.close()
        data = _make_tar()
        digest = hashlib.sha1(data).hexdigest()
        if digest == _last_digest and not force:
            return False
        _client().bucket(BUCKET).blob(OBJECT).upload_from_string(data, content_type="application/gzip")
        _last_digest = digest
        log.info("persist: GCS へバックアップ (%d bytes)", len(data))
        return True
    except Exception as e:
        log.warning("persist: バックアップ失敗 %s", e)
        return False


def start_background() -> None:
    if not BUCKET:
        return
    def loop():
        while True:
            time.sleep(INTERVAL)
            backup()
    threading.Thread(target=loop, daemon=True, name="gcs-sync").start()
