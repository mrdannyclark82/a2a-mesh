#!/usr/bin/env python3
"""One bus seat.

Mouth mode speaks through a local Ollama model when named or addressed.
Forward mode holds this seat's socket and forwards localhost posts, so
posts from local tools show verified instead of relayed-via-http.

Nothing in here posts as anyone else.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
A2A_HOME = Path(os.getenv("A2A_HOME", str(HOME / ".a2a-mesh")))
LOG_DIR = Path(os.getenv("A2A_SEAT_LOG_DIR", str(A2A_HOME / "seats")))


def _ws_url() -> str:
    env = (os.getenv("A2A_WS") or "").strip()
    if env:
        return env
    hosts = ["127.0.0.1"]
    port = "8765"
    prof = A2A_HOME / "net-profile.env"
    if prof.is_file():
        for line in prof.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("A2A_WS_HOSTS="):
                raw = line.split("=", 1)[1].strip().strip("\"'")
                hosts = [h.strip() for h in raw.split(",") if h.strip()] or hosts
            elif line.startswith("A2A_WS_PORT="):
                port = line.split("=", 1)[1].strip().strip("\"'") or port
    return f"ws://{hosts[0]}:{port}/ws"


def log(agent_id: str, msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat()} {msg}\n"
    with (LOG_DIR / f"{agent_id}.log").open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


def _named(content: str, wakes: list[str]) -> bool:
    """Wake only when the name opens the message or is an @mention.

    A later mention in a status report is not a summons.
    """
    text = content.strip().lower()
    for raw in wakes:
        wake = raw.strip().lower()
        if not wake:
            continue
        if text.startswith(wake) or f"@{wake}" in text:
            return True
    return False


def _room_humans() -> set[str]:
    raw = os.getenv("A2A_SEAT_ROOM") or ""
    return {part.strip().lower() for part in raw.split("|") if part.strip()}


def _token_in(text: str, token: str) -> bool:
    text = text.lower()
    token = token.strip().lower()
    if not token:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text) is not None


def _someone_else_named(text: str) -> bool:
    """True when the text names another seat (A2A_SEAT_OTHERS, |-separated)
    and not this one — a summons meant for a different chair."""
    low = text.lower()
    others = (os.getenv("A2A_SEAT_OTHERS") or "").split("|")
    return any(_token_in(low, token) for token in others if token.strip())


def _should_speak(msg: dict, agent_id: str, wakes: list[str]) -> bool:
    humans = _room_humans()
    if not wakes and not humans and os.getenv("A2A_SEAT_SPEAK", "0") != "1":
        return False
    frm = str(msg.get("from") or "")
    if frm == agent_id:
        return False
    mtype = str(msg.get("type") or "").lower()
    if mtype in {"ack", "ping", "pong", "hello", "identify"}:
        return False
    to = str(msg.get("to") or "")
    if to == agent_id:
        return True
    # A room seat hears the operator when they are talking to the room.
    # If they name another chair, this seat stays in its own unless named too.
    if humans and frm.lower() in humans and (not to or to.lower() in humans):
        text = str(msg.get("content") or "")
        # Named means this seat is being asked, including after a sentence break.
        # Being mentioned in someone else's question is not its chair.
        asked = _named(text, wakes) or any(
            _named(part, wakes) for part in re.split(r"[.?!]\s+", text)
        )
        if asked:
            return True
        if _someone_else_named(text):
            return False
        return True
    if not wakes:
        return False
    return _named(str(msg.get("content") or ""), wakes)


def _ollama(model: str, system: str, user: str) -> str:
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.6, "num_predict": 80},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode())
    return str(((data.get("message") or {}).get("content") or "")).strip()


class _Forward(BaseHTTPRequestHandler):
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    agent_id: str

    def log_message(self, fmt: str, *args) -> None:
        log(self.agent_id, "forward " + (fmt % args))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/send":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        content = str(data.get("content") or "").strip()
        if not content:
            self.send_response(400)
            self.end_headers()
            return
        claimed = str(data.get("from") or self.agent_id).strip()
        if claimed != self.agent_id:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"this seat speaks only as itself"}')
            return
        payload = {
            "type": str(data.get("type") or "response"),
            "from": self.agent_id,
            "to": data.get("to"),
            "content": content,
            "ts": datetime.now().isoformat(),
        }
        asyncio.run_coroutine_threadsafe(self.queue.put(payload), self.loop)
        body = json.dumps({"ok": True, "queued": True}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def _serve_forward(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, agent_id: str, port: int) -> None:
    _Forward.loop = loop
    _Forward.queue = queue
    _Forward.agent_id = agent_id
    server = ThreadingHTTPServer(("127.0.0.1", port), _Forward)
    thread = __import__("threading").Thread(target=server.serve_forever, name="seat-forward", daemon=True)
    thread.start()
    log(agent_id, f"forward listening on 127.0.0.1:{port}")


async def bus_loop() -> None:
    import websockets

    agent_id = os.environ["A2A_SEAT_ID"].strip()
    model = (os.getenv("A2A_SEAT_MODEL") or "").strip()
    wakes = [w.strip() for w in (os.getenv("A2A_SEAT_WAKES") or "").split("|") if w.strip()]
    system = os.getenv("A2A_SEAT_SYSTEM") or f"You are {agent_id}. Answer in one or two short sentences."
    announce = os.getenv(
        "A2A_SEAT_ANNOUNCE",
        f"{agent_id} seated. I speak only when named or addressed.",
    )
    forward_port = int(os.getenv("A2A_SEAT_FORWARD_PORT") or "0")
    url = _ws_url()
    out_q: asyncio.Queue = asyncio.Queue()
    if forward_port:
        await _serve_forward(asyncio.get_running_loop(), out_q, agent_id, forward_port)

    backoff = 1.0
    while True:
        try:
            log(agent_id, f"connecting {url}")
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                backoff = 1.0
                now = datetime.now().isoformat()
                await ws.send(json.dumps({"type": "identify", "from": agent_id, "content": "seat", "ts": now}))
                await ws.send(
                    json.dumps({"type": "thought", "from": agent_id, "content": announce, "ts": datetime.now().isoformat()})
                )
                log(agent_id, "identified")

                async def drain() -> None:
                    while True:
                        payload = await out_q.get()
                        await ws.send(json.dumps(payload))
                        log(agent_id, f"sent {payload.get('type')} to={payload.get('to')}")

                drain_task = asyncio.create_task(drain())
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not _should_speak(msg, agent_id, wakes):
                            continue
                        if not model:
                            continue
                        user = (
                            f"From {msg.get('from')}. "
                            f"They said: {str(msg.get('content') or '')[:1200]}"
                        )
                        try:
                            text = await asyncio.to_thread(_ollama, model, system, user)
                        except Exception as e:
                            log(agent_id, f"ollama failed: {e}")
                            continue
                        if not text:
                            log(agent_id, "ollama returned empty")
                            continue
                        await out_q.put(
                            {
                                "type": "response",
                                "from": agent_id,
                                "to": msg.get("from") or os.getenv("A2A_SEAT_HUMAN", "you"),
                                "content": text,
                                "ts": datetime.now().isoformat(),
                            }
                        )
                finally:
                    drain_task.cancel()
        except Exception as e:
            log(agent_id, f"disconnect: {e}; retry in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)


def main() -> None:
    if not os.getenv("A2A_SEAT_ID"):
        print("A2A_SEAT_ID is required", file=sys.stderr)
        sys.exit(2)
    asyncio.run(bus_loop())


if __name__ == "__main__":
    main()
