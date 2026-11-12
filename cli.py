"""FileProbe – Kommandozeile für Claude/ToolHub.

Jeder Befehl gibt genau ein JSON-Objekt auf stdout aus. Bei Fehlern: Meldung (als JSON)
auf stderr und Exit-Code 1 (Aufruffehler: 2). Befehle, die Dateien schreiben, kennen
--dry-run und zeigen dann nur, was passieren würde.

    python cli.py identify --path datei.bin
    python cli.py archive-list --path spiel.pak
    python cli.py archive-extract --path spiel.pak --out ziel --media-only --dry-run
    python cli.py registry-snapshot --roots "HKCU\\Software" --label vorher
    python cli.py registry-compare --before a.pireg --after b.pireg
    python cli.py watch --process spiel.exe --seconds 30
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import io
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from inspector import __version__, archives, formats, registry, watcher  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "snapshots")


class CliError(Exception):
    pass


def _file(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
    if not os.path.isfile(path):
        raise CliError(f"Datei nicht gefunden: {path}")
    return path


def _limit(items: list, limit: int) -> tuple[list, bool]:
    if limit and limit > 0 and len(items) > limit:
        return items[:limit], True
    return items, False


# --------------------------------------------------------------------------- #
# Dateien
# --------------------------------------------------------------------------- #

def cmd_identify(a):
    path = _file(a.path)
    an = formats.analyze(path, find_embedded_files=not a.no_embedded)
    emb, trunc = _limit(an.embedded, a.limit)
    return {
        "path": path,
        "size": an.size,
        "detected": [{"name": m.name, "category": m.category, "ext": m.ext, "confidence": m.confidence,
                      "detail": m.detail} for m in an.matches],
        "best": an.best.name if an.best else None,
        "entropy": round(an.entropy, 4),
        "entropy_verdict": formats.entropy_verdict(an.entropy),
        "chi_square": round(an.chi2, 1),
        "hints": an.hints,
        "embedded": [{"offset": e.offset, "type": e.name, "category": e.category, "ext": e.ext, "length": e.length}
                     for e in emb],
        "embedded_total": len(an.embedded),
        "embedded_truncated": trunc,
        "entropy_profile": {"block_size": an.block_size,
                            "blocks": [round(b, 2) for b in an.blocks[:: max(1, len(an.blocks) // 64)]]},
    }


def cmd_strings(a):
    path = _file(a.path)
    with open(path, "rb") as f:
        data = f.read(a.max_bytes)
    found = formats.extract_strings(data, len(data), a.min_length, max_count=100000)
    if a.filter:
        found = [s for s in found if a.filter.lower() in s[1].lower()]
    items, trunc = _limit(found, a.limit)
    return {"path": path, "scanned_bytes": len(data), "count": len(found), "truncated": trunc,
            "strings": [{"offset": o, "text": s} for o, s in items]}


def cmd_hexdump(a):
    path = _file(a.path)
    ofs = int(a.offset, 0)
    length = max(1, min(a.length, 65536))
    with open(path, "rb") as f:
        f.seek(ofs)
        data = f.read(length)
    from inspector.gui.common import hexdump
    return {"path": path, "offset": ofs, "length": len(data), "hex": data.hex(), "dump": hexdump(data, ofs)}


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #

def _open(a):
    try:
        return archives.open_archive(_file(a.path), force_carve=a.carve)
    except archives.ArchiveError as e:
        raise CliError(str(e)) from e


def _select(r: archives.Reader, patterns: str | None, kind: str | None):
    ents = r.entries
    if patterns:
        pats = [p.strip() for p in patterns.split(";") if p.strip()]
        ents = [e for e in ents if any(fnmatch.fnmatch(e.name.lower(), p.lower()) or e.name == p for p in pats)]
    if kind:
        ents = [e for e in ents if (e.kind or "").lower() == kind.lower()]
    return ents


def cmd_archive_list(a):
    r = _open(a)
    try:
        ents = _select(r, a.filter, a.kind)
        items, trunc = _limit(ents, a.limit)
        return {"path": r.src.path, "format": r.format_name, "notes": r.notes, "total_entries": len(r.entries),
                "matching": len(ents), "truncated": trunc, "total_size": sum(e.size for e in ents),
                "entries": [{"name": e.name, "size": e.size, "kind": e.kind, "info": e.info} for e in items]}
    finally:
        r.close()


def cmd_archive_extract(a):
    r = _open(a)
    try:
        ents = _select(r, a.entries, a.kind)
        if not ents:
            raise CliError("Keine passenden Einträge im Archiv.")
        out = os.path.abspath(a.out)
        plan: list = []
        n, errors = archives.extract(r, ents, out, convert=not a.no_convert, only_media=a.media_only,
                                     dry_run=a.dry_run, plan=plan)
        files, trunc = _limit(plan, a.limit)
        return {"dry_run": a.dry_run, "archive": r.src.path, "format": r.format_name, "out": out,
                "count": n, "bytes": sum(p["size"] for p in plan), "errors": errors,
                "files": files, "files_truncated": trunc}
    finally:
        r.close()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def _roots(s: str) -> list[str]:
    try:
        roots = [registry.normalize_root(r) for r in s.split(";") if r.strip()]
    except ValueError as e:
        raise CliError(str(e)) from e
    if not roots:
        raise CliError("Keine Registry-Zweige angegeben.")
    return roots


def cmd_registry_snapshot(a):
    roots = _roots(a.roots)
    label = a.label or "snapshot"
    out = os.path.abspath(a.out) if a.out else os.path.join(
        SNAP_DIR, time.strftime("%Y-%m-%d_%H-%M-%S") + f"_{label}.pireg")
    if a.dry_run:
        return {"dry_run": True, "roots": roots, "label": label, "out": out,
                "note": "Liest die Zweige nur (KEY_READ) und würde die Snapshot-Datei schreiben."}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    st = registry.take_snapshot(roots, out, label)
    return {"dry_run": False, "roots": roots, "label": label, "out": out, "keys": st.keys, "values": st.values,
            "access_denied": st.denied, "seconds": round(st.seconds, 2), "file_size": os.path.getsize(out)}


def cmd_registry_snapshots(a):
    folder = os.path.abspath(a.folder) if a.folder else SNAP_DIR
    snaps = []
    for p in sorted(glob.glob(os.path.join(folder, "*.pireg")), key=os.path.getmtime):
        try:
            m = registry.read_meta(p)
        except (OSError, ValueError, EOFError):
            continue
        snaps.append({"path": p, "created": m.get("created"), "label": m.get("label"), "roots": m.get("roots", []),
                      "file_size": os.path.getsize(p)})
    return {"folder": folder, "count": len(snaps), "snapshots": snaps}


def cmd_registry_compare(a):
    before, after = _file(a.before), _file(a.after)
    try:
        changes, info = registry.compare(before, after)
    except ValueError as e:
        raise CliError(str(e)) from e
    if a.filter:
        f = a.filter.lower()
        changes = [c for c in changes if f in c.key.lower() or f in c.name.lower() or f in c.new.lower()
                   or f in c.old.lower()]
    items, trunc = _limit(changes, a.limit)
    counts: dict[str, int] = {}
    for c in changes:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return {"before": before, "after": after, "roots_compared": info["roots"],
            "keys_added": info["keys_added"], "keys_removed": info["keys_removed"],
            "keys_changed": info["keys_changed"], "counts": counts, "total_changes": len(changes),
            "truncated": trunc,
            "changes": [{"kind": c.kind, "key": c.key, "value": c.name, "type": c.vtype, "old": c.old, "new": c.new}
                        for c in items]}


# --------------------------------------------------------------------------- #
# Datei-Wächter
# --------------------------------------------------------------------------- #

def cmd_processes(a):
    procs = [p for p in watcher.list_processes() if p[2]]
    if a.filter:
        f = a.filter.lower()
        procs = [p for p in procs if f in p[1].lower() or f in p[2].lower()]
    items, trunc = _limit(sorted(procs, key=lambda p: p[1].lower()), a.limit)
    return {"count": len(procs), "truncated": trunc,
            "processes": [{"pid": pid, "name": n, "path": p, "folder": os.path.dirname(p)} for pid, n, p in items]}


def _process_folders(spec: str) -> list[dict]:
    hits = []
    for pid, name, path in watcher.list_processes():
        if not path:
            continue
        if (spec.isdigit() and int(spec) == pid) or spec.lower() in (name.lower(), os.path.splitext(name)[0].lower()):
            hits.append({"pid": pid, "name": name, "folder": os.path.dirname(path)})
    if not hits:
        raise CliError(f"Kein laufender Prozess „{spec}“ gefunden (Name wie spiel.exe oder PID).")
    return hits


def cmd_watch(a):
    seconds = max(1.0, min(float(a.seconds), 600.0))
    folders: list[str] = []
    procs = []
    if a.process:
        procs = _process_folders(a.process)
        folders += [p["folder"] for p in procs]
    if a.folders:
        folders += [os.path.abspath(f.strip()) for f in a.folders.split(";") if f.strip()]
    if a.default_folders or not folders:
        folders += watcher.default_folders()
    seen, uniq = set(), []
    for f in folders:
        if f.lower() not in seen:
            seen.add(f.lower())
            uniq.append(f)
    pats = list(watcher.DEFAULT_EXCLUDES) + [p.strip() for p in (a.exclude or "").split(";") if p.strip()]

    events = []
    lock = threading.Lock()

    def cb(ev):
        with lock:
            events.append(ev)

    ws = [watcher.DirWatcher(f, cb) for f in uniq]
    for w in ws:
        w.start()
    for w in ws:
        w.ready.wait(2)
    time.sleep(seconds)
    for w in ws:
        w.stop()
    for w in ws:
        w.join(2)

    files: dict[str, dict] = {}
    log = []
    ignored = 0
    root_pats = {w.path: [p for p in pats if not watcher.is_excluded(w.path + "\\", [p])] for w in ws}
    for ev in events:
        if ev.action in ("Fehler", "Überlauf"):
            log.append({"time": time.strftime("%H:%M:%S", time.localtime(ev.time)), "action": ev.action,
                        "path": ev.path})
            continue
        if watcher.is_excluded(ev.path, root_pats.get(ev.root, pats)):
            ignored += 1
            continue
        if ev.action != "gelöscht" and os.path.isdir(ev.path):
            continue
        ts = time.strftime("%H:%M:%S", time.localtime(ev.time))
        log.append({"time": ts, "action": ev.action, "path": ev.path})
        d = files.setdefault(ev.path.lower(), {"path": ev.path, "changes": 0, "role": watcher.guess_role(ev.path)})
        d["changes"] += 1
        d["last_action"] = ev.action
        d["last_time"] = ts
    for d in files.values():
        try:
            d["size"] = os.path.getsize(d["path"])
        except OSError:
            d["size"] = None
    summary = sorted(files.values(), key=lambda d: -d["changes"])
    summary, trunc = _limit(summary, a.limit)
    log, log_trunc = _limit(log, a.limit)
    return {"seconds": seconds, "processes": procs,
            "folders": [{"path": w.path, "error": w.error} for w in ws],
            "note": "Windows meldet nicht, welcher Prozess schreibt; gezeigt wird alles in den überwachten Ordnern.",
            "files_changed": len(files), "ignored_events": ignored, "files": summary, "files_truncated": trunc,
            "events": log, "events_truncated": log_trunc}


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description=f"FileProbe {__version__} – JSON-Kommandozeile")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, func, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="JSON ausgeben (Standard, für ToolHub)")
        sp.set_defaults(func=func)
        return sp

    sp = add("identify", cmd_identify, "Dateiformat, Entropie, Hinweise, eingebettete Dateien")
    sp.add_argument("--path", required=True)
    sp.add_argument("--no-embedded", action="store_true")
    sp.add_argument("--limit", type=int, default=200)

    sp = add("strings", cmd_strings, "Lesbare Zeichenketten")
    sp.add_argument("--path", required=True)
    sp.add_argument("--min-length", type=int, default=5)
    sp.add_argument("--filter")
    sp.add_argument("--max-bytes", type=int, default=16 << 20)
    sp.add_argument("--limit", type=int, default=500)

    sp = add("hexdump", cmd_hexdump, "Bytes ab Offset")
    sp.add_argument("--path", required=True)
    sp.add_argument("--offset", default="0")
    sp.add_argument("--length", type=int, default=256)

    sp = add("archive-list", cmd_archive_list, "Inhalt eines Spiel-Archivs")
    sp.add_argument("--path", required=True)
    sp.add_argument("--filter", help="Muster mit * und ?, mehrere mit ; trennen")
    sp.add_argument("--kind")
    sp.add_argument("--carve", action="store_true")
    sp.add_argument("--limit", type=int, default=500)

    sp = add("archive-extract", cmd_archive_extract, "Einträge entpacken")
    sp.add_argument("--path", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--entries", help="Muster mit * und ?, mehrere mit ; trennen (Standard: alle)")
    sp.add_argument("--kind")
    sp.add_argument("--media-only", action="store_true")
    sp.add_argument("--no-convert", action="store_true")
    sp.add_argument("--carve", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--limit", type=int, default=500)

    sp = add("registry-snapshot", cmd_registry_snapshot, "Registry-Zweige sichern (nur lesen)")
    sp.add_argument("--roots", default="HKCU\\Software;HKLM\\SOFTWARE")
    sp.add_argument("--label")
    sp.add_argument("--out")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("registry-snapshots", cmd_registry_snapshots, "Vorhandene Snapshots")
    sp.add_argument("--folder")

    sp = add("registry-compare", cmd_registry_compare, "Zwei Snapshots vergleichen")
    sp.add_argument("--before", required=True)
    sp.add_argument("--after", required=True)
    sp.add_argument("--filter")
    sp.add_argument("--limit", type=int, default=1000)

    sp = add("processes", cmd_processes, "Laufende Programme mit Pfad")
    sp.add_argument("--filter")
    sp.add_argument("--limit", type=int, default=300)

    sp = add("watch", cmd_watch, "Dateiänderungen N Sekunden beobachten")
    sp.add_argument("--seconds", type=float, default=15)
    sp.add_argument("--process")
    sp.add_argument("--folders")
    sp.add_argument("--default-folders", action="store_true")
    sp.add_argument("--exclude")
    sp.add_argument("--limit", type=int, default=300)
    return p


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if isinstance(s, io.TextIOWrapper):
            s.reconfigure(encoding="utf-8")
    parser = build_parser()

    def parse_error(msg):
        print(json.dumps({"error": msg, "usage": parser.format_usage().strip()}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

    parser.error = parse_error
    for sp in parser._subparsers._group_actions[0].choices.values():
        sp.error = parse_error
    a = parser.parse_args(argv)
    try:
        result = a.func(a)
    except CliError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 – alles als JSON-Fehler melden
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
