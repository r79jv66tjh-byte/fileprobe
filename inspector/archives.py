"""Leser für Spiel-Archive. Nur lesen – das Archiv selbst wird nie verändert.

Unterstützt:
- ZIP / PK3 / PK4 (und alles ZIP-basierte)
- Doom-WAD (IWAD/PWAD) inkl. Umwandlung von Grafiken/Flats/Sounds
- Quake-WAD2 / Half-Life-WAD3 inkl. Texturen → PNG (WAD3)
- Quake-PAK ("PACK")
- Unreal-Engine-PAK (Version 1–9, unverschlüsselt, zlib/unkomprimiert)
- Unity-Asset-Bundles (UnityFS, LZ4/LZMA/unkomprimiert) – listet die enthaltenen Dateien
- Valve-VPK (v1/v2, inkl. _000.vpk-Teilarchive)
- Godot-PCK (Format 1 und 2, unverschlüsselt)
- Fallback für alles andere: Signatur-Suche nach eingebetteten Bildern/Sounds/Archiven
"""

from __future__ import annotations

import io
import lzma
import mmap
import os
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field

from . import converters, formats
from .lz4 import decompress_block as lz4_decompress


class ArchiveError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Quellen (Datei oder Bytes im Speicher, z. B. verschachtelte Archive)
# --------------------------------------------------------------------------- #

class Source:
    def __init__(self, path: str | None = None, data: bytes | None = None, label: str = ""):
        self.path = path
        self.label = label or (os.path.basename(path) if path else "Speicher")
        self._f = None
        self._mm = None
        if data is not None:
            self.buf = data
            self.size = len(data)
        else:
            self._f = open(path, "rb")
            self.size = os.fstat(self._f.fileno()).st_size
            if self.size:
                self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
                self.buf = self._mm
            else:
                self.buf = b""

    def read(self, offset: int, n: int) -> bytes:
        if offset < 0 or n < 0 or offset + n > self.size:
            raise ArchiveError(f"Lesen außerhalb der Datei (Offset {offset:#x}, {n} Bytes)")
        return bytes(self.buf[offset:offset + n])

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._f is not None:
            self._f.close()
            self._f = None


@dataclass
class Entry:
    name: str
    size: int
    offset: int = 0
    csize: int = 0
    kind: str = ""
    info: str = ""
    ref: object = field(default=None, repr=False)


class Reader:
    format_name = "?"

    def __init__(self, src: Source):
        self.src = src
        self.entries: list[Entry] = []
        self.notes: list[str] = []

    @staticmethod
    def probe(src: Source, head: bytes) -> bool:
        return False

    def read(self, e: Entry) -> bytes:
        return self.src.read(e.offset, e.size)

    def convert(self, e: Entry, data: bytes) -> tuple[str, bytes] | None:
        """Optional: (neuer Dateiname, Bytes) in einem gängigen Format."""
        return None

    def close(self):
        self.src.close()


def _kind(name: str) -> str:
    return formats.category_for_name(name)


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("latin-1")


# --------------------------------------------------------------------------- #
# ZIP
# --------------------------------------------------------------------------- #

class ZipReader(Reader):
    format_name = "ZIP-Archiv"

    @staticmethod
    def probe(src, head):
        return head[:4] in (b"PK\x03\x04", b"PK\x05\x06")

    def __init__(self, src):
        super().__init__(src)
        fobj = src._f if src._f is not None else io.BytesIO(src.buf)
        try:
            self.z = zipfile.ZipFile(fobj)
        except zipfile.BadZipFile as e:
            raise ArchiveError(f"ZIP beschädigt: {e}") from e
        for i in self.z.infolist():
            if i.is_dir():
                continue
            enc = i.flag_bits & 1
            method = {0: "gespeichert", 8: "Deflate", 12: "bzip2", 14: "LZMA"}.get(i.compress_type, f"Methode {i.compress_type}")
            self.entries.append(Entry(i.filename, i.file_size, i.header_offset, i.compress_size, _kind(i.filename),
                                      method + (", verschlüsselt" if enc else ""), i))

    def read(self, e):
        if e.ref.flag_bits & 1:
            raise ArchiveError("Eintrag ist passwortgeschützt")
        try:
            return self.z.read(e.ref)
        except (NotImplementedError, zipfile.BadZipFile, zlib.error) as ex:
            raise ArchiveError(str(ex)) from ex


# --------------------------------------------------------------------------- #
# Doom-WAD
# --------------------------------------------------------------------------- #

class DoomWadReader(Reader):
    format_name = "Doom-WAD"

    @staticmethod
    def probe(src, head):
        return head[:4] in (b"IWAD", b"PWAD")

    def __init__(self, src):
        super().__init__(src)
        hdr = src.read(0, 12)
        n, ofs = struct.unpack_from("<iI", hdr, 4)
        self.format_name = "Doom-WAD (" + hdr[:4].decode() + ")"
        if n < 0 or ofs + 16 * n > src.size:
            raise ArchiveError("Verzeichnis liegt außerhalb der Datei")
        d = src.read(ofs, 16 * n)
        section = ""
        self.palette = None
        counts: dict[str, int] = {}
        for i in range(n):
            pos, size = struct.unpack_from("<iI", d, 16 * i)
            name = _cstr(d[16 * i + 8:16 * i + 16]).upper()
            if name.endswith("_START") or name in ("S_START", "SS_START", "F_START", "FF_START", "P_START", "PP_START"):
                section = name[0]
                continue
            if name.endswith("_END"):
                section = ""
                continue
            if size == 0:
                continue      # Marker (Kartennamen usw.)
            if pos < 0 or pos + size > src.size:
                self.notes.append(f"{name}: liegt außerhalb der Datei, übersprungen")
                continue
            uniq = name
            if name in counts:
                counts[name] += 1
                uniq = f"{name}~{counts[name]}"
            else:
                counts[name] = 0
            kind, info = self._classify(name, section, size)
            prefix = {"S": "sprites/", "F": "flats/", "P": "patches/"}.get(section, "")
            self.entries.append(Entry(prefix + uniq, size, pos, kind=kind, info=info, ref=(name, section)))
            if name == "PLAYPAL" and size >= 768:
                self.palette = src.read(pos, 768)
        if self.palette is None:
            self.notes.append("Keine PLAYPAL-Palette in dieser WAD – Grafiken werden in Graustufen umgewandelt.")

    @staticmethod
    def _classify(name, section, size):
        if section == "F" and size == 4096:
            return "Bild", "Flat 64×64"
        if section in ("S", "P"):
            return "Bild", "Grafik (Patch)"
        if name.startswith("DS") or name.startswith("DP"):
            return "Audio", "Sound (DMX)" if name.startswith("DS") else "PC-Speaker-Sound"
        if name.startswith("D_") or name.startswith("MUS_"):
            return "Audio", "Musik (MUS/MIDI)"
        if name in ("PLAYPAL", "COLORMAP"):
            return "Spieldaten", "Palette" if name == "PLAYPAL" else "Farbtabelle"
        if name in ("THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SEGS", "SSECTORS", "NODES", "SECTORS",
                    "REJECT", "BLOCKMAP", "BEHAVIOR", "TEXTMAP"):
            return "Karte", "Kartendaten"
        if name in ("TEXTURE1", "TEXTURE2", "PNAMES"):
            return "Spieldaten", "Texturdefinition"
        if name in ("TITLEPIC", "HELP1", "HELP2", "CREDIT", "INTERPIC", "STBAR", "STARMS", "VICTORY2", "ENDPIC") \
                or name.startswith(("M_", "ST", "WI", "BRDR", "AMMNUM", "END")):
            return "Bild", "Grafik (Patch)"
        return "", ""

    def convert(self, e, data):
        name, section = e.ref
        base = e.name.replace("~", "_")
        if section == "F" and len(data) == 4096:
            return base + ".png", converters.doom_flat_to_png(data, self.palette)
        if converters.is_dmx_sound(data):
            return base + ".wav", converters.dmx_to_wav(data)
        if data[:4] == b"MUS\x1a":
            return base + ".mus", data
        if converters.is_doom_patch(data):
            try:
                return base + ".png", converters.doom_patch_to_png(data, self.palette)
            except (struct.error, IndexError, ValueError):
                return None
        return None


# --------------------------------------------------------------------------- #
# Quake-WAD2 / Half-Life-WAD3
# --------------------------------------------------------------------------- #

class Wad23Reader(Reader):
    format_name = "WAD2/WAD3"
    TYPES = {0x40: "Palette", 0x42: "Bild (qpic)", 0x43: "Textur (Miptex)", 0x44: "Textur (Miptex, Quake)",
             0x45: "Konsolenbild", 0x46: "Schrift"}

    @staticmethod
    def probe(src, head):
        return head[:4] in (b"WAD2", b"WAD3")

    def __init__(self, src):
        super().__init__(src)
        hdr = src.read(0, 12)
        self.version = hdr[:4].decode()
        self.format_name = "Half-Life-WAD3" if self.version == "WAD3" else "Quake-WAD2"
        n, ofs = struct.unpack_from("<iI", hdr, 4)
        if n < 0 or ofs + 32 * n > src.size:
            raise ArchiveError("Verzeichnis liegt außerhalb der Datei")
        d = src.read(ofs, 32 * n)
        for i in range(n):
            pos, dsize, size, typ, comp = struct.unpack_from("<iIIBB", d, 32 * i)
            name = _cstr(d[32 * i + 16:32 * i + 32])
            info = self.TYPES.get(typ, f"Typ {typ:#x}") + (", komprimiert" if comp else "")
            kind = "Bild" if typ in (0x42, 0x43, 0x44) else ""
            if pos + dsize > src.size:
                continue
            self.entries.append(Entry(name, dsize, pos, kind=kind, info=info, ref=typ))

    def convert(self, e, data):
        safe = e.name.replace("*", "#")
        try:
            if e.ref == 0x43 or (e.ref == 0x44 and self.version == "WAD3"):
                return safe + ".png", converters.miptex_to_png(data)
            if e.ref == 0x42 and self.version == "WAD3":
                return safe + ".png", converters.qpic_to_png(data)
        except (struct.error, ValueError):
            return None
        return None


# --------------------------------------------------------------------------- #
# Quake-PAK
# --------------------------------------------------------------------------- #

class QuakePakReader(Reader):
    format_name = "Quake-PAK"

    @staticmethod
    def probe(src, head):
        return head[:4] == b"PACK"

    def __init__(self, src):
        super().__init__(src)
        ofs, ln = struct.unpack_from("<iI", src.read(4, 8))
        if ofs < 12 or ofs + ln > src.size or ln % 64:
            raise ArchiveError("Verzeichnis ungültig")
        d = src.read(ofs, ln)
        for i in range(ln // 64):
            name = _cstr(d[64 * i:64 * i + 56])
            pos, size = struct.unpack_from("<iI", d, 64 * i + 56)
            if pos + size <= src.size:
                self.entries.append(Entry(name, size, pos, kind=_kind(name)))


# --------------------------------------------------------------------------- #
# Unreal-Engine-PAK
# --------------------------------------------------------------------------- #

UE_MAGIC = 0x5A6F12E1


class UnrealPakReader(Reader):
    format_name = "Unreal-Engine-PAK"

    @staticmethod
    def probe(src, head):
        tail = src.read(max(0, src.size - 300), min(300, src.size))
        return struct.pack("<I", UE_MAGIC) in tail

    def __init__(self, src):
        super().__init__(src)
        tstart = max(0, src.size - 300)
        tail = src.read(tstart, src.size - tstart)
        i = tail.rfind(struct.pack("<I", UE_MAGIC))
        # Mehrere Kandidaten (z. B. Magic in Hash) -> den mit plausibler Version nehmen
        while i >= 0:
            ver = struct.unpack_from("<I", tail, i + 4)[0]
            if 1 <= ver <= 20:
                break
            i = tail.rfind(struct.pack("<I", UE_MAGIC), 0, i)
        if i < 0:
            raise ArchiveError("Kein Unreal-PAK-Footer gefunden")
        self.version, idx_ofs, idx_size = struct.unpack_from("<IqQ", tail, i + 4)
        self.format_name = f"Unreal-Engine-PAK (Version {self.version})"
        if self.version >= 4 and i >= 1 and tail[i - 1] == 1:
            raise ArchiveError("Der Index dieses Unreal-PAK ist verschlüsselt (AES) – ohne Schlüssel nicht lesbar.")
        if self.version >= 10:
            raise ArchiveError(f"Pak-Version {self.version} (UE 4.26+/UE5) nutzt einen neuen Index-Aufbau, den "
                               "dieses Tool nur erkennt, aber nicht entpackt (dafür z. B. FModel oder repak).")
        if idx_ofs < 0 or idx_ofs + idx_size > src.size:
            raise ArchiveError("Index liegt außerhalb der Datei")
        idx = src.read(idx_ofs, idx_size)
        last_err = None
        for method_u8 in ((False, True) if self.version == 8 else (False,)):
            try:
                self.entries = []
                self._parse_index(idx, method_u8)
                return
            except (struct.error, ArchiveError, UnicodeDecodeError) as e:
                last_err = e
        raise ArchiveError(f"Index nicht lesbar: {last_err}")

    @staticmethod
    def _fstring(b, p):
        n = struct.unpack_from("<i", b, p)[0]
        p += 4
        if n == 0:
            return "", p
        if n < 0:
            s = b[p:p - 2 * n].decode("utf-16-le")
            return s.rstrip("\0"), p - 2 * n
        if n > 4096:
            raise ArchiveError("unplausible Stringlänge")
        return b[p:p + n].decode("utf-8", "replace").rstrip("\0"), p + n

    def _entry(self, b, p, method_u8):
        start = p
        offset, size, usize = struct.unpack_from("<qqq", b, p)
        p += 24
        if self.version < 8:
            comp = struct.unpack_from("<i", b, p)[0]
            p += 4
            comp = {0: 0, 1: 1, 2: 1}.get(comp & 0x0F, 99)   # 1 = zlib/gzip, 99 = Custom/Oodle
        else:
            if method_u8:
                comp = b[p]
                p += 1
            else:
                comp = struct.unpack_from("<I", b, p)[0]
                p += 4
            if comp > 16:
                raise ArchiveError("unplausible Kompressionsmethode")
        if self.version <= 1:
            p += 8
        p += 20
        blocks = []
        flags = 0
        bsize = 0
        if self.version >= 3:
            if comp != 0:
                nb = struct.unpack_from("<i", b, p)[0]
                p += 4
                if not 0 <= nb < 1_000_000:
                    raise ArchiveError("unplausible Blockanzahl")
                for _ in range(nb):
                    blocks.append(struct.unpack_from("<qq", b, p))
                    p += 16
            flags = b[p]
            bsize = struct.unpack_from("<I", b, p + 1)[0]
            p += 5
        return (offset, size, usize, comp, blocks, flags, bsize, p - start), p

    def _parse_index(self, idx, method_u8):
        mount, p = self._fstring(idx, 0)
        count = struct.unpack_from("<i", idx, p)[0]
        p += 4
        if not 0 <= count < 5_000_000:
            raise ArchiveError("unplausible Eintragsanzahl")
        self.mount = mount
        for _ in range(count):
            name, p = self._fstring(idx, p)
            ent, p = self._entry(idx, p, method_u8)
            offset, size, usize, comp, blocks, flags, bsize, hdrlen = ent
            if offset + size > self.src.size:
                raise ArchiveError(f"{name}: Daten außerhalb der Datei")
            info = {0: "unkomprimiert", 1: "zlib"}.get(comp, "Oodle/anderes (nicht entpackbar)" if self.version < 8
                                                       else f"Methode #{comp}")
            if flags & 1:
                info += ", verschlüsselt"
            self.entries.append(Entry(name, usize, offset, size, _kind(name), info, ent))

    def read(self, e):
        offset, size, usize, comp, blocks, flags, bsize, hdrlen = e.ref
        if flags & 1:
            raise ArchiveError("Eintrag ist verschlüsselt (AES)")
        if comp == 0:
            return self.src.read(offset + hdrlen, usize)
        out = bytearray()
        for bs, be in blocks:
            if self.version >= 5:      # relative Block-Offsets
                bs += offset
                be += offset
            chunk = self.src.read(bs, be - bs)
            try:
                out += zlib.decompress(chunk, 47)     # zlib oder gzip automatisch
            except zlib.error as ex:
                raise ArchiveError("Block ist nicht zlib-komprimiert (vermutlich Oodle) – nicht entpackbar") from ex
        if len(out) != usize:
            raise ArchiveError(f"Entpackte Größe {len(out)} statt {usize}")
        return bytes(out)


# --------------------------------------------------------------------------- #
# Unity UnityFS
# --------------------------------------------------------------------------- #

class UnityFSReader(Reader):
    format_name = "Unity-Asset-Bundle (UnityFS)"

    @staticmethod
    def probe(src, head):
        return head[:8] == b"UnityFS\0"

    def __init__(self, src):
        super().__init__(src)
        b = src.buf
        p = 8
        self.fmt = struct.unpack_from(">I", b, p)[0]
        p += 4

        def cstr(p):
            e = b.find(b"\0", p, p + 256)
            if e < 0:
                raise ArchiveError("Header beschädigt")
            return bytes(b[p:e]).decode("ascii", "replace"), e + 1

        self.player_ver, p = cstr(p)
        self.engine_ver, p = cstr(p)
        total, csize, usize, flags = struct.unpack_from(">qIII", b, p)
        p += 20
        self.format_name = f"Unity-Asset-Bundle (UnityFS {self.fmt}, Unity {self.engine_ver})"
        if self.fmt >= 7:
            p = (p + 15) & ~15
        if flags & 0x80:
            info_raw = src.read(src.size - csize, csize)
        else:
            info_raw = src.read(p, csize)
            p += csize
        info = self._decompress(info_raw, flags & 0x3F, usize)
        if flags & 0x200:
            p = (p + 15) & ~15
        q = 16
        nblocks = struct.unpack_from(">i", info, q)[0]
        q += 4
        self.blocks = []
        cofs = p
        uofs = 0
        for _ in range(nblocks):
            u, c, bf = struct.unpack_from(">IIH", info, q)
            q += 10
            self.blocks.append((uofs, u, cofs, c, bf & 0x3F))
            uofs += u
            cofs += c
        if cofs > src.size:
            raise ArchiveError("Datenblöcke reichen über das Dateiende hinaus (Datei abgeschnitten?)")
        nnodes = struct.unpack_from(">i", info, q)[0]
        q += 4
        for _ in range(nnodes):
            off, size, nf = struct.unpack_from(">qqI", info, q)
            q += 20
            e = info.find(b"\0", q)
            name = info[q:e].decode("utf-8", "replace")
            q = e + 1
            if name.lower().endswith(".ress"):
                kind, inf = "Spieldaten", "Textur-/Mesh-Rohdaten"
            elif name.lower().endswith(".resource"):
                kind, inf = "Audio", "Audio-Rohdaten (meist FMOD/FSB5)"
            elif nf & 4:
                kind, inf = "Spieldaten", "SerializedFile (Assets)"
            else:
                kind, inf = _kind(name), ""
            self.entries.append(Entry(name, size, off, kind=kind, info=inf))
        comps = {b[4] for b in self.blocks}
        names = {0: "unkomprimiert", 1: "LZMA", 2: "LZ4", 3: "LZ4HC"}
        self.notes.append("Blöcke: " + ", ".join(names.get(c, f"#{c}") for c in sorted(comps)) +
                          f" ({len(self.blocks)} Stück). Bilder/Sounds stecken in den enthaltenen Dateien – "
                          "„Eintrag durchsuchen“ findet eingebettete PNG/OGG/WAV/FSB5.")
        self._cache: dict[int, bytes] = {}

    @staticmethod
    def _decompress(data: bytes, comp: int, usize: int) -> bytes:
        if comp == 0:
            return data
        if comp in (2, 3):
            return lz4_decompress(data, usize)
        if comp == 1:
            props = data[0]
            dict_size = struct.unpack_from("<I", data, 1)[0]
            lc, rem = props % 9, props // 9
            lp, pb = rem % 5, rem // 5
            d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{
                "id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}])
            out = d.decompress(data[5:], usize)
            if len(out) != usize:
                raise ArchiveError("LZMA-Block unvollständig")
            return out
        raise ArchiveError(f"Unbekannte Kompression #{comp}")

    def _block(self, i):
        if i not in self._cache:
            if len(self._cache) > 64:
                self._cache.clear()
            uofs, u, cofs, c, comp = self.blocks[i]
            self._cache[i] = self._decompress(self.src.read(cofs, c), comp, u)
        return self._cache[i]

    def read(self, e):
        out = bytearray()
        start, end = e.offset, e.offset + e.size
        for i, (uofs, u, _c, _cs, _comp) in enumerate(self.blocks):
            if uofs + u <= start or uofs >= end:
                continue
            blk = self._block(i)
            out += blk[max(0, start - uofs):min(u, end - uofs)]
        if len(out) != e.size:
            raise ArchiveError("Daten unvollständig")
        return bytes(out)


# --------------------------------------------------------------------------- #
# Valve VPK
# --------------------------------------------------------------------------- #

class VpkReader(Reader):
    format_name = "Valve-VPK"

    @staticmethod
    def probe(src, head):
        return head[:4] == b"\x34\x12\xaa\x55"

    def __init__(self, src):
        super().__init__(src)
        sig, ver, tree = struct.unpack_from("<III", src.read(0, 12))
        if ver not in (1, 2):
            raise ArchiveError(f"VPK-Version {ver} nicht unterstützt")
        self.format_name = f"Valve-VPK (Version {ver})"
        hdr = 12 if ver == 1 else 28
        self.data_base = hdr + tree
        d = src.read(hdr, tree)
        p = 0

        def s():
            nonlocal p
            e = d.index(b"\0", p)
            r = d[p:e].decode("utf-8", "replace")
            p = e + 1
            return r

        self.missing = set()
        while True:
            ext = s()
            if not ext:
                break
            while True:
                path = s()
                if not path:
                    break
                while True:
                    fn = s()
                    if not fn:
                        break
                    crc, pre, arc, ofs, ln, term = struct.unpack_from("<IHHIIH", d, p)
                    p += 18
                    preload = d[p:p + pre]
                    p += pre
                    full = (f"{path}/" if path.strip() else "") + fn + (f".{ext}" if ext.strip() else "")
                    info = "im Verzeichnis-VPK" if arc == 0x7FFF else f"Teilarchiv {arc:03d}"
                    self.entries.append(Entry(full, pre + ln, ofs, kind=_kind(full), info=info, ref=(arc, preload, ln)))
        if src.path and not src.path.lower().endswith("_dir.vpk") and any(e.ref[0] != 0x7FFF for e in self.entries):
            self.notes.append("Achtung: Diese VPK verweist auf Teilarchive (_000.vpk …); "
                              "öffne die Datei mit der Endung _dir.vpk.")

    def read(self, e):
        arc, preload, ln = e.ref
        if ln == 0:
            return preload
        if arc == 0x7FFF:
            return preload + self.src.read(self.data_base + e.offset, ln)
        if not self.src.path:
            raise ArchiveError("Teilarchiv nicht erreichbar")
        part = re.sub(r"_dir\.vpk$", f"_{arc:03d}.vpk", self.src.path, flags=re.I)
        if part == self.src.path or not os.path.exists(part):
            raise ArchiveError(f"Teilarchiv {os.path.basename(part)} fehlt")
        with open(part, "rb") as f:
            f.seek(e.offset)
            data = f.read(ln)
        if len(data) != ln:
            raise ArchiveError("Teilarchiv zu kurz")
        return preload + data


# --------------------------------------------------------------------------- #
# Godot PCK
# --------------------------------------------------------------------------- #

class GodotPckReader(Reader):
    format_name = "Godot-PCK"

    @staticmethod
    def probe(src, head):
        return head[:4] == b"GDPC"

    def __init__(self, src):
        super().__init__(src)
        b = src.read(0, min(src.size, 200))
        fmt, maj, mi, pa = struct.unpack_from("<IIII", b, 4)
        self.format_name = f"Godot-PCK (Format {fmt}, Godot {maj}.{mi}.{pa})"
        p = 20
        base = 0
        if fmt == 2:
            flags, base = struct.unpack_from("<IQ", b, p)
            p += 12
            if flags & 1:
                raise ArchiveError("Das Verzeichnis dieses PCK ist verschlüsselt.")
        elif fmt != 1:
            raise ArchiveError(f"PCK-Format {fmt} wird nicht unterstützt")
        p += 64
        count = struct.unpack_from("<I", src.read(p, 4))[0]
        p += 4
        for _ in range(count):
            ln = struct.unpack_from("<I", src.read(p, 4))[0]
            if ln > 4096:
                raise ArchiveError("Verzeichnis beschädigt")
            name = src.read(p + 4, ln).rstrip(b"\0").decode("utf-8", "replace")
            p += 4 + ln
            ofs, size = struct.unpack_from("<QQ", src.read(p, 16))
            p += 32
            eflags = 0
            if fmt == 2:
                eflags = struct.unpack_from("<I", src.read(p, 4))[0]
                p += 4
            name = name.removeprefix("res://")
            info = "verschlüsselt" if eflags & 1 else ""
            self.entries.append(Entry(name, size, base + ofs, kind=_kind(name), info=info, ref=eflags))

    def read(self, e):
        if e.ref & 1:
            raise ArchiveError("Datei ist verschlüsselt")
        return super().read(e)


# --------------------------------------------------------------------------- #
# Fallback: Signatur-Suche
# --------------------------------------------------------------------------- #

class CarveReader(Reader):
    format_name = "Unbekannt – Signatur-Suche"

    def __init__(self, src, cancel=None):
        super().__init__(src)
        hits = formats.find_embedded(src.buf, skip_offset0=False, cancel=cancel)
        for h in hits:
            if h.length is None or (h.offset == 0 and h.length >= src.size):
                continue
            name = f"{h.offset:010X}{h.ext}"
            self.entries.append(Entry(name, h.length, h.offset, kind=h.category, info=h.name))
        self.notes.append("Kein bekanntes Archivformat: angezeigt werden Dateien, die anhand ihrer Signatur "
                          "in den Rohdaten gefunden wurden (PNG, JPEG, GIF, BMP, WAV, OGG, FSB5, ZIP, 7z …).")


READERS = [ZipReader, DoomWadReader, Wad23Reader, QuakePakReader, UnityFSReader, VpkReader, GodotPckReader,
           UnrealPakReader]


def open_archive(path: str | None = None, data: bytes | None = None, label: str = "",
                 force_carve: bool = False, cancel=None) -> Reader:
    src = Source(path, data, label)
    try:
        head = src.read(0, min(src.size, 64))
        if not force_carve:
            for cls in READERS:
                if cls.probe(src, head):
                    return cls(src)
        return CarveReader(src, cancel)
    except Exception:
        src.close()
        raise


# --------------------------------------------------------------------------- #
# Sicheres Entpacken
# --------------------------------------------------------------------------- #

_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_relpath(name: str) -> str:
    """Archiv-Pfad -> sicherer relativer Pfad (kein '..', kein Laufwerk, keine verbotenen Zeichen)."""
    parts = []
    for part in re.split(r"[\\/]+", name):
        part = _BAD.sub("_", part).strip().rstrip(".")
        if part in ("", ".", ".."):
            continue
        if part.split(".")[0].upper() in _RESERVED:
            part = "_" + part
        parts.append(part[:200])
    return os.path.join(*parts) if parts else "unbenannt"


def target_path(outdir: str, name: str) -> str:
    out = os.path.abspath(outdir)
    p = os.path.abspath(os.path.join(out, safe_relpath(name)))
    if os.path.commonpath([out, p]) != out:
        raise ArchiveError(f"Unsicherer Pfad: {name}")
    return p


def is_media(entry: Entry, data: bytes | None = None) -> bool:
    if entry.kind in ("Bild", "Audio"):
        return True
    if data is not None:
        return any(s.category in ("Bild", "Audio") for s in formats.match_signatures(data[:4096]))
    return False


def extract(reader: Reader, entries: list[Entry], outdir: str, convert: bool = True,
            only_media: bool = False, progress=None, cancel=None, dry_run: bool = False,
            plan: list | None = None) -> tuple[int, list[str]]:
    """Schreibt Einträge nach `outdir`. Gibt (Anzahl, Fehlerliste) zurück.

    dry_run=True schreibt nichts; `plan` (falls übergeben) erhält je Datei
    {"entry", "target", "size", "converted"} – auch beim echten Entpacken.
    """
    done = 0
    errors = []
    planned: set[str] = set()
    for i, e in enumerate(entries):
        if cancel is not None and cancel():
            break
        if progress:
            progress(i, len(entries), e.name)
        try:
            data = reader.read(e)
            name = e.name
            conv = reader.convert(e, data) if convert else None
            if conv:
                name, data = conv
            elif not os.path.splitext(name)[1]:
                sig = formats.match_signatures(data[:4096])
                if sig:
                    name += sig[0].ext
            if only_media and not (conv and name.lower().endswith((".png", ".wav"))) and not is_media(e, data):
                continue
            path = target_path(outdir, name)
            base, ext = os.path.splitext(path)
            k = 1
            while os.path.exists(path) or path.lower() in planned:
                path = f"{base}_{k}{ext}"
                k += 1
            planned.add(path.lower())
            if plan is not None:
                plan.append({"entry": e.name, "target": path, "size": len(data), "converted": bool(conv)})
            if not dry_run:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
            done += 1
        except Exception as ex:  # noqa: BLE001 – einzelner Eintrag darf das Entpacken nicht abbrechen
            errors.append(f"{e.name}: {ex}")
    return done, errors
