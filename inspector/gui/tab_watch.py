"""Tab 4: Datei-Änderungs-Wächter."""

from __future__ import annotations

import os
import queue
import subprocess
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import watcher
from .common import fmt_size, make_tree

MAX_LOG = 5000


class WatchTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.q: queue.Queue = queue.Queue()
        self.watchers: list[watcher.DirWatcher] = []
        self.files: dict[str, dict] = {}     # Pfad (klein) -> Daten
        self.log_count = 0
        self.ignored = 0

        ttk.Label(self, text="Zeigt live, welche Dateien in den gewählten Ordnern geschrieben werden – z. B. Spiel "
                             "starten, speichern, Optionen ändern und dann unter „Dateien“ Spielstände und Configs "
                             "finden. Windows verrät dabei nicht, welches Programm schreibt; füge deshalb gezielt die "
                             "Ordner des Spiels hinzu.", foreground="#374151", wraplength=1150).pack(fill="x")

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=(6, 0))

        left = ttk.Frame(pw)
        pw.add(left, weight=1)
        ttk.Label(left, text="Überwachte Ordner (mit Unterordnern):", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.folder_list = tk.Listbox(left, height=8, selectmode="extended", activestyle="none")
        self.folder_list.pack(fill="both", expand=True)
        for f in watcher.default_folders():
            self.folder_list.insert("end", f)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Ordner…", command=self.add_folder).pack(side="left")
        ttk.Button(row, text="Ordner eines Programms…", command=self.add_process).pack(side="left", padx=4)
        ttk.Button(row, text="Entfernen", command=self.remove_folder).pack(side="left")
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Button(row, text="Standardordner", command=self.reset_folders).pack(side="left")
        ttk.Label(left, text="Ignorieren, wenn der Pfad enthält (eine Zeile je Muster):").pack(anchor="w", pady=(8, 0))
        self.excl = tk.Text(left, height=8, width=40, font=("Consolas", 9))
        self.excl.pack(fill="both", expand=True)
        self.excl.insert("1.0", "\n".join(p.rstrip("\\").rstrip("\\") + ("\\" if p.endswith("\\") else "")
                                          for p in watcher.DEFAULT_EXCLUDES))
        self.dirs_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Auch Ordner-Ereignisse anzeigen", variable=self.dirs_var).pack(anchor="w", pady=4)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=4)
        self.start_btn = ttk.Button(row, text="▶ Überwachung starten", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(row, text="■ Stoppen", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status = ttk.Label(left, text="Gestoppt.", foreground="#6b7280", wraplength=380)
        self.status.pack(fill="x")

        right = ttk.Frame(pw, padding=(8, 0, 0, 0))
        pw.add(right, weight=3)
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Label(row, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.refill())
        ttk.Entry(row, textvariable=self.filter_var, width=30).pack(side="left", padx=4)
        self.only_hits = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Nur Spielstände/Configs", variable=self.only_hits, command=self.refill).pack(
            side="left", padx=6)
        ttk.Button(row, text="Liste leeren", command=self.clear).pack(side="right")
        ttk.Button(row, text="Exportieren…", command=self.export).pack(side="right", padx=4)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, pady=(4, 0))
        f = ttk.Frame(nb)
        self.sum = make_tree(f, [("path", "Datei", 430), ("count", "Änderungen", 85), ("last", "Zuletzt", 75),
                                 ("act", "Letzte Aktion", 110), ("size", "Größe", 80), ("role", "Vermutung", 150)])
        self.sum.frame.pack(fill="both", expand=True)
        self.sum.tag_configure("save", foreground="#047857")
        self.sum.tag_configure("cfg", foreground="#1d4ed8")
        self.sum.tag_configure("gone", foreground="#9ca3af")
        self.sum.bind("<Double-1>", lambda e: self.show_in_explorer())
        nb.add(f, text="Dateien (Zusammenfassung)")
        f = ttk.Frame(nb)
        self.log = make_tree(f, [("time", "Zeit", 80), ("act", "Aktion", 120), ("path", "Pfad", 700)])
        self.log.frame.pack(fill="both", expand=True)
        nb.add(f, text="Ereignisse (live)")
        row = ttk.Frame(right)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Im Explorer zeigen", command=self.show_in_explorer).pack(side="left")
        ttk.Button(row, text="Im Format-Erkenner analysieren", command=self.analyze).pack(side="left", padx=4)
        ttk.Button(row, text="Pfad kopieren", command=self.copy_path).pack(side="left")

        self.after(250, self.drain)

    # ------------------------------------------------------------------ #
    def folders(self) -> list[str]:
        return list(self.folder_list.get(0, "end"))

    def add_folder(self):
        p = filedialog.askdirectory(title="Ordner überwachen")
        if p:
            self._add(os.path.normpath(p))

    def _add(self, p):
        if p.lower() not in (f.lower() for f in self.folders()):
            self.folder_list.insert("end", p)
            if self.watchers:
                self._start_one(p)

    def remove_folder(self):
        for i in reversed(self.folder_list.curselection()):
            self.folder_list.delete(i)
        if self.watchers:
            self.status.configure(text="Änderungen an der Ordnerliste gelten nach Stoppen und Neustarten.")

    def reset_folders(self):
        self.folder_list.delete(0, "end")
        for f in watcher.default_folders():
            self.folder_list.insert("end", f)

    def add_process(self):
        ProcessPicker(self, self._add)

    def patterns(self) -> list[str]:
        return [ln.strip() for ln in self.excl.get("1.0", "end").splitlines() if ln.strip()]

    # ------------------------------------------------------------------ #
    def start(self):
        if self.watchers:
            return
        if not self.folders():
            messagebox.showinfo("Datei-Wächter", "Bitte mindestens einen Ordner hinzufügen.")
            return
        for f in self.folders():
            self._start_one(f)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.after(400, self._report_errors)

    def _start_one(self, folder):
        w = watcher.DirWatcher(folder, self.q.put)
        w.start()
        self.watchers.append(w)

    def _report_errors(self):
        bad = [w for w in self.watchers if w.error]
        ok = len(self.watchers) - len(bad)
        txt = f"Überwache {ok} Ordner seit {time.strftime('%H:%M:%S')}."
        if bad:
            txt += " Nicht überwachbar: " + "; ".join(f"{w.path} ({w.error})" for w in bad)
        self.status.configure(text=txt, foreground="#047857" if not bad else "#b45309")

    def stop(self):
        for w in self.watchers:
            w.stop()
        for w in self.watchers:
            w.join(1)
        self.watchers = []
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status.configure(text=f"Gestoppt. {len(self.files)} Dateien erfasst, {self.ignored} Ereignisse ignoriert.",
                              foreground="#6b7280")

    def clear(self):
        self.files.clear()
        self.ignored = 0
        self.sum.delete(*self.sum.get_children())
        self.log.delete(*self.log.get_children())
        self.log_count = 0

    # ------------------------------------------------------------------ #
    def drain(self):
        pats = None
        self._root_pats = {}
        n = 0
        try:
            while n < 3000:
                ev: watcher.Event = self.q.get_nowait()
                n += 1
                if pats is None:
                    pats = self.patterns()
                self.handle(ev, pats)
        except queue.Empty:
            pass
        self.after(250, self.drain)

    def handle(self, ev: watcher.Event, pats):
        ts = time.strftime("%H:%M:%S", time.localtime(ev.time))
        if ev.action in ("Fehler", "Überlauf"):
            self._log(ts, ev.action, f"{ev.root}: {ev.path}")
            return
        # Muster, die schon auf den überwachten Ordner selbst passen, gelten dort nicht
        # (wer z. B. Temp ausdrücklich überwacht, will dessen Dateien sehen).
        root_pats = self._root_pats.get(ev.root)
        if root_pats is None:
            root_pats = [p for p in pats if not watcher.is_excluded(ev.root + "\\", [p])]
            self._root_pats[ev.root] = root_pats
        if watcher.is_excluded(ev.path, root_pats):
            self.ignored += 1
            return
        if not self.dirs_var.get() and ev.action != "gelöscht" and os.path.isdir(ev.path):
            return
        self._log(ts, ev.action, ev.path)
        key = ev.path.lower()
        d = self.files.get(key)
        if d is None:
            d = self.files[key] = {"path": ev.path, "count": 0, "role": watcher.guess_role(ev.path), "iid": None}
        d["count"] += 1
        d["last"] = ts
        d["act"] = ev.action
        try:
            d["size"] = os.path.getsize(ev.path) if ev.action != "gelöscht" else None
        except OSError:
            d["size"] = None
        self._upsert(d)

    def _log(self, ts, act, path):
        flt = self.filter_var.get().lower()
        if flt and flt not in path.lower():
            return
        self.log.insert("", 0, values=(ts, act, path))
        self.log_count += 1
        if self.log_count > MAX_LOG:
            kids = self.log.get_children()
            self.log.delete(*kids[MAX_LOG:])
            self.log_count = MAX_LOG

    def _visible(self, d) -> bool:
        flt = self.filter_var.get().lower()
        if flt and flt not in d["path"].lower():
            return False
        if self.only_hits.get() and d["role"] not in ("möglicher Spielstand", "Einstellungen/Config"):
            return False
        return True

    def _upsert(self, d):
        vis = self._visible(d)
        tag = {"möglicher Spielstand": "save", "Einstellungen/Config": "cfg"}.get(d["role"], "")
        if d["act"] == "gelöscht":
            tag = "gone"
        vals = (d["path"], d["count"], d["last"], d["act"], fmt_size(d["size"]) if d["size"] is not None else "–",
                d["role"])
        iid = d["iid"]
        if iid and self.sum.exists(iid):
            if vis:
                self.sum.item(iid, values=vals, tags=(tag,))
            else:
                self.sum.delete(iid)
                d["iid"] = None
        elif vis:
            d["iid"] = self.sum.insert("", 0, values=vals, tags=(tag,))

    def refill(self):
        self.sum.delete(*self.sum.get_children())
        for d in sorted(self.files.values(), key=lambda d: d["count"]):
            d["iid"] = None
            self._upsert(d)

    # ------------------------------------------------------------------ #
    def _selected_path(self) -> str | None:
        for tree in (self.sum, self.log):
            sel = tree.selection()
            if sel and tree.winfo_ismapped():
                return tree.set(sel[0], "path")
        return None

    def show_in_explorer(self):
        p = self._selected_path()
        if not p:
            return
        if os.path.exists(p):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        elif os.path.isdir(os.path.dirname(p)):
            os.startfile(os.path.dirname(p))      # öffnet nur den Ordner im Explorer

    def analyze(self):
        p = self._selected_path()
        if p and os.path.isfile(p):
            self.app.analyze_file(p)
        elif p:
            messagebox.showinfo("Datei-Wächter", "Die Datei existiert nicht mehr.")

    def copy_path(self):
        p = self._selected_path()
        if p:
            self.clipboard_clear()
            self.clipboard_append(p)

    def export(self):
        if not self.files:
            return
        p = filedialog.asksaveasfilename(title="Liste speichern", defaultextension=".txt",
                                         filetypes=[("Textdatei", "*.txt")],
                                         initialfile=time.strftime("datei_waechter_%Y-%m-%d_%H-%M.txt"))
        if not p:
            return
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write("Änderungen\tZuletzt\tAktion\tGröße\tVermutung\tDatei\n")
            for d in sorted(self.files.values(), key=lambda d: -d["count"]):
                f.write(f"{d['count']}\t{d['last']}\t{d['act']}\t{d['size'] if d['size'] is not None else ''}\t"
                        f"{d['role']}\t{d['path']}\n")
        messagebox.showinfo("Export", f"Gespeichert:\n{p}")

    def shutdown(self):
        for w in self.watchers:
            w.stop()


class ProcessPicker(tk.Toplevel):
    """Liste laufender Programme; fügt den Ordner der gewählten .exe hinzu."""

    def __init__(self, master, on_pick):
        super().__init__(master)
        self.title("Ordner eines laufenden Programms hinzufügen")
        self.geometry("760x480")
        self.on_pick = on_pick
        row = ttk.Frame(self, padding=6)
        row.pack(fill="x")
        ttk.Label(row, text="Filter:").pack(side="left")
        self.var = tk.StringVar()
        self.var.trace_add("write", lambda *a: self.fill())
        e = ttk.Entry(row, textvariable=self.var)
        e.pack(side="left", fill="x", expand=True, padx=4)
        e.focus_set()
        self.tree = make_tree(self, [("pid", "PID", 70), ("name", "Programm", 200), ("path", "Pfad", 460)])
        self.tree.frame.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", lambda ev: self.pick())
        row = ttk.Frame(self, padding=6)
        row.pack(fill="x")
        ttk.Button(row, text="Programmordner hinzufügen", command=self.pick).pack(side="left")
        ttk.Label(row, text="Tipp: Viele Spiele speichern unter „Dokumente\\My Games“ oder „Saved Games“ – "
                            "die sind schon in den Standardordnern.", foreground="#6b7280").pack(side="left", padx=8)
        self.procs = sorted((p for p in watcher.list_processes() if p[2]), key=lambda p: p[1].lower())
        self.fill()

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        flt = self.var.get().lower()
        for pid, name, path in self.procs:
            if flt and flt not in name.lower() and flt not in path.lower():
                continue
            self.tree.insert("", "end", values=(pid, name, path))

    def pick(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self.tree.set(sel[0], "path")
        folder = os.path.dirname(path)
        if folder.lower().startswith(os.environ.get("WINDIR", r"C:\Windows").lower()):
            if not messagebox.askyesno("Hinweis", "Das ist ein Windows-Systemordner – trotzdem überwachen?", parent=self):
                return
        self.on_pick(folder)
        self.destroy()
