"""Tab 2: Archiv-Entpacker für Spieldateien."""

from __future__ import annotations

import os
import tkinter as tk
import winsound
from tkinter import filedialog, messagebox, ttk

from .. import archives, formats
from .common import (APP_TEMP, Background, ImagePreview, ReadOnlyText, fmt_size, hexdump, make_tree,
                     safe_temp_copy)

PREVIEW_LIMIT = 64 << 20
MAX_ROWS = 50000
KINDS = ["Alle", "Bild", "Audio", "Text", "Spieldaten", "Karte", "Ausführbar", "Archiv", "ohne Typ"]


class ArchiveTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.bg = Background(self)
        self.stack: list[archives.Reader] = []     # verschachtelte Archive
        self.preview_data: bytes | None = None
        self.preview_name = ""

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.back_btn = ttk.Button(top, text="◀ Zurück", command=self.go_back, state="disabled")
        self.back_btn.pack(side="left", padx=(0, 6))
        ttk.Label(top, text="Archiv:").pack(side="left")
        self.path_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.path_var)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self.open_path(self.path_var.get().strip().strip('"')))
        ttk.Button(top, text="Archiv öffnen…", command=self.choose).pack(side="left")
        self.carve_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Nur Signatur-Suche", variable=self.carve_var).pack(side="left", padx=6)

        self.info = ttk.Label(self, text="Unterstützt: ZIP/PK3, Doom-WAD, Quake-WAD2/Half-Life-WAD3, Quake-PAK, "
                                         "Unreal-PAK (bis v9), Unity-Bundles (UnityFS), Valve-VPK, Godot-PCK – "
                                         "alles andere per Signatur-Suche.", foreground="#6b7280", wraplength=1100)
        self.info.pack(fill="x", pady=(6, 2))

        flt = ttk.Frame(self)
        flt.pack(fill="x", pady=(2, 4))
        ttk.Label(flt, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.fill())
        ttk.Entry(flt, textvariable=self.filter_var, width=30).pack(side="left", padx=4)
        ttk.Label(flt, text="Typ:").pack(side="left", padx=(8, 0))
        self.kind_var = tk.StringVar(value="Alle")
        cb = ttk.Combobox(flt, textvariable=self.kind_var, values=KINDS, state="readonly", width=12)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.fill())
        self.count_lbl = ttk.Label(flt, text="", foreground="#6b7280")
        self.count_lbl.pack(side="left", padx=10)

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)
        self.tree = make_tree(pw, [("name", "Name", 380), ("size", "Größe", 90), ("kind", "Typ", 90),
                                   ("info", "Info", 200)])
        self.tree.sort_key_funcs = {"size": lambda iid: (0, self._entry(iid).size)}
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.preview())
        self.tree.bind("<Double-1>", lambda e: self.open_nested())
        pw.add(self.tree.frame, weight=3)

        right = ttk.Frame(pw, padding=(8, 0, 0, 0))
        pw.add(right, weight=2)
        self.prev_lbl = ttk.Label(right, text="Vorschau", font=("Segoe UI", 10, "bold"), wraplength=420)
        self.prev_lbl.pack(fill="x")
        self.prev_info = ttk.Label(right, text="", foreground="#6b7280", wraplength=420)
        self.prev_info.pack(fill="x")
        self.image = ImagePreview(right)
        self.image.pack(fill="x", pady=4)
        row = ttk.Frame(right)
        row.pack(fill="x")
        self.play_btn = ttk.Button(row, text="▶ Abspielen (WAV)", command=self.play, state="disabled")
        self.play_btn.pack(side="left")
        ttk.Button(row, text="■ Stopp", command=lambda: winsound.PlaySound(None, 0)).pack(side="left", padx=4)
        self.text = ReadOnlyText(right, mono=True, wrap="none", height=12)
        self.text.pack(fill="both", expand=True, pady=(4, 0))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(6, 0))
        self.conv_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="Umwandeln in PNG/WAV (Doom, Half-Life)", variable=self.conv_var,
                        command=self.preview).pack(side="left")
        ttk.Button(bottom, text="Ausgewählte entpacken…", command=lambda: self.extract("sel")).pack(side="left", padx=4)
        ttk.Button(bottom, text="Bilder & Sounds entpacken…", command=lambda: self.extract("media")).pack(side="left")
        ttk.Button(bottom, text="Alles entpacken…", command=lambda: self.extract("all")).pack(side="left", padx=4)
        ttk.Separator(bottom, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(bottom, text="Eintrag durchsuchen (Signaturen)",
                   command=lambda: self.open_nested(force_carve=True)).pack(side="left")
        ttk.Button(bottom, text="Eintrag als Archiv öffnen", command=self.open_nested).pack(side="left", padx=4)
        ttk.Button(bottom, text="Im Format-Erkenner", command=self.to_format).pack(side="left")
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", pady=(6, 0))
        self.status = ttk.Label(self, text="", foreground="#6b7280")
        self.status.pack(fill="x")

    # ------------------------------------------------------------------ #
    @property
    def reader(self) -> archives.Reader | None:
        return self.stack[-1] if self.stack else None

    def _entry(self, iid) -> archives.Entry:
        return self.reader.entries[int(iid)]

    def choose(self):
        p = filedialog.askopenfilename(title="Spiel-Archiv öffnen", filetypes=[
            ("Spiel-Archive", "*.pak *.pk3 *.pk4 *.zip *.wad *.unity3d *.bundle *.assets *.vpk *.pck *.ab"),
            ("Alle Dateien", "*.*")])
        if p:
            self.open_path(os.path.normpath(p))

    def open_path(self, path: str):
        if not path or not os.path.isfile(path):
            messagebox.showwarning("Archiv", "Bitte eine vorhandene Datei angeben.")
            return
        self.path_var.set(path)
        carve = self.carve_var.get()
        self.status.configure(text=f"Öffne {os.path.basename(path)} …")
        self.bg.run(lambda prog: archives.open_archive(path, force_carve=carve, cancel=self.bg.is_cancelled),
                    lambda r: self._opened(r, reset=True), self._failed)

    def _failed(self, e):
        self.status.configure(text=f"Fehler: {e}")
        messagebox.showerror("Archiv", str(e))

    def _opened(self, r: archives.Reader, reset: bool):
        if reset:
            for old in self.stack:
                old.close()
            self.stack = []
        self.stack.append(r)
        self.back_btn.configure(state="normal" if len(self.stack) > 1 else "disabled")
        trail = " › ".join(x.src.label for x in self.stack)
        notes = "  ".join(r.notes)
        self.info.configure(text=f"{r.format_name}  —  {trail}" + (f"\n{notes}" if notes else ""),
                            foreground="#111827")
        self.status.configure(text=f"{len(r.entries)} Einträge.")
        self.fill()
        self.clear_preview()

    def go_back(self):
        if len(self.stack) > 1:
            self.stack.pop().close()
            r = self.stack.pop()
            self._opened(r, reset=False)

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        r = self.reader
        if not r:
            return
        flt = self.filter_var.get().lower()
        kind = self.kind_var.get()
        shown = 0
        total_size = 0
        for i, e in enumerate(r.entries):
            if flt and flt not in e.name.lower() and flt not in e.info.lower():
                continue
            if kind != "Alle" and (e.kind or "ohne Typ") != kind:
                continue
            total_size += e.size
            if shown < MAX_ROWS:
                self.tree.insert("", "end", iid=str(i), values=(e.name, fmt_size(e.size), e.kind, e.info))
            shown += 1
        more = f" (nur die ersten {MAX_ROWS} angezeigt – Filter benutzen)" if shown > MAX_ROWS else ""
        self.count_lbl.configure(text=f"{shown} von {len(r.entries)} Einträgen, {fmt_size(total_size)}{more}")

    def selected(self) -> list[archives.Entry]:
        return [self._entry(i) for i in self.tree.selection()]

    # ------------------------------------------------------------------ #
    def clear_preview(self):
        self.preview_data = None
        self.image.clear()
        self.text.set("")
        self.prev_lbl.configure(text="Vorschau")
        self.prev_info.configure(text="")
        self.play_btn.configure(state="disabled")

    def preview(self):
        sel = self.selected()
        if len(sel) != 1:
            self.clear_preview()
            return
        e = sel[0]
        self.prev_lbl.configure(text=e.name)
        if e.size > PREVIEW_LIMIT:
            self.clear_preview()
            self.prev_lbl.configure(text=e.name)
            self.prev_info.configure(text=f"{fmt_size(e.size)} – zu groß für die Vorschau, bitte entpacken.")
            return
        try:
            data = self.reader.read(e)
        except Exception as ex:  # noqa: BLE001
            self.clear_preview()
            self.prev_lbl.configure(text=e.name)
            self.prev_info.configure(text=f"Nicht lesbar: {ex}")
            return
        name = e.name
        conv = self.reader.convert(e, data) if self.conv_var.get() else None
        if conv:
            name, data = conv
        self.preview_data, self.preview_name = data, name
        sigs = formats.match_signatures(data[:4096])
        what = sigs[0].name if sigs else "unbekannt"
        desc = self.image.show(data)
        info = f"{fmt_size(len(data))} – Inhalt: {what}" + (f" – umgewandelt zu {os.path.basename(name)}" if conv else "")
        if desc:
            info += f" – {desc}"
            self.text.set("")
        else:
            t = None
            if len(data) < 2_000_000:
                head = data[:20000]
                kind = formats._text_kind(head, len(data), None)
                if kind:
                    t = head.decode("utf-8", "replace") if "UTF-16" not in kind.name else head.decode("utf-16", "replace")
            self.text.set(t if t is not None else hexdump(data[:8192]))
        is_wav = data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        self.play_btn.configure(state="normal" if is_wav else "disabled")
        if any(s.name.startswith(("Ogg", "FMOD")) for s in sigs):
            info += " – Abspielen nur für WAV; zum Anhören entpacken."
        self.prev_info.configure(text=info)

    def play(self):
        if not self.preview_data:
            return
        os.makedirs(APP_TEMP, exist_ok=True)
        p = os.path.join(APP_TEMP, "vorschau.wav")
        try:
            winsound.PlaySound(None, 0)
            with open(p, "wb") as f:
                f.write(self.preview_data)
            winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except (OSError, RuntimeError) as ex:
            messagebox.showwarning("Abspielen", f"Konnte nicht abgespielt werden (evtl. komprimiertes WAV):\n{ex}")

    # ------------------------------------------------------------------ #
    def extract(self, mode: str):
        r = self.reader
        if not r:
            return
        if mode == "sel":
            ents = self.selected()
            if not ents:
                messagebox.showinfo("Entpacken", "Bitte zuerst Einträge auswählen.")
                return
        else:
            ents = list(r.entries)
        out = filedialog.askdirectory(title="Zielordner wählen (das Archiv selbst bleibt unverändert)")
        if not out:
            return
        conv = self.conv_var.get()
        self.progress.configure(maximum=max(1, len(ents)), value=0)

        def work(prog):
            return archives.extract(r, ents, out, convert=conv, only_media=(mode == "media"),
                                    progress=lambda i, n, name: prog(i, n, name), cancel=self.bg.is_cancelled)

        def on_prog(i, n, name):
            self.progress.configure(value=i)
            self.status.configure(text=f"Entpacke {i + 1}/{n}: {name}")

        def done(res):
            n, errors = res
            self.progress.configure(value=self.progress["maximum"])
            self.status.configure(text=f"{n} Datei(en) nach {out} entpackt, {len(errors)} Fehler.")
            msg = f"{n} Datei(en) entpackt nach:\n{out}"
            if errors:
                msg += f"\n\n{len(errors)} Fehler, z. B.:\n" + "\n".join(errors[:8])
            messagebox.showinfo("Entpacken", msg)

        if not self.bg.run(work, done, self._failed, on_prog):
            messagebox.showinfo("Entpacken", "Es läuft bereits ein Vorgang.")

    def open_nested(self, force_carve: bool = False):
        sel = self.selected()
        if len(sel) != 1:
            return
        e = sel[0]
        try:
            data = self.reader.read(e)
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Archiv", f"Eintrag nicht lesbar:\n{ex}")
            return
        try:
            r = archives.open_archive(data=data, label=os.path.basename(e.name), force_carve=force_carve)
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Archiv", f"Kein lesbares Archiv:\n{ex}")
            return
        if not force_carve and isinstance(r, archives.CarveReader) and not r.entries:
            r.close()
            messagebox.showinfo("Archiv", "Der Eintrag ist kein bekanntes Archiv und enthält keine erkennbaren "
                                          "eingebetteten Dateien.")
            return
        self._opened(r, reset=False)

    def to_format(self):
        sel = self.selected()
        if len(sel) != 1:
            p = self.path_var.get().strip()
            if p and os.path.isfile(p):
                self.app.analyze_file(p)
            return
        e = sel[0]
        try:
            data = self.reader.read(e)
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Archiv", str(ex))
            return
        self.app.analyze_file(safe_temp_copy(data, e.name), display_name=os.path.basename(e.name))
