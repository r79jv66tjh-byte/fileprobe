"""Hauptfenster von FileProbe."""

from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from tkinter import ttk

from .. import __version__, formats
from .tab_archive import ArchiveTab
from .tab_format import FormatTab
from .tab_registry import RegistryTab
from .tab_watch import WatchTab


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class App(tk.Tk):
    def __init__(self, open_path: str | None = None):
        super().__init__()
        self.title(f"FileProbe {__version__} – Daten & Dateien untersuchen")
        self.geometry("1280x820")
        self.minsize(980, 640)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=22)
        style.configure("TNotebook.Tab", padding=(14, 5))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.format_tab = FormatTab(self.nb, self)
        self.archive_tab = ArchiveTab(self.nb, self)
        self.registry_tab = RegistryTab(self.nb, self, os.path.join(app_dir(), "snapshots"))
        self.watch_tab = WatchTab(self.nb, self)
        self.nb.add(self.format_tab, text="🔍 Format-Erkenner")
        self.nb.add(self.archive_tab, text="📦 Archiv-Entpacker")
        self.nb.add(self.registry_tab, text="🗂 Registry-Vergleich")
        self.nb.add(self.watch_tab, text="👁 Datei-Wächter")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if open_path and os.path.isfile(open_path):
            self.after(200, lambda: self.open_any(open_path))

    def analyze_file(self, path: str, display_name: str | None = None):
        self.nb.select(self.format_tab)
        self.format_tab.open_path(path, display_name)

    def open_archive(self, path: str):
        self.nb.select(self.archive_tab)
        self.archive_tab.open_path(path)

    def open_any(self, path: str):
        """Startparameter: Archive im Archiv-Tab, alles andere im Format-Erkenner."""
        try:
            with open(path, "rb") as f:
                head = f.read(64)
            sigs = formats.match_signatures(head)
        except OSError:
            sigs = []
        if any(s.category == "Spiel-Archiv" or s.ext in (".zip",) for s in sigs):
            self.open_archive(path)
        else:
            self.analyze_file(path)

    def on_close(self):
        self.watch_tab.shutdown()
        self.destroy()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    App(argv[0] if argv else None).mainloop()
