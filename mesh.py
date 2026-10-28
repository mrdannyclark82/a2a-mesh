#!/usr/bin/env python3
"""mesh — the operator CLI for the A2A mesh.

    python3 mesh.py templates                 list the starter templates
    python3 mesh.py show promoter             print one template
    python3 mesh.py validate promoter         check a template (or a file path)
    python3 mesh.py add-agent promoter --name herald
    python3 mesh.py remove-agent herald

add-agent is the easy door: it validates the template, writes the
registry entry (registry.json — the hub probes it, the thread UI
builds its rail from it, no restart needed), creates the agent's
inbox, and writes a seat env file under A2A_HOME/seats/. Source that
file and run bus_seat.py to give the agent a live chair.

A bad spec fails loudly: every problem is printed and nothing is
written. There are no silent dead seats.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import agent_templates as at


def _fail(err: at.TemplateError) -> int:
    for e in err.errors:
        print(f"error: {e}", file=sys.stderr)
    for w in err.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 2


def _warn(warnings) -> None:
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)


def cmd_templates(_args) -> int:
    try:
        rows = at.list_templates()
    except at.TemplateError as e:
        return _fail(e)
    if not rows:
        print(f"no templates found in {at.templates_dir()}")
        return 0
    for r in rows:
        print(f"{r['template']:<14} {r['title']} — {r['description']}")
    return 0


def cmd_show(args) -> int:
    try:
        spec, warnings = at.get_template(args.template)
    except at.TemplateError as e:
        return _fail(e)
    _warn(warnings)
    print(json.dumps(spec, indent=2, sort_keys=True))
    return 0


def cmd_validate(args) -> int:
    target = Path(args.target)
    try:
        if target.is_file():
            spec, warnings = at.load_template_file(target)
        else:
            spec, warnings = at.get_template(args.target)
    except at.TemplateError as e:
        return _fail(e)
    _warn(warnings)
    print(f"ok: {spec['template']} is a valid template")
    return 0


def cmd_add_agent(args) -> int:
    reg_path = Path(args.registry).expanduser() if args.registry else at.registry_path()
    try:
        spec, warnings = at.get_template(args.template)
        entry, env, more = at.instantiate(spec, args.name, args.model)
        warnings += more
    except at.TemplateError as e:
        return _fail(e)
    _warn(warnings)

    if args.dry_run:
        print("dry run — nothing written. Registry entry would be:")
        print(json.dumps(entry, indent=2, sort_keys=True))
        return 0

    existed = reg_path.is_file()
    try:
        entry, env, warnings2 = at.add_agent(
            args.template,
            agent_name=args.name,
            model_override=args.model,
            registry=reg_path,
        )
    except at.TemplateError as e:
        return _fail(e)
    _warn(warnings2)

    home = at.a2a_home()
    env_file = home / "seats" / f"{entry['id']}.env"
    print(f"added {entry['id']} ({entry['name']}) from template {entry['template']!r}")
    print(f"  registry: {reg_path}" + ("" if existed else "  (created)"))
    print(f"  inbox:    {entry['inbox']}")
    print(f"  seat env: {env_file}")
    print("  it is now in the registry: the thread UI rail picks it up on its next refresh,")
    print("  and it can be DM'd and dispatched to immediately (mail lands in its inbox).")
    print("  to give it a live chair:")
    print(f"    {at.seat_command(entry, env_file)}")
    return 0


def cmd_remove_agent(args) -> int:
    reg_path = Path(args.registry).expanduser() if args.registry else at.registry_path()
    try:
        removed = at.remove_agent(args.agent_id, registry=reg_path)
    except at.TemplateError as e:
        return _fail(e)
    print(f"removed {removed['id']} from {reg_path} (its inbox and seat env file are left in place)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="mesh", description="A2A mesh operator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("templates", help="list starter templates").set_defaults(fn=cmd_templates)
    s = sub.add_parser("show", help="print one template")
    s.add_argument("template")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("validate", help="validate a template name or file")
    s.add_argument("target")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("add-agent", help="add an agent to the registry from a template")
    s.add_argument("template")
    s.add_argument("--name", default=None, help="agent name (seat id is its slug); default: the template's name")
    s.add_argument("--model", default=None, help="override the template's model binding")
    s.add_argument("--registry", default=None, help="registry path (default: A2A_REGISTRY or ./registry.json)")
    s.add_argument("--dry-run", action="store_true", help="validate and print the entry; write nothing")
    s.set_defaults(fn=cmd_add_agent)
    s = sub.add_parser("remove-agent", help="remove an agent from the registry")
    s.add_argument("agent_id")
    s.add_argument("--registry", default=None)
    s.set_defaults(fn=cmd_remove_agent)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
