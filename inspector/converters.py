"""Umwandlung klassischer Spiel-Ressourcen in normale Formate (PNG/WAV).

- Doom: Grafiken im Patch-Format, Flats (64×64), DMX-Sounds
- Half-Life (WAD3): Texturen (Miptex) und qpic-Bilder
"""

from __future__ import annotations

import io
import struct
import wave

from PIL import Image

GRAY_PALETTE = bytes(v for i in range(256) for v in (i, i, i))


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ----------------------------- Doom --------------------------------------- #

def is_doom_patch(data: bytes) -> bool:
    if len(data) < 12:
        return False
    w, h, _lo, _to = struct.unpack_from("<hhhh", data, 0)
    if not (0 < w <= 4096 and 0 < h <= 4096) or len(data) < 8 + 4 * w:
        return False
    first = 8 + 4 * w
    for x in range(w):
        ofs = struct.unpack_from("<I", data, 8 + 4 * x)[0]
        if ofs < first or ofs >= len(data):
            return False
    return True


def doom_patch_to_png(data: bytes, palette: bytes | None) -> bytes:
    w, h, _lo, _to = struct.unpack_from("<hhhh", data, 0)
    pix = bytearray(w * h)
    alpha = bytearray(w * h)
    for x in range(w):
        p = struct.unpack_from("<I", data, 8 + 4 * x)[0]
        top = -1
        while p < len(data):
            delta = data[p]
            if delta == 0xFF:
                break
            # "Tall patches": topdelta <= vorheriger -> relativ
            top = delta if delta > top else top + delta
            ln = data[p + 1]
            src = data[p + 3:p + 3 + ln]
            for k, c in enumerate(src):
                y = top + k
                if 0 <= y < h:
                    pix[y * w + x] = c
                    alpha[y * w + x] = 255
            p += 4 + ln
    img = Image.frombytes("P", (w, h), bytes(pix))
    img.putpalette(palette or GRAY_PALETTE)
    rgba = img.convert("RGBA")
    rgba.putalpha(Image.frombytes("L", (w, h), bytes(alpha)))
    return _png(rgba)


def doom_flat_to_png(data: bytes, palette: bytes | None) -> bytes:
    img = Image.frombytes("P", (64, 64), data[:4096])
    img.putpalette(palette or GRAY_PALETTE)
    return _png(img.convert("RGB"))


def is_dmx_sound(data: bytes) -> bool:
    if len(data) < 8:
        return False
    fmt, rate, n = struct.unpack_from("<HHI", data, 0)
    return fmt == 3 and 4000 <= rate <= 48000 and 8 + n <= len(data) + 1 and n > 0


def dmx_to_wav(data: bytes) -> bytes:
    _fmt, rate, n = struct.unpack_from("<HHI", data, 0)
    samples = data[8:8 + n]
    if len(samples) > 32:      # 16 Füllbytes vorne und hinten
        samples = samples[16:-16]
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)      # 8 Bit, vorzeichenlos wie im Original
        w.setframerate(rate)
        w.writeframes(samples)
    return out.getvalue()


# ----------------------------- Half-Life WAD3 ------------------------------ #

def miptex_to_png(data: bytes) -> bytes:
    name = data[:16].split(b"\0", 1)[0].decode("ascii", "replace")
    w, h = struct.unpack_from("<II", data, 16)
    offs = struct.unpack_from("<4I", data, 24)
    if not (0 < w <= 8192 and 0 < h <= 8192) or offs[0] + w * h > len(data):
        raise ValueError("ungültige Miptex-Größe")
    pix = data[offs[0]:offs[0] + w * h]
    pal_ofs = offs[3] + (w // 8) * (h // 8) + 2
    palette = data[pal_ofs:pal_ofs + 768]
    if len(palette) < 768:
        palette = GRAY_PALETTE
    return _paletted_png(pix, w, h, palette, transparent_last=name.startswith("{"))


def qpic_to_png(data: bytes) -> bytes:
    w, h = struct.unpack_from("<II", data, 0)
    if not (0 < w <= 8192 and 0 < h <= 8192) or 8 + w * h > len(data):
        raise ValueError("ungültige qpic-Größe")
    pix = data[8:8 + w * h]
    pal_ofs = 8 + w * h + 2
    palette = data[pal_ofs:pal_ofs + 768]
    if len(palette) < 768:
        palette = GRAY_PALETTE
    return _paletted_png(pix, w, h, palette)


def _paletted_png(pix: bytes, w: int, h: int, palette: bytes, transparent_last: bool = False) -> bytes:
    img = Image.frombytes("P", (w, h), pix)
    img.putpalette(palette)
    if transparent_last:
        img.info["transparency"] = 255
        return _png(img.convert("RGBA"))
    return _png(img.convert("RGB"))
