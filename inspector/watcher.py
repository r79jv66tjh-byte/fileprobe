"""Live-Überwachung von Ordnern (ReadDirectoryChangesW) und Prozessliste.

Die Ordner werden nur mit FILE_LIST_DIRECTORY geöffnet (lesen); es wird nichts verändert.
Windows meldet bei dieser Schnittstelle nicht, *welcher* Prozess geschrieben hat – deshalb
lässt sich der Ordner eines laufenden Programms gezielt hinzufügen, und die Zusammenfassung
zeigt, welche Dateien während der Beobachtung geschrieben wurden.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x7
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE = wintypes.HANDLE(-1).value

NOTIFY = 0x1 | 0x2 | 0x8 | 0x10 | 0x40   # Dateiname, Ordnername, Größe, Schreibzeit, Erstellung

ACTIONS = {1: "erstellt", 2: "gelöscht", 3: "geändert", 4: "umbenannt (alt)", 5: "umbenannt (neu)"}

k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.CreateFileW.restype = wintypes.HANDLE
k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID, wintypes.LPVOID]
k32.ReadDirectoryChangesW.restype = wintypes.BOOL
k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
k32.CancelIoEx.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL


@dataclass
class Event:
    time: float
    action: str
    path: str
    root: str


class DirWatcher(threading.Thread):
    """Überwacht einen Ordner rekursiv und ruft `callback(Event)` auf (aus dem Hintergrund-Thread)."""

    def __init__(self, path: str, callback, recursive: bool = True):
        super().__init__(daemon=True, name=f"watch:{path}")
        self.path = os.path.abspath(path)
        self.callback = callback
        self.recursive = recursive
        self._stop_evt = threading.Event()
        self._dir_handle = None
        self.error: str | None = None
        self.ready = threading.Event()

    def run(self):
        h = k32.CreateFileW(self.path, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None, OPEN_EXISTING,
                            FILE_FLAG_BACKUP_SEMANTICS, None)
        if h == INVALID_HANDLE or h is None:
            self.error = ctypes.FormatError(ctypes.get_last_error()).strip()
            self.ready.set()
            return
        self._dir_handle = h
        buf = ctypes.create_string_buffer(64 * 1024)
        got = wintypes.DWORD()
        self.ready.set()
        try:
            while not self._stop_evt.is_set():
                ok = k32.ReadDirectoryChangesW(h, buf, len(buf), self.recursive, NOTIFY, ctypes.byref(got), None, None)
                if self._stop_evt.is_set():
                    break
                if not ok:
                    err = ctypes.get_last_error()
                    if err != 995:            # ERROR_OPERATION_ABORTED = gestoppt
                        self.error = ctypes.FormatError(err).strip()
                        self.callback(Event(time.time(), "Fehler", self.error, self.path))
                    break
                now = time.time()
                if got.value == 0:
                    self.callback(Event(now, "Überlauf", "(zu viele Änderungen auf einmal – einige fehlen)", self.path))
                    continue
                raw = buf.raw[:got.value]
                ofs = 0
                while True:
                    nxt, action, ln = int.from_bytes(raw[ofs:ofs + 4], "little"), \
                        int.from_bytes(raw[ofs + 4:ofs + 8], "little"), int.from_bytes(raw[ofs + 8:ofs + 12], "little")
                    name = raw[ofs + 12:ofs + 12 + ln].decode("utf-16-le", "replace")
                    self.callback(Event(now, ACTIONS.get(action, str(action)), os.path.join(self.path, name), self.path))
                    if nxt == 0:
                        break
                    ofs += nxt
        finally:
            k32.CloseHandle(h)
            self._dir_handle = None

    def stop(self):
        self._stop_evt.set()
        h = self._dir_handle
        if h is not None:
            k32.CancelIoEx(h, None)


# --------------------------------------------------------------------------- #
# Prozesse (für „Ordner eines laufenden Programms hinzufügen“)
# --------------------------------------------------------------------------- #

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]


def list_processes() -> list[tuple[int, str, str]]:
    """(PID, Name, voller Pfad oder '') aller Prozesse."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE:
        return []
    out = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            pid = pe.th32ProcessID
            out.append((pid, pe.szExeFile, process_path(pid)))
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def process_path(pid: int) -> str:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


# --------------------------------------------------------------------------- #
# Hilfen für die Auswertung
# --------------------------------------------------------------------------- #

def default_folders() -> list[str]:
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, "Documents"),
        os.path.join(home, "Saved Games"),
        os.environ.get("APPDATA", ""),
        os.environ.get("LOCALAPPDATA", ""),
        os.path.join(home, "AppData", "LocalLow"),
    ]
    # Dokumente können umgeleitet sein (OneDrive)
    od = os.environ.get("OneDrive")
    if od:
        cands.append(os.path.join(od, "Documents"))
        cands.append(os.path.join(od, "Dokumente"))
    seen, out = set(), []
    for c in cands:
        if c and os.path.isdir(c) and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


DEFAULT_EXCLUDES = [
    r"\AppData\Local\Temp\\", r"\AppData\Local\Microsoft\\", r"\AppData\Roaming\Microsoft\\",
    r"\AppData\Local\Packages\\", r"\AppData\Local\Google\\", r"\AppData\Local\Mozilla\\",
    r"\AppData\Roaming\Mozilla\\", r"\AppData\Local\BraveSoftware\\", r"\AppData\Local\Microsoft\Edge\\",
    r"\AppData\Local\Claude", r"\AppData\Roaming\Claude", r"\AppData\Local\NVIDIA\\",
    r"\AppData\Local\D3DSCache\\", r"\AppData\Local\CrashDumps\\", ".tmp", "~$", r"\INetCache\\",
    r"\.git\\", r"\__pycache__\\", r"\AppData\Roaming\Code\\", r"\AppData\Local\pip\\",
]


def is_excluded(path: str, patterns: list[str]) -> bool:
    low = path.lower()
    for p in patterns:
        p = p.strip().lower()
        if not p:
            continue
        if p.endswith("\\\\"):
            p = p[:-1]
        if p in low:
            return True
    return False


SAVE_WORDS = ("save", "spielstand", "profile", "slot", "progress", "savegame", "saved games", "persist")
CONFIG_EXT = (".ini", ".cfg", ".config", ".json", ".xml", ".yaml", ".yml", ".toml", ".conf", ".prefs", ".vdf")
SAVE_EXT = (".sav", ".save", ".sl2", ".dat", ".bin", ".es3", ".rpgsave", ".rvdata2", ".gam", ".svg_", ".slot")


def guess_role(path: str) -> str:
    low = path.lower()
    name = os.path.basename(low)
    ext = os.path.splitext(name)[1]
    if ext in (".log", ".txt") and ("log" in name or ext == ".log"):
        return "Logdatei"
    if any(w in low for w in SAVE_WORDS) or ext in SAVE_EXT:
        return "möglicher Spielstand"
    if ext in CONFIG_EXT or "config" in name or "settings" in name or "options" in name:
        return "Einstellungen/Config"
    if ext in (".cache", ".tmp", ".lock") or "cache" in low:
        return "Cache/temporär"
    return ""
