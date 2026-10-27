#!/usr/bin/env python3
"""
A2A notify → wake worker
------------------------
Tails the hub's notification tape (notifications.jsonl under A2A_HOME).

On each new notification:
  1. Write a wake flag for the addressee under A2A_HOME/wake/ (plus a
     "last" breadcrumb for the most recent record).
  2. Refresh the addressee's wake/<id>-latest.md note (and room posts
     do the same for agents whose registry entry sets wake_on_room).
  3. If the record is a dispatch and the addressee's registry entry
     sets auto_process with a process_command, run that command
     (global kill switch: A2A_AUTO_PROCESS=0).

Does NOT re-broadcast on the bus (avoids loops).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
A2A_HOME = Path(os.getenv("A2A_HOME", str(HOME / ".a2a-mesh")))
NOTIF_LOG = Path(
    os.getenv(
        "A2A_NOTIF_LOG",
        str(A2A_HOME / "notifications.jsonl"),
    )
)
WAKE_DIR = Path(os.getenv("A2A_WAKE_DIR", str(A2A_HOME / "wake")))
STATE_FILE = WAKE_DIR / "worker-state.json"
LOG_PATH = Path(
    os.getenv("A2A_WORKER_LOG", str(A2A_HOME / "logs" / "a2a-notify-worker.log"))
)
LOCK_DIR = WAKE_DIR / "locks"
REGISTRY_PATH = Path(os.getenv("A2A_REGISTRY", str(ROOT / "registry.json")))

# Global kill switch for registry auto_process commands.
AUTO_PROCESS = os.getenv("A2A_AUTO_PROCESS", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def load_agents() -> dict:
    """Registry agents by id. A missing registry means no wake rules."""
    try:
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return {a["id"]: a for a in reg.get("agents") or [] if a.get("id")}
    except Exception:
        return {}


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"offset": 0, "processed": 0}


def save_state(st: dict) -> None:
    WAKE_DIR.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")


def write_wake(agent_key: str, record: dict) -> Path:
    WAKE_DIR.mkdir(parents=True, exist_ok=True)
    path = WAKE_DIR / f"{agent_key}.json"
    payload = {
        "woken_at": datetime.now().isoformat(),
        "record": record,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # also append to all.jsonl
    with (WAKE_DIR / "all.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def wake_agent(agent_id: str, record: dict, agents: dict) -> None:
    """File-based wake for one agent (its poller or client watches these)."""
    WAKE_DIR.mkdir(parents=True, exist_ok=True)
    flag = WAKE_DIR / f"{agent_id}.flag"
    flag.write_text(datetime.now().isoformat() + "\n", encoding="utf-8")
    latest = WAKE_DIR / f"{agent_id}-latest.md"
    content = str(record.get("content") or "")
    frm = record.get("from")
    mtype = record.get("type")
    ts = record.get("ts")
    inbox = (agents.get(agent_id) or {}).get("inbox")
    body = (
        f"# Wake — {agent_id}\n\n"
        f"**ts:** {ts}\n"
        f"**from:** {frm}\n"
        f"**type:** {mtype}\n"
        f"**to:** {record.get('to')}\n\n"
        f"{content}\n\n"
        f"---\n"
        + (f"Also check your inbox: `{inbox}`\n" if inbox else "")
        + f"Hub tape: `{NOTIF_LOG}`\n"
    )
    latest.write_text(body, encoding="utf-8")
    write_wake(agent_id, record)
    log(f"wake written for {agent_id} ({mtype} from {frm})")


def _process_lock(name: str) -> bool:
    """Simple non-blocking lock; return True if we acquired it."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_DIR / f"{name}.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        # stale lock? if older than 10 min, steal
        try:
            age = time.time() - lock.stat().st_mtime
            if age > 600:
                lock.unlink(missing_ok=True)
                return _process_lock(name)
        except Exception:
            pass
        return False


def _process_unlock(name: str) -> None:
    lock = LOCK_DIR / f"{name}.lock"
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


def run_process(cmd: list[str], label: str) -> dict:
    if not _process_lock(label):
        log(f"skip {label}: lock held")
        return {"ok": False, "error": "locked"}
    try:
        log(f"wake process: {' '.join(cmd)}")
        env = {
            **os.environ,
            "PYTHONPATH": f"{ROOT}:{os.environ.get('PYTHONPATH', '')}",
            "PATH": f"{HOME / '.local' / 'bin'}:/usr/local/bin:/usr/bin:{os.environ.get('PATH', '')}",
        }
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=float(os.getenv("A2A_PROCESS_TIMEOUT", "600")),
            env=env,
            cwd=str(HOME),
        )
        log(
            f"{label} exit={p.returncode} stdout_tail={(p.stdout or '')[-200:]!r} "
            f"stderr_tail={(p.stderr or '')[-120:]!r}"
        )
        return {
            "ok": p.returncode == 0,
            "code": p.returncode,
            "stdout": (p.stdout or "")[-500:],
            "stderr": (p.stderr or "")[-300:],
        }
    except subprocess.TimeoutExpired:
        log(f"{label} TIMEOUT")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        log(f"{label} error: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _process_unlock(label)


def handle_record(record: dict) -> None:
    mtype = str(record.get("type") or "").lower()
    if mtype in ("pong", "client_list", "connect", "disconnect"):
        return  # Ignore low-level system messages
    to = str(record.get("to") or "").strip()

    # Always: global wake breadcrumb
    write_wake("last", record)

    agents = load_agents()
    if to:
        # Addressed: wake that agent (flag + latest note).
        if to in agents:
            wake_agent(to, record, agents)
    elif mtype in ("thought", "dispatch", "response", "plan"):
        # Room post: wake the agents who opted into room traffic.
        for agent_id, agent in agents.items():
            if agent.get("wake_on_room"):
                wake_agent(agent_id, record, agents)

    # Targeted auto-process: a dispatch runs the addressee's
    # registry process_command when it opted in (auto_process).
    if mtype == "dispatch" and to and AUTO_PROCESS:
        agent = agents.get(to) or {}
        command = agent.get("process_command")
        if agent.get("auto_process") and isinstance(command, list) and command:
            run_process([str(part) for part in command], to)


def tail_loop(once: bool = False) -> None:
    st = load_state()
    offset = int(st.get("offset") or 0)
    log(
        f"notify worker start offset={offset} auto_process={AUTO_PROCESS} "
        f"log={NOTIF_LOG}"
    )
    NOTIF_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not NOTIF_LOG.exists():
        NOTIF_LOG.touch()

    while True:
        try:
            size = NOTIF_LOG.stat().st_size
            if size < offset:
                # rotated / truncated
                log(f"notif log truncated (size={size} < offset={offset}); reset")
                offset = 0
            if size > offset:
                with NOTIF_LOG.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        handle_record(rec)
                        st["processed"] = int(st.get("processed") or 0) + 1
                    except Exception as e:
                        log(f"handle error: {e}")
                st["offset"] = offset
                save_state(st)
        except FileNotFoundError:
            NOTIF_LOG.touch()
            offset = 0
        except Exception as e:
            log(f"tail loop error: {e}")
            time.sleep(2)

        if once:
            break
        time.sleep(0.5)


def main() -> None:
    ap = argparse.ArgumentParser(description="A2A notify → wake worker")
    ap.add_argument(
        "--once",
        action="store_true",
        help="Process new lines once and exit (for tests)",
    )
    args = ap.parse_args()
    tail_loop(once=args.once)


if __name__ == "__main__":
    main()
