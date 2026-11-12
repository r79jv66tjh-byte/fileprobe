"""Erzeugt Beispieldateien zum Ausprobieren und für die Tests.

    python tools/make_samples.py [Zielordner]      (Standard: ./beispiele)

Alle Dateien werden hier selbst gebaut – es wird nichts heruntergeladen.
"""

from __future__ import annotations

import io
import json
import lzma
import os
import random
import struct
import sys
import wave
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from inspector.lz4 import compress_block_simple  # noqa: E402

RNG = random.Random(1234)


# --------------------------------------------------------------------------- #
# Einfache Medien
# --------------------------------------------------------------------------- #

def png_bytes(w=16, h=12, color=(200, 30, 30)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    for x in range(w):
        img.putpixel((x, x % h), (0, 0, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def jpeg_bytes() -> bytes:
    img = Image.new("RGB", (32, 24), (10, 120, 40))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return out.getvalue()


def gif_bytes() -> bytes:
    img = Image.new("P", (20, 10), 3)
    img.putpalette([i % 256 for i in range(768)])
    out = io.BytesIO()
    img.save(out, "GIF")
    return out.getvalue()


def bmp_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (9, 7), (1, 2, 3)).save(out, "BMP")
    return out.getvalue()


def wav_bytes(n=800, rate=11025) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        w.writeframes(bytes((128 + int(100 * ((i // 20) % 2 * 2 - 1))) & 0xFF for i in range(n)))
    return out.getvalue()


def ogg_bytes() -> bytes:
    """Zwei minimale Ogg-Seiten (Struktur gültig, Inhalt Platzhalter, CRC 0)."""
    def page(htype, seq, payload):
        segs = []
        n = len(payload)
        while n >= 255:
            segs.append(255)
            n -= 255
        segs.append(n)
        return (b"OggS" + bytes([0, htype]) + struct.pack("<qIII", 0, 0x1234, seq, 0) +
                bytes([len(segs)]) + bytes(segs) + payload)
    return page(2, 0, b"\x01vorbis" + b"\0" * 23) + page(4, 1, b"x" * 300)


# --------------------------------------------------------------------------- #
# Doom-WAD
# --------------------------------------------------------------------------- #

def doom_palette() -> bytes:
    pal = bytearray()
    for i in range(256):
        pal += bytes((i, (i * 2) & 0xFF, 255 - i))
    return bytes(pal) * 14


def doom_patch(w=4, h=3) -> bytes:
    """Patch: Spalte x hat die Farbindizes 10*x + y, Spalte 3 hat oben eine Lücke (transparent)."""
    cols = []
    for x in range(w):
        if x == 3:
            px = bytes(10 * x + y for y in range(1, h))
            cols.append(bytes([1, len(px), 0]) + px + b"\0\xff")
        else:
            px = bytes(10 * x + y for y in range(h))
            cols.append(bytes([0, len(px), 0]) + px + b"\0\xff")
    hdr = struct.pack("<hhhh", w, h, 0, 0)
    first = 8 + 4 * w
    offs = []
    o = first
    for c in cols:
        offs.append(o)
        o += len(c)
    return hdr + struct.pack(f"<{w}I", *offs) + b"".join(cols)


def dmx_sound(n=200, rate=11025) -> bytes:
    body = b"\x80" * 16 + bytes((i * 3) & 0xFF for i in range(n)) + b"\x80" * 16
    return struct.pack("<HHI", 3, rate, len(body)) + body


def build_wad(lumps: list[tuple[str, bytes]], kind=b"PWAD") -> bytes:
    data = bytearray(b"\0" * 12)
    dirs = []
    for name, payload in lumps:
        dirs.append((len(data) if payload else 0, len(payload), name))
        data += payload
    ofs = len(data)
    for pos, size, name in dirs:
        data += struct.pack("<iI8s", pos, size, name.encode())
    struct.pack_into("<4siI", data, 0, kind, len(dirs), ofs)
    return bytes(data)


def doom_wad() -> bytes:
    return build_wad([
        ("PLAYPAL", doom_palette()),
        ("MAP01", b""),
        ("THINGS", b"\0" * 20),
        ("TITLEPIC", doom_patch()),
        ("DSPISTOL", dmx_sound()),
        ("F_START", b""),
        ("FLOOR1", bytes(i % 256 for i in range(4096))),
        ("F_END", b""),
        ("S_START", b""),
        ("TROOA1", doom_patch(5, 4)),
        ("S_END", b""),
    ], b"IWAD")


# --------------------------------------------------------------------------- #
# Half-Life WAD3
# --------------------------------------------------------------------------- #

def miptex(name: str, w=16, h=16) -> bytes:
    pal = bytes(v for i in range(256) for v in (i, 0, 255 - i))
    mips = [bytes((x + y) % 256 for y in range(h) for x in range(w))]
    for k in (2, 4, 8):
        mips.append(b"\0" * ((w // k) * (h // k)))
    ofs = 40
    offs = []
    for m in mips:
        offs.append(ofs)
        ofs += len(m)
    return (name.encode().ljust(16, b"\0") + struct.pack("<II4I", w, h, *offs) + b"".join(mips) +
            struct.pack("<H", 256) + pal + b"\0\0")


def wad3() -> bytes:
    items = [("BRICK", miptex("BRICK")), ("{FENCE", miptex("{FENCE", 8, 8))]
    data = bytearray(b"\0" * 12)
    dirs = []
    for name, payload in items:
        dirs.append((len(data), len(payload), name))
        data += payload
    ofs = len(data)
    for pos, size, name in dirs:
        data += struct.pack("<iIIBBH16s", pos, size, size, 0x43, 0, 0, name.encode())
    struct.pack_into("<4siI", data, 0, b"WAD3", len(dirs), ofs)
    return bytes(data)


# --------------------------------------------------------------------------- #
# Quake-PAK
# --------------------------------------------------------------------------- #

def quake_pak(files: dict[str, bytes]) -> bytes:
    data = bytearray(b"\0" * 12)
    dirs = []
    for name, payload in files.items():
        dirs.append((name, len(data), len(payload)))
        data += payload
    ofs = len(data)
    for name, pos, size in dirs:
        data += name.encode().ljust(56, b"\0") + struct.pack("<iI", pos, size)
    struct.pack_into("<4siI", data, 0, b"PACK", ofs, 64 * len(dirs))
    return bytes(data)


# --------------------------------------------------------------------------- #
# Unreal-PAK (Legacy-Index, Version 3 oder 8)
# --------------------------------------------------------------------------- #

def _fstr(s: str) -> bytes:
    b = s.encode() + b"\0"
    return struct.pack("<i", len(b)) + b


def ue_pak(files: dict[str, bytes], version=8, compress=("Content/b.txt",)) -> bytes:
    def entry_bytes(offset, size, usize, comp, blocks):
        b = struct.pack("<qqq", offset, size, usize)
        b += struct.pack("<I" if version >= 8 else "<i", comp)
        b += b"\0" * 20
        if comp:
            b += struct.pack("<i", len(blocks))
            for s, e in blocks:
                b += struct.pack("<qq", s, e)
        b += b"\0" + struct.pack("<I", 65536 if comp else 0)
        return b

    data = bytearray()
    index_entries = []
    for name, payload in files.items():
        off = len(data)
        comp = 1 if name in compress else 0
        if comp:
            chunks = [zlib.compress(payload[i:i + 64]) for i in range(0, len(payload), 64)]
            # Header-Länge vorab bestimmen (Blockliste mit Platzhaltern)
            hdr_len = len(entry_bytes(0, 0, 0, comp, [(0, 0)] * len(chunks)))
            blocks = []
            pos = hdr_len
            for c in chunks:
                start = pos if version >= 5 else off + pos
                blocks.append((start, start + len(c)))
                pos += len(c)
            csize = sum(len(c) for c in chunks)
            hdr = entry_bytes(0, csize, len(payload), comp, blocks)
            data += hdr + b"".join(chunks)
            index_entries.append((name, entry_bytes(off, csize, len(payload), comp, blocks)))
        else:
            hdr = entry_bytes(0, len(payload), len(payload), 0, [])
            data += hdr + payload
            index_entries.append((name, entry_bytes(off, len(payload), len(payload), 0, [])))
    idx = _fstr("../../../") + struct.pack("<i", len(index_entries))
    for name, eb in index_entries:
        idx += _fstr(name) + eb
    idx_ofs = len(data)
    data += idx
    footer = b"\0" * 16 + b"\0" + struct.pack("<IIqQ", 0x5A6F12E1, version, idx_ofs, len(idx)) + b"\0" * 20
    if version >= 8:
        footer += b"Zlib".ljust(32, b"\0") + b"\0" * 32 * 4
    return bytes(data + footer)


# --------------------------------------------------------------------------- #
# Unity UnityFS
# --------------------------------------------------------------------------- #

def lzma_unity(data: bytes) -> bytes:
    lc, lp, pb, dsize = 3, 0, 2, 1 << 16
    raw = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA1, "dict_size": dsize, "lc": lc, "lp": lp, "pb": pb}])
    return bytes([(pb * 5 + lp) * 9 + lc]) + struct.pack("<I", dsize) + raw


def unityfs(nodes: list[tuple[str, bytes, int]], comp=2, fmt=7, block_size=1000) -> bytes:
    payload = b"".join(d for _, d, _ in nodes)
    blocks = []
    for i in range(0, len(payload), block_size):
        u = payload[i:i + block_size]
        if comp == 2:
            c = compress_block_simple(u)
        elif comp == 1:
            c = lzma_unity(u)
        else:
            c = u
        blocks.append((u, c))
    info = b"\0" * 16 + struct.pack(">i", len(blocks))
    for u, c in blocks:
        info += struct.pack(">IIH", len(u), len(c), comp)
    info += struct.pack(">i", len(nodes))
    off = 0
    for name, d, flags in nodes:
        info += struct.pack(">qqI", off, len(d), flags) + name.encode() + b"\0"
        off += len(d)
    cinfo = compress_block_simple(info) if comp == 2 else (lzma_unity(info) if comp == 1 else info)
    head = b"UnityFS\0" + struct.pack(">I", fmt) + b"5.x.x\0" + b"2021.3.5f1\0"
    flags = comp | 0x40
    body_head_len = len(head) + 20
    head += struct.pack(">qIII", 0, len(cinfo), len(info), flags)
    if fmt >= 7:
        head += b"\0" * ((16 - len(head) % 16) % 16)
    out = head + cinfo + b"".join(c for _, c in blocks)
    out = out[:body_head_len - 20] + struct.pack(">q", len(out)) + out[body_head_len - 12:]
    return out


def fsb5_bytes(data_len=100) -> bytes:
    return b"FSB5" + struct.pack("<IIIIII", 1, 1, 8, 0, data_len, 15) + b"\0" * 32 + b"\0" * 8 + b"\x55" * data_len


# --------------------------------------------------------------------------- #
# Valve VPK (v2) und Godot PCK
# --------------------------------------------------------------------------- #

def vpk(files: dict[str, bytes], split_name: str | None = None) -> tuple[bytes, bytes]:
    """Gibt (dir.vpk, _000.vpk) zurück. Dateien mit 'big' im Namen landen im Teilarchiv."""
    by_ext: dict[str, dict[str, list]] = {}
    for full, payload in files.items():
        path, _, fn = full.rpartition("/")
        fn, _, ext = fn.rpartition(".")
        by_ext.setdefault(ext, {}).setdefault(path or " ", []).append((fn, payload))
    tree = bytearray()
    dir_data = bytearray()
    part = bytearray()
    for ext, paths in by_ext.items():
        tree += ext.encode() + b"\0"
        for path, fl in paths.items():
            tree += path.encode() + b"\0"
            for fn, payload in fl:
                tree += fn.encode() + b"\0"
                pre = payload[:4]
                rest = payload[4:]
                if "big" in fn:
                    arc, ofs = 0, len(part)
                    part += rest
                else:
                    arc, ofs = 0x7FFF, len(dir_data)
                    dir_data += rest
                tree += struct.pack("<IHHIIH", zlib.crc32(payload), len(pre), arc, ofs, len(rest), 0xFFFF) + pre
            tree += b"\0"
        tree += b"\0"
    tree += b"\0"
    hdr = struct.pack("<IIIIIII", 0x55AA1234, 2, len(tree), len(dir_data), 0, 0, 0)
    return hdr + bytes(tree) + bytes(dir_data), bytes(part)


def godot_pck(files: dict[str, bytes], fmt=2) -> bytes:
    hdr = b"GDPC" + struct.pack("<IIII", fmt, 4 if fmt == 2 else 3, 2, 1)
    if fmt == 2:
        hdr_extra_pos = len(hdr)
        hdr += struct.pack("<IQ", 0, 0)
    hdr += b"\0" * 64 + struct.pack("<I", len(files))
    dir_ = bytearray()
    names = []
    for name in files:
        nb = ("res://" + name).encode()
        nb += b"\0" * ((4 - len(nb) % 4) % 4)
        names.append(nb)
    dir_len = sum(4 + len(nb) + 32 + (4 if fmt == 2 else 0) for nb in names)
    base = len(hdr) + dir_len
    ofs = 0 if fmt == 2 else base
    for nb, payload in zip(names, files.values()):
        dir_ += struct.pack("<I", len(nb)) + nb + struct.pack("<QQ", ofs, len(payload)) + b"\0" * 16
        if fmt == 2:
            dir_ += struct.pack("<I", 0)
        ofs += len(payload)
    out = bytearray(hdr + dir_ + b"".join(files.values()))
    if fmt == 2:
        struct.pack_into("<IQ", out, hdr_extra_pos, 0, base)
    return bytes(out)


# --------------------------------------------------------------------------- #
# Alles schreiben
# --------------------------------------------------------------------------- #

def make_all(out: str) -> dict[str, str]:
    os.makedirs(out, exist_ok=True)
    png, wav, jpg = png_bytes(), wav_bytes(), jpeg_bytes()
    files: dict[str, bytes] = {}

    files["bild_ohne_endung"] = png
    files["eigentlich_ein_bild.txt"] = jpg
    files["animation.gif"] = gif_bytes()
    files["bild.bmp"] = bmp_bytes()
    files["ton.wav"] = wav
    files["musik.ogg"] = ogg_bytes()
    files["einstellungen.ini"] = b"[Video]\r\nWidth=1920\r\nHeight=1080\r\n\r\n[Audio]\r\nVolume=80\r\n"
    files["spielstand.json"] = json.dumps({"level": 7, "gold": 1234, "name": "Held"}, indent=2).encode()
    files["zufall_verschluesselt.bin"] = bytes(RNG.getrandbits(8) for _ in range(256 * 1024))
    text = ("Dies ist ein Beispieltext für komprimierte Daten. " * 4000).encode()
    files["komprimiert_roh.bin"] = zlib.compress(text, 9)[2:-4] + bytes(RNG.getrandbits(8) for _ in range(20000))
    files["xor_verschluesselt.dat"] = bytes(b ^ 0x5A for b in png + b"\0" * 2000)
    blob = bytearray(bytes(RNG.getrandbits(8) for _ in range(5000)))
    blob += png + bytes(RNG.getrandbits(8) for _ in range(3000)) + wav + b"\0" * 1000 + jpg + b"\x11" * 500
    files["container_unbekannt.bin"] = bytes(blob)

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("textures/wand.png", png)
        z.writestr("sounds/schritt.wav", wav)
        z.writestr("scripts/main.lua", "print('hallo')")
        z.writestr("../boese_ausserhalb.txt", "darf nicht nach außen entpackt werden")
    files["mod.pk3"] = zbuf.getvalue()

    files["doom_beispiel.wad"] = doom_wad()
    files["halflife_texturen.wad"] = wad3()
    files["quake.pak"] = quake_pak({"gfx/bild.png": png, "sound/ton.wav": wav, "maps/start.bsp": b"BSP" * 30})
    files["unreal_v8.pak"] = ue_pak({"Content/a.png": png, "Content/b.txt": text[:1000], "Content/c.wav": wav}, 8)
    files["unreal_v3.pak"] = ue_pak({"Content/a.png": png, "Content/b.txt": text[:1000]}, 3)
    cab = bytes(RNG.getrandbits(8) for _ in range(1500)) + png + b"\0" * 700
    nodes = [("CAB-0123456789abcdef", cab, 4), ("CAB-0123456789abcdef.resource", fsb5_bytes(), 0),
             ("CAB-0123456789abcdef.resS", b"\x10" * 3000, 0)]
    files["bundle_lz4.unity3d"] = unityfs(nodes, comp=2)
    files["bundle_lzma.unity3d"] = unityfs(nodes, comp=1, fmt=6)
    files["bundle_roh.unity3d"] = unityfs(nodes, comp=0)
    d, p = vpk({"materials/wand.png": png, "sound/big_music.wav": wav, "readme.txt": b"Hallo VPK!"})
    files["pak01_dir.vpk"] = d
    files["pak01_000.vpk"] = p
    files["spiel_godot4.pck"] = godot_pck({"icon.png": png, "sfx/klick.wav": wav}, 2)
    files["spiel_godot3.pck"] = godot_pck({"icon.png": png, "sfx/klick.wav": wav}, 1)

    paths = {}
    for name, data in files.items():
        p = os.path.join(out, name)
        with open(p, "wb") as f:
            f.write(data)
        paths[name] = p
    return paths


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "beispiele")
    made = make_all(target)
    print(f"{len(made)} Beispieldateien in {target}")
