#!/usr/bin/env python3
"""
A2A Mesh Hub
------------
Local agent-to-agent network:
  - WebSocket bus  ws://127.0.0.1:8765/ws  (JSON messages: {from, type, to, content, ts})
  - HTTP control   http://127.0.0.1:8766  (/health /agents /ping /send /dispatch)

Registers every agent in registry.json and probes reachability.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import websockets
    from websockets.server import serve
except ImportError:
    print("websockets required", file=sys.stderr)
    sys.exit(1)

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
# Runtime state lives under A2A_HOME (default ~/.a2a-mesh); every path
# can be overridden individually by its own environment variable.
A2A_HOME = Path(os.getenv("A2A_HOME", str(HOME / ".a2a-mesh")))
REGISTRY_PATH = Path(os.getenv("A2A_REGISTRY", str(ROOT / "registry.json")))
STATE_PATH = Path(os.getenv("A2A_STATE", str(A2A_HOME / "hub-state.json")))
LOG_PATH = Path(os.getenv("A2A_LOG", str(A2A_HOME / "logs" / "a2a-hub.log")))
PID_PATH = Path(os.getenv("A2A_PID", str(A2A_HOME / "hub.pid")))

# Live clients on the WS bus
CLIENTS: Set[Any] = set()
# Optional identity announced by clients: websocket -> agent_id
CLIENT_IDS: Dict[Any, str] = {}
# Recent messages for HTTP tail
RECENT: List[dict] = []
MAX_RECENT = 200
# Side-channel notifications (file + desktop) — not re-broadcast on bus
NOTIF_LOG = Path(
    os.getenv(
        "A2A_NOTIF_LOG",
        str(A2A_HOME / "notifications.jsonl"),
    )
)
# Skip noise types so hello/pong don't spam every citizen
NOTIF_SKIP_TYPES = frozenset(
    {"hello", "pong", "notify", "ack", "ping", "raw"}
)


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(data, indent=2) + "\n")


def probe_path(paths: List[str]) -> dict:
    missing = []
    found = []
    for p in paths or []:
        ep = expand(p)
        if ep.exists():
            found.append(str(ep))
        else:
            missing.append(p)
    ok = len(found) > 0 and len(missing) == 0
    # partial OK if at least one critical path exists
    if found and missing:
        ok = True
        status = "partial"
    elif found:
        status = "ok"
    else:
        status = "missing"
        ok = False
    return {"ok": ok, "status": status, "found": found, "missing": missing}


def probe_http(url: str, timeout: float = 2.0) -> dict:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(500).decode("utf-8", errors="replace")
            return {"ok": True, "status": "ok", "code": resp.status, "body": body[:200]}
    except Exception as e:
        return {"ok": False, "status": "down", "error": str(e)}


def probe_tcp(host: str, port: int, timeout: float = 1.0) -> dict:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"ok": True, "status": "ok"}
    except Exception as e:
        return {"ok": False, "status": "down", "error": str(e)}


def probe_bin(cmd: List[str], timeout: float = 15.0) -> dict:
    try:
        # resolve first token via PATH
        exe = cmd[0]
        which = subprocess.run(["which", exe], capture_output=True, text=True)
        if which.returncode != 0 and not Path(exe).exists():
            return {"ok": False, "status": "missing", "error": f"{exe} not on PATH"}
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONPATH": f"{ROOT}:{os.environ.get('PYTHONPATH','')}"},
        )
        return {
            "ok": p.returncode == 0,
            "status": "ok" if p.returncode == 0 else "error",
            "returncode": p.returncode,
            "stdout": (p.stdout or "")[-400:],
            "stderr": (p.stderr or "")[-200:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout"}
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)}


def probe_ollama_model(model: str) -> dict:
    tags = probe_http("http://127.0.0.1:11434/api/tags")
    if not tags.get("ok"):
        return {"ok": False, "status": "ollama_down", "error": tags.get("error")}
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode())
        names = [m.get("name", "") for m in data.get("models", [])]
        hit = any(model == n or n.startswith(model.split(":")[0]) for n in names)
        return {"ok": hit, "status": "ok" if hit else "model_missing", "models_sample": names[:8]}
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)}


def probe_agent(agent: dict) -> dict:
    reach = agent.get("reach", "path")
    result: dict = {
        "id": agent["id"],
        "name": agent.get("name"),
        "kind": agent.get("kind"),
        "lane": agent.get("lane"),
        "reach": reach,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if reach == "marker":
        pr = probe_path(agent.get("paths") or [])
        result.update(pr)
        # marker reachability is a path check only (the session itself is external)
        result["note"] = "marker agent: path check only; the session itself is external"
    elif reach == "path":
        result.update(probe_path(agent.get("paths") or []))
    elif reach == "http":
        result.update(probe_http(agent.get("url", "")))
    elif reach == "tcp":
        result.update(probe_tcp(agent.get("host", "127.0.0.1"), int(agent.get("port", 0))))
    elif reach == "ollama_model":
        result.update(probe_ollama_model(agent.get("model", "")))
    elif reach in ("bin", "process"):
        cmd = agent.get("command") or []
        # for process kinds, prefer --help or status-like short commands already in registry
        result.update(probe_bin(cmd if cmd else ["true"]))
        # also path check
        if agent.get("paths"):
            pr = probe_path(agent["paths"])
            result["paths"] = pr
            if not pr.get("ok") and result.get("ok"):
                result["ok"] = False
                result["status"] = "path_missing"
    else:
        result.update({"ok": False, "status": "unknown_reach"})
    return result


def probe_all(reg: dict) -> dict:
    agents = reg.get("agents") or []
    results = [probe_agent(a) for a in agents]
    ok_n = sum(1 for r in results if r.get("ok"))
    out = {
        "network": reg.get("network"),
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(results),
            "ok": ok_n,
            "down": len(results) - ok_n,
        },
        "agents": results,
        "clients_connected": len(CLIENTS),
    }
    save_state(out)
    return out


def _client_list() -> List[dict]:
    out = []
    for c in list(CLIENTS):
        peer = getattr(c, "remote_address", None)
        out.append(
            {
                "peer": list(peer) if peer else None,
                "agent_id": CLIENT_IDS.get(c),
            }
        )
    return out


def _delivery_targets(msg: dict) -> Set[Any]:
    """Sockets that receive this message live.

    No `to` (a room post): every connected client, as always.
    With `to` (a direct post): only seats identified as the addressee
    or as the sender. True direct delivery — other seats' sockets
    never see the bytes. The hub's own notification log still records
    the message: that is the operator's archive on the operator's
    server, and the thread UI reads from it.
    """
    to = msg.get("to")
    if not to:
        return set(CLIENTS)
    want = {str(to), str(msg.get("from") or "")}
    return {c for c in CLIENTS if CLIENT_IDS.get(c) in want}


def fanout_notifications(msg: dict) -> dict:
    """Notify registered agents when a real bus message arrives.

    Channels (no bus re-entry — avoids notify loops):
      1) notifications.jsonl (the tape; the thread UI reads it)
      2) each registry agent with inbox + notify!=false → append
         (direct posts land in the addressee's inbox only)
      3) desktop notify-send (if available)
      4) optional soft speak if A2A_SPEAK_NOTIFY=1 and A2A_SPEAK_CMD set
    Live WS clients already receive the raw message via broadcast.
    """
    mtype = str(msg.get("type") or "thought").lower()
    if mtype in NOTIF_SKIP_TYPES:
        return {"ok": True, "skipped": mtype}

    frm = str(msg.get("from") or "unknown")
    to = msg.get("to")
    content = str(msg.get("content") or "")[:4000]
    ts = msg.get("ts") or datetime.now().isoformat()
    summary = f"[{mtype}] {frm}" + (f" → {to}" if to else "") + f": {content[:120]}"

    record = {
        "ts": ts,
        "from": frm,
        "type": mtype,
        "to": to,
        "content": content,
        "summary": summary,
        "live_clients": len(CLIENTS),
        "badge": msg.get("badge"),
        "true_source": msg.get("true_source"),
        "verified": msg.get("verified"),
        "posted_via": msg.get("posted_via"),
        "claimed_from": msg.get("claimed_from"),
        "verify_note": msg.get("verify_note"),
    }

    # 1) global notification log
    try:
        NOTIF_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NOTIF_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"notif log fail: {e}")

    inboxes: List[str] = []
    # 2) registry citizens with inboxes
    try:
        reg = load_registry()
        for agent in reg.get("agents") or []:
            if agent.get("notify") is False:
                continue
            inbox = agent.get("inbox")
            if not inbox:
                continue
            # Direct posts land in the addressee's mailbox only;
            # room posts still notify every inbox (working mailbox).
            if to and agent.get("id") != to:
                continue
            path = expand(str(inbox))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n### hub notify {ts}\n"
                    f"**from:** {frm}  **type:** {mtype}"
                    + (f"  **to:** {to}" if to else "")
                    + f"\n\n{content}\n"
                )
            inboxes.append(str(path))
    except Exception as e:
        log(f"notif inbox fanout fail: {e}")

    # 3) desktop notification
    desktop = False
    try:
        title = f"A2A · {frm}"
        body = f"{mtype}: {content[:180]}"
        r = subprocess.run(
            ["notify-send", "-a", "A2A Mesh", "-u", "normal", title, body],
            capture_output=True,
            timeout=3,
        )
        desktop = r.returncode == 0
    except Exception as e:
        log(f"notify-send skip: {e}")

    # 4) optional voice (off by default). A2A_SPEAK_CMD names a program
    # that speaks its argument (e.g. a local TTS wrapper).
    spoke = False
    if os.getenv("A2A_SPEAK_NOTIFY", "").strip() in ("1", "true", "yes"):
        try:
            speak_cmd = (os.getenv("A2A_SPEAK_CMD") or "").strip()
            speak = Path(speak_cmd).expanduser() if speak_cmd else None
            if speak is not None and speak.exists():
                line = f"Hub message from {frm}. {content[:80]}"
                subprocess.Popen(
                    [str(speak), line],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                spoke = True
        except Exception as e:
            log(f"speak notify skip: {e}")

    log(f"notify fanout type={mtype} from={frm} inboxes={len(inboxes)} desktop={desktop}")
    return {
        "ok": True,
        "inboxes": inboxes,
        "desktop": desktop,
        "spoke": spoke,
        "summary": summary,
    }


# Sender ids treated as the operator (the human at the thread UI).
# Set A2A_HUMAN_SENDERS to your own handle(s), comma-separated.
HUMAN_SENDERS = {
    part.strip()
    for part in (os.getenv("A2A_HUMAN_SENDERS") or "you").split(",")
    if part.strip()
}

def apply_http_soft_badge(msg: dict, claimed: str, seat: str | None) -> dict:
    """Soft badge for HTTP /send. Never blocks. A registry post_via is not proof.

    The bundled thread UI is the operator's chair. Any other HTTP post keeps
    the claimed name and wears relayed, so orchestration stays visible.
    """
    msg["posted_via"] = "http"
    msg["from"] = (claimed or "").strip() or "anonymous"
    if seat == "thread-ui" and msg["from"] in HUMAN_SENDERS:
        msg["badge"] = "verified"
        msg["verified"] = True
        msg["true_source"] = "thread-ui"
        msg["verify_note"] = "bundled thread UI seat"
        return msg
    msg["badge"] = "relayed"
    msg["verified"] = False
    msg["true_source"] = "http"
    msg["claimed_from"] = msg["from"]
    msg["verify_note"] = "http has no proved socket"
    return msg


async def ws_handler(websocket):
    CLIENTS.add(websocket)
    peer = getattr(websocket, "remote_address", None)
    log(f"ws connect {peer} (n={len(CLIENTS)})")
    try:
        # hello
        await websocket.send(
            json.dumps(
                {
                    "from": "a2a-hub",
                    "type": "hello",
                    "content": "local a2a hub — send {type:identify, from:agent-id} to name yourself",
                    "ts": datetime.now().isoformat(),
                }
            )
        )
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                msg = {"from": "unknown", "type": "raw", "content": raw[:500]}
            if "ts" not in msg:
                msg["ts"] = datetime.now().isoformat()
            # client self-identify
            if str(msg.get("type") or "").lower() == "identify":
                aid = str(msg.get("from") or msg.get("agent_id") or "").strip()
                if aid:
                    CLIENT_IDS[websocket] = aid
                    log(f"ws identify {peer} -> {aid}")
                    await websocket.send(
                        json.dumps(
                            {
                                "from": "a2a-hub",
                                "type": "ack",
                                "content": f"identified as {aid}",
                                "ts": datetime.now().isoformat(),
                            }
                        )
                    )
                continue
            # Soft badge. The socket is the proof. The claimed name stays visible.
            claimed = str(msg.get("from") or "").strip()
            proved = CLIENT_IDS.get(websocket)
            msg["posted_via"] = "ws"
            msg["true_source"] = proved or "unidentified-socket"
            if proved and claimed == proved:
                msg["badge"] = "verified"
                msg["verified"] = True
                msg["verify_note"] = "socket identity"
            else:
                msg["badge"] = "relayed"
                msg["verified"] = False
                msg["verify_note"] = "claimed name differs from socket" if proved else "socket not identified"
                if claimed and claimed != "unknown":
                    msg["from"] = claimed
                    msg["claimed_from"] = claimed
                else:
                    msg["from"] = proved or "anonymous"
                if proved and claimed and claimed != proved:
                    log(f"ws relay {peer}: claimed {claimed}, true_source {proved}")
            RECENT.append(msg)
            if len(RECENT) > MAX_RECENT:
                del RECENT[: len(RECENT) - MAX_RECENT]
            # live delivery (scoped: direct posts reach only their seats)
            dead = []
            for c in list(_delivery_targets(msg)):
                if c is websocket:
                    continue
                try:
                    await c.send(json.dumps(msg))
                except Exception:
                    dead.append(c)
            for c in dead:
                CLIENTS.discard(c)
                CLIENT_IDS.pop(c, None)
            # side-channel notify all citizens
            try:
                fanout_notifications(msg)
            except Exception as e:
                log(f"fanout error: {e}")
            # optional: command dispatch via bus
            if msg.get("type") == "ping":
                await websocket.send(
                    json.dumps(
                        {
                            "from": "a2a-hub",
                            "type": "pong",
                            "content": msg.get("from", ""),
                            "ts": datetime.now().isoformat(),
                        }
                    )
                )
    except Exception as e:
        log(f"ws error: {e}")
    finally:
        CLIENTS.discard(websocket)
        CLIENT_IDS.pop(websocket, None)
        log(f"ws disconnect {peer} (n={len(CLIENTS)})")


async def broadcast(msg: dict) -> int:
    if "ts" not in msg:
        msg["ts"] = datetime.now().isoformat()
    RECENT.append(msg)
    if len(RECENT) > MAX_RECENT:
        del RECENT[: len(RECENT) - MAX_RECENT]
    raw = json.dumps(msg)
    n = 0
    dead = []
    for c in list(_delivery_targets(msg)):
        try:
            await c.send(raw)
            n += 1
        except Exception:
            dead.append(c)
    for c in dead:
        CLIENTS.discard(c)
        CLIENT_IDS.pop(c, None)
    # side-channel: every citizen gets a working notification
    try:
        fanout_notifications(msg)
    except Exception as e:
        log(f"fanout error: {e}")
    return n


def dispatch_to_agent(agent_id: str, content: str) -> dict:
    """Route a task to a reachable agent implementation.

    Adapters, in order:
      1) dispatch_command — a registry command list; the task text is
         appended as the final argument (e.g. ["my-agent", "feed"]).
      2) inbox append — persona / bus_node kinds (and swarm-* ids) with
         a registry inbox get the task appended to that file.
      3) ollama_model — the task runs through the local model CLI.
    Anything else reports that no adapter is implemented for it.
    """
    reg = load_registry()
    agent = next((a for a in reg["agents"] if a["id"] == agent_id), None)
    if not agent:
        return {"ok": False, "error": f"unknown agent {agent_id}"}

    _path_env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PATH": f"{HOME / '.local' / 'bin'}:/usr/local/bin:/usr/bin:/bin:{os.environ.get('PATH', '')}",
        "HOME": str(HOME),
    }

    command = agent.get("dispatch_command")
    if isinstance(command, list) and command:
        try:
            p = subprocess.run(
                [str(part) for part in command] + [content],
                capture_output=True,
                text=True,
                timeout=120,
                env=_path_env,
            )
            return {
                "ok": p.returncode == 0,
                "stdout": p.stdout[-500:],
                "stderr": p.stderr[-300:],
                "note": f"ran dispatch_command for {agent_id}",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if agent.get("kind") in ("persona", "bus_node") or agent_id.startswith("swarm-"):
        inbox = agent.get("inbox")
        if inbox:
            path = expand(inbox)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n\n## A2A dispatch {datetime.now().isoformat()}\n\n{content}\n"
                )
            return {"ok": True, "inbox": str(path), "note": "appended to agent inbox"}
        return {"ok": False, "error": "no inbox for persona/bus_node"}

    if agent.get("kind") == "ollama_model":
        model = agent.get("model", "llama3.2")
        try:
            p = subprocess.run(
                [
                    "ollama",
                    "run",
                    model,
                    content[:2000],
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return {
                "ok": p.returncode == 0,
                "stdout": (p.stdout or "")[-1500:],
                "stderr": (p.stderr or "")[-300:],
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {
        "ok": False,
        "error": f"dispatch not implemented for {agent_id} (kind={agent.get('kind')})",
        "hint": "use /send on bus or implement adapter",
    }


# --- minimal HTTP server (stdlib) ---
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log("http " + (fmt % args))

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/"):
            self._json(
                200,
                {
                    "ok": True,
                    "service": "a2a-hub",
                    "clients": len(CLIENTS),
                    "recent": len(RECENT),
                    "clients_detail": _client_list(),
                },
            )
            return
        if path == "/clients":
            self._json(
                200,
                {
                    "ok": True,
                    "count": len(CLIENTS),
                    "clients": _client_list(),
                },
            )
            return
        if path == "/agents":
            self._json(200, probe_all(load_registry()))
            return
        if path == "/recent":
            self._json(200, {"messages": RECENT[-50:]})
            return
        if path == "/registry":
            self._json(200, load_registry())
            return
        if path == "/notifications":
            # last N side-channel notifies
            lines: List[dict] = []
            if NOTIF_LOG.exists():
                for line in NOTIF_LOG.read_text(encoding="utf-8").splitlines()[-50:]:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            self._json(200, {"ok": True, "notifications": lines})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "bad json"})
            return
        path = self.path.split("?")[0]
        if path == "/ping":
            agent_id = data.get("agent_id") or data.get("id")
            reg = load_registry()
            if agent_id:
                agent = next((a for a in reg["agents"] if a["id"] == agent_id), None)
                if not agent:
                    self._json(404, {"ok": False, "error": "unknown agent"})
                    return
                self._json(200, probe_agent(agent))
                return
            self._json(200, probe_all(reg))
            return
        if path == "/send":
            claimed_from = str(data.get("from", "http-client"))
            seat = data.get("seat")
            msg = apply_http_soft_badge(
                {
                    "type": data.get("type", "thought"),
                    "to": data.get("to"),
                    "content": data.get("content", ""),
                    "ts": datetime.now().isoformat(),
                },
                claimed_from,
                str(seat).strip() if isinstance(seat, str) else None,
            )
            # schedule broadcast on the running loop
            fut = asyncio.run_coroutine_threadsafe(broadcast(msg), LOOP)
            try:
                n = fut.result(timeout=5)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            self._json(200, {"ok": True, "delivered": n, "message": msg})
            return
        if path == "/dispatch":
            agent_id = data.get("agent_id") or data.get("to")
            content = data.get("content") or data.get("task") or ""
            if not agent_id or not content:
                self._json(400, {"ok": False, "error": "agent_id and content required"})
                return
            result = dispatch_to_agent(agent_id, content)
            # also announce on bus
            asyncio.run_coroutine_threadsafe(
                broadcast(
                    {
                        "from": "a2a-hub",
                        "type": "dispatch",
                        "to": agent_id,
                        "content": content[:500],
                        "result_ok": result.get("ok"),
                        "ts": datetime.now().isoformat(),
                    }
                ),
                LOOP,
            )
            self._json(200, result)
            return
        self._json(404, {"ok": False, "error": "not found"})


LOOP: asyncio.AbstractEventLoop


def _parse_hosts(*env_keys: str, fallback: str = "127.0.0.1") -> List[str]:
    """Comma-separated multi-bind list from first non-empty env, else fallback."""
    from net_bind import parse_hosts, with_tailscale
    hosts = parse_hosts(*env_keys, fallback=fallback)
    return with_tailscale(hosts)


def _http_base_candidates(http_port: int) -> List[str]:
    env = (os.getenv("A2A_HTTP") or "").strip()
    bases = []
    if env:
        bases.append(env.rstrip("/"))
    extra = ["127.0.0.1"]
    try:
        from net_bind import tailscale_ipv4
        ts = tailscale_ipv4()
        if ts:
            extra.append(ts)
    except Exception:
        pass
    for h in extra:
        bases.append(f"http://{h}:{http_port}")
    # de-dupe preserve order
    out: List[str] = []
    for b in bases:
        if b not in out:
            out.append(b)
    return out


async def main_async(
    ws_hosts: List[str],
    ws_port: int,
    http_port: int,
    http_hosts: List[str],
):
    global LOOP
    LOOP = asyncio.get_running_loop()
    reg = load_registry()
    # initial probe
    status = probe_all(reg)
    log(f"probe summary {status['summary']}")

    # HTTP: dual-bind supported (e.g. 127.0.0.1 + LAN) for house-lan + local CLI
    for http_host in http_hosts:
        httpd = ThreadingHTTPServer((http_host, http_port), Handler)
        t = threading.Thread(
            target=httpd.serve_forever,
            daemon=True,
            name=f"a2a-http-{http_host}",
        )
        t.start()
        log(f"HTTP control http://{http_host}:{http_port}  (/health /agents /ping /send /dispatch)")

    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()) + "\n")

    # WebSocket: one server per bind host
    servers = []
    for host in ws_hosts:
        srv = await serve(ws_handler, host, ws_port)
        servers.append(srv)
        log(f"WebSocket bus ws://{host}:{ws_port}/ws  (clients use /ws path optional)")
    try:
        await asyncio.Future()  # run forever
    finally:
        for srv in servers:
            srv.close()
            await srv.wait_closed()


def cmd_serve():
    reg = load_registry()
    bus = reg.get("bus") or {}
    # Env wins over registry. Multi-bind via A2A_*_HOSTS (comma list).
    ws_port = int(os.getenv("A2A_WS_PORT") or bus.get("port", 8765))
    http_port = int(os.getenv("A2A_HTTP_PORT") or bus.get("http_port", 8766))
    single_ws = os.getenv("A2A_WS_HOST") or bus.get("host") or "127.0.0.1"
    single_http = os.getenv("A2A_HTTP_HOST") or bus.get("http_host") or "127.0.0.1"
    if (os.getenv("A2A_WS_HOSTS") or "").strip():
        ws_hosts = _parse_hosts("A2A_WS_HOSTS")
    else:
        ws_hosts = _parse_hosts(fallback=single_ws)
    if (os.getenv("A2A_HTTP_HOSTS") or "").strip():
        http_hosts = _parse_hosts("A2A_HTTP_HOSTS")
    else:
        http_hosts = _parse_hosts(fallback=single_http)
    try:
        asyncio.run(main_async(ws_hosts, ws_port, http_port, http_hosts))
    except KeyboardInterrupt:
        log("hub stopped")
    finally:
        PID_PATH.unlink(missing_ok=True)


def _hub_http_port() -> int:
    return int(os.getenv("A2A_HTTP_PORT") or "8766")


def cmd_status():
    # Prefer live hub over offline probe; try localhost then LAN (house-lan).
    for base in _http_base_candidates(_hub_http_port()):
        try:
            with urllib.request.urlopen(f"{base}/agents", timeout=5) as r:
                print(r.read().decode())
                return
        except Exception:
            continue
    print(json.dumps(probe_all(load_registry()), indent=2))


def cmd_ping(agent_id: Optional[str]):
    data = json.dumps({"agent_id": agent_id} if agent_id else {}).encode()
    for base in _http_base_candidates(_hub_http_port()):
        req = urllib.request.Request(
            f"{base}/ping",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(r.read().decode())
                return
        except Exception:
            continue
    # offline probe
    reg = load_registry()
    if agent_id:
        agent = next((a for a in reg["agents"] if a["id"] == agent_id), None)
        print(json.dumps(probe_agent(agent) if agent else {"ok": False}, indent=2))
    else:
        print(json.dumps(probe_all(reg), indent=2))


def main():
    p = argparse.ArgumentParser(description="A2A Mesh Hub")
    p.add_argument(
        "cmd",
        nargs="?",
        default="serve",
        choices=["serve", "status", "ping", "probe"],
    )
    p.add_argument("--agent", default=None)
    args = p.parse_args()
    if args.cmd == "serve":
        cmd_serve()
    elif args.cmd in ("status", "probe"):
        cmd_status()
    elif args.cmd == "ping":
        cmd_ping(args.agent)


if __name__ == "__main__":
    main()
