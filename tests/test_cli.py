"""Tests der Claude-Schnittstelle (cli.py + toolhub.json).

Jeder Befehl aus toolhub.json wird so ausgeführt, wie ToolHub es tut
(run-Liste, "python" -> aktueller Interpreter, Flags aus "args"), und muss
gültiges JSON auf stdout liefern.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import make_samples as ms  # noqa: E402

with open(os.path.join(ROOT, "toolhub.json"), encoding="utf-8") as _f:
    MANIFEST = json.load(_f)
COMMANDS = {c["name"]: c for c in MANIFEST["commands"]}


def build_argv(cmd: dict, args: dict, confirm: bool = False) -> list[str]:
    argv = [sys.executable if cmd["run"][0] == "python" else cmd["run"][0]] + cmd["run"][1:]
    spec = {a["name"]: a for a in cmd["args"]}
    for name, value in args.items():
        a = spec[name]                       # KeyError = Argument nicht deklariert
        if a["type"] == "boolean":
            if value:
                argv.append(a["flag"])
        else:
            argv += [a["flag"], str(value)]
    for a in cmd["args"]:
        if a.get("required") and a["name"] not in args:
            raise AssertionError(f"Pflichtargument {a['name']} fehlt im Test")
    if not cmd["readonly"] and not confirm and "--dry-run" not in argv:
        argv.append("--dry-run")             # wie ToolHub ohne confirm
    return argv


def run_cmd(name: str, args: dict | None = None, confirm: bool = False):
    cmd = COMMANDS[name]
    argv = build_argv(cmd, args or {}, confirm)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=cmd.get("timeout", 120), env=env,
                       stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


class TestManifest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(MANIFEST["name"], "FileProbe")
        self.assertTrue(MANIFEST["description"])
        for c in MANIFEST["commands"]:
            with self.subTest(cmd=c["name"]):
                self.assertEqual(c["run"][:2], ["python", "cli.py"])
                self.assertEqual(c["run"][-1], "--json")
                self.assertIsInstance(c["readonly"], bool)
                self.assertTrue(c["description"])
                for a in c["args"]:
                    self.assertIn(a["type"], ("string", "integer", "number", "boolean"))
                    self.assertTrue(a["flag"].startswith("--"))
                    self.assertTrue(a["description"])
                if not c["readonly"]:
                    self.assertIn("dry_run", [a["name"] for a in c["args"]])

    def test_flags_exist_in_cli(self):
        sys.path.insert(0, ROOT)
        import cli
        parser = cli.build_parser()
        subs = parser._subparsers._group_actions[0].choices
        for c in MANIFEST["commands"]:
            with self.subTest(cmd=c["name"]):
                self.assertIn(c["name"], subs)
                opts = {o for act in subs[c["name"]]._actions for o in act.option_strings}
                for a in c["args"]:
                    self.assertIn(a["flag"], opts)

    def test_with_toolhub_loader_if_present(self):
        th = os.path.join(os.path.dirname(ROOT), "ToolHub")
        if not os.path.isdir(os.path.join(th, "toolhub")):
            self.skipTest("ToolHub nicht vorhanden")
        sys.path.insert(0, th)
        from pathlib import Path

        from toolhub import manifest
        m = manifest.load(Path(ROOT) / "toolhub.json")
        self.assertEqual(m.errors, [])
        self.assertEqual(len(m.commands), len(MANIFEST["commands"]))
        r = manifest.run(m, "processes", {"filter": "python", "limit": 3})
        self.assertTrue(r.ok, r.stderr)
        self.assertIn("processes", r.data)
        argv, dry = manifest.build_argv(m, m.command("archive-extract"), {"path": "x", "out": "y"})
        self.assertTrue(dry)
        self.assertIn("--dry-run", argv)


class TestCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="fp_cli_")
        cls.p = ms.make_all(os.path.join(cls.dir, "samples"))
        cls.tested = set()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)
        missing = set(COMMANDS) - cls.tested
        if missing:
            raise AssertionError(f"Ohne Test: {sorted(missing)}")

    def ok(self, name, args=None, confirm=False):
        code, out, err = run_cmd(name, args, confirm)
        self.assertEqual(code, 0, f"{name}: {err}")
        data = json.loads(out)
        self.assertIsInstance(data, dict)
        self.tested.add(name)
        return data

    def test_identify(self):
        d = self.ok("identify", {"path": self.p["bild_ohne_endung"]})
        self.assertEqual(d["best"], "PNG-Bild")
        d = self.ok("identify", {"path": self.p["container_unbekannt.bin"], "limit": 2})
        self.assertEqual((d["embedded_total"], len(d["embedded"]), d["embedded_truncated"]), (3, 2, True))
        d = self.ok("identify", {"path": self.p["xor_verschluesselt.dat"], "no_embedded": True})
        self.assertTrue(any("0x5A" in h for h in d["hints"]))

    def test_strings(self):
        d = self.ok("strings", {"path": self.p["spielstand.json"], "filter": "gold", "min_length": 4})
        self.assertTrue(d["strings"])
        self.assertTrue(all("gold" in s["text"].lower() for s in d["strings"]))

    def test_hexdump(self):
        d = self.ok("hexdump", {"path": self.p["bild_ohne_endung"], "offset": "0x0", "length": 8})
        self.assertEqual(d["hex"], "89504e470d0a1a0a")

    def test_archive_list(self):
        d = self.ok("archive-list", {"path": self.p["doom_beispiel.wad"], "kind": "Bild"})
        self.assertEqual(sorted(e["name"] for e in d["entries"]), ["TITLEPIC", "flats/FLOOR1", "sprites/TROOA1"])
        d = self.ok("archive-list", {"path": self.p["pak01_dir.vpk"], "filter": "*.png;*.txt", "limit": 1})
        self.assertEqual((d["matching"], d["truncated"]), (2, True))
        d = self.ok("archive-list", {"path": self.p["container_unbekannt.bin"], "carve": True})
        self.assertEqual(len(d["entries"]), 3)

    def test_archive_extract_dry_run_and_real(self):
        out = os.path.join(self.dir, "extract")
        d = self.ok("archive-extract", {"path": self.p["doom_beispiel.wad"], "out": out, "media_only": True})
        self.assertTrue(d["dry_run"])
        self.assertEqual(d["count"], 4)
        self.assertFalse(os.path.exists(out), "dry-run darf nichts schreiben")
        d = self.ok("archive-extract", {"path": self.p["doom_beispiel.wad"], "out": out, "media_only": True},
                    confirm=True)
        self.assertFalse(d["dry_run"])
        for f in d["files"]:
            self.assertTrue(os.path.isfile(f["target"]))
        self.assertEqual(sorted(os.path.basename(f["target"]) for f in d["files"]),
                         ["DSPISTOL.wav", "FLOOR1.png", "TITLEPIC.png", "TROOA1.png"])
        d = self.ok("archive-extract", {"path": self.p["mod.pk3"], "out": out, "entries": "*.png", "dry_run": True})
        self.assertEqual([f["entry"] for f in d["files"]], ["textures/wand.png"])

    def test_registry_snapshot_and_compare(self):
        a, b = os.path.join(self.dir, "snaps", "a.pireg"), os.path.join(self.dir, "snaps", "b.pireg")
        d = self.ok("registry-snapshot", {"roots": "HKCU\\Control Panel\\Desktop", "label": "vorher", "out": a})
        self.assertTrue(d["dry_run"])
        self.assertFalse(os.path.exists(a))
        d = self.ok("registry-snapshot", {"roots": "HKCU\\Control Panel\\Desktop", "label": "vorher", "out": a},
                    confirm=True)
        self.assertGreater(d["values"], 5)
        self.ok("registry-snapshot", {"roots": "HKCU\\Control Panel\\Desktop", "label": "nachher", "out": b},
                confirm=True)
        d = self.ok("registry-snapshots", {"folder": os.path.dirname(a)})
        self.assertEqual(sorted(s["label"] for s in d["snapshots"]), ["nachher", "vorher"])
        d = self.ok("registry-compare", {"before": a, "after": b, "limit": 10})
        self.assertEqual(d["roots_compared"], ["hkey_current_user\\control panel\\desktop"])
        self.assertIn("changes", d)

    def test_processes(self):
        d = self.ok("processes", {"filter": "python", "limit": 50})
        self.assertTrue(any(p["pid"] == os.getpid() for p in d["processes"]) or d["truncated"])

    def test_watch(self):
        folder = os.path.join(self.dir, "watch")
        os.makedirs(folder)

        def writer():
            time.sleep(1.5)
            for i in range(3):
                with open(os.path.join(folder, "slot1.sav"), "wb") as f:
                    f.write(b"x" * (i + 1))
                time.sleep(0.2)

        t = threading.Thread(target=writer)
        t.start()
        d = self.ok("watch", {"folders": folder, "seconds": 3})
        t.join()
        files = {os.path.basename(f["path"]): f for f in d["files"]}
        self.assertIn("slot1.sav", files)
        self.assertEqual(files["slot1.sav"]["role"], "möglicher Spielstand")
        self.assertEqual(d["folders"], [{"path": folder, "error": None}])
        d = self.ok("watch", {"process": str(os.getpid()), "seconds": 1, "limit": 5})
        self.assertEqual(d["processes"][0]["pid"], os.getpid())

    def test_errors(self):
        code, out, err = run_cmd("identify", {"path": os.path.join(self.dir, "gibt_es_nicht.bin")})
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("nicht gefunden", json.loads(err)["error"])
        code, out, err = run_cmd("archive-list", {"path": self.p["unreal_v8.pak"], "kind": "Gibtsnicht"})
        self.assertEqual(code, 0)
        code, _, err = run_cmd("watch", {"process": "kein_solcher_prozess_123.exe", "seconds": 1})
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(err))
        p = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "identify", "--json"],
                           capture_output=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("error", json.loads(p.stderr.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
