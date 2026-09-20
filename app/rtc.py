"""会議室のシグナリング（WebSocket）。WebRTC のメッシュ接続を仲介するだけで、映像・音声は通らない。

部屋ごとに {名前: WebSocket} を持ち、offer / answer / ice を宛先に中継する。
新しく入った人には既存メンバーの一覧を返し、既存メンバーが offer を作る（新参者は待つ）。
"""
import asyncio
import json
import logging
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("rtc")

rooms: dict[str, dict[str, WebSocket]] = {}
states: dict[str, dict[str, dict]] = {}   # room -> name -> {mic, cam}


async def _send(ws: WebSocket, msg: dict) -> None:
    try:
        await ws.send_text(json.dumps(msg, ensure_ascii=False))
    except Exception:
        pass


async def broadcast(room: str, msg: dict, exclude: str | None = None) -> None:
    for name, ws in list(rooms.get(room, {}).items()):
        if name != exclude:
            await _send(ws, msg)


async def handle(ws: WebSocket, room: str, name: str) -> None:
    await ws.accept()
    peers = rooms.setdefault(room, {})
    if name in peers:   # 同名の再接続は古い方を閉じる
        try:
            await peers[name].close()
        except Exception:
            pass
    peers[name] = ws
    states.setdefault(room, {})[name] = {"mic": True, "cam": True, "screen": False, "hand": False}
    await _send(ws, {"type": "welcome", "peers": [p for p in peers if p != name], "states": states[room]})
    await broadcast(room, {"type": "peer-joined", "name": name, "at": datetime.now().isoformat()}, exclude=name)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            if t in ("offer", "answer", "ice"):
                target = peers.get(msg.get("to", ""))
                if target:
                    await _send(target, {**msg, "from": name})
            elif t == "state":
                states[room][name] = {"mic": bool(msg.get("mic", True)), "cam": bool(msg.get("cam", True)),
                                      "screen": bool(msg.get("screen", False)), "hand": bool(msg.get("hand", False))}
                await broadcast(room, {"type": "state", "name": name, **states[room][name]}, exclude=name)
            elif t == "chat":
                text = str(msg.get("text", ""))[:500]
                await broadcast(room, {"type": "chat", "name": name, "text": text, "at": datetime.now().strftime("%H:%M")})
                try:
                    from app import db
                    from app.connectors import meet
                    conn = db.connect(); meet.add_chat(conn, room, name, text); conn.close()
                except Exception as e:
                    log.warning("meeting chat save failed: %s", e)
            elif t == "caption":
                text = str(msg.get("text", ""))[:500]
                await broadcast(room, {"type": "caption", "name": name, "text": text, "final": bool(msg.get("final"))}, exclude=name)
                if msg.get("final") and text.strip():
                    try:
                        from app import db
                        from app.connectors import meet
                        conn = db.connect(); meet.add_caption(conn, room, name, text.strip()); conn.close()
                    except Exception as e:
                        log.warning("caption save failed: %s", e)
            elif t == "reaction":
                await broadcast(room, {"type": "reaction", "name": name, "emoji": str(msg.get("emoji", "👍"))[:4]})
            elif t == "notes":
                await broadcast(room, {"type": "notes", "name": name, "body": str(msg.get("body", ""))[:20000]}, exclude=name)
            elif t == "end":
                await broadcast(room, {"type": "ended", "by": name})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws error %s/%s: %s", room, name, e)
    finally:
        if peers.get(name) is ws:
            peers.pop(name, None)
            states.get(room, {}).pop(name, None)
        if not peers:
            rooms.pop(room, None)
            states.pop(room, None)
        await broadcast(room, {"type": "peer-left", "name": name})


def room_members(room: str) -> list[str]:
    return list(rooms.get(room, {}).keys())


# ---------- チャットの更新通知（WebSocket push） ----------

chat_subscribers: set[WebSocket] = set()


async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    chat_subscribers.add(ws)
    try:
        while True:
            raw = await ws.receive_text()   # ping か typing 通知
            if raw.startswith("{"):
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "typing":
                    for other in list(chat_subscribers):
                        if other is not ws:
                            try:
                                await other.send_text(raw)
                            except Exception:
                                chat_subscribers.discard(other)
    except Exception:
        pass
    finally:
        chat_subscribers.discard(ws)


def notify_chat(channel: str, msg_id: str) -> None:
    """投稿があったことを購読者に知らせる（同期コードから呼ぶ）"""
    import asyncio
    payload = json.dumps({"type": "message", "channel": channel, "id": msg_id})
    if _main_loop is None:
        return
    for ws in list(chat_subscribers):
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(payload), _main_loop)
        except Exception:
            chat_subscribers.discard(ws)


_main_loop = None


def bind_loop(loop) -> None:
    global _main_loop
    _main_loop = loop


# ---------- Wiki 同時編集（Yjs の更新を中継し、全体状態を保存） ----------

wiki_rooms: dict[str, dict[str, WebSocket]] = {}


async def wiki_ws(ws: WebSocket, page_id: str, name: str) -> None:
    """クライアント間で Yjs の update（base64）を中継する。サーバーは CRDT を解釈せず、
    クライアントが定期的に送る全体状態（encodeStateAsUpdate）を保存して、後から来た人に渡す"""
    from app import db
    from app.connectors import wiki
    await ws.accept()
    room = wiki_rooms.setdefault(page_id, {})
    key = name
    n = 1
    while key in room:   # 同じ人が2タブで開いたとき
        n += 1; key = f"{name}#{n}"
    room[key] = ws
    conn = db.connect()
    state = wiki.collab_state(conn, page_id)
    conn.close()
    await _send(ws, {"type": "welcome", "peers": [k for k in room if k != key], "state": state})
    for other_key, other in list(room.items()):
        if other is not ws:
            await _send(other, {"type": "peer-joined", "name": key})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            if t == "update":
                for other_key, other in list(room.items()):
                    if other is not ws:
                        try:
                            await other.send_text(json.dumps({"type": "update", "data": msg.get("data", ""), "from": key}))
                        except Exception:
                            room.pop(other_key, None)
            elif t == "state":
                try:
                    conn = db.connect(); wiki.save_collab_state(conn, page_id, str(msg.get("data", "")), name); conn.close()
                except Exception as e:
                    log.warning("wiki state save failed: %s", e)
            elif t == "cursor":
                for other_key, other in list(room.items()):
                    if other is not ws:
                        try:
                            await other.send_text(json.dumps({"type": "cursor", "name": key, "pos": msg.get("pos"), "line": msg.get("line")}))
                        except Exception:
                            room.pop(other_key, None)
            elif t == "saved":   # 誰かが改訂として保存した → 他の人に知らせる
                for other_key, other in list(room.items()):
                    if other is not ws:
                        try:
                            await other.send_text(json.dumps({"type": "saved", "name": key}))
                        except Exception:
                            pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("wiki ws error %s/%s: %s", page_id, key, e)
    finally:
        if room.get(key) is ws:
            room.pop(key, None)
        if not room:
            wiki_rooms.pop(page_id, None)
        for other_key, other in list(room.items()):
            try:
                await other.send_text(json.dumps({"type": "peer-left", "name": key}))
            except Exception:
                pass
