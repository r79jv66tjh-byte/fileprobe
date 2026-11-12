"""LZ4-Block-Dekompression in reinem Python (für Unity-Bundles)."""


class LZ4Error(ValueError):
    pass


def decompress_block(src: bytes, uncompressed_size: int | None = None) -> bytes:
    dst = bytearray()
    i = 0
    n = len(src)
    try:
        while i < n:
            token = src[i]
            i += 1
            lit = token >> 4
            if lit == 15:
                while True:
                    b = src[i]
                    i += 1
                    lit += b
                    if b != 255:
                        break
            if i + lit > n:
                raise LZ4Error("Literal läuft über das Blockende")
            dst += src[i:i + lit]
            i += lit
            if i >= n:
                break
            off = src[i] | (src[i + 1] << 8)
            i += 2
            if off == 0 or off > len(dst):
                raise LZ4Error("ungültiger Rückverweis")
            ml = token & 15
            if ml == 15:
                while True:
                    b = src[i]
                    i += 1
                    ml += b
                    if b != 255:
                        break
            ml += 4
            start = len(dst) - off
            if ml <= off:
                dst += dst[start:start + ml]
            else:
                pattern = bytes(dst[start:])
                dst += (pattern * (ml // off + 1))[:ml]
            if uncompressed_size is not None and len(dst) > uncompressed_size:
                raise LZ4Error("Ausgabe größer als erwartet")
    except IndexError as e:
        raise LZ4Error("Block abgeschnitten") from e
    if uncompressed_size is not None and len(dst) != uncompressed_size:
        raise LZ4Error(f"Größe {len(dst)} statt {uncompressed_size}")
    return bytes(dst)


def compress_block_simple(data: bytes) -> bytes:
    """Kleiner, gieriger LZ4-Kompressor (nur für Tests und Beispieldateien)."""
    out = bytearray()
    n = len(data)
    table: dict[bytes, int] = {}
    i = 0
    anchor = 0

    def emit(lit: bytes, off: int | None, ml: int):
        ll = len(lit)
        tok_l = min(ll, 15)
        tok_m = 0 if off is None else min(ml - 4, 15)
        out.append(tok_l << 4 | tok_m)
        if ll >= 15:
            r = ll - 15
            while r >= 255:
                out.append(255)
                r -= 255
            out.append(r)
        out.extend(lit)
        if off is None:
            return
        out.extend((off & 0xFF, off >> 8))
        if ml - 4 >= 15:
            r = ml - 4 - 15
            while r >= 255:
                out.append(255)
                r -= 255
            out.append(r)

    while i + 12 < n:          # LZ4 verlangt: letzte 5 Bytes sind Literale, letzter Match endet >=12 vor Ende
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is not None and i - cand < 65535 and data[cand:cand + 4] == key:
            ml = 4
            while i + ml < n - 5 and data[cand + ml] == data[i + ml]:
                ml += 1
            emit(data[anchor:i], i - cand, ml)
            i += ml
            anchor = i
        else:
            i += 1
    emit(data[anchor:], None, 0)
    return bytes(out)
