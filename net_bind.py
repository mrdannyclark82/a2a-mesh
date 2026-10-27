"""LAN + tailnet bind helpers. Prefer explicit hosts over 0.0.0.0."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Iterable, List, Optional


def tailscale_ipv4(timeout: float = 3.0) -> Optional[str]:
    bin_ = shutil.which("tailscale")
    if not bin_:
        return None
    try:
        p = subprocess.run(
            [bin_, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    ip = (p.stdout or "").strip().split()[0] if p.stdout else ""
    if ip.startswith("100.") or ip.startswith("fd7a:"):
        return ip
    return ip or None


def parse_hosts(*env_keys: str, fallback: str = "127.0.0.1") -> List[str]:
    raw = ""
    for k in env_keys:
        v = (os.environ.get(k) or "").strip()
        if v:
            raw = v
            break
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return hosts or [fallback]


def with_tailscale(hosts: Iterable[str], *, enabled: bool = True) -> List[str]:
    """Append this node's Tailscale IPv4 unless already all-ifaces or present."""
    out: List[str] = []
    for h in hosts:
        if h and h not in out:
            out.append(h)
    if not enabled:
        return out
    flag = (os.environ.get("A2A_BIND_TAILSCALE") or "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return out
    if "0.0.0.0" in out or "::" in out:
        return out
    ts = tailscale_ipv4()
    if ts and ts not in out:
        out.append(ts)
    return out
