"""Dateiformat-Erkennung: Signaturen, Entropie, eingebettete Dateien, Hinweise.

Alles hier liest nur. Dateien werden per mmap (ACCESS_READ) bzw. 'rb' geöffnet.
"""

from __future__ import annotations

import json
import math
import mmap
import os
import re
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

# --------------------------------------------------------------------------- #
# Signatur-Datenbank
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Signature:
    name: str
    category: str
    ext: str
    magic: bytes
    offset: int = 0
    validate: object = None   # f(buf) -> bool, buf beginnt bei der Signatur
    carve: object = None      # f(buf, pos) -> Länge oder None
    embedded: bool = False    # stark genug, um mitten in Dateien gesucht zu werden


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u32be(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


# ---- Validierer ------------------------------------------------------------ #

def _v_jpeg(b):
    return len(b) > 3 and (0xE0 <= b[3] <= 0xEF or b[3] in (0xDB, 0xC0, 0xC2, 0xC4, 0xFE, 0xEE))


def _v_bmp(b):
    if len(b) < 30:
        return False
    size, res, dataofs, hdr = struct.unpack_from("<IIII", b, 2)
    return res == 0 and hdr in (12, 40, 52, 56, 64, 108, 124) and 26 <= dataofs < size and size > 30


def _v_riff(kind):
    def f(b):
        return len(b) >= 12 and b[8:12] == kind and _u32(b, 4) >= 4
    return f


def _v_ogg(b):
    return len(b) >= 27 and b[4] == 0


def _v_pe(b):
    if len(b) < 0x40:
        return False
    lfanew = _u32(b, 0x3C)
    return 0x40 <= lfanew < len(b) - 4 and b[lfanew:lfanew + 4] == b"PE\0\0"


def _v_7z(b):
    return len(b) >= 32 and b[6] == 0


def _v_gzip(b):
    return len(b) >= 10 and b[2] == 8 and b[3] & 0xE0 == 0


def _v_wad(b):
    if len(b) < 12:
        return False
    n, ofs = struct.unpack_from("<iI", b, 4)
    return 0 <= n < 1_000_000 and ofs >= 12


def _v_fsb5(b):
    return len(b) >= 60 and _u32(b, 4) in (0, 1) and _u32(b, 8) < 100_000


def _v_zip(b):
    return len(b) >= 30 and _u16(b, 4) < 100 and _u16(b, 26) < 4096


def _v_dds(b):
    return len(b) >= 128 and _u32(b, 4) == 124


def _v_ico(b):
    return len(b) >= 22 and 0 < _u16(b, 4) < 256 and b[9] == 0


def _v_tiff(b):
    return len(b) >= 8 and 8 <= _u32(b, 4) < 1 << 31


def _v_unity(b):
    return len(b) >= 16 and b[7] == 0


def _v_mp3(b):
    # MPEG-1/2 Layer III Frame-Header: 0xFFE (11 Sync-Bits), gültige Bitrate/Rate
    if len(b) < 4 or b[0] != 0xFF or b[1] & 0xE0 != 0xE0:
        return False
    layer = (b[1] >> 1) & 3
    bitrate = b[2] >> 4
    rate = (b[2] >> 2) & 3
    return layer != 0 and bitrate not in (0, 15) and rate != 3


# ---- Längenbestimmung (Carving) ------------------------------------------- #

def _c_png(b, p):
    i = p + 8
    n = len(b)
    while i + 12 <= n:
        ln = struct.unpack_from(">I", b, i)[0]
        typ = b[i + 4:i + 8]
        if ln > 0x7FFFFFFF:
            return None
        i += 12 + ln
        if typ == b"IEND":
            return i - p if i <= n else None
    return None


def _c_jpeg(b, p):
    n = len(b)
    i = p + 2
    while i + 4 <= n:
        if b[i] != 0xFF:
            return None
        m = b[i + 1]
        if m == 0xFF:          # Füllbyte
            i += 1
            continue
        if m == 0xD9:
            return i + 2 - p
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            i += 2
            continue
        ln = struct.unpack_from(">H", b, i + 2)[0]
        if ln < 2:
            return None
        i += 2 + ln
        if m == 0xDA:          # Scan-Daten bis zum nächsten echten Marker
            while True:
                j = b.find(b"\xff", i)
                if j < 0 or j + 1 >= n:
                    return None
                nx = b[j + 1]
                if nx == 0x00 or 0xD0 <= nx <= 0xD7 or nx == 0xFF:
                    i = j + 1 if nx == 0xFF else j + 2
                    continue
                i = j
                break
    return None


def _c_gif(b, p):
    n = len(b)
    if p + 13 > n:
        return None
    flags = b[p + 10]
    i = p + 13
    if flags & 0x80:
        i += 3 * (2 ** ((flags & 7) + 1))

    def subblocks(i):
        while i < n:
            ln = b[i]
            i += 1
            if ln == 0:
                return i
            i += ln
        return None

    while i < n:
        t = b[i]
        if t == 0x3B:
            return i + 1 - p
        if t == 0x21:
            i = subblocks(i + 2)
        elif t == 0x2C:
            if i + 10 > n:
                return None
            lf = b[i + 9]
            i += 10
            if lf & 0x80:
                i += 3 * (2 ** ((lf & 7) + 1))
            i = subblocks(i + 1)
        else:
            return None
        if i is None:
            return None
    return None


def _c_bmp(b, p):
    return _u32(b, p + 2)


def _c_riff(b, p):
    ln = _u32(b, p + 4) + 8
    return ln + (ln & 1)


def _c_ogg(b, p):
    n = len(b)
    i = p
    serial = None
    while i + 27 <= n and b[i:i + 4] == b"OggS":
        htype = b[i + 5]
        ser = _u32(b, i + 14)
        if serial is None:
            serial = ser
        nseg = b[i + 26]
        if i + 27 + nseg > n:
            return None
        body = sum(b[i + 27:i + 27 + nseg])
        i += 27 + nseg + body
        if htype & 4 and ser == serial:
            return i - p
    return i - p if i > p else None


def _c_7z(b, p):
    nofs, nsize = struct.unpack_from("<QQ", b, p + 12)
    return 32 + nofs + nsize


def _c_zip(b, p):
    e = b.find(b"PK\x05\x06", p)
    if e < 0 or e + 22 > len(b):
        return None
    return e + 22 + _u16(b, e + 20) - p


def _c_fsb5(b, p):
    ver, nsamp, shdr, names, data = struct.unpack_from("<IIIII", b, p + 4)
    hsize = 64 if ver == 0 else 60
    return hsize + shdr + names + data


def _c_unity(b, p):
    # UnityFS: Signatur, Version, zwei Strings, int64 Gesamtgröße (BE)
    end = b.find(b"\0", p + 12, p + 200)
    end = b.find(b"\0", end + 1, p + 300) if end >= 0 else -1
    if end < 0:
        return None
    return struct.unpack_from(">q", b, end + 1)[0]


def _c_wad(b, p):
    n, ofs = struct.unpack_from("<iI", b, p + 4)
    return ofs + n * 16


SIGNATURES: list[Signature] = [
    # Bilder
    Signature("PNG-Bild", "Bild", ".png", b"\x89PNG\r\n\x1a\n", carve=_c_png, embedded=True),
    Signature("JPEG-Bild", "Bild", ".jpg", b"\xff\xd8\xff", validate=_v_jpeg, carve=_c_jpeg, embedded=True),
    Signature("GIF-Bild", "Bild", ".gif", b"GIF89a", carve=_c_gif, embedded=True),
    Signature("GIF-Bild", "Bild", ".gif", b"GIF87a", carve=_c_gif, embedded=True),
    Signature("BMP-Bild", "Bild", ".bmp", b"BM", validate=_v_bmp, carve=_c_bmp, embedded=True),
    Signature("WebP-Bild", "Bild", ".webp", b"RIFF", validate=_v_riff(b"WEBP"), carve=_c_riff, embedded=True),
    Signature("DirectDraw-Textur (DDS)", "Bild", ".dds", b"DDS ", validate=_v_dds, embedded=True),
    Signature("KTX-Textur", "Bild", ".ktx", b"\xabKTX 11\xbb"),
    Signature("KTX2-Textur", "Bild", ".ktx2", b"\xabKTX 20\xbb"),
    Signature("TIFF-Bild", "Bild", ".tif", b"II*\0", validate=_v_tiff),
    Signature("TIFF-Bild", "Bild", ".tif", b"MM\0*"),
    Signature("Photoshop-Datei", "Bild", ".psd", b"8BPS"),
    Signature("Icon (ICO)", "Bild", ".ico", b"\0\0\1\0", validate=_v_ico),
    Signature("QOI-Bild", "Bild", ".qoi", b"qoif"),
    # Audio / Video
    Signature("WAVE-Audio", "Audio", ".wav", b"RIFF", validate=_v_riff(b"WAVE"), carve=_c_riff, embedded=True),
    Signature("Ogg-Container (Vorbis/Opus)", "Audio", ".ogg", b"OggS", validate=_v_ogg, carve=_c_ogg, embedded=True),
    Signature("FLAC-Audio", "Audio", ".flac", b"fLaC"),
    Signature("MP3-Audio (ID3)", "Audio", ".mp3", b"ID3"),
    Signature("MP3-Audio", "Audio", ".mp3", b"\xff", validate=_v_mp3),
    Signature("MIDI", "Audio", ".mid", b"MThd"),
    Signature("FMOD-Soundbank (FSB5)", "Audio", ".fsb", b"FSB5", validate=_v_fsb5, carve=_c_fsb5, embedded=True),
    Signature("Wwise-Soundbank", "Audio", ".bnk", b"BKHD"),
    Signature("AVI-Video", "Video", ".avi", b"RIFF", validate=_v_riff(b"AVI "), carve=_c_riff),
    Signature("Bink-Video", "Video", ".bik", b"BIK"),
    Signature("Bink2-Video", "Video", ".bk2", b"KB2"),
    Signature("Matroska/WebM-Video", "Video", ".mkv", b"\x1a\x45\xdf\xa3"),
    Signature("MP4/QuickTime", "Video", ".mp4", b"ftyp", offset=4),
    # Archive / Kompression
    Signature("ZIP-Archiv", "Archiv", ".zip", b"PK\x03\x04", validate=_v_zip, carve=_c_zip, embedded=True),
    Signature("ZIP-Archiv (leer)", "Archiv", ".zip", b"PK\x05\x06"),
    Signature("RAR-Archiv", "Archiv", ".rar", b"Rar!\x1a\x07"),
    Signature("7-Zip-Archiv", "Archiv", ".7z", b"7z\xbc\xaf\x27\x1c", validate=_v_7z, carve=_c_7z, embedded=True),
    Signature("Cabinet-Archiv (CAB)", "Archiv", ".cab", b"MSCF"),
    Signature("gzip-komprimiert", "Komprimiert", ".gz", b"\x1f\x8b", validate=_v_gzip),
    Signature("bzip2-komprimiert", "Komprimiert", ".bz2", b"BZh"),
    Signature("xz-komprimiert", "Komprimiert", ".xz", b"\xfd7zXZ\0"),
    Signature("Zstandard-komprimiert", "Komprimiert", ".zst", b"\x28\xb5\x2f\xfd"),
    Signature("LZ4-Frame", "Komprimiert", ".lz4", b"\x04\x22\x4d\x18"),
    # Spiel-Archive
    Signature("Unity-Asset-Bundle (UnityFS)", "Spiel-Archiv", ".unity3d", b"UnityFS\0", validate=_v_unity, carve=_c_unity, embedded=True),
    Signature("Unity-Web-Bundle", "Spiel-Archiv", ".unity3d", b"UnityWeb\0"),
    Signature("Unity-Raw-Bundle", "Spiel-Archiv", ".unity3d", b"UnityRaw\0"),
    Signature("Doom-WAD (IWAD)", "Spiel-Archiv", ".wad", b"IWAD", validate=_v_wad, carve=_c_wad),
    Signature("Doom-WAD (PWAD)", "Spiel-Archiv", ".wad", b"PWAD", validate=_v_wad, carve=_c_wad),
    Signature("Quake-WAD2", "Spiel-Archiv", ".wad", b"WAD2", validate=_v_wad),
    Signature("Half-Life-WAD3", "Spiel-Archiv", ".wad", b"WAD3", validate=_v_wad),
    Signature("Quake-PAK", "Spiel-Archiv", ".pak", b"PACK"),
    Signature("Valve-VPK", "Spiel-Archiv", ".vpk", b"\x34\x12\xaa\x55"),
    Signature("Godot-PCK", "Spiel-Archiv", ".pck", b"GDPC"),
    # Programme / Code
    Signature("Windows-Programm/Bibliothek (PE)", "Ausführbar", ".exe", b"MZ", validate=_v_pe),
    Signature("ELF-Programm (Linux)", "Ausführbar", ".elf", b"\x7fELF"),
    Signature("Java-Klasse", "Ausführbar", ".class", b"\xca\xfe\xba\xbe"),
    Signature("Lua-Bytecode", "Ausführbar", ".luac", b"\x1bLua"),
    Signature("WebAssembly", "Ausführbar", ".wasm", b"\0asm"),
    # Dokumente / Daten
    Signature("PDF-Dokument", "Dokument", ".pdf", b"%PDF-"),
    Signature("OLE2-Verbunddatei (alt. Office/MSI)", "Dokument", ".doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    Signature("RTF-Dokument", "Dokument", ".rtf", b"{\\rtf"),
    Signature("SQLite-Datenbank", "Datenbank", ".sqlite", b"SQLite format 3\0"),
    Signature("Windows-Registry-Hive", "Datenbank", ".dat", b"regf"),
    Signature("Windows-Verknüpfung (LNK)", "Dokument", ".lnk", b"L\0\0\0\x01\x14\x02\0"),
    Signature("TrueType-Schrift", "Schrift", ".ttf", b"\0\1\0\0\0"),
    Signature("OpenType-Schrift", "Schrift", ".otf", b"OTTO"),
    Signature("WOFF-Schrift", "Schrift", ".woff", b"wOFF"),
    Signature("WOFF2-Schrift", "Schrift", ".woff2", b"wOF2"),
    Signature("ISO-Abbild", "Archiv", ".iso", b"CD001", offset=0x8001),
    Signature("TAR-Archiv", "Archiv", ".tar", b"ustar", offset=257),
]

EMBEDDED_SIGNATURES = [s for s in SIGNATURES if s.embedded]

# Grobe Zuordnung von Endungen zu Kategorien (für Listen in Archiven)
EXT_CATEGORY = {}
for _s in SIGNATURES:
    EXT_CATEGORY.setdefault(_s.ext, _s.category)
EXT_CATEGORY.update({
    ".jpeg": "Bild", ".tga": "Bild", ".pcx": "Bild", ".lmp": "Spieldaten", ".mus": "Audio",
    ".opus": "Audio", ".wem": "Audio", ".txt": "Text", ".json": "Text", ".xml": "Text",
    ".ini": "Text", ".cfg": "Text", ".lua": "Text", ".shader": "Text", ".vmt": "Text",
    ".vtf": "Bild", ".mdl": "Modell", ".bsp": "Karte", ".uasset": "Spieldaten",
    ".uexp": "Spieldaten", ".ubulk": "Spieldaten", ".umap": "Karte", ".tex": "Bild",
    ".ress": "Spieldaten", ".resource": "Audio", ".dll": "Ausführbar", ".exe": "Ausführbar",
})


def category_for_name(name: str) -> str:
    return EXT_CATEGORY.get(os.path.splitext(name)[1].lower(), "")


# --------------------------------------------------------------------------- #
# Erkennung
# --------------------------------------------------------------------------- #

HEAD_SIZE = 0x10000


@dataclass
class Match:
    name: str
    category: str
    ext: str
    confidence: str          # "sicher", "wahrscheinlich", "möglich"
    detail: str = ""


def match_signatures(buf: bytes, at: int = 0) -> list[Signature]:
    """Alle Signaturen, die an Position `at` (bzw. deren Offset) passen."""
    hits = []
    for s in SIGNATURES:
        p = at + s.offset
        if buf[p:p + len(s.magic)] != s.magic:
            continue
        if s.validate is not None:
            try:
                if not s.validate(buf[at:at + 4096] if s.offset == 0 else buf[at:]):
                    continue
            except (struct.error, IndexError):
                continue
        hits.append(s)
    return hits


def _pe_detail(b: bytes) -> str:
    try:
        lf = _u32(b, 0x3C)
        machine = _u16(b, lf + 4)
        chars = _u16(b, lf + 22)
        magic = _u16(b, lf + 24)
        bits = {0x10B: "32-Bit", 0x20B: "64-Bit"}.get(magic, "?")
        arch = {0x14C: "x86", 0x8664: "x64", 0xAA64: "ARM64"}.get(machine, hex(machine))
        kind = "DLL" if chars & 0x2000 else "EXE"
        sub = _u16(b, lf + 24 + 68)
        subs = {2: "Fenster-Anwendung", 3: "Konsolen-Anwendung", 1: "Treiber/nativ"}.get(sub, "")
        net = ""
        # Datenverzeichnis 14 (CLR) -> .NET
        dd = lf + 24 + (96 if magic == 0x10B else 112)
        if dd + 15 * 8 <= len(b) and _u32(b, dd + 14 * 8) != 0:
            net = ", .NET"
        return f"{kind}, {bits}, {arch}{net}" + (f", {subs}" if subs else "")
    except struct.error:
        return ""


def _text_kind(head: bytes, size: int, path: str | None) -> Match | None:
    sample = head[:8192]
    if not sample:
        return None
    enc = "UTF-8"
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:]
    elif sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = sample.decode("utf-16")
            return Match("Text (UTF-16)", "Text", ".txt", "sicher", _text_flavor(text))
        except UnicodeDecodeError:
            return None
    elif len(sample) >= 64 and sample[1::2].count(0) > len(sample) * 0.4 and sample[0::2].count(0) < 4:
        try:
            text = sample.decode("utf-16-le")
            return Match("Text (UTF-16 LE ohne BOM)", "Text", ".txt", "wahrscheinlich", _text_flavor(text))
        except UnicodeDecodeError:
            pass
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start < len(sample) - 4:      # Fehler nicht nur am abgeschnittenen Ende
            try:
                text = sample.decode("cp1252")
                enc = "Windows-1252"
            except UnicodeDecodeError:
                return None
        else:
            text = sample[:e.start].decode("utf-8")
    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    if not text or printable / len(text) < 0.97:
        return None
    flavor = _text_flavor(text)
    if flavor == "JSON" and size <= 20_000_000 and path:
        try:
            with open(path, "rb") as f:
                json.loads(f.read().decode("utf-8-sig"))
            flavor = "JSON (gültig)"
        except (ValueError, UnicodeDecodeError):
            flavor = "JSON-ähnlich (nicht vollständig gültig)"
    ext = {"JSON": ".json", "XML": ".xml", "INI/Config": ".ini", "HTML": ".html", "CSV": ".csv"}.get(
        flavor.split(" ")[0], ".txt")
    return Match(f"Text ({enc})", "Text", ext, "wahrscheinlich", flavor)


def _text_flavor(text: str) -> str:
    t = text.lstrip()
    low = t[:200].lower()
    is_ini = bool(re.search(r"^\s*\[[^\]\n]+\]\s*$", t[:4000], re.M) and
                  re.search(r"^\s*[\w.\- ]+\s*=", t[:4000], re.M))
    if t[:1] == "{" or (t[:1] == "[" and not is_ini):
        return "JSON"
    if low.startswith("<?xml") or (t.startswith("<") and "</" in t[:4000] and "<html" not in low):
        return "XML"
    if "<html" in low or "<!doctype html" in low:
        return "HTML"
    if is_ini:
        return "INI/Config"
    lines = [ln for ln in t[:4000].splitlines() if ln.strip()][:10]
    if len(lines) >= 3 and len({ln.count(";") for ln in lines}) == 1 and lines[0].count(";") >= 2:
        return "CSV (Semikolon)"
    if len(lines) >= 3 and len({ln.count(",") for ln in lines}) == 1 and lines[0].count(",") >= 2:
        return "CSV"
    if re.search(r"^\s*[\w.\-]+\s*[=:]\s*\S", t[:4000], re.M):
        return "Schlüssel/Wert-Text (Config)"
    return "Klartext"


def _detect_special(f, size: int, head: bytes) -> Match | None:
    """Formate, die man nicht am Anfang erkennt (z. B. Unreal-PAK am Dateiende)."""
    if size >= 44:
        f.seek(max(0, size - 300))
        tail = f.read()
        i = tail.rfind(struct.pack("<I", 0x5A6F12E1))
        if i >= 0 and i + 12 <= len(tail):
            ver = _u32(tail, i + 4)
            if 1 <= ver <= 20:
                return Match("Unreal-Engine-PAK", "Spiel-Archiv", ".pak", "sicher", f"Pak-Version {ver}")
    # Unity SerializedFile (.assets / CAB-…): Header mit Metadaten-Größe, Dateigröße, Version
    if size >= 48:
        try:
            msize, fsize, ver, dofs = struct.unpack_from(">IIII", head, 0)
            if 9 <= ver <= 30 and (fsize == size or ver >= 22) and 0 < msize < size:
                if ver >= 22:
                    msize, fsize, dofs = struct.unpack_from(">IQQ", head, 20)
                    if fsize != size:
                        raise ValueError
                return Match("Unity-SerializedFile (.assets)", "Spiel-Archiv", ".assets", "wahrscheinlich",
                             f"Format-Version {ver}")
        except (struct.error, ValueError):
            pass
    return None


# --------------------------------------------------------------------------- #
# Entropie
# --------------------------------------------------------------------------- #

def entropy_of_counts(counts: np.ndarray) -> float:
    n = counts.sum()
    if n == 0:
        return 0.0
    p = counts[counts > 0] / n
    return float(-(p * np.log2(p)).sum())


def byte_histogram(mm, size: int, chunk: int = 16 << 20) -> np.ndarray:
    counts = np.zeros(256, dtype=np.int64)
    for start in range(0, size, chunk):
        arr = np.frombuffer(mm, dtype=np.uint8, count=min(chunk, size - start), offset=start)
        counts += np.bincount(arr, minlength=256)
    return counts


def block_entropy(mm, size: int, blocks: int = 400) -> tuple[int, list[float]]:
    if size == 0:
        return 0, []
    bs = max(256, -(-size // blocks))
    out = []
    for start in range(0, size, bs):
        arr = np.frombuffer(mm, dtype=np.uint8, count=min(bs, size - start), offset=start)
        out.append(entropy_of_counts(np.bincount(arr, minlength=256)))
    return bs, out


def chi_square(counts: np.ndarray) -> float:
    n = counts.sum()
    if n == 0:
        return 0.0
    e = n / 256
    return float(((counts - e) ** 2 / e).sum())


# --------------------------------------------------------------------------- #
# Eingebettete Dateien suchen und ausschneiden
# --------------------------------------------------------------------------- #

@dataclass
class Embedded:
    offset: int
    name: str
    category: str
    ext: str
    length: int | None      # None = Ende unbekannt


def find_embedded(buf, max_hits: int = 20000, skip_offset0: bool = True,
                  sigs: list[Signature] | None = None, cancel=None) -> list[Embedded]:
    """Sucht starke Signaturen an beliebigen Stellen in `buf` (bytes oder mmap)."""
    size = len(buf)
    found: dict[int, Embedded] = {}
    for s in sigs or EMBEDDED_SIGNATURES:
        pos = buf.find(s.magic, 0)
        hits = 0
        while pos >= 0 and hits < max_hits:
            if cancel is not None and cancel():
                return sorted(found.values(), key=lambda e: e.offset)
            ok = not (skip_offset0 and pos == 0) and pos not in found
            if ok and s.validate is not None:
                try:
                    ok = bool(s.validate(buf[pos:pos + 4096]))
                except (struct.error, IndexError):
                    ok = False
            if ok:
                length = None
                if s.carve is not None:
                    try:
                        length = s.carve(buf, pos)
                    except (struct.error, IndexError, ValueError, OverflowError):
                        length = None
                    if length is not None and (length <= len(s.magic) or pos + length > size):
                        length = None
                # Formate ohne verlässliche Länge (oder mit kaputter) nur aufnehmen, wenn sie kein Fehlalarm sind
                if s.carve is not None and length is None:
                    ok = False
                if ok:
                    found[pos] = Embedded(pos, s.name, s.category, s.ext, length)
                    hits += 1
            pos = buf.find(s.magic, pos + 1)
    # Teile eines bereits gefundenen Objekts gleichen Typs (z. B. weitere ZIP-Einträge, weitere
    # Ogg-Seiten) sind keine eigenen eingebetteten Dateien. Die Datei selbst (Offset 0) zählt mit.
    spans: dict[str, tuple[int, int]] = {}
    if skip_offset0:
        for s in sigs or EMBEDDED_SIGNATURES:
            if buf[:len(s.magic)] == s.magic and s.carve is not None:
                try:
                    ln = s.carve(buf, 0)
                except (struct.error, IndexError, ValueError, OverflowError):
                    ln = None
                spans[s.name] = (0, ln if ln and ln <= size else size)
    result = []
    for e in sorted(found.values(), key=lambda e: e.offset):
        sp = spans.get(e.name)
        if sp and sp[0] < e.offset < sp[1]:
            continue
        result.append(e)
        if e.length:
            spans[e.name] = (e.offset, e.offset + e.length)
    return result


# --------------------------------------------------------------------------- #
# Hauptanalyse
# --------------------------------------------------------------------------- #

@dataclass
class Analysis:
    path: str
    size: int
    matches: list[Match] = field(default_factory=list)
    entropy: float = 0.0
    chi2: float = 0.0
    block_size: int = 0
    blocks: list[float] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    embedded: list[Embedded] = field(default_factory=list)
    strings: list[tuple[int, str]] = field(default_factory=list)
    head: bytes = b""
    display_name: str = ""

    @property
    def best(self) -> Match | None:
        return self.matches[0] if self.matches else None


def entropy_verdict(h: float) -> str:
    if h < 1.0:
        return "sehr niedrig (fast nur gleiche Bytes, z. B. leer/aufgefüllt)"
    if h < 5.0:
        return "niedrig (Text oder einfach strukturierte Daten)"
    if h < 7.2:
        return "mittel (Programmcode, unkomprimierte Bilder/Sounds, gemischte Daten)"
    if h < 7.9:
        return "hoch (größtenteils komprimiert)"
    return "sehr hoch (komprimiert oder verschlüsselt)"


def extract_strings(buf, limit_bytes: int = 4 << 20, min_len: int = 5, max_count: int = 5000):
    data = bytes(buf[:limit_bytes])
    out = []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data):
        out.append((m.start(), m.group().decode("ascii")))
        if len(out) >= max_count:
            return out
    if len(out) < max_count:
        for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, data):
            out.append((m.start(), m.group().decode("utf-16-le") + "   [UTF-16]"))
            if len(out) >= max_count:
                break
        out.sort()
    return out


def _try_decompress(head: bytes) -> str | None:
    if len(head) >= 2 and head[0] == 0x78 and (head[0] << 8 | head[1]) % 31 == 0:
        try:
            out = zlib.decompressobj().decompress(head, 1 << 20)
            if out:
                return f"zlib-Datenstrom (lässt sich entpacken, Anfang ergibt {len(out)} Bytes)"
        except zlib.error:
            pass
    try:
        d = zlib.decompressobj(-15)
        out = d.decompress(head[:65536], 1 << 20)
        if len(out) > 2 * min(len(head), 65536) and len(out) > 4096:
            return "möglicherweise rohe Deflate-Daten (ohne Header)"
    except zlib.error:
        pass
    return None


def _xor_guess(head: bytes, counts: np.ndarray) -> str | None:
    """Einfaches Ein-Byte-XOR erkennen: Schlüssel probieren, ob eine bekannte Signatur entsteht."""
    if len(head) < 16:
        return None
    sample = np.frombuffer(head[:4096], dtype=np.uint8)
    for key in range(1, 256):
        dec = (sample ^ key).tobytes()
        for s in match_signatures(dec):
            if len(s.magic) >= 3 and s.category not in ("Text",):
                return f"Mit XOR-Schlüssel 0x{key:02X} entsteht am Dateianfang: {s.name} – die Datei ist vermutlich einfach XOR-verschlüsselt."
    top = int(counts.argmax())
    share = counts[top] / max(1, counts.sum())
    if top not in (0x00, 0x20, 0xFF) and share > 0.15:
        dec = (sample ^ top).tobytes()
        if _text_kind(dec, len(dec), None) and not _text_kind(head[:4096], 4096, None):
            return (f"Häufigstes Byte ist 0x{top:02X} ({share:.0%}); mit XOR 0x{top:02X} wird daraus lesbarer Text "
                    "– vermutlich einfach XOR-verschlüsselt.")
    return None


def analyze(path: str, find_embedded_files: bool = True, cancel=None, display_name: str | None = None) -> Analysis:
    size = os.path.getsize(path)
    a = Analysis(path, size)
    a.display_name = display_name or os.path.basename(path)
    if size == 0:
        a.hints.append("Die Datei ist leer.")
        return a
    with open(path, "rb") as f:
        head = f.read(HEAD_SIZE)
        a.head = head
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            _analyze(a, f, mm, head, find_embedded_files, cancel)
        finally:
            mm.close()
    return a


def _analyze(a: Analysis, f, mm, head: bytes, find_emb: bool, cancel):
    size = a.size
    # Signaturen (ISO/TAR stehen hinter dem Kopf -> Puffer reicht bis 0x8006)
    for s in match_signatures(head):
        detail = ""
        if s.ext == ".exe":
            detail = _pe_detail(head)
            if "DLL" in detail:
                s = Signature(s.name, s.category, ".dll", s.magic)
        elif s.ext == ".zip":
            detail = _zip_detail(f)
        elif s.name.startswith("Unity-Asset"):
            detail = _unity_detail(head)
        elif s.name == "MP3-Audio":
            # Ein einzelnes 0xFF-Byte ist schwach: nur wenn ein zweiter Frame passt
            if not _mp3_second_frame(head):
                continue
        conf = "sicher" if len(s.magic) >= 4 or s.validate else "wahrscheinlich"
        a.matches.append(Match(s.name, s.category, s.ext, conf, detail))
    special = _detect_special(f, size, head)
    if special:
        a.matches.append(special)
    if not a.matches:
        t = _text_kind(head, size, a.path)
        if t:
            a.matches.append(t)

    # Entropie
    counts = byte_histogram(mm, size)
    a.entropy = entropy_of_counts(counts)
    a.chi2 = chi_square(counts)
    a.block_size, a.blocks = block_entropy(mm, size)

    # Hinweise
    a.hints.append(f"Entropie {a.entropy:.3f}".replace(".", ",") + f" Bit/Byte: {entropy_verdict(a.entropy)}.")
    ext = os.path.splitext(a.display_name)[1].lower()
    best = a.best
    if best and ext and best.ext != ext and best.category not in ("Text",):
        same = {".jpg": {".jpeg", ".jpe"}, ".tif": {".tiff"}, ".exe": {".dll", ".sys", ".scr", ".ocx", ".cpl"},
                ".dll": {".exe", ".sys", ".pyd", ".ocx"}, ".zip": {".jar", ".apk", ".docx", ".xlsx", ".pptx",
                                                                    ".pk3", ".pk4", ".odt", ".epub", ".nupkg", ".whl", ".vsix", ".xpi"},
                ".mp4": {".m4a", ".mov", ".3gp"}, ".ogg": {".oga", ".opus", ".ogv"}, ".doc": {".xls", ".ppt", ".msi", ".msg"},
                ".unity3d": {".bundle", ".ab", ".assetbundle"}, ".pak": {".pk"}, ".wav": {".wem"}, ".mkv": {".webm"},
                ".ttf": {".ttc"}, ".gz": {".tgz"}, ".dat": set(), ".assets": {".resource", ""}}
        if ext not in same.get(best.ext, set()):
            a.hints.append(f"Die Endung „{ext}“ passt nicht zum erkannten Inhalt ({best.name}, üblich: {best.ext}).")
    if not a.matches:
        if size >= 1024 and a.entropy > 7.9:
            uniform = a.chi2 < 256 + 4 * math.sqrt(2 * 255) * max(1.0, 1.0)
            if uniform:
                a.hints.append("Keine bekannte Signatur, Bytes sind praktisch gleichverteilt (Chi² "
                               f"{a.chi2:.0f}, Zufall ≈ 255): wahrscheinlich VERSCHLÜSSELT oder zufällig.")
            else:
                a.hints.append("Keine bekannte Signatur, sehr hohe Entropie, aber nicht völlig gleichverteilt (Chi² "
                               f"{a.chi2:.0f}): wahrscheinlich KOMPRIMIERT (eigenes Format oder roher Datenstrom).")
        elif a.entropy < 7.2:
            a.hints.append("Keine bekannte Signatur und mittlere/niedrige Entropie: vermutlich ein eigenes, "
                           "unverschlüsseltes Binärformat (z. B. Spielstand). Zeichenketten und Hex-Ansicht helfen weiter.")
        dz = _try_decompress(head)
        if dz:
            a.hints.append(dz)
        x = _xor_guess(head, counts)
        if x:
            a.hints.append(x)
    zero_share = counts[0] / size
    if zero_share > 0.5 and size > 4096:
        a.hints.append(f"{zero_share:.0%} der Bytes sind 0x00 (viel Auffüllung oder dünn besetzte Daten).")
    if a.blocks and len(a.blocks) > 8:
        hi = sum(1 for b in a.blocks if b > 7.5)
        if 0.1 < hi / len(a.blocks) < 0.9:
            a.hints.append(f"{hi} von {len(a.blocks)} Abschnitten haben hohe Entropie: die Datei mischt "
                           "komprimierte und unkomprimierte Teile (typisch für Container/Archive).")

    if find_emb:
        a.embedded = find_embedded(mm, cancel=cancel)
        if a.embedded:
            kinds = {}
            for e in a.embedded:
                kinds[e.name] = kinds.get(e.name, 0) + 1
            a.hints.append("Eingebettet gefunden: " + ", ".join(f"{n}× {k}" for k, n in kinds.items()) + ".")
    a.strings = extract_strings(mm)


def _mp3_second_frame(b: bytes) -> bool:
    try:
        br_tab = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
        sr_tab = [44100, 48000, 32000]
        if (b[1] >> 3) & 3 != 3 or (b[1] >> 1) & 3 != 1:
            return False
        br = br_tab[b[2] >> 4] * 1000
        sr = sr_tab[(b[2] >> 2) & 3]
        ln = 144 * br // sr + ((b[2] >> 1) & 1)
        return _v_mp3(b[ln:ln + 4])
    except (IndexError, ZeroDivisionError):
        return False


def _zip_detail(f) -> str:
    import zipfile
    try:
        f.seek(0)
        with zipfile.ZipFile(f) as z:
            names = z.namelist()
            enc = any(i.flag_bits & 1 for i in z.infolist())
        kind = ""
        low = {n.lower() for n in names[:5000]}
        if "[content_types].xml" in low:
            kind = "Office-Dokument (OOXML)"
        elif "meta-inf/manifest.mf" in low:
            kind = "Java-Archiv (JAR)"
        elif "androidmanifest.xml" in low:
            kind = "Android-App (APK)"
        elif "mimetype" in low:
            kind = "OpenDocument/EPUB"
        return f"{len(names)} Einträge" + (", verschlüsselt" if enc else "") + (f", {kind}" if kind else "")
    except Exception:  # noqa: BLE001 – kaputte ZIPs nur als Hinweis
        return "Zentralverzeichnis nicht lesbar (beschädigt oder abgeschnitten)"


def _unity_detail(head: bytes) -> str:
    try:
        ver = struct.unpack_from(">I", head, 8)[0]
        engine = head[12:300].split(b"\0", 2)[1].decode("ascii", "replace")
        return f"Bundle-Format {ver}, Unity {engine}"
    except (struct.error, IndexError):
        return ""


def carve_bytes(buf, e: Embedded) -> bytes:
    if e.length is None:
        return bytes(buf[e.offset:e.offset + (1 << 20)])
    return bytes(buf[e.offset:e.offset + e.length])
