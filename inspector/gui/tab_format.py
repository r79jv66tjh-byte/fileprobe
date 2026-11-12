"""Tab 1: Dateiformat-Erkenner."""

from __future__ import annotations

import mmap
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import formats
from .common import (Background, EntropyChart, ImagePreview, ReadOnlyText, fmt_size, hexdump, make_tree)

HEX_LIMIT = 64 * 1024


class FormatTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.bg = Background(self)
        self.analysis: formats.Analysis | None = None

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Datei:").pack(side="left")
        self.path_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.path_var)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self.start())
        ttk.Button(top, text="Datei wählen…", command=self.choose).pack(side="left")
        ttk.Button(top, text="Analysieren", command=self.start).pack(side="left", padx=(6, 0))
        self.emb_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Eingebettete Dateien suchen", variable=self.emb_var).pack(side="left", padx=8)
        ttk.Button(top, text="Im Archiv-Tab öffnen", command=self.to_archive).pack(side="left")

        self.status = ttk.Label(self, text="Wähle eine Datei – sie wird nur gelesen, nie ausgeführt.",
                                foreground="#6b7280")
        self.status.pack(fill="x", pady=(6, 4))

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)
        self.result = ReadOnlyText(pw, height=20)
        pw.add(self.result, weight=2)

        nb = ttk.Notebook(pw)
        pw.add(nb, weight=3)

        # Entropie
        f = ttk.Frame(nb, padding=6)
        self.chart = EntropyChart(f)
        self.chart.pack(fill="both", expand=True)
        legend = ttk.Frame(f)
        legend.pack(fill="x", pady=(4, 0))
        for col, txt in (("#3b82f6", "< 5 Text/einfache Daten"), ("#10b981", "5–7,2 Code/Rohdaten"),
                         ("#f59e0b", "7,2–7,9 komprimiert"), ("#dc2626", "> 7,9 komprimiert/verschlüsselt")):
            tk.Label(legend, text="  ", background=col).pack(side="left", padx=(8, 3))
            ttk.Label(legend, text=txt).pack(side="left")
        nb.add(f, text="Entropie-Verlauf")

        # Eingebettet
        f = ttk.Frame(nb, padding=6)
        self.emb = make_tree(f, [("ofs", "Offset", 110), ("typ", "Typ", 220), ("size", "Größe", 100)])
        self.emb.frame.pack(fill="both", expand=True)
        self.emb.bind("<<TreeviewSelect>>", lambda e: self.preview_embedded())
        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Ausgewählte speichern…", command=lambda: self.save_embedded(False)).pack(side="left")
        ttk.Button(row, text="Alle speichern…", command=lambda: self.save_embedded(True)).pack(side="left", padx=6)
        self.emb_preview = ImagePreview(f, (360, 200))
        self.emb_preview.pack(fill="x")
        nb.add(f, text="Eingebettete Dateien")

        # Hex
        f = ttk.Frame(nb, padding=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Ab Offset (hex):").pack(side="left")
        self.hex_ofs = tk.StringVar(value="0")
        e = ttk.Entry(row, textvariable=self.hex_ofs, width=14)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.show_hex())
        ttk.Button(row, text="Anzeigen", command=self.show_hex).pack(side="left")
        ttk.Label(row, text=f"(zeigt {HEX_LIMIT // 1024} KB ab dem Offset)", foreground="#6b7280").pack(side="left", padx=6)
        self.hex = ReadOnlyText(f, mono=True, wrap="none")
        self.hex.pack(fill="both", expand=True, pady=(4, 0))
        nb.add(f, text="Hex-Ansicht")

        # Zeichenketten
        f = ttk.Frame(nb, padding=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Filter:").pack(side="left")
        self.str_filter = tk.StringVar()
        self.str_filter.trace_add("write", lambda *a: self.fill_strings())
        ttk.Entry(row, textvariable=self.str_filter).pack(side="left", fill="x", expand=True, padx=4)
        self.strings = make_tree(f, [("ofs", "Offset", 100), ("s", "Zeichenkette", 500)])
        self.strings.frame.pack(fill="both", expand=True, pady=(4, 0))
        nb.add(f, text="Zeichenketten")

    # ------------------------------------------------------------------ #
    def choose(self):
        p = filedialog.askopenfilename(title="Datei zum Untersuchen wählen")
        if p:
            self.path_var.set(os.path.normpath(p))
            self.start()

    def open_path(self, path: str, display_name: str | None = None):
        self.path_var.set(path)
        self.start(display_name)

    def start(self, display_name: str | None = None):
        path = self.path_var.get().strip().strip('"')
        if not path or not os.path.isfile(path):
            messagebox.showwarning("Format-Erkenner", "Bitte eine vorhandene Datei angeben.")
            return
        emb = self.emb_var.get()
        self.status.configure(text=f"Analysiere {os.path.basename(path)} …")
        self.bg.run(lambda prog: formats.analyze(path, emb, self.bg.is_cancelled, display_name),
                    self.show, self.failed)

    def failed(self, e):
        self.status.configure(text=f"Fehler: {e}")
        messagebox.showerror("Format-Erkenner", f"Die Datei konnte nicht gelesen werden:\n{e}")

    def show(self, a: formats.Analysis):
        self.analysis = a
        name = a.display_name or os.path.basename(a.path)
        self.status.configure(text=f"{name} – {fmt_size(a.size)} analysiert.")
        size_txt = f"{a.size:,}".replace(",", ".")
        parts = [(f"{name}\n", "h"), (f"{size_txt} Bytes ({fmt_size(a.size)})\n\n", "muted")]
        if a.matches:
            parts.append(("Erkannt:\n", "b"))
            for m in a.matches:
                parts.append((f"  • {m.name}", "b"))
                parts.append((f"  [{m.category}, {m.confidence}, übliche Endung {m.ext}]\n", "muted"))
                if m.detail:
                    parts.append((f"      {m.detail}\n", ""))
        else:
            parts.append(("Kein bekanntes Format erkannt.\n", "b"))
        parts.append(("\nEntropie: ", "b"))
        ent_txt = f"{a.entropy:.3f}".replace(".", ",")
        chi_txt = f"{a.chi2:,.0f}".replace(",", ".")
        parts.append((f"{ent_txt} Bit/Byte (max. 8), Chi² {chi_txt}\n", ""))
        parts.append(("\nHinweise:\n", "b"))
        for h in a.hints:
            tag = "warn" if any(w in h for w in ("passt nicht", "VERSCHL", "XOR", "KOMPRIM")) else ""
            parts.append((f"  • {h}\n", tag))
        if a.embedded:
            parts.append((f"\n{len(a.embedded)} eingebettete Datei(en) – siehe Tab „Eingebettete Dateien“.\n", "muted"))
        if a.best and a.best.category in ("Archiv", "Spiel-Archiv"):
            parts.append(("\nTipp: „Im Archiv-Tab öffnen“ zeigt den Inhalt.\n", "muted"))
        if a.best and a.best.category == "Ausführbar":
            parts.append(("\nDies ist ausführbarer Code. FileProbe führt ihn nicht aus.\n", "warn"))
        self.result.set(parts=parts)
        self.chart.set(a.blocks, a.block_size, [(e.offset, e.name) for e in a.embedded])
        self.emb.delete(*self.emb.get_children())
        for i, e in enumerate(a.embedded):
            self.emb.insert("", "end", iid=str(i), values=(f"0x{e.offset:08X}", e.name, fmt_size(e.length)))
        self.emb_preview.clear()
        self.hex_ofs.set("0")
        self.hex.set(hexdump(a.head[:HEX_LIMIT]))
        self.fill_strings()

    def fill_strings(self):
        self.strings.delete(*self.strings.get_children())
        if not self.analysis:
            return
        flt = self.str_filter.get().lower()
        n = 0
        for ofs, s in self.analysis.strings:
            if flt and flt not in s.lower():
                continue
            self.strings.insert("", "end", values=(f"0x{ofs:08X}", s))
            n += 1
            if n >= 5000:
                break

    def show_hex(self):
        if not self.analysis:
            return
        try:
            ofs = int(self.hex_ofs.get().strip().removeprefix("0x"), 16)
        except ValueError:
            messagebox.showwarning("Hex-Ansicht", "Offset bitte hexadezimal angeben, z. B. 1A00.")
            return
        with open(self.analysis.path, "rb") as f:
            f.seek(ofs)
            data = f.read(HEX_LIMIT)
        self.hex.set(hexdump(data, ofs) if data else "(hinter dem Dateiende)")

    def _read_embedded(self, e: formats.Embedded) -> bytes:
        with open(self.analysis.path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                return formats.carve_bytes(mm, e)
            finally:
                mm.close()

    def preview_embedded(self):
        sel = self.emb.selection()
        if not sel or not self.analysis:
            return
        e = self.analysis.embedded[int(sel[0])]
        if e.category == "Bild" and e.length:
            self.emb_preview.show(self._read_embedded(e))
        else:
            self.emb_preview.clear()

    def save_embedded(self, all_: bool):
        if not self.analysis or not self.analysis.embedded:
            return
        items = self.analysis.embedded if all_ else [self.analysis.embedded[int(i)] for i in self.emb.selection()]
        if not items:
            messagebox.showinfo("Speichern", "Bitte zuerst Einträge auswählen.")
            return
        out = filedialog.askdirectory(title="Zielordner für die ausgeschnittenen Dateien")
        if not out:
            return
        base = os.path.splitext(os.path.basename(self.analysis.display_name or self.analysis.path))[0]
        n = 0
        for e in items:
            data = self._read_embedded(e)
            p = os.path.join(out, f"{base}_{e.offset:08X}{e.ext}")
            with open(p, "wb") as f:
                f.write(data)
            n += 1
        unknown = sum(1 for e in items if e.length is None)
        msg = f"{n} Datei(en) gespeichert in\n{out}"
        if unknown:
            msg += f"\n\nBei {unknown} Datei(en) war das Ende unbekannt – dort wurde 1 MB gespeichert."
        messagebox.showinfo("Speichern", msg)

    def to_archive(self):
        p = self.path_var.get().strip().strip('"')
        if p and os.path.isfile(p):
            self.app.open_archive(p)
