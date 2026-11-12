"""Registry-Snapshots (nur lesen) und Vergleich.

Ein Snapshot wird direkt beim Einlesen als gzip-Textdatei gespeichert, damit auch
große Zweige (HKLM\\SOFTWARE) nicht komplett im Speicher liegen müssen:

    #PIREG 1
    M {"created": ..., "roots": [...], ...}
    K "HKEY_CURRENT_USER\\Software\\Foo"
    V ["Name", 1, "Wert"]

Die Registry wird ausschließlich mit KEY_READ geöffnet.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import winreg
from dataclasses import dataclass

HIVES = {
    "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
    "HKEY_USERS": winreg.HKEY_USERS,
    "HKEY_CURRENT_CONFIG": winreg.HKEY_CURRENT_CONFIG,
}
SHORT = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE", "HKCR": "HKEY_CLASSES_ROOT",
         "HKU": "HKEY_USERS", "HKCC": "HKEY_CURRENT_CONFIG"}

TYPE_NAMES = {
    winreg.REG_NONE: "REG_NONE", winreg.REG_SZ: "REG_SZ", winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
    winreg.REG_BINARY: "REG_BINARY", winreg.REG_DWORD: "REG_DWORD", winreg.REG_DWORD_BIG_ENDIAN: "REG_DWORD_BE",
    winreg.REG_LINK: "REG_LINK", winreg.REG_MULTI_SZ: "REG_MULTI_SZ", winreg.REG_RESOURCE_LIST: "REG_RESOURCE_LIST",
    winreg.REG_FULL_RESOURCE_DESCRIPTOR: "REG_FULL_RESOURCE_DESCRIPTOR",
    winreg.REG_RESOURCE_REQUIREMENTS_LIST: "REG_RESOURCE_REQUIREMENTS_LIST", winreg.REG_QWORD: "REG_QWORD",
}

ACCESS = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
MAX_VALUE_TEXT = 512


def normalize_root(path: str) -> str:
    path = path.strip().strip("\\").replace("/", "\\")
    hive, _, rest = path.partition("\\")
    hive = SHORT.get(hive.upper(), hive.upper())
    if hive not in HIVES:
        raise ValueError(f"Unbekannter Registry-Zweig: {path}")
    return hive + ("\\" + rest if rest else "")


def value_text(typ: int, data) -> str:
    """Kompakte, vergleichbare Textdarstellung eines Werts."""
    if data is None:
        s = ""
    elif typ in (winreg.REG_DWORD, winreg.REG_QWORD, winreg.REG_DWORD_BIG_ENDIAN) and isinstance(data, int):
        s = f"0x{data:08x} ({data})"
    elif typ == winreg.REG_MULTI_SZ and isinstance(data, list):
        s = " | ".join(data)
    elif isinstance(data, (bytes, bytearray)):
        s = data.hex(" ")
        if len(s) > MAX_VALUE_TEXT:
            digest = hashlib.sha1(data).hexdigest()[:12]
            s = s[:MAX_VALUE_TEXT] + f" … ({len(data)} Bytes, sha1 {digest})"
        return s
    else:
        s = str(data)
    if len(s) > MAX_VALUE_TEXT:
        digest = hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        s = s[:MAX_VALUE_TEXT] + f" … ({len(s)} Zeichen, sha1 {digest})"
    return s


@dataclass
class SnapshotStats:
    keys: int = 0
    values: int = 0
    denied: int = 0
    seconds: float = 0.0
    cancelled: bool = False


def take_snapshot(roots: list[str], out_path: str, label: str = "", progress=None, cancel=None) -> SnapshotStats:
    """Liest die angegebenen Zweige rekursiv (nur lesen) und schreibt sie nach `out_path`."""
    roots = [normalize_root(r) for r in roots]
    st = SnapshotStats()
    t0 = time.time()
    tmp = out_path + ".tmp"
    last = [0.0]
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5, newline="\n") as w:
        w.write("#PIREG 1\n")
        meta = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "label": label, "roots": roots,
                "computer": os.environ.get("COMPUTERNAME", "")}
        w.write("M " + json.dumps(meta, ensure_ascii=False) + "\n")

        def walk(h, path, depth=0):
            if cancel is not None and cancel():
                st.cancelled = True
                return
            w.write("K " + json.dumps(path, ensure_ascii=False) + "\n")
            st.keys += 1
            try:
                nsub, nval, _ = winreg.QueryInfoKey(h)
            except OSError:
                st.denied += 1
                return
            for i in range(nval):
                try:
                    name, data, typ = winreg.EnumValue(h, i)
                except OSError:
                    continue
                w.write("V " + json.dumps([name, typ, value_text(typ, data)], ensure_ascii=False) + "\n")
                st.values += 1
            subs = []
            for i in range(nsub):
                try:
                    subs.append(winreg.EnumKey(h, i))
                except OSError:
                    break
            if progress and time.time() - last[0] > 0.25:
                last[0] = time.time()
                progress(st, path)
            if depth > 400:
                return
            for s in subs:
                try:
                    with winreg.OpenKey(h, s, 0, ACCESS) as ch:
                        walk(ch, path + "\\" + s, depth + 1)
                except OSError:
                    st.denied += 1
                if st.cancelled:
                    return

        for root in roots:
            hive, _, sub = root.partition("\\")
            try:
                with winreg.OpenKey(HIVES[hive], sub, 0, ACCESS) as h:
                    walk(h, root)
            except FileNotFoundError:
                pass
            except OSError:
                st.denied += 1
            if st.cancelled:
                break
    st.seconds = time.time() - t0
    if st.cancelled:
        os.remove(tmp)
    else:
        os.replace(tmp, out_path)
    return st


def read_meta(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as r:
        if r.readline().strip() != "#PIREG 1":
            raise ValueError("Keine FileProbe-Snapshot-Datei")
        line = r.readline()
        return json.loads(line[2:]) if line.startswith("M ") else {}


def _iter(path: str):
    """Liefert (Schlüssel, [Werte]) je Schlüssel."""
    with gzip.open(path, "rt", encoding="utf-8") as r:
        if r.readline().strip() != "#PIREG 1":
            raise ValueError(f"{os.path.basename(path)} ist keine Snapshot-Datei")
        key = None
        vals = []
        for line in r:
            tag = line[:2]
            if tag == "K ":
                if key is not None:
                    yield key, vals
                key = json.loads(line[2:])
                vals = []
            elif tag == "V ":
                vals.append(json.loads(line[2:]))
        if key is not None:
            yield key, vals


def _digest(vals) -> bytes:
    h = hashlib.blake2b(digest_size=12)
    for name, typ, text in sorted(vals, key=lambda v: v[0].lower()):
        h.update(f"{name.lower()}\0{typ}\0{text}\1".encode("utf-8", "surrogatepass"))
    return h.digest()


@dataclass
class Change:
    kind: str        # siehe KINDS
    key: str
    name: str = ""
    old: str = ""
    new: str = ""
    vtype: str = ""


KEY_ADDED = "Schlüssel neu"
KEY_REMOVED = "Schlüssel gelöscht"
VAL_ADDED = "Wert neu"
VAL_REMOVED = "Wert gelöscht"
VAL_CHANGED = "Wert geändert"


def _within(key_l: str, roots_l: list[str]) -> bool:
    return any(key_l == r or key_l.startswith(r + "\\") for r in roots_l)


def compare(before: str, after: str, progress=None) -> tuple[list[Change], dict]:
    """Vergleicht zwei Snapshot-Dateien. Nur gemeinsam erfasste Zweige werden verglichen."""
    ma, mb = read_meta(before), read_meta(after)
    ra = [r.lower() for r in ma.get("roots", [])]
    rb = [r.lower() for r in mb.get("roots", [])]
    common = [r for r in ra if _within(r, rb)] + [r for r in rb if _within(r, ra) and r not in ra]

    if progress:
        progress("Lese Snapshot „vorher“ …")
    da = {k.lower(): (k, _digest(v)) for k, v in _iter(before) if _within(k.lower(), common)}
    if progress:
        progress("Lese Snapshot „nachher“ …")
    db = {k.lower(): (k, _digest(v)) for k, v in _iter(after) if _within(k.lower(), common)}

    added = [db[k][0] for k in db.keys() - da.keys()]
    removed = [da[k][0] for k in da.keys() - db.keys()]
    changed = {k for k in da.keys() & db.keys() if da[k][1] != db[k][1]}
    need = changed | {k.lower() for k in added} | {k.lower() for k in removed}
    del da, db

    if progress:
        progress("Sammle geänderte Werte …")
    va = {k.lower(): (k, v) for k, v in _iter(before) if k.lower() in need}
    vb = {k.lower(): (k, v) for k, v in _iter(after) if k.lower() in need}

    changes: list[Change] = []
    tn = TYPE_NAMES.get
    for k in sorted(added, key=str.lower):
        changes.append(Change(KEY_ADDED, k))
        for name, typ, text in vb.get(k.lower(), (k, []))[1]:
            changes.append(Change(VAL_ADDED, k, name, "", text, tn(typ, str(typ))))
    for k in sorted(removed, key=str.lower):
        changes.append(Change(KEY_REMOVED, k))
        for name, typ, text in va.get(k.lower(), (k, []))[1]:
            changes.append(Change(VAL_REMOVED, k, name, text, "", tn(typ, str(typ))))
    for kl in sorted(changed):
        key = vb[kl][0]
        a = {n.lower(): (n, t, x) for n, t, x in va[kl][1]}
        b = {n.lower(): (n, t, x) for n, t, x in vb[kl][1]}
        for n in sorted(b.keys() - a.keys()):
            name, t, x = b[n]
            changes.append(Change(VAL_ADDED, key, name, "", x, tn(t, str(t))))
        for n in sorted(a.keys() - b.keys()):
            name, t, x = a[n]
            changes.append(Change(VAL_REMOVED, key, name, x, "", tn(t, str(t))))
        for n in sorted(a.keys() & b.keys()):
            if a[n][1:] != b[n][1:]:
                name = b[n][0]
                ta, tb = tn(a[n][1], str(a[n][1])), tn(b[n][1], str(b[n][1]))
                changes.append(Change(VAL_CHANGED, key, name, a[n][2], b[n][2], tb if ta == tb else f"{ta} → {tb}"))
    info = {"before": ma, "after": mb, "roots": common, "keys_added": len(added),
            "keys_removed": len(removed), "keys_changed": len(changed)}
    return changes, info


def export_text(changes: list[Change], info: dict, path: str):
    """Unterschiede als Textdatei (UTF-8) – bewusst kein .reg, damit nichts versehentlich importiert wird."""
    b, a = info["before"], info["after"]
    lines = [
        "Registry-Vergleich",
        f"Vorher:  {b.get('created')} {b.get('label', '')}",
        f"Nachher: {a.get('created')} {a.get('label', '')}",
        "Zweige: " + ", ".join(info["roots"]),
        f"Schlüssel neu: {info['keys_added']}, gelöscht: {info['keys_removed']}, geändert: {info['keys_changed']}",
        "",
    ]
    for c in changes:
        if c.kind in (KEY_ADDED, KEY_REMOVED):
            lines.append(f"[{c.kind}] {c.key}")
        else:
            lines.append(f"  [{c.kind}] {c.key} : {c.name or '(Standard)'} ({c.vtype})")
            if c.old:
                lines.append(f"      alt: {c.old}")
            if c.new:
                lines.append(f"      neu: {c.new}")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
