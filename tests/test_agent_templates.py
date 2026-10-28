"""Tests for the A2A mesh agent-template system.

Run from the mesh-public tree with the stdlib only:

    python3 -m unittest discover -s tests -v

Covers template loading and validation, instantiation, the
registry add/remove flow, the `mesh` CLI end to end, and the thread
UI's add-agent endpoints. Everything runs against temp dirs and
scratch registries — the real registry.json is never touched.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_templates as at  # noqa: E402

STARTERS = {"promoter", "researcher", "coder", "monitor", "greeter", "documentarian", "sarii"}


def make_spec(**overrides):
    spec = {
        "template": "mini",
        "version": 1,
        "title": "Mini",
        "description": "A minimal valid template used by the tests.",
        "role": "You are Mini, a test agent. Answer briefly, and only when addressed by name.",
        "model": {"provider": "ollama", "name": "llama3.2"},
        "tools": ["mesh.dm"],
        "seat": {"kind": "ollama_model", "lane": "local"},
    }
    spec.update(overrides)
    return spec


def write_spec(directory: Path, spec, filename="mini.json") -> Path:
    p = Path(directory) / filename
    p.write_text(json.dumps(spec), encoding="utf-8")
    return p


class StarterTemplatesTest(unittest.TestCase):
    def test_all_starters_present_and_valid(self):
        rows = at.list_templates()
        self.assertEqual({r["template"] for r in rows}, STARTERS)
        for r in rows:
            spec, warnings = at.get_template(r["template"])
            errors, _ = at.validate_template(spec)
            self.assertEqual(errors, [], f"{r['template']}: {errors}")

    def test_starters_use_rail_visible_kinds(self):
        for name in STARTERS:
            spec, _ = at.get_template(name)
            self.assertIn(spec["seat"]["kind"], at.CHAT_KINDS, name)

    def test_promoter_encodes_drafts_only_and_approval(self):
        spec, _ = at.get_template("promoter")
        rules = " ".join(spec["rules"])
        self.assertIn("drafts-only", rules)
        self.assertIn("approval-required", rules)
        self.assertIn("never publish", spec["role"].lower())
        self.assertIn("notify.email", spec["tools"])

    def test_documentarian_encodes_factual_law(self):
        spec, _ = at.get_template("documentarian")
        rules = " ".join(spec["rules"])
        self.assertIn("factual-only", rules)
        self.assertIn("professional-and-alluring", rules)

    def test_sarii_encodes_local_data_and_notify_shape(self):
        spec, _ = at.get_template("sarii")
        rules = " ".join(spec["rules"])
        self.assertIn("local-data-only", rules)
        self.assertIn("notify-benefit-parts-how", rules)
        self.assertIn("cross-promotion-feed", rules)
        self.assertIn("approval", spec["role"].lower())


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, spec, filename="mini.json"):
        return at.load_template_file(write_spec(self.dir, spec, filename))

    def expect_error(self, spec, needle, filename="mini.json"):
        with self.assertRaises(at.TemplateError) as ctx:
            self.load(spec, filename)
        self.assertTrue(
            any(needle in e for e in ctx.exception.errors),
            f"expected an error mentioning {needle!r}, got {ctx.exception.errors}",
        )

    def test_valid_spec_loads(self):
        spec, _ = self.load(make_spec())
        self.assertEqual(spec["template"], "mini")

    def test_missing_role_fails(self):
        spec = make_spec()
        del spec["role"]
        self.expect_error(spec, "role")

    def test_short_role_fails(self):
        self.expect_error(make_spec(role="Be nice."), "role")

    def test_bad_template_slug_fails(self):
        self.expect_error(make_spec(template="Mini Agent"), "template", filename="other.json")

    def test_template_file_name_must_agree(self):
        with self.assertRaises(at.TemplateError) as ctx:
            self.load(make_spec(), filename="different.json")
        self.assertTrue(any("must agree" in e for e in ctx.exception.errors))

    def test_duplicate_tools_fail(self):
        self.expect_error(make_spec(tools=["mesh.dm", "mesh.dm"]), "twice")

    def test_malformed_tool_fails(self):
        self.expect_error(make_spec(tools=["Mesh.DM"]), "malformed")

    def test_empty_tools_fail(self):
        self.expect_error(make_spec(tools=[]), "tools")

    def test_unknown_tool_warns_but_loads(self):
        spec, warnings = self.load(make_spec(tools=["mesh.dm", "custom.thing"]))
        self.assertTrue(any("not in the mesh vocabulary" in w for w in warnings))

    def test_non_rail_kind_fails_loudly(self):
        spec = make_spec()
        spec["seat"] = {"kind": "robot", "lane": "local"}
        self.expect_error(spec, "rail")

    def test_ollama_without_model_name_fails(self):
        self.expect_error(make_spec(model={"provider": "ollama"}), "model.name")

    def test_real_credential_in_settings_fails(self):
        spec = make_spec(settings={"api_key": "sk-live-1234567890abcdef"})
        self.expect_error(spec, "placeholders only")

    def test_placeholder_credential_is_fine(self):
        spec, _ = self.load(make_spec(settings={"api_key": "$MY_API_KEY", "contact": "OPERATOR_EMAIL"}))
        self.assertEqual(spec["template"], "mini")

    def test_broken_json_fails(self):
        p = self.dir / "broken.json"
        p.write_text("{not json", encoding="utf-8")
        with self.assertRaises(at.TemplateError) as ctx:
            at.load_template_file(p)
        self.assertTrue(any("not valid JSON" in e for e in ctx.exception.errors))

    def test_unknown_template_name_fails(self):
        with self.assertRaises(at.TemplateError) as ctx:
            at.get_template("nope", self.dir)
        self.assertTrue(any("unknown template" in e for e in ctx.exception.errors))


class SlugifyTest(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(at.slugify("Herald"), "herald")
        self.assertEqual(at.slugify("My Agent!"), "my-agent")
        self.assertEqual(at.slugify("  spaced   name "), "spaced-name")
        self.assertEqual(at.slugify("sarii-forge"), "sarii-forge")


class InstantiateTest(unittest.TestCase):
    def test_defaults_from_template(self):
        spec, _ = at.get_template("greeter")
        entry, env, _ = at.instantiate(spec)
        self.assertEqual(entry["id"], "greeter")
        self.assertEqual(entry["name"], "Greeter")
        self.assertEqual(entry["reach"], "ollama_model")
        self.assertEqual(entry["model"], "llama3.2")
        self.assertTrue(entry["inbox"].endswith("inbox/greeter.md"))
        self.assertEqual(env["A2A_SEAT_ID"], "greeter")
        self.assertEqual(env["A2A_SEAT_MODEL"], "llama3.2")
        self.assertIn("Greeter", env["A2A_SEAT_SYSTEM"])

    def test_custom_name_and_model_override(self):
        spec, _ = at.get_template("promoter")
        entry, env, _ = at.instantiate(spec, "Town Crier", model_override="qwen3:8b")
        self.assertEqual(entry["id"], "town-crier")
        self.assertEqual(entry["name"], "Town Crier")
        self.assertEqual(entry["model"], "qwen3:8b")
        self.assertEqual(env["A2A_SEAT_MODEL"], "qwen3:8b")

    def test_unusable_name_fails(self):
        spec, _ = at.get_template("greeter")
        with self.assertRaises(at.TemplateError):
            at.instantiate(spec, "!!!")

    def test_model_none_becomes_marker_persona(self):
        spec = make_spec(model={"provider": "none"})
        spec["seat"] = {"kind": "persona", "lane": "local"}
        entry, env, _ = at.instantiate(spec, "Quiet One")
        self.assertEqual(entry["reach"], "marker")
        self.assertIn("paths", entry)
        self.assertNotIn("A2A_SEAT_MODEL", env)


class AddFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "a2a-home"
        self.reg = base / "registry.json"
        self.env = mock.patch.dict(os.environ, {"A2A_HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_add_creates_registry_inbox_and_seat_env(self):
        entry, _env, _w = at.add_agent("greeter", registry=self.reg)
        self.assertTrue(self.reg.is_file())
        reg = json.loads(self.reg.read_text())
        self.assertEqual([a["id"] for a in reg["agents"]], ["greeter"])
        self.assertEqual(reg["bus"]["port"], 8765)  # skeleton keeps the bus block
        self.assertTrue((self.home / "inbox" / "greeter.md").is_file())
        env_text = (self.home / "seats" / "greeter.env").read_text()
        self.assertIn("A2A_SEAT_ID=greeter", env_text)
        self.assertIn("A2A_SEAT_MODEL", env_text)

    def test_seat_env_pins_the_registry_bus_address(self):
        existing = {"agents": [], "bus": {"host": "127.0.0.1", "port": 18765}, "version": 1}
        self.reg.write_text(json.dumps(existing))
        at.add_agent("greeter", registry=self.reg)
        env_text = (self.home / "seats" / "greeter.env").read_text()
        self.assertIn("A2A_WS=ws://127.0.0.1:18765/ws", env_text)

    def test_add_preserves_existing_registry(self):
        existing = {
            "agents": [{"id": "you", "name": "You", "kind": "human"}],
            "bus": {"port": 9999},
            "network": "test-net",
            "version": 1,
        }
        self.reg.write_text(json.dumps(existing))
        at.add_agent("monitor", agent_name="Watchman", registry=self.reg)
        reg = json.loads(self.reg.read_text())
        self.assertEqual([a["id"] for a in reg["agents"]], ["you", "watchman"])
        self.assertEqual(reg["bus"]["port"], 9999)
        self.assertEqual(reg["network"], "test-net")

    def test_duplicate_add_fails_and_writes_nothing_new(self):
        at.add_agent("greeter", registry=self.reg)
        before = self.reg.read_text()
        with self.assertRaises(at.TemplateError) as ctx:
            at.add_agent("greeter", registry=self.reg)
        self.assertTrue(any("already" in e for e in ctx.exception.errors))
        self.assertEqual(self.reg.read_text(), before)

    def test_remove_agent(self):
        at.add_agent("greeter", registry=self.reg)
        removed = at.remove_agent("greeter", registry=self.reg)
        self.assertEqual(removed["id"], "greeter")
        reg = json.loads(self.reg.read_text())
        self.assertEqual(reg["agents"], [])
        with self.assertRaises(at.TemplateError):
            at.remove_agent("greeter", registry=self.reg)

    def test_corrupt_registry_fails_loudly(self):
        self.reg.write_text("{oops")
        with self.assertRaises(at.TemplateError):
            at.add_agent("greeter", registry=self.reg)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.reg = base / "registry.json"
        self.env = {**os.environ, "A2A_HOME": str(base / "home"), "A2A_REGISTRY": str(self.reg)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "mesh.py"), *argv],
            capture_output=True, text=True, env=self.env, cwd=str(ROOT), timeout=30,
        )

    def test_templates_lists_all_starters(self):
        r = self.run_cli("templates")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in STARTERS:
            self.assertIn(name, r.stdout)

    def test_add_agent_end_to_end(self):
        r = self.run_cli("add-agent", "documentarian", "--name", "Archivist")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added archivist", r.stdout)
        reg = json.loads(self.reg.read_text())
        entry = reg["agents"][0]
        self.assertEqual(entry["template"], "documentarian")
        self.assertIn(entry["kind"], at.CHAT_KINDS)

    def test_add_duplicate_fails_loudly(self):
        self.run_cli("add-agent", "greeter")
        r = self.run_cli("add-agent", "greeter")
        self.assertEqual(r.returncode, 2)
        self.assertIn("already", r.stderr)

    def test_dry_run_writes_nothing(self):
        r = self.run_cli("add-agent", "coder", "--name", "Builder", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("builder", r.stdout)
        self.assertFalse(self.reg.exists())

    def test_validate_bad_file_fails(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps(make_spec(role="")))
        r = self.run_cli("validate", str(bad))
        self.assertEqual(r.returncode, 2)
        self.assertIn("error:", r.stderr)


class ThreadUiTest(unittest.TestCase):
    """Boot thread_server.py on a scratch port with a scratch registry
    and exercise the add-agent endpoints over real HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.reg = base / "registry.json"
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        env = {
            **os.environ,
            "A2A_HOME": str(base / "home"),
            "A2A_REGISTRY": str(self.reg),
            "A2A_UI_PORT": str(self.port),
            "A2A_UI_BIND": "127.0.0.1",
            "A2A_HUB_HTTP": "http://127.0.0.1:9",  # dead on purpose: add endpoints must not need the hub
        }
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "thread_server.py")],
            env=env, cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.get("/api/templates")
                return
            except Exception:
                if self.proc.poll() is not None:
                    self.fail("thread_server exited during startup")
                time.sleep(0.2)
        self.fail("thread_server did not come up")

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def get_text(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read().decode()

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_page_has_add_agent_form(self):
        _status, html = self.get_text("/")
        self.assertIn('id="apanel"', html)
        self.assertIn("Add agent from template", html)

    def test_templates_endpoint(self):
        _status, data = self.get("/api/templates")
        self.assertTrue(data["ok"])
        self.assertEqual({t["template"] for t in data["templates"]}, STARTERS)

    def test_add_agent_then_it_is_in_the_registry(self):
        status, data = self.post("/api/agents", {"template": "greeter", "name": "Welcomer"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["agent"]["id"], "welcomer")
        self.assertIn(data["agent"]["kind"], at.CHAT_KINDS)  # rail-visible => DM-able
        reg = json.loads(self.reg.read_text())
        self.assertEqual([a["id"] for a in reg["agents"]], ["welcomer"])

    def test_add_duplicate_is_a_loud_400(self):
        self.post("/api/agents", {"template": "greeter", "name": "Welcomer"})
        status, data = self.post("/api/agents", {"template": "greeter", "name": "Welcomer"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertTrue(any("already" in e for e in data["errors"]))

    def test_add_unknown_template_is_a_loud_400(self):
        status, data = self.post("/api/agents", {"template": "nope", "name": "X"})
        self.assertEqual(status, 400)
        self.assertTrue(any("unknown template" in e for e in data["errors"]))

    def test_add_unusable_name_is_a_loud_400(self):
        status, data = self.post("/api/agents", {"template": "greeter", "name": "!!!"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertTrue(data["errors"])


if __name__ == "__main__":
    unittest.main()
