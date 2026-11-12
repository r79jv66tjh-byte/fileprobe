"""Tests für FileProbe:  python -m unittest discover -s tests -v"""

import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from PIL import Image  # noqa: E402

import make_samples as ms  # noqa: E402
from inspector import archives, formats, lz4, registry, watcher  # noqa: E402


def _sha(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Samples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="pi_test_")
        cls.p = ms.make_all(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)


class TestFormats(Samples):
    def best(self, name):
        a = formats.analyze(self.p[name])
        return a, (a.best.name if a.best else None)

    def test_signatures(self):
        expect = {
            "bild_ohne_endung": "PNG-Bild", "eigentlich_ein_bild.txt": "JPEG-Bild", "animation.gif": "GIF-Bild",
            "bild.bmp": "BMP-Bild", "ton.wav": "WAVE-Audio", "musik.ogg": "Ogg-Container (Vorbis/Opus)",
            "mod.pk3": "ZIP-Archiv", "doom_beispiel.wad": "Doom-WAD (IWAD)", "halflife_texturen.wad": "Half-Life-WAD3",
            "quake.pak": "Quake-PAK", "unreal_v8.pak": "Unreal-Engine-PAK", "bundle_lz4.unity3d":
                "Unity-Asset-Bundle (UnityFS)", "pak01_dir.vpk": "Valve-VPK", "spiel_godot4.pck": "Godot-PCK",
        }
        for name, want in expect.items():
            with self.subTest(name=name):
                self.assertEqual(self.best(name)[1], want)

    def test_text_kinds(self):
        a, _ = self.best("einstellungen.ini")
        self.assertEqual(a.best.detail, "INI/Config")
        a, _ = self.best("spielstand.json")
        self.assertEqual(a.best.detail, "JSON (gültig)")

    def test_extension_mismatch_hint(self):
        a, _ = self.best("eigentlich_ein_bild.txt")
        self.assertTrue(any("passt nicht" in h for h in a.hints))
        a, _ = self.best("mod.pk3")      # .pk3 ist ein normales ZIP -> kein Hinweis
        self.assertFalse(any("passt nicht" in h for h in a.hints))

    def test_entropy_encrypted_vs_compressed(self):
        a, best = self.best("zufall_verschluesselt.bin")
        self.assertIsNone(best)
        self.assertGreater(a.entropy, 7.99)
        self.assertTrue(any("VERSCHLÜSSELT" in h for h in a.hints))
        a, best = self.best("komprimiert_roh.bin")
        self.assertIsNone(best)
        self.assertTrue(any("KOMPRIMIERT" in h for h in a.hints))
        self.assertTrue(any("Deflate" in h for h in a.hints))

    def test_xor_detection(self):
        a, _ = self.best("xor_verschluesselt.dat")
        self.assertTrue(any("0x5A" in h and "PNG" in h for h in a.hints), a.hints)

    def test_embedded_exact(self):
        a, _ = self.best("container_unbekannt.bin")
        png, wav, jpg = ms.png_bytes(), ms.wav_bytes(), ms.jpeg_bytes()
        got = {(e.name, e.offset, e.length) for e in a.embedded}
        self.assertIn(("PNG-Bild", 5000, len(png)), got)
        self.assertIn(("WAVE-Audio", 5000 + len(png) + 3000, len(wav)), got)
        self.assertIn(("JPEG-Bild", 5000 + len(png) + 3000 + len(wav) + 1000, len(jpg)), got)
        self.assertEqual(len(a.embedded), 3)

    def test_no_self_parts_as_embedded(self):
        for name in ("mod.pk3", "musik.ogg"):
            a, _ = self.best(name)
            self.assertEqual(a.embedded, [], name)

    def test_pe_detection_on_python_exe(self):
        a = formats.analyze(sys.executable, find_embedded_files=False)
        self.assertEqual(a.best.category, "Ausführbar")
        self.assertIn("EXE", a.best.detail)

    def test_block_entropy_profile(self):
        a, _ = self.best("container_unbekannt.bin")
        self.assertTrue(a.blocks)
        self.assertGreater(max(a.blocks), 7.0)
        self.assertLess(min(a.blocks), 1.0)       # Bereich mit 0x11-Bytes


class TestLZ4(unittest.TestCase):
    def test_roundtrip(self):
        for data in (b"", b"abc", b"a" * 5000, bytes(range(256)) * 40, os.urandom(3000) + b"xyz" * 900):
            c = lz4.compress_block_simple(data)
            self.assertEqual(lz4.decompress_block(c, len(data)), data)

    def test_overlap_copy(self):
        # Token: 1 Literal 'a', Match Offset 1, Länge 4+6=10  -> 11 x 'a', dann 5 Literale
        block = bytes([0x16]) + b"a" + b"\x01\x00" + bytes([0x50]) + b"bcdef"
        self.assertEqual(lz4.decompress_block(block), b"a" * 11 + b"bcdef")

    def test_corrupt(self):
        with self.assertRaises(lz4.LZ4Error):
            lz4.decompress_block(b"\x10a\x05\x00", 10)


class TestArchives(Samples):
    def check(self, name, expected: dict, fmt_part: str):
        r = archives.open_archive(self.p[name])
        try:
            self.assertIn(fmt_part, r.format_name)
            got = {e.name: r.read(e) for e in r.entries}
            for k, v in expected.items():
                self.assertIn(k, got, f"{name}: {k} fehlt, vorhanden: {list(got)}")
                self.assertEqual(got[k], v, f"{name}: {k} Inhalt falsch")
            return r, got
        finally:
            r.close()

    def test_zip(self):
        self.check("mod.pk3", {"textures/wand.png": ms.png_bytes(), "sounds/schritt.wav": ms.wav_bytes()}, "ZIP")

    def test_quake_pak(self):
        self.check("quake.pak", {"gfx/bild.png": ms.png_bytes(), "sound/ton.wav": ms.wav_bytes()}, "Quake")

    def test_unreal(self):
        text = ("Dies ist ein Beispieltext für komprimierte Daten. " * 4000).encode()[:1000]
        for name in ("unreal_v8.pak", "unreal_v3.pak"):
            self.check(name, {"Content/a.png": ms.png_bytes(), "Content/b.txt": text}, "Unreal")

    def test_unity_all_compressions(self):
        cab_png = ms.png_bytes()
        for name in ("bundle_lz4.unity3d", "bundle_lzma.unity3d", "bundle_roh.unity3d"):
            r, got = self.check(name, {"CAB-0123456789abcdef.resource": ms.fsb5_bytes(),
                                       "CAB-0123456789abcdef.resS": b"\x10" * 3000}, "UnityFS")
            self.assertIn(cab_png, got["CAB-0123456789abcdef"])
            # Eintrag durchsuchen findet PNG
            sub = archives.open_archive(data=got["CAB-0123456789abcdef"], force_carve=True)
            self.assertEqual([sub.read(e) for e in sub.entries], [cab_png])
            sub2 = archives.open_archive(data=got["CAB-0123456789abcdef.resource"], force_carve=False)
            self.assertIsInstance(sub2, archives.CarveReader)

    def test_vpk_with_part_archive(self):
        self.check("pak01_dir.vpk", {"materials/wand.png": ms.png_bytes(), "sound/big_music.wav": ms.wav_bytes(),
                                     "readme.txt": b"Hallo VPK!"}, "VPK")

    def test_godot(self):
        for name in ("spiel_godot4.pck", "spiel_godot3.pck"):
            self.check(name, {"icon.png": ms.png_bytes(), "sfx/klick.wav": ms.wav_bytes()}, "Godot")

    def test_carve_reader(self):
        r, got = self.check("container_unbekannt.bin", {}, "Signatur")
        self.assertEqual(sorted(got.values(), key=len),
                         sorted([ms.png_bytes(), ms.wav_bytes(), ms.jpeg_bytes()], key=len))

    def test_doom_conversion(self):
        r = archives.open_archive(self.p["doom_beispiel.wad"])
        try:
            ents = {e.name: e for e in r.entries}
            self.assertIn("flats/FLOOR1", ents)
            self.assertIn("sprites/TROOA1", ents)
            name, png = r.convert(ents["TITLEPIC"], r.read(ents["TITLEPIC"]))
            self.assertEqual(name, "TITLEPIC.png")
            img = Image.open(io.BytesIO(png)).convert("RGBA")
            self.assertEqual(img.size, (4, 3))
            # Palette: Index i -> (i, 2i, 255-i); Pixel (x=2,y=1) hat Index 21
            self.assertEqual(img.getpixel((2, 1)), (21, 42, 234, 255))
            self.assertEqual(img.getpixel((3, 0))[3], 0)        # Lücke ist transparent
            self.assertEqual(img.getpixel((3, 1)), (31, 62, 224, 255))
            name, wav = r.convert(ents["DSPISTOL"], r.read(ents["DSPISTOL"]))
            with wave.open(io.BytesIO(wav)) as w:
                self.assertEqual((w.getframerate(), w.getnframes(), w.getsampwidth()), (11025, 200, 1))
                self.assertEqual(w.readframes(3), bytes([0, 3, 6]))
            name, flat = r.convert(ents["flats/FLOOR1"], r.read(ents["flats/FLOOR1"]))
            img = Image.open(io.BytesIO(flat))
            self.assertEqual(img.size, (64, 64))
            self.assertEqual(img.getpixel((5, 1)), (69, 138, 186))   # Index 64+5
        finally:
            r.close()

    def test_wad3_conversion(self):
        r = archives.open_archive(self.p["halflife_texturen.wad"])
        try:
            ents = {e.name: e for e in r.entries}
            _, png = r.convert(ents["BRICK"], r.read(ents["BRICK"]))
            img = Image.open(io.BytesIO(png))
            self.assertEqual(img.size, (16, 16))
            self.assertEqual(img.getpixel((3, 2)), (5, 0, 250))
            _, png = r.convert(ents["{FENCE"], r.read(ents["{FENCE"]))
            self.assertEqual(Image.open(io.BytesIO(png)).mode, "RGBA")
        finally:
            r.close()

    def test_safe_extraction(self):
        out = tempfile.mkdtemp(prefix="pi_out_")
        try:
            r = archives.open_archive(self.p["mod.pk3"])
            n, errors = archives.extract(r, r.entries, out)
            r.close()
            self.assertEqual((n, errors), (4, []))
            parent = os.path.dirname(out)
            self.assertFalse(os.path.exists(os.path.join(parent, "boese_ausserhalb.txt")))
            self.assertTrue(os.path.exists(os.path.join(out, "boese_ausserhalb.txt")))
            for bad in ("C:\\Windows\\x.dll", "..\\..\\a", "/etc/passwd", "CON.txt", "a/b:c?.png"):
                p = archives.target_path(out, bad)
                self.assertTrue(p.startswith(os.path.abspath(out) + os.sep), p)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_extract_only_media_with_conversion(self):
        out = tempfile.mkdtemp(prefix="pi_out_")
        try:
            r = archives.open_archive(self.p["doom_beispiel.wad"])
            n, errors = archives.extract(r, r.entries, out, convert=True, only_media=True)
            r.close()
            files = sorted(os.path.relpath(os.path.join(d, f), out).replace("\\", "/")
                           for d, _, fs in os.walk(out) for f in fs)
            self.assertEqual(errors, [])
            self.assertEqual(files, ["DSPISTOL.wav", "TITLEPIC.png", "flats/FLOOR1.png", "sprites/TROOA1.png"])
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_source_unchanged(self):
        import hashlib
        before = {k: _sha(v) for k, v in self.p.items()}
        for k, v in self.p.items():
            formats.analyze(v)
            try:
                r = archives.open_archive(v)
                for e in r.entries:
                    try:
                        r.read(e)
                    except archives.ArchiveError:
                        pass
                r.close()
            except archives.ArchiveError:
                pass
        after = {k: _sha(v) for k, v in self.p.items()}
        self.assertEqual(before, after)


class TestRegistry(unittest.TestCase):
    def write_snap(self, path, roots, keys: dict):
        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as w:
            w.write("#PIREG 1\n")
            w.write("M " + json.dumps({"created": "x", "roots": roots}) + "\n")
            for k, vals in keys.items():
                w.write("K " + json.dumps(k) + "\n")
                for v in vals:
                    w.write("V " + json.dumps(v) + "\n")

    def test_compare_synthetic(self):
        d = tempfile.mkdtemp()
        try:
            a, b = os.path.join(d, "a.pireg"), os.path.join(d, "b.pireg")
            root = "HKEY_CURRENT_USER\\Software"
            self.write_snap(a, [root], {
                root: [],
                root + "\\Alt": [["x", 1, "1"]],
                root + "\\Spiel": [["Lautstaerke", 4, "0x00000050 (80)"], ["Name", 1, "Held"]],
                "HKEY_LOCAL_MACHINE\\SOFTWARE\\Ignoriert": [],
            })
            self.write_snap(b, [root, "HKEY_LOCAL_MACHINE\\SOFTWARE"], {
                root: [],
                root + "\\SPIEL": [["lautstaerke", 4, "0x00000064 (100)"], ["Pfad", 1, "C:\\Spiele"]],
                root + "\\Neu": [["Installiert", 4, "0x00000001 (1)"]],
                "HKEY_LOCAL_MACHINE\\SOFTWARE\\NurNachher": [],
            })
            changes, info = registry.compare(a, b)
            got = {(c.kind, c.key.lower(), c.name.lower(), c.old, c.new) for c in changes}
            r = root.lower()
            self.assertEqual(got, {
                (registry.KEY_ADDED, r + "\\neu", "", "", ""),
                (registry.VAL_ADDED, r + "\\neu", "installiert", "", "0x00000001 (1)"),
                (registry.KEY_REMOVED, r + "\\alt", "", "", ""),
                (registry.VAL_REMOVED, r + "\\alt", "x", "1", ""),
                (registry.VAL_CHANGED, r + "\\spiel", "lautstaerke", "0x00000050 (80)", "0x00000064 (100)"),
                (registry.VAL_ADDED, r + "\\spiel", "pfad", "", "C:\\Spiele"),
                (registry.VAL_REMOVED, r + "\\spiel", "name", "Held", ""),
            })
            self.assertEqual(info["roots"], [r])       # HKLM nur in einem Snapshot -> nicht verglichen
            registry.export_text(changes, info, os.path.join(d, "diff.txt"))
            with open(os.path.join(d, "diff.txt"), encoding="utf-8-sig") as f:
                self.assertIn("Wert geändert", f.read())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_real_snapshot_readonly(self):
        d = tempfile.mkdtemp()
        try:
            a, b = os.path.join(d, "a.pireg"), os.path.join(d, "b.pireg")
            st = registry.take_snapshot(["HKCU\\Control Panel"], a, "vorher")
            self.assertGreater(st.keys, 5)
            self.assertGreater(st.values, 10)
            registry.take_snapshot(["HKEY_CURRENT_USER\\Control Panel"], b, "nachher")
            self.assertEqual(registry.read_meta(a)["roots"], ["HKEY_CURRENT_USER\\Control Panel"])
            changes, _ = registry.compare(a, b)
            self.assertLessEqual(len(changes), 3)    # in Sekunden ändert sich hier praktisch nichts
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_value_text(self):
        import winreg
        self.assertEqual(registry.value_text(winreg.REG_DWORD, 255), "0x000000ff (255)")
        self.assertEqual(registry.value_text(winreg.REG_MULTI_SZ, ["a", "b"]), "a | b")
        long = registry.value_text(winreg.REG_BINARY, bytes(2000))
        self.assertIn("2000 Bytes", long)
        self.assertNotEqual(long, registry.value_text(winreg.REG_BINARY, bytes(1999) + b"\1"))


class TestWatcher(unittest.TestCase):
    def test_events(self):
        d = tempfile.mkdtemp(prefix="pi_watch_")
        events = []
        lock = threading.Lock()

        def cb(ev):
            with lock:
                events.append(ev)

        w = watcher.DirWatcher(d, cb)
        w.start()
        w.ready.wait(2)
        time.sleep(0.2)
        try:
            os.makedirs(os.path.join(d, "Saves"))
            p = os.path.join(d, "Saves", "slot1.sav")
            with open(p, "wb") as f:
                f.write(b"1")
            time.sleep(0.1)
            with open(p, "ab") as f:
                f.write(b"2")
            os.rename(p, p + ".bak")
            os.remove(p + ".bak")
            deadline = time.time() + 3
            while time.time() < deadline:
                with lock:
                    acts = {(e.action, os.path.basename(e.path)) for e in events}
                if ("gelöscht", "slot1.sav.bak") in acts:
                    break
                time.sleep(0.05)
        finally:
            w.stop()
            w.join(2)
            shutil.rmtree(d, ignore_errors=True)
        self.assertFalse(w.is_alive())
        self.assertIn(("erstellt", "slot1.sav"), acts)
        self.assertIn(("geändert", "slot1.sav"), acts)
        self.assertIn(("umbenannt (neu)", "slot1.sav.bak"), acts)
        self.assertIn(("gelöscht", "slot1.sav.bak"), acts)

    def test_bad_folder(self):
        w = watcher.DirWatcher(os.path.join(tempfile.gettempdir(), "gibt_es_nicht_123456"), lambda e: None)
        w.start()
        w.join(2)
        self.assertIsNotNone(w.error)

    def test_helpers(self):
        self.assertEqual(watcher.guess_role(r"C:\Users\x\Saved Games\Spiel\slot1.sav"), "möglicher Spielstand")
        self.assertEqual(watcher.guess_role(r"C:\Spiel\settings.ini"), "Einstellungen/Config")
        self.assertTrue(watcher.is_excluded(r"C:\Users\x\AppData\Local\Temp\a.txt", watcher.DEFAULT_EXCLUDES))
        self.assertFalse(watcher.is_excluded(r"C:\Users\x\Documents\My Games\a.sav", watcher.DEFAULT_EXCLUDES))
        procs = watcher.list_processes()
        self.assertTrue(any(pid == os.getpid() for pid, _, _ in procs))
        self.assertTrue(any(path.lower() == sys.executable.lower() for pid, _, path in procs if pid == os.getpid()))


if __name__ == "__main__":
    unittest.main()
