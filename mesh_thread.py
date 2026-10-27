#!/usr/bin/env python3
"""Mesh chat thread — renders the A2A tape as a readable chat transcript.

Watches the hub tape (notifications.jsonl under A2A_HOME) and appends
chat-style entries to mesh-thread.md in the same directory. Senders named
in A2A_THREAD_EXCLUDE (comma-separated ids) are left out. Skips
ping/pong/hello noise. Collapses consecutive repeats of the same message
(heartbeat spam): the first is shown, repeats are counted, and a small
"(repeated xN)" note is added when the run ends.

State (offset, last fingerprint, repeat count) persists alongside the
thread so restarts don't double-print.
"""
from __future__ import annotations
import hashlib
import json, os, time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
ADIR = Path(os.getenv("A2A_HOME", str(HOME / ".a2a-mesh")))
TAPE = Path(os.getenv("A2A_NOTIF_LOG", str(ADIR / "notifications.jsonl")))
THREAD = ADIR / "mesh-thread.md"
OFFSET = ADIR / "mesh-thread.offset"
LASTFP = ADIR / "mesh-thread.lastfp"

EXCLUDE_FROM = {
    part.strip()
    for part in (os.getenv("A2A_THREAD_EXCLUDE") or "").split(",")
    if part.strip()
}
NOISE_TYPES = {"ping", "pong", "hello", "identify", "ack"}
REGISTRY = Path(os.getenv("A2A_REGISTRY", str(ROOT / "registry.json")))

def display_name(agent_id: str, registry: dict) -> str:
    for a in registry.get("agents", []):
        if a.get("id") == agent_id:
            return a.get("name", agent_id)
    return agent_id or "hub"

def fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H:%M")
    except Exception:
        return ts[:16]

def fingerprint(entry: dict) -> str:
    h = hashlib.sha1()
    h.update(str(entry.get("from", "")).encode())
    h.update(str(entry.get("type", "")).encode())
    h.update(str(entry.get("content", ""))[:300].encode())
    return h.hexdigest()

def render(entry: dict, registry: dict) -> str | None:
    frm = entry.get("from", "")
    if frm in EXCLUDE_FROM:
        return None
    if str(entry.get("type", "")).lower() in NOISE_TYPES:
        return None
    content = (entry.get("content", "") or entry.get("summary", "")).strip()
    if not content:
        return None
    to = entry.get("to")
    who = display_name(frm, registry)
    target = f" \u2192 {display_name(to, registry)}" if to else ""
    content = content.replace("\n", " ")
    if len(content) > 4000:
        content = content[:4000] + "\u2026"
    badge = entry.get("badge")
    stamp = f" [{badge} via {entry.get('true_source')}]" if badge else ""
    return f"**[{fmt_ts(entry.get('ts',''))}] {who}**{target} *({entry.get('type','')})*{stamp}\n{content}\n"

def load_state():
    try:
        offset = int(OFFSET.read_text().strip())
    except Exception:
        offset = 0
    try:
        last_fp, rep = LASTFP.read_text().strip().split(":")
        rep = int(rep)
    except Exception:
        last_fp, rep = "", 0
    return offset, last_fp, rep

def save_state(offset: int, last_fp: str, rep: int) -> None:
    OFFSET.write_text(str(offset))
    LASTFP.write_text(f"{last_fp}:{rep}")

def main() -> None:
    try:
        registry = json.loads(REGISTRY.read_text())
    except Exception:
        registry = {}
    if not THREAD.exists():
        THREAD.write_text("# Mesh Thread \u2014 A2A chatter (everyone on the bus)\n\n")
    offset, last_fp, repeat_run = load_state()
    while True:
        try:
            lines = TAPE.read_text().splitlines()
        except FileNotFoundError:
            time.sleep(5)
            continue
        new = lines[offset:]
        if new:
            out: list[str] = []
            for line in new:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                fp = fingerprint(entry)
                if fp == last_fp and fp:
                    repeat_run += 1
                    continue
                if repeat_run > 1:
                    out.append(f"*(\u2191 repeated x{repeat_run})*\n")
                r = render(entry, registry)
                if r:
                    out.append(r)
                    last_fp, repeat_run = fp, 1
                else:
                    # excluded/noise: break the repeat chain silently
                    last_fp, repeat_run = fp, 0
            if out:
                with open(THREAD, "a") as f:
                    f.write("\n".join(out) + "\n")
            offset = len(lines)
            save_state(offset, last_fp, repeat_run)
        time.sleep(5)

if __name__ == "__main__":
    main()
