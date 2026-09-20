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
    states.setdefault(room, {})[name] = {"mic": True, "cam": True}
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
                states[room][name] = {"mic": bool(msg.get("mic", True)), "cam": bool(msg.get("cam", True))}
                await broadcast(room, {"type": "state", "name": name, **states[room][name]}, exclude=name)
            elif t == "chat":
                await broadcast(room, {"type": "chat", "name": name, "text": str(msg.get("text", ""))[:500],
                                       "at": datetime.now().strftime("%H:%M")})
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
