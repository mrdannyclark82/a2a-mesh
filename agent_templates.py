#!/usr/bin/env python3
"""Agent templates for the A2A mesh.

A template is one small JSON file in templates/ describing a
specialized agent: its name, its role (the system prompt its seat
speaks with), its model binding, the tools it is allowed to use, and
its seat settings. Copy one, edit it in minutes, then add the agent:

    python3 mesh.py add-agent promoter --name herald

Adding an agent writes a registry entry (registry.json is the mesh's
source of truth: the hub probes it, the thread UI builds its rail
from it), creates the agent's inbox file, and writes a seat env file
you can source to give the agent a live chair with bus_seat.py.

Validation is loud on purpose. A bad spec raises TemplateError with
every problem listed at once — a seat kind the UI cannot render, a
missing role, a model binding with no model — instead of producing a
registry entry that sits in the rail and never speaks.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
HOME = Path.home()

# Seat kinds the thread UI renders as DM-able threads. Keep in step
# with CHAT_KINDS in thread_server.py — an agent whose kind is not in
# this set would land in the registry but never appear in the rail,
# which is exactly the silent dead seat validation exists to prevent.
CHAT_KINDS = frozenset(
    {"bus_node", "human", "cli_agent", "persona", "local_agent", "ollama_model"}
)

# The mesh's tool vocabulary. A template's "tools" list is a
# whitelist: the seat may use only what is listed. Well-formed but
# unknown names produce a warning (your own tools are your business);
# malformed names are errors.
KNOWN_TOOLS = frozenset(
    {
        "mesh.read",      # read the bus tape / registry
        "mesh.post",      # post to the room
        "mesh.dm",        # send direct messages
        "mesh.dispatch",  # dispatch tasks to other agents
        "notify.email",   # send an email notification (approvals)
        "notify.desktop", # local desktop notification
        "files.read",
        "files.write",
        "web.search",
        "web.fetch",
        "code.read",
        "code.edit",
        "code.test",
        "shell.run",
    }
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9_]+)*$")
MIN_ROLE_CHARS = 20

# Settings keys that smell like credentials. Templates carry
# placeholders only — a real secret in a template is an error.
_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "token", "access_token", "password", "secret", "access_key"}
)
_SECRET_VALUE_OK_PREFIXES = ("$", "YOUR_", "REPLACE_", "CHANGEME", "<", "OPERATOR_")
_SECRET_SMELLS = ("sk-", "ghp_", "gho_", "AKIA", "-----BEGIN", "xoxb-", "xoxp-")


class TemplateError(Exception):
    """A template or add request is invalid. Carries every problem."""

    def __init__(self, errors: List[str], warnings: Optional[List[str]] = None):
        self.errors = list(errors)
        self.warnings = list(warnings or [])
        super().__init__("; ".join(self.errors))


def templates_dir() -> Path:
    env = (os.getenv("A2A_TEMPLATES_DIR") or "").strip()
    return Path(env).expanduser() if env else ROOT / "templates"


def registry_path() -> Path:
    env = (os.getenv("A2A_REGISTRY") or "").strip()
    return Path(env).expanduser() if env else ROOT / "registry.json"


def a2a_home() -> Path:
    env = (os.getenv("A2A_HOME") or "").strip()
    return Path(env).expanduser() if env else HOME / ".a2a-mesh"


def _home_form(p: Path) -> str:
    """Render a path the way registry.example.json does: ~/... when
    the path lives under the operator's home, absolute otherwise."""
    try:
        rel = p.resolve().relative_to(HOME.resolve())
        return "~/" + str(rel)
    except ValueError:
        return str(p)


def slugify(name: str) -> str:
    """Turn an operator-chosen name into a seat id."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(name).strip().lower())
    return slug.strip("-_")


# ------------------------------------------------------------------
# Loading and validation
# ------------------------------------------------------------------

def _secret_scan(value: Any, where: str, errors: List[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k).lower()
            if key in _SECRET_KEYS and isinstance(v, str) and v.strip():
                if not v.strip().upper().startswith(_SECRET_VALUE_OK_PREFIXES):
                    errors.append(
                        f"{where}.{k}: looks like a real credential — "
                        "templates carry placeholders only (e.g. $ENV_VAR or YOUR_VALUE)"
                    )
            _secret_scan(v, f"{where}.{k}", errors)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _secret_scan(v, f"{where}[{i}]", errors)
    elif isinstance(value, str):
        for smell in _SECRET_SMELLS:
            if smell in value:
                errors.append(f"{where}: value looks like a credential ({smell}…) — placeholders only")


def validate_template(spec: Any, source: str = "template") -> Tuple[List[str], List[str]]:
    """Return (errors, warnings) for a parsed template spec."""
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(spec, dict):
        return [f"{source}: template must be a JSON object"], warnings

    known_keys = {
        "template", "version", "title", "description", "role",
        "model", "tools", "seat", "rules", "settings",
    }
    for key in sorted(set(spec) - known_keys):
        warnings.append(f"{source}: unknown top-level key {key!r} (ignored)")

    tpl = spec.get("template")
    if not isinstance(tpl, str) or not SLUG_RE.match(tpl or ""):
        errors.append(
            f"{source}: 'template' must be a slug like 'promoter' "
            "(lowercase letters, digits, '-' or '_')"
        )

    for field in ("title", "description"):
        v = spec.get(field)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{source}: '{field}' is required and must be a non-empty string")

    role = spec.get("role")
    if not isinstance(role, str) or len(role.strip()) < MIN_ROLE_CHARS:
        errors.append(
            f"{source}: 'role' (the seat's system prompt) is required, "
            f"at least {MIN_ROLE_CHARS} characters — a seat with no role is a dead seat"
        )

    model = spec.get("model")
    provider = None
    if not isinstance(model, dict):
        errors.append(f"{source}: 'model' is required and must be an object like "
                      '{"provider": "ollama", "name": "llama3.2"}')
    else:
        provider = model.get("provider")
        if provider not in ("ollama", "none"):
            errors.append(f"{source}: model.provider must be 'ollama' or 'none'")
        elif provider == "ollama":
            name = model.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{source}: model.name is required when provider is 'ollama' "
                              "(a placeholder like 'llama3.2' is fine — edit it to your model)")

    tools = spec.get("tools")
    if not isinstance(tools, list) or not tools:
        errors.append(f"{source}: 'tools' is required: a non-empty whitelist list")
    else:
        seen = set()
        for t in tools:
            if not isinstance(t, str) or not TOOL_RE.match(t):
                errors.append(f"{source}: tool {t!r} is malformed (want dotted lowercase, e.g. 'mesh.dm')")
                continue
            if t in seen:
                errors.append(f"{source}: tool {t!r} listed twice")
            seen.add(t)
            if t not in KNOWN_TOOLS:
                warnings.append(f"{source}: tool {t!r} is not in the mesh vocabulary (allowed, but check the spelling)")

    seat = spec.get("seat")
    if not isinstance(seat, dict):
        errors.append(f"{source}: 'seat' is required and must be an object")
    else:
        kind = seat.get("kind")
        if kind not in CHAT_KINDS:
            errors.append(
                f"{source}: seat.kind {kind!r} is not a thread-UI kind "
                f"({', '.join(sorted(CHAT_KINDS))}) — the agent would never appear in the rail"
            )
        if provider == "none" and kind == "ollama_model":
            errors.append(f"{source}: seat.kind 'ollama_model' needs model.provider 'ollama'")
        wakes = seat.get("wakes", [])
        if not isinstance(wakes, list) or any(not isinstance(w, str) for w in wakes):
            errors.append(f"{source}: seat.wakes must be a list of strings")
        for flag in ("notify", "wake_on_room", "speak"):
            if flag in seat and not isinstance(seat[flag], bool):
                errors.append(f"{source}: seat.{flag} must be true or false")

    rules = spec.get("rules", [])
    if not isinstance(rules, list) or any(not isinstance(r, str) for r in rules):
        errors.append(f"{source}: 'rules' must be a list of strings")

    settings = spec.get("settings", {})
    if not isinstance(settings, dict):
        errors.append(f"{source}: 'settings' must be an object")
    else:
        _secret_scan(settings, f"{source}.settings", errors)

    return errors, warnings


def load_template_file(path: Path) -> Tuple[dict, List[str]]:
    """Parse and validate one template file. Raises TemplateError."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise TemplateError([f"{path}: cannot read template file ({e})"])
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TemplateError([f"{path}: not valid JSON ({e})"])
    errors, warnings = validate_template(spec, source=path.name)
    if isinstance(spec, dict) and spec.get("template") != path.stem:
        errors.append(
            f"{path.name}: 'template' is {spec.get('template')!r} but the file is "
            f"{path.name!r} — the name and the file must agree"
        )
    _secret_scan(spec, path.name, errors) if isinstance(spec, dict) else None
    errors = list(dict.fromkeys(errors))  # settings are scanned twice by design; report once
    if errors:
        raise TemplateError(errors, warnings)
    return spec, warnings


def list_templates(directory: Optional[Path] = None) -> List[dict]:
    """All templates in the directory, validated. Invalid ones raise."""
    d = directory or templates_dir()
    out = []
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        spec, _warnings = load_template_file(path)
        out.append(
            {
                "template": spec["template"],
                "title": spec.get("title", spec["template"]),
                "description": spec.get("description", ""),
                "model": (spec.get("model") or {}).get("name"),
                "tools": list(spec.get("tools") or []),
                "path": str(path),
            }
        )
    return out


def get_template(name: str, directory: Optional[Path] = None) -> Tuple[dict, List[str]]:
    d = directory or templates_dir()
    path = d / f"{name}.json"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in d.glob("*.json"))) if d.is_dir() else ""
        raise TemplateError(
            [f"unknown template {name!r}" + (f" — available: {available}" if available else "")]
        )
    return load_template_file(path)


# ------------------------------------------------------------------
# Instantiation: template + name -> registry entry (+ seat env)
# ------------------------------------------------------------------

def instantiate(
    spec: dict,
    agent_name: Optional[str] = None,
    model_override: Optional[str] = None,
) -> Tuple[dict, Dict[str, str], List[str]]:
    """Build (registry_entry, seat_env, warnings) from a template.

    agent_name is the operator-chosen name; the seat id is its slug.
    The registry entry is what the hub and thread UI consume; the
    seat env is what bus_seat.py needs to give the agent a live chair.
    """
    warnings: List[str] = []
    display = (agent_name or spec.get("title") or spec["template"]).strip()
    agent_id = slugify(display) if agent_name else spec["template"]
    if not SLUG_RE.match(agent_id):
        raise TemplateError(
            [f"agent name {display!r} does not reduce to a valid seat id "
             "(lowercase letters, digits, '-' or '_'; give --name a simpler name)"]
        )

    seat = spec.get("seat") or {}
    model = dict(spec.get("model") or {})
    if model_override:
        model["name"] = model_override
        model["provider"] = "ollama"
    provider = model.get("provider", "none")
    home = a2a_home()
    inbox_path = home / "inbox" / f"{agent_id}.md"
    inbox_dir = home / "inbox"

    entry: Dict[str, Any] = {
        "id": agent_id,
        "name": display,
        "kind": seat.get("kind", "persona"),
        "lane": seat.get("lane", "local"),
        "description": spec.get("description", ""),
        "inbox": _home_form(inbox_path),
        "notify": bool(seat.get("notify", True)),
        "wake_on_room": bool(seat.get("wake_on_room", False)),
        "template": spec["template"],
        "role": spec.get("role", ""),
        "tools": list(spec.get("tools") or []),
    }
    if spec.get("rules"):
        entry["rules"] = list(spec["rules"])
    if spec.get("settings"):
        entry["settings"] = dict(spec["settings"])
    if provider == "ollama":
        entry["reach"] = "ollama_model"
        entry["model"] = model.get("name", "")
    else:
        entry["reach"] = "marker"
        entry["paths"] = [_home_form(inbox_dir)]

    wakes = [str(w) for w in (seat.get("wakes") or [])]
    if not wakes:
        wakes = [agent_id]
    env: Dict[str, str] = {
        "A2A_SEAT_ID": agent_id,
        "A2A_SEAT_SYSTEM": spec.get("role", ""),
        "A2A_SEAT_WAKES": "|".join(wakes),
        "A2A_SEAT_ANNOUNCE": str(
            seat.get("announce") or f"{display} seated. I speak only when named or addressed."
        ),
    }
    if provider == "ollama":
        env["A2A_SEAT_MODEL"] = model.get("name", "")
    if seat.get("speak"):
        env["A2A_SEAT_SPEAK"] = "1"
    return entry, env, warnings


def seat_env_text(env: Dict[str, str]) -> str:
    import shlex

    lines = ["# Seat environment — source this, then run bus_seat.py"]
    for k in sorted(env):
        lines.append(f"export {k}={shlex.quote(str(env[k]))}")
    return "\n".join(lines) + "\n"


def seat_command(entry: dict, env_file: Path) -> str:
    return (
        f"set -a; . {env_file}; set +a; "
        f"python3 {ROOT / 'bus_seat.py'}"
    )


# ------------------------------------------------------------------
# Registry mutation
# ------------------------------------------------------------------

def _empty_registry() -> dict:
    return {
        "agents": [],
        "bus": {"host": "127.0.0.1", "http_host": "127.0.0.1", "http_port": 8766, "path": "/ws", "port": 8765},
        "network": "local-a2a",
        "version": 1,
    }


def read_registry(path: Path) -> dict:
    if not path.is_file():
        return _empty_registry()
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TemplateError([f"{path}: registry is not valid JSON ({e}) — fix it by hand, nothing was changed"])
    if not isinstance(reg, dict) or not isinstance(reg.get("agents"), list):
        raise TemplateError([f"{path}: registry must be an object with an 'agents' list"])
    return reg


def write_registry(path: Path, reg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_agent(
    template_name: str,
    agent_name: Optional[str] = None,
    model_override: Optional[str] = None,
    registry: Optional[Path] = None,
    directory: Optional[Path] = None,
    write_files: bool = True,
) -> Tuple[dict, Dict[str, str], List[str]]:
    """Validate, instantiate, and register an agent from a template.

    Returns (entry, seat_env, warnings). Raises TemplateError listing
    every problem; on error nothing is written — no half-added seats.
    """
    reg_path = registry or registry_path()
    spec, warnings = get_template(template_name, directory)
    entry, env, more = instantiate(spec, agent_name, model_override)
    warnings += more

    reg = read_registry(reg_path)
    existing = {a.get("id") for a in reg["agents"] if isinstance(a, dict)}
    if entry["id"] in existing:
        raise TemplateError(
            [f"agent id {entry['id']!r} is already in {reg_path} — pick a different --name"],
            warnings,
        )

    # The seat must dial the mesh it was added to: bus_seat.py falls
    # back to ws://127.0.0.1:8765/ws, which on a non-default mesh (or a
    # machine running another mesh) seats the agent on the WRONG bus.
    # Pin the address from this registry's bus block into the seat env.
    bus = reg.get("bus") or {}
    env["A2A_WS"] = f"ws://{bus.get('host', '127.0.0.1')}:{bus.get('port', 8765)}/ws"

    if write_files:
        reg["agents"].append(entry)
        write_registry(reg_path, reg)
        home = a2a_home()
        inbox = home / "inbox" / f"{entry['id']}.md"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        if not inbox.exists():
            inbox.write_text(
                f"# Inbox — {entry['name']} ({entry['id']})\n\n"
                f"Template: {entry['template']}. Mail and dispatches for this agent land here.\n",
                encoding="utf-8",
            )
        seat_dir = home / "seats"
        seat_dir.mkdir(parents=True, exist_ok=True)
        (seat_dir / f"{entry['id']}.env").write_text(seat_env_text(env), encoding="utf-8")
    return entry, env, warnings


def remove_agent(agent_id: str, registry: Optional[Path] = None) -> dict:
    reg_path = registry or registry_path()
    reg = read_registry(reg_path)
    kept = [a for a in reg["agents"] if not (isinstance(a, dict) and a.get("id") == agent_id)]
    if len(kept) == len(reg["agents"]):
        raise TemplateError([f"no agent {agent_id!r} in {reg_path}"])
    removed = next(a for a in reg["agents"] if isinstance(a, dict) and a.get("id") == agent_id)
    reg["agents"] = kept
    write_registry(reg_path, reg)
    return removed
