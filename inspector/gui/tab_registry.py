"""Tab 3: Registry-Snapshot-Vergleich (nur lesen)."""

from __future__ import annotations

import glob
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import registry
from .common import Background, make_tree

PRESETS = [
    ("HKEY_CURRENT_USER\\Software", "Benutzer-Software (HKCU\\Software)", True),
    ("HKEY_LOCAL_MACHINE\\SOFTWARE", "System-Software (HKLM\\SOFTWARE) – groß, dauert etwas", True),
    ("HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Services", "Dienste & Treiber (HKLM\\…\\Services)", False),
    ("HKEY_CURRENT_USER\\Environment", "Benutzer-Umgebungsvariablen", False),
    ("HKEY_CLASSES_ROOT", "Dateizuordnungen & COM (HKCR) – sehr groß", False),
]
COLORS = {registry.KEY_ADDED: "#047857", registry.VAL_ADDED: "#059669", registry.KEY_REMOVED: "#b91c1c",
          registry.VAL_REMOVED: "#dc2626", registry.VAL_CHANGED: "#1d4ed8"}
MAX_ROWS = 30000


class RegistryTab(ttk.Frame):
    def __init__(self, master, app, snapshot_dir: str):
        super().__init__(master, padding=8)
        self.app = app
        self.bg = Background(self)
        self.snap_dir = snapshot_dir
        self.changes: list[registry.Change] = []
        self.info: dict = {}

        ttk.Label(self, text="So geht's: 1. Snapshot „vorher“ erstellen  2. Programm installieren/starten/ändern  "
                             "3. Snapshot „nachher“ erstellen  4. Vergleichen.  Die Registry wird dabei nur gelesen.",
                  foreground="#374151", wraplength=1150).pack(fill="x")

        top = ttk.Frame(self)
        top.pack(fill="x", pady=6)
        lf = ttk.LabelFrame(top, text="Zweige", padding=6)
        lf.pack(side="left", fill="both", expand=True)
        self.root_vars = []
        for path, label, on in PRESETS:
            v = tk.BooleanVar(value=on)
            ttk.Checkbutton(lf, text=label, variable=v).pack(anchor="w")
            self.root_vars.append((path, v))
        row = ttk.Frame(lf)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Weitere (mit ; trennen):").pack(side="left")
        self.extra_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.extra_var).pack(side="left", fill="x", expand=True, padx=4)

        sf = ttk.LabelFrame(top, text="Snapshots", padding=6)
        sf.pack(side="left", fill="both", expand=True, padx=(8, 0))
        row = ttk.Frame(sf)
        row.pack(fill="x")
        ttk.Button(row, text="Snapshot „vorher“ erstellen", command=lambda: self.snapshot("vorher")).pack(side="left")
        ttk.Button(row, text="Snapshot „nachher“ erstellen", command=lambda: self.snapshot("nachher")).pack(
            side="left", padx=4)
        self.cancel_btn = ttk.Button(row, text="Abbrechen", command=self.bg.cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.snap_status = ttk.Label(sf, text="", foreground="#6b7280")
        self.snap_status.pack(fill="x", pady=4)
        grid = ttk.Frame(sf)
        grid.pack(fill="x")
        self.before_var, self.after_var = tk.StringVar(), tk.StringVar()
        self.before_cb = self._snap_row(grid, 0, "Vorher:", self.before_var)
        self.after_cb = self._snap_row(grid, 1, "Nachher:", self.after_var)
        grid.columnconfigure(1, weight=1)
        row = ttk.Frame(sf)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Vergleichen", command=self.compare).pack(side="left")
        ttk.Button(row, text="Snapshot-Ordner öffnen", command=lambda: os.startfile(self.snap_dir)).pack(
            side="left", padx=4)
        ttk.Label(row, text=self.snap_dir, foreground="#9ca3af").pack(side="left", padx=4)

        res = ttk.Frame(self)
        res.pack(fill="x")
        self.summary = ttk.Label(res, text="", font=("Segoe UI", 10, "bold"))
        self.summary.pack(side="left")
        row = ttk.Frame(self)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.fill())
        ttk.Entry(row, textvariable=self.filter_var, width=40).pack(side="left", padx=4)
        self.kind_vars = {}
        for k in (registry.KEY_ADDED, registry.KEY_REMOVED, registry.VAL_ADDED, registry.VAL_REMOVED,
                  registry.VAL_CHANGED):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(row, text=k, variable=v, command=self.fill).pack(side="left", padx=3)
            self.kind_vars[k] = v
        ttk.Button(row, text="Als Text exportieren…", command=self.export).pack(side="right")
        ttk.Button(row, text="Pfad kopieren", command=self.copy_path).pack(side="right", padx=4)

        self.tree = make_tree(self, [("kind", "Art", 120), ("key", "Schlüssel", 430), ("name", "Wert", 150),
                                     ("type", "Typ", 100), ("old", "Alt", 200), ("new", "Neu", 260)])
        self.tree.frame.pack(fill="both", expand=True)
        for k, c in COLORS.items():
            self.tree.tag_configure(k, foreground=c)
        self.refresh_list()

    def _snap_row(self, grid, r, label, var):
        ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w")
        cb = ttk.Combobox(grid, textvariable=var, state="readonly")
        cb.grid(row=r, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(grid, text="Datei…", command=lambda: self._pick(var)).grid(row=r, column=2)
        return cb

    def _pick(self, var):
        p = filedialog.askopenfilename(title="Snapshot wählen", initialdir=self.snap_dir,
                                       filetypes=[("Registry-Snapshot", "*.pireg"), ("Alle Dateien", "*.*")])
        if p:
            self.files[os.path.basename(p) + "  (extern)"] = p
            var.set(os.path.basename(p) + "  (extern)")
            self._update_combos()

    # ------------------------------------------------------------------ #
    def refresh_list(self, select_after: str | None = None):
        self.files = {}
        for p in sorted(glob.glob(os.path.join(self.snap_dir, "*.pireg")), key=os.path.getmtime):
            try:
                m = registry.read_meta(p)
                label = f"{m.get('created', '?')}  {m.get('label', '')}  ({len(m.get('roots', []))} Zweige)"
            except (OSError, ValueError, EOFError):
                continue
            self.files[label] = p
        self._update_combos()
        labels = list(self.files)
        befores = [k for k in labels if "vorher" in k]
        afters = [k for k in labels if "nachher" in k]
        if befores and not self.before_var.get():
            self.before_var.set(befores[-1])
        if afters and not self.after_var.get():
            self.after_var.set(afters[-1])
        if select_after:
            for k, v in self.files.items():
                if v == select_after:
                    (self.before_var if "vorher" in k else self.after_var).set(k)

    def _update_combos(self):
        vals = list(self.files)
        self.before_cb.configure(values=vals)
        self.after_cb.configure(values=vals)

    def roots(self) -> list[str]:
        r = [p for p, v in self.root_vars if v.get()]
        for extra in self.extra_var.get().split(";"):
            if extra.strip():
                r.append(extra.strip())
        return r

    def snapshot(self, label: str):
        try:
            roots = [registry.normalize_root(r) for r in self.roots()]
        except ValueError as e:
            messagebox.showerror("Registry", str(e))
            return
        if not roots:
            messagebox.showinfo("Registry", "Bitte mindestens einen Zweig auswählen.")
            return
        os.makedirs(self.snap_dir, exist_ok=True)
        out = os.path.join(self.snap_dir, time.strftime("%Y-%m-%d_%H-%M-%S") + f"_{label}.pireg")

        def num(n):
            return f"{n:,}".replace(",", ".")

        def prog(st, path):
            self.snap_status.configure(text=f"{num(st.keys)} Schlüssel, {num(st.values)} Werte … {path[-90:]}")

        def done(st):
            self.cancel_btn.configure(state="disabled")
            if st.cancelled:
                self.snap_status.configure(text="Abgebrochen – kein Snapshot gespeichert.")
                return
            secs = f"{st.seconds:.1f}".replace(".", ",")
            self.snap_status.configure(
                text=f"Snapshot „{label}“ gespeichert: {num(st.keys)} Schlüssel, {num(st.values)} Werte, "
                     f"{num(st.denied)} ohne Leserecht, {secs} s")
            if label == "vorher":
                self.before_var.set("")
            else:
                self.after_var.set("")
            self.refresh_list(select_after=out)

        def err(e):
            self.cancel_btn.configure(state="disabled")
            messagebox.showerror("Registry", str(e))

        if self.bg.run(lambda p: registry.take_snapshot(roots, out, label, p, self.bg.is_cancelled), done, err, prog):
            self.cancel_btn.configure(state="normal")
            self.snap_status.configure(text="Lese Registry …")

    def compare(self):
        a, b = self.files.get(self.before_var.get()), self.files.get(self.after_var.get())
        if not a or not b:
            messagebox.showinfo("Registry", "Bitte einen Snapshot „vorher“ und einen „nachher“ wählen.")
            return
        if a == b:
            messagebox.showinfo("Registry", "Vorher und Nachher sind dieselbe Datei.")
            return

        def done(res):
            self.changes, self.info = res
            i = self.info
            if not i["roots"]:
                self.summary.configure(text="Die beiden Snapshots haben keine gemeinsamen Zweige.")
            else:
                self.summary.configure(
                    text=f"{len(self.changes)} Unterschiede  –  Schlüssel: {i['keys_added']} neu, "
                         f"{i['keys_removed']} gelöscht, {i['keys_changed']} mit geänderten Werten")
            self.snap_status.configure(text="Vergleich fertig.")
            self.fill()

        self.bg.run(lambda p: registry.compare(a, b, p), done,
                    lambda e: messagebox.showerror("Registry", str(e)),
                    lambda msg: self.snap_status.configure(text=msg))

    def filtered(self):
        flt = self.filter_var.get().lower()
        kinds = {k for k, v in self.kind_vars.items() if v.get()}
        for c in self.changes:
            if c.kind not in kinds:
                continue
            if flt and flt not in c.key.lower() and flt not in c.name.lower() and flt not in c.new.lower() \
                    and flt not in c.old.lower():
                continue
            yield c

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        for n, c in enumerate(self.filtered()):
            if n >= MAX_ROWS:
                break
            name = c.name if c.name or c.kind in (registry.KEY_ADDED, registry.KEY_REMOVED) else "(Standard)"
            self.tree.insert("", "end", values=(c.kind, c.key, name, c.vtype, c.old, c.new), tags=(c.kind,))

    def export(self):
        if not self.changes:
            return
        p = filedialog.asksaveasfilename(title="Unterschiede speichern", defaultextension=".txt",
                                         filetypes=[("Textdatei", "*.txt")],
                                         initialfile=time.strftime("registry_vergleich_%Y-%m-%d.txt"))
        if p:
            registry.export_text(list(self.filtered()), self.info, p)
            messagebox.showinfo("Export", f"Gespeichert:\n{p}")

    def copy_path(self):
        sel = self.tree.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(self.tree.set(sel[0], "key"))
