"""Gemeinsame GUI-Bausteine."""

from __future__ import annotations

import io
import os
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

MONO = ("Consolas", 10)
APP_TEMP = os.path.join(tempfile.gettempdir(), "FileProbe")


def fmt_size(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1024
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}".replace(".", ",")
    return str(n)


def hexdump(data: bytes, base: int = 0) -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(f"{b:02X}" for b in chunk)
        hx = hx[:23] + " " + hx[23:] if len(chunk) > 8 else hx
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:08X}  {hx:<49}  {asc}")
    return "\n".join(lines)


class Background:
    """Führt eine Funktion im Hintergrund aus und liefert das Ergebnis im GUI-Thread."""

    def __init__(self, widget: tk.Misc):
        self.widget = widget
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.cancelled = threading.Event()

    def run(self, func, on_done, on_error=None, on_progress=None):
        if self.busy:
            return False
        self.busy = True
        self.cancelled.clear()

        def progress(*a):
            self.q.put(("progress", a))

        def worker():
            try:
                res = func(progress)
                self.q.put(("done", res))
            except Exception as e:  # noqa: BLE001 – Fehler gehen an die GUI
                self.q.put(("error", e))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                while True:
                    kind, val = self.q.get_nowait()
                    if kind == "progress":
                        if on_progress:
                            on_progress(*val)
                        continue
                    self.busy = False
                    if kind == "done":
                        on_done(val)
                    elif on_error:
                        on_error(val)
                    return
            except queue.Empty:
                pass
            self.widget.after(80, poll)

        self.widget.after(80, poll)
        return True

    def cancel(self):
        self.cancelled.set()

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set()


class ReadOnlyText(ttk.Frame):
    def __init__(self, master, mono=False, wrap="word", height=10, **kw):
        super().__init__(master)
        self.text = tk.Text(self, wrap=wrap, height=height, font=MONO if mono else ("Segoe UI", 10),
                            relief="flat", borderwidth=4, **kw)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set, state="disabled")
        if wrap == "none":
            sbx = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
            self.text.configure(xscrollcommand=sbx.set)
            sbx.grid(row=1, column=0, sticky="ew")
        self.text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.text.tag_configure("h", font=("Segoe UI", 11, "bold"))
        self.text.tag_configure("b", font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("warn", foreground="#b45309")
        self.text.tag_configure("muted", foreground="#6b7280")

    def set(self, content: str = "", parts: list[tuple[str, str]] | None = None):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if parts:
            for txt, tag in parts:
                self.text.insert("end", txt, tag)
        else:
            self.text.insert("1.0", content)
        self.text.configure(state="disabled")


def make_tree(master, columns: list[tuple[str, str, int]], height=15, show_tree=False) -> ttk.Treeview:
    """columns: (id, Überschrift, Breite). Klick auf Überschrift sortiert."""
    frame = ttk.Frame(master)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="tree headings" if show_tree else "headings",
                        height=height, selectmode="extended")
    for cid, title, width in columns:
        tree.heading(cid, text=title, command=lambda c=cid: sort_tree(tree, c))
        tree.column(cid, width=width, minwidth=40, stretch=cid == columns[-1][0] or width > 250)
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    sbx = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=sb.set, xscrollcommand=sbx.set)
    tree.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")
    sbx.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    tree.frame = frame
    tree.sort_keys = {}
    return tree


def sort_tree(tree: ttk.Treeview, col: str):
    rev = tree.sort_keys.get(col, False)
    keyf = getattr(tree, "sort_key_funcs", {}).get(col)
    items = [(tree.set(i, col), i) for i in tree.get_children("")]

    def k(v):
        if keyf:
            return keyf(v[1])
        s = v[0]
        try:
            return (0, float(s.replace(".", "").replace(",", ".").split()[0]))
        except (ValueError, IndexError):
            return (1, s.lower())

    items.sort(key=k, reverse=rev)
    for idx, (_, i) in enumerate(items):
        tree.move(i, "", idx)
    tree.sort_keys[col] = not rev


class ImagePreview(ttk.Label):
    def __init__(self, master, max_size=(420, 320)):
        super().__init__(master, anchor="center")
        self.max_size = max_size
        self._photo = None

    def show(self, data: bytes) -> str | None:
        """Zeigt ein Bild; gibt eine Beschreibung zurück oder None, wenn es kein Bild ist."""
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:  # noqa: BLE001 – kein Bild
            self.clear()
            return None
        desc = f"{img.format or '?'} {img.width}×{img.height} {img.mode}"
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        mw, mh = self.max_size
        scale = min(mw / img.width, mh / img.height)
        if scale >= 2:
            scale = min(int(scale), 24)
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        elif scale < 1:
            img = img.copy()
            img.thumbnail((mw, mh))
        self._photo = ImageTk.PhotoImage(img)
        self.configure(image=self._photo)
        return desc

    def clear(self):
        self._photo = None
        self.configure(image="")


class EntropyChart(tk.Canvas):
    def __init__(self, master, **kw):
        super().__init__(master, background="white", highlightthickness=0, height=220, **kw)
        self.blocks: list[float] = []
        self.block_size = 0
        self.markers: list[tuple[int, str]] = []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self.delete("hover"))

    def set(self, blocks, block_size, markers=None):
        self.blocks = blocks
        self.block_size = block_size
        self.markers = markers or []
        self.redraw()

    @staticmethod
    def color(h):
        if h > 7.9:
            return "#dc2626"
        if h > 7.2:
            return "#f59e0b"
        if h > 5.0:
            return "#10b981"
        return "#3b82f6"

    def _geom(self):
        w, h = self.winfo_width(), self.winfo_height()
        return 46, 10, max(10, w - 56), max(10, h - 36)

    def redraw(self):
        self.delete("all")
        x0, y0, w, h = self._geom()
        for v in (0, 2, 4, 6, 8):
            y = y0 + h - h * v / 8
            self.create_line(x0, y, x0 + w, y, fill="#e5e7eb")
            self.create_text(x0 - 6, y, text=str(v), anchor="e", fill="#6b7280", font=("Segoe UI", 8))
        self.create_text(8, y0 + h / 2, text="Bit/Byte", angle=90, fill="#6b7280", font=("Segoe UI", 8))
        if not self.blocks:
            self.create_text(x0 + w / 2, y0 + h / 2, text="Noch keine Datei analysiert", fill="#9ca3af")
            return
        n = len(self.blocks)
        bw = w / n
        for i, v in enumerate(self.blocks):
            x = x0 + i * bw
            self.create_rectangle(x, y0 + h - h * v / 8, x + max(bw, 1), y0 + h, fill=self.color(v), width=0)
        total = n * self.block_size
        for off, label in self.markers[:200]:
            x = x0 + w * off / max(1, total)
            self.create_line(x, y0, x, y0 + h, fill="#7c3aed", dash=(2, 2))
        self.create_text(x0, y0 + h + 14, text="0", anchor="w", fill="#6b7280", font=("Segoe UI", 8))
        self.create_text(x0 + w, y0 + h + 14, text=f"{fmt_size(total)}", anchor="e", fill="#6b7280",
                         font=("Segoe UI", 8))
        self.create_text(x0 + w / 2, y0 + h + 14, text="Position in der Datei  (violett = eingebettete Datei)",
                         fill="#6b7280", font=("Segoe UI", 8))

    def _hover(self, e):
        self.delete("hover")
        if not self.blocks:
            return
        x0, y0, w, h = self._geom()
        i = int((e.x - x0) / (w / len(self.blocks)))
        if 0 <= i < len(self.blocks):
            off = i * self.block_size
            txt = f"Offset 0x{off:X}: {self.blocks[i]:.2f} Bit/Byte"
            self.create_text(e.x + 10, max(e.y - 10, 12), text=txt, anchor="w", tags="hover",
                             font=("Segoe UI", 9, "bold"), fill="#111827")


def safe_temp_copy(data: bytes, name: str) -> str:
    """Legt eine Kopie zur Analyse im Temp-Ordner ab (wird nie ausgeführt)."""
    from ..archives import safe_relpath
    os.makedirs(APP_TEMP, exist_ok=True)
    base = os.path.basename(safe_relpath(name))
    path = os.path.join(APP_TEMP, base + ".probe")   # neutrale Endung: Kopie ist nie ausführbar
    with open(path, "wb") as f:
        f.write(data)
    return path
