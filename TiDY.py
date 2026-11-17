#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
 _____ _ ______   __
|_   _(_)  _ \ \ / /
  | | | | | | \ V /      TiDY  -  your Downloads folder, always tidy.
  | | | | |_| || |
  |_| |_|____/ |_|       Folder Monitor & Automatic File Sorter

TiDY watches one folder (your Downloads folder by default).  The moment a
file is created or dropped there, Windows sends TiDY a "something changed"
event, and TiDY moves the file into a sub-folder that matches its type:

    Downloads/photo.jpg   ->  Downloads/Images/photo.jpg
    Downloads/notes.pdf   ->  Downloads/Documents/notes.pdf
    Downloads/song.mp3    ->  Downloads/Music/song.mp3

* 100 % Python, standard library only (tkinter, ctypes, threading, ...).
* Works completely offline - nothing is ever uploaded or downloaded.
* Waits until downloads are finished before touching them.
* Never deletes or overwrites anything; every move can be undone.

Run it with:   py TiDY.py        (or just double-click TiDY.py)
"""

import base64
import ctypes
import datetime
import errno
import io
import json
import math
import os
import queue
import random
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
import wave
import zipfile
import zlib

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog

APP_NAME = "TiDY"
APP_VERSION = "1.0.0"
APP_TAGLINE = "Your Downloads folder, always tidy."
IS_WIN = sys.platform.startswith("win")

if IS_WIN:
    from ctypes import wintypes
    import winreg

    try:
        import winsound
    except ImportError:  # pragma: no cover
        winsound = None
else:  # pragma: no cover
    wintypes = None
    winreg = None
    winsound = None


# =============================================================================
#  Small helpers
# =============================================================================

def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % n
            return ("%.1f %s" % (n, unit)).replace(".0 ", " ")
        n /= 1024.0
    return "0 B"


def time_ago(ts):
    d = max(0, time.time() - ts)
    if d < 10:
        return "just now"
    if d < 60:
        return "%d sec ago" % d
    if d < 3600:
        return "%d min ago" % (d // 60)
    if d < 86400:
        h = int(d // 3600)
        return "%d hour%s ago" % (h, "" if h == 1 else "s")
    days = int(d // 86400)
    return "yesterday" if days == 1 else "%d days ago" % days


def clock_time(ts):
    t = datetime.datetime.fromtimestamp(ts)
    return t.strftime("%I:%M %p").lstrip("0")


def day_label(ts):
    day = datetime.date.fromtimestamp(ts)
    today = datetime.date.today()
    if day == today:
        return "Today"
    if day == today - datetime.timedelta(days=1):
        return "Yesterday"
    return day.strftime("%A, %d %B").replace(" 0", " ")


def today_key():
    return datetime.date.today().isoformat()


def norm(path):
    """Normalised, case-insensitive (on Windows) form of a path for comparisons."""
    return os.path.normcase(os.path.abspath(path))


def plural(n, word, many=None):
    return "%s %s" % (format(n, ","), word if n == 1 else (many or word + "s"))


# =============================================================================
#  Windows integration (all optional - every call fails gracefully)
# =============================================================================

def enable_dpi_awareness():
    """Crisp rendering on high-DPI screens instead of blurry bitmap scaling."""
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


APP_USER_MODEL_ID = "TiDY.FolderTidy.1"


def set_app_user_model_id():
    """Lets Windows show TiDY's own icon in the taskbar instead of Python's."""
    if IS_WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except Exception:
            pass


if IS_WIN:
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def parse(cls, text):
            g = cls()
            ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
            return g


def known_folder(guid_text, fallback):
    """A Windows 'known folder' (Downloads, Desktop, ...) - even if the user moved
    it to another drive or to OneDrive."""
    if IS_WIN:
        try:
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(GUID.parse(guid_text)), 0, None,
                                                         ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path and os.path.isdir(path):
                    return path
        except Exception:
            pass
    return fallback


def known_downloads_dir():
    return known_folder("{374DE290-123F-4565-9164-39C4925E467B}",
                        os.path.join(os.path.expanduser("~"), "Downloads"))


def system_uses_dark_theme():
    if IS_WIN:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return value == 0
        except Exception:
            return False
    return False


def window_handle(widget):
    """Native HWND of a Tk toplevel (the frame that owns the title bar)."""
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        return hwnd or widget.winfo_id()
    except Exception:
        return None


def style_native_window(widget, caption_hex, text_hex, dark, rounded=True):
    """Windows 11: tint the title bar to match the app and use rounded corners."""
    if not IS_WIN:
        return
    try:
        hwnd = window_handle(widget)
        dwm = ctypes.windll.dwmapi

        def setattr_(attr, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), ctypes.c_uint(attr),
                                      ctypes.byref(v), ctypes.sizeof(v))

        def colorref(hx):
            r, g, b = int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16)
            return r | (g << 8) | (b << 16)

        setattr_(20, 1 if dark else 0)          # DWMWA_USE_IMMERSIVE_DARK_MODE
        if rounded:
            setattr_(33, 2)                     # DWMWA_WINDOW_CORNER_PREFERENCE = round
        if caption_hex:
            setattr_(35, colorref(caption_hex))  # DWMWA_CAPTION_COLOR
            setattr_(36, colorref(text_hex))     # DWMWA_TEXT_COLOR
            setattr_(34, colorref(caption_hex))  # DWMWA_BORDER_COLOR
    except Exception:
        pass


def hide_stray_console():
    """When TiDY.py is double-clicked, Windows opens a black console window
    next to the app.  Hide it - but only if no shell (cmd, PowerShell, a code
    editor terminal...) shares that console, so we never hide a real terminal."""
    if not IS_WIN:
        return
    try:
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return
        pids = (ctypes.c_uint32 * 16)()
        count = k32.GetConsoleProcessList(pids, 16)
        if count == 0 or count > 16:
            return
        k32.OpenProcess.restype = wintypes.HANDLE
        for i in range(count):
            h = k32.OpenProcess(0x1000, False, pids[i])  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(1024)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            k32.CloseHandle(h)
            if not ok:
                return
            exe = os.path.basename(buf.value).lower()
            if exe not in ("python.exe", "pythonw.exe", "py.exe", "pyw.exe") and \
                    not exe.startswith("python3"):
                return  # a real terminal is attached - leave it alone
        ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def work_area():
    """Screen area that is not covered by the taskbar: (x0, y0, x1, y1)."""
    if IS_WIN:
        try:
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    return None


def open_path(path):
    try:
        if IS_WIN:
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def reveal_in_explorer(path):
    """Open the folder that contains *path* and highlight the file."""
    if not os.path.exists(path):
        parent = os.path.dirname(path)
        return open_path(parent) if os.path.isdir(parent) else False
    if IS_WIN:
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        except Exception:
            pass
    return open_path(os.path.dirname(path))


def pythonw_executable():
    exe = sys.executable or "python"
    folder, name = os.path.split(exe)
    if name.lower() == "python.exe":
        candidate = os.path.join(folder, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    if not IS_WIN:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def set_autostart(enable, script=None):
    """Start TiDY (quietly, in the tray) when the user signs in to Windows."""
    if not IS_WIN:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            if script is None:  # prefer the installed copy, if there is one
                script = installed_script() if is_installed() else os.path.abspath(__file__)
            cmd = '"%s" "%s" --background' % (pythonw_executable(), script)
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def risky_folder_reason(path):
    """TiDY moves files around, so refuse folders where that could hurt."""
    p = norm(path)
    drive, rest = os.path.splitdrive(p)
    if rest in ("", "\\", "/") or p == norm(os.path.dirname(p)):
        return "That is the root of a drive. Please pick a normal folder, like Downloads."
    home = norm(os.path.expanduser("~"))
    if p == home:
        return "That is your whole user folder. Please pick a folder inside it, like Downloads."
    for env in ("WINDIR", "SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
                "APPDATA", "LOCALAPPDATA"):
        sysdir = os.environ.get(env)
        if sysdir and (p == norm(sysdir) or p.startswith(norm(sysdir) + os.sep)):
            return "That is a system folder. TiDY only tidies your own folders."
    if p == norm(DATA_DIR) or p.startswith(norm(DATA_DIR) + os.sep):
        return "That is where TiDY keeps its own settings."
    return None


# =============================================================================
#  Where TiDY keeps its settings (never inside the watched folder)
# =============================================================================

def _data_dir():
    custom = os.environ.get("TIDY_DATA_DIR")
    if custom:
        return os.path.abspath(custom)
    base = os.environ.get("APPDATA") if IS_WIN else None
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


DATA_DIR = _data_dir()
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
LOG_FILE = os.path.join(DATA_DIR, "tidy.log")
CACHE_DIR = os.path.join(DATA_DIR, "cache")


def log(*parts):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                   " ".join(str(p) for p in parts)))
    except Exception:
        pass


def log_exception(where):
    log("ERROR in", where, "\n" + traceback.format_exc())


# Colour names usable by rules.  (light-theme shade, dark-theme shade)
PALETTE = {
    "pink":   ("#DB2777", "#F472B6"),
    "orange": ("#EA580C", "#FB923C"),
    "violet": ("#7C3AED", "#A78BFA"),
    "blue":   ("#2563EB", "#60A5FA"),
    "amber":  ("#D97706", "#FBBF24"),
    "green":  ("#059669", "#34D399"),
    "cyan":   ("#0891B2", "#22D3EE"),
    "red":    ("#DC2626", "#F87171"),
    "lime":   ("#65A30D", "#A3E635"),
    "slate":  ("#64748B", "#94A3B8"),
}
PALETTE_ORDER = ["pink", "orange", "violet", "blue", "amber", "green", "cyan", "red", "lime", "slate"]

DEFAULT_RULES = [
    {"id": "images", "name": "Images", "icon": "image", "color": "pink",
     "exts": "jpg jpeg png gif bmp webp svg ico tif tiff heic heif avif jfif raw cr2 nef arw dng psd ai eps".split()},
    {"id": "videos", "name": "Videos", "icon": "video", "color": "orange",
     "exts": "mp4 mkv mov avi wmv flv webm m4v mpg mpeg 3gp ts vob".split()},
    {"id": "music", "name": "Music", "icon": "music", "color": "violet",
     "exts": "mp3 wav flac aac ogg m4a wma opus aiff alac mid midi".split()},
    {"id": "documents", "name": "Documents", "icon": "doc", "color": "blue",
     "exts": ("pdf doc docx odt rtf txt md tex xls xlsx ods csv ppt pptx odp key pages "
              "numbers epub mobi xps one log").split()},
    {"id": "archives", "name": "Archives", "icon": "archive", "color": "amber",
     "exts": "zip rar 7z tar gz tgz bz2 xz zst cab iso img lz".split()},
    {"id": "programs", "name": "Programs", "icon": "app", "color": "green",
     "exts": "exe msi msix msixbundle appx appxbundle bat cmd apk dmg pkg deb rpm appimage jar".split()},
    {"id": "code", "name": "Code", "icon": "code", "color": "cyan",
     "exts": ("py ipynb js ts jsx tsx html htm css scss json xml yaml yml toml ini c cpp h hpp cs "
              "java kt go rs php rb swift lua sh ps1 sql r dart vue").split()},
    {"id": "others", "name": "Others", "icon": "other", "color": "slate", "exts": [], "catch_all": True},
]
for _r in DEFAULT_RULES:
    _r.setdefault("on", True)
    _r["builtin"] = True
    _r.setdefault("catch_all", False)


def fresh_default_rules():
    return json.loads(json.dumps(DEFAULT_RULES))


DEFAULT_SETTINGS = {
    "folder": None,             # filled with the Downloads folder on first run
    "resume": True,             # start watching again when TiDY opens
    "was_watching": False,
    "close_to_tray": True,
    "popups": True,
    "sound": True,
    "animations": True,
    "theme": "system",          # system | light | dark
    "tutorial_done": False,
    "kept": [],                 # files the user put back with "Undo" - never re-sorted
    "rules": None,
    "stats": {"total": 0, "bytes": 0, "by_rule": {}, "days": {}},
    "geometry": None,
}


class Store:
    """Loads and saves settings.json + history.json (atomic writes)."""

    MAX_HISTORY = 600

    def __init__(self):
        self.settings = self._load(SETTINGS_FILE, {})
        for key, value in DEFAULT_SETTINGS.items():
            if key not in self.settings:
                self.settings[key] = json.loads(json.dumps(value))
        s = self.settings
        if not s.get("folder"):
            s["folder"] = known_downloads_dir()
        if not isinstance(s.get("rules"), list) or not s["rules"]:
            s["rules"] = fresh_default_rules()
        self._repair_rules()
        st = s.setdefault("stats", {})
        for key, value in DEFAULT_SETTINGS["stats"].items():
            st.setdefault(key, json.loads(json.dumps(value)))
        # forget "kept" files that no longer exist
        s["kept"] = [p for p in s.get("kept", []) if isinstance(p, str) and os.path.exists(p)][-500:]
        history = self._load(HISTORY_FILE, [])
        self.history = history if isinstance(history, list) else []
        self._lock = threading.Lock()

    def _repair_rules(self):
        rules = []
        seen = set()
        for r in self.settings["rules"]:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            r.setdefault("id", "rule%d" % random.randint(1000, 999999))
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            r.setdefault("icon", "folder")
            if r.get("color") not in PALETTE:
                r["color"] = "slate"
            r["exts"] = [str(e).lower().lstrip(".") for e in r.get("exts", []) if str(e).strip(".")]
            r.setdefault("on", True)
            r.setdefault("builtin", False)
            r.setdefault("catch_all", False)
            rules.append(r)
        catch = [r for r in rules if r["catch_all"]]
        if not catch:
            rules.append(fresh_default_rules()[-1])
        else:  # exactly one catch-all, always last
            rules = [r for r in rules if not r["catch_all"]] + [catch[0]]
        self.settings["rules"] = rules

    @staticmethod
    def _load(path, default):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except Exception:
            log_exception("loading " + path)
            try:  # keep the broken file for inspection, start fresh
                os.replace(path, path + ".broken")
            except Exception:
                pass
            return default

    @staticmethod
    def _write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)

    def save(self):
        with self._lock:
            try:
                self._write(SETTINGS_FILE, self.settings)
                self._write(HISTORY_FILE, self.history[: self.MAX_HISTORY])
            except Exception:
                log_exception("saving settings")

    # --- history & statistics -------------------------------------------------
    def record_move(self, entry):
        self.history.insert(0, entry)
        del self.history[self.MAX_HISTORY:]
        st = self.settings["stats"]
        st["total"] = st.get("total", 0) + 1
        st["bytes"] = st.get("bytes", 0) + int(entry.get("size") or 0)
        by = st.setdefault("by_rule", {})
        by[entry["rule"]] = by.get(entry["rule"], 0) + 1
        days = st.setdefault("days", {})
        key = today_key()
        days[key] = days.get(key, 0) + 1
        for old in sorted(days)[:-30]:
            del days[old]

    def record_undo(self, entry):
        st = self.settings["stats"]
        by = st.setdefault("by_rule", {})
        if by.get(entry["rule"], 0) > 0:
            by[entry["rule"]] -= 1
        st["total"] = max(0, st.get("total", 0) - 1)
        st["bytes"] = max(0, st.get("bytes", 0) - int(entry.get("size") or 0))
        day = datetime.date.fromtimestamp(entry.get("time", time.time())).isoformat()
        days = st.setdefault("days", {})
        if days.get(day, 0) > 0:
            days[day] -= 1

    def today_count(self):
        return self.settings["stats"].get("days", {}).get(today_key(), 0)

    def kept_set(self):
        return set(norm(p) for p in self.settings.get("kept", []))


# =============================================================================
#  The sorting engine  (runs in background threads, talks to the UI by queue)
# =============================================================================

# Browsers and download managers write into these while a download is running.
PARTIAL_EXTS = (".crdownload", ".part", ".partial", ".download", ".opdownload", ".tmp",
                ".temp", ".!ut", ".!qb", ".!bt", ".aria2", ".xltd", ".bc!", ".td", ".crswap",
                ".downloading", ".dlpart", ".filepart", ".unconfirmed")
JUNK_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "ehthumbs.db"}

ACTION_ADDED, ACTION_REMOVED, ACTION_MODIFIED, ACTION_RENAMED_OLD, ACTION_RENAMED_NEW = 1, 2, 3, 4, 5


def partial_ext(name):
    low = name.lower()
    for ext in PARTIAL_EXTS:
        if low.endswith(ext):
            return ext
    return None


def strip_partial(name):
    ext = partial_ext(name)
    return name[: -len(ext)] if ext else name


def is_junk(name):
    low = name.lower()
    if low in JUNK_NAMES or low.startswith("~$") or low.startswith(".~lock"):
        return True
    return (not IS_WIN) and low.startswith(".")


def is_hidden(st):
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x2 or attrs & 0x4)  # HIDDEN or SYSTEM


def short_error(exc):
    if isinstance(exc, PermissionError):
        return "the file is in use or protected"
    if isinstance(exc, FileNotFoundError):
        return "the file disappeared"
    return (getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__).rstrip(".")


class Rulebook:
    """Turns the list of rules into a fast extension -> rule lookup."""

    def __init__(self, rules):
        self.by_ext = {}
        self.catch_all = None
        for rule in rules:
            if not rule.get("on", True):
                continue
            if rule.get("catch_all"):
                self.catch_all = rule
                continue
            for ext in rule.get("exts", []):
                self.by_ext.setdefault(ext.lower(), rule)  # the first rule wins

    def match(self, name):
        low = name.lower()
        parts = low.split(".")
        if len(parts) > 2:  # e.g. "backup.tar.gz"
            double = ".".join(parts[-2:])
            if double in self.by_ext:
                return self.by_ext[double]
        ext = os.path.splitext(low)[1].lstrip(".")
        if ext and ext in self.by_ext:
            return self.by_ext[ext]
        return self.catch_all


def unique_target(folder, name):
    """photo.jpg -> photo (1).jpg -> photo (2).jpg ...  (never overwrite)."""
    base, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    n = 1
    while os.path.lexists(candidate):
        candidate = os.path.join(folder, "%s (%d)%s" % (base, n, ext))
        n += 1
    return candidate


def move_file(src, dst_dir, name):
    if os.path.exists(dst_dir) and not os.path.isdir(dst_dir):
        raise OSError(errno.EEXIST, "a file named '%s' is in the way" % os.path.basename(dst_dir))
    os.makedirs(dst_dir, exist_ok=True)
    for _ in range(64):
        dst = unique_target(dst_dir, name)
        try:
            os.rename(src, dst)  # instant on the same drive, never overwrites on Windows
            return dst
        except FileExistsError:
            continue
        except OSError as exc:
            if getattr(exc, "winerror", None) == 17 or exc.errno == errno.EXDEV:
                shutil.move(src, dst)  # the sub-folder lives on another drive
                return dst
            raise
    raise OSError(errno.EEXIST, "no free file name left")


class PollingWatcher:
    """Fallback watcher: compares folder snapshots (used if native events fail)."""

    def __init__(self, folder, on_change, on_error, interval=0.8):
        self.folder, self.on_change, self.on_error = folder, on_change, on_error
        self.interval = interval
        self._stop = threading.Event()
        self._prev = {}
        self._thread = threading.Thread(target=self._run, name="TiDY-poll", daemon=True)

    def _snapshot(self):
        snap = {}
        with os.scandir(self.folder) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                    snap[entry.name] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
        return snap

    def start(self):
        self._prev = self._snapshot()
        self._thread.start()

    def _run(self):
        prev = self._prev
        while not self._stop.wait(self.interval):
            try:
                cur = self._snapshot()
            except OSError:
                if not self._stop.is_set():
                    self.on_error("The watched folder can't be reached any more.")
                return
            for name, sig in cur.items():
                old = prev.get(name)
                if old is None:
                    self.on_change(ACTION_ADDED, name)
                elif old != sig:
                    self.on_change(ACTION_MODIFIED, name)
            for name in prev:
                if name not in cur:
                    self.on_change(ACTION_REMOVED, name)
            prev = cur

    def stop(self):
        self._stop.set()
        if threading.current_thread() is not self._thread and self._thread.is_alive():
            self._thread.join(2.0)


class WindowsWatcher:
    """Real-time change events straight from Windows (ReadDirectoryChangesW).

    Windows wakes this thread the instant a file is created, renamed or
    written in the folder - no polling, no CPU use while nothing happens."""

    BUFFER = 64 * 1024
    FILTER = 0x01 | 0x02 | 0x08 | 0x10 | 0x40  # name, dir name, size, last write, creation

    def __init__(self, folder, on_change, on_error):
        self.folder, self.on_change, self.on_error = folder, on_change, on_error
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._handle = None
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._CreateFileW = k32.CreateFileW
        self._CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._CreateFileW.restype = wintypes.HANDLE
        self._Read = k32.ReadDirectoryChangesW
        self._Read.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                               wintypes.LPVOID]
        self._Read.restype = wintypes.BOOL
        self._CancelIoEx = k32.CancelIoEx
        self._CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        self._CancelIoEx.restype = wintypes.BOOL
        self._CloseHandle = k32.CloseHandle
        self._CloseHandle.argtypes = [wintypes.HANDLE]
        self._CloseHandle.restype = wintypes.BOOL
        self._thread = threading.Thread(target=self._run, name="TiDY-watch", daemon=True)

    def start(self):
        handle = self._CreateFileW(
            self.folder, 0x0001,           # FILE_LIST_DIRECTORY
            0x1 | 0x2 | 0x4,               # let others read / write / delete meanwhile
            None, 3,                       # OPEN_EXISTING
            0x02000000,                    # FILE_FLAG_BACKUP_SEMANTICS (needed for folders)
            None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot open the folder for watching")
        self._handle = handle
        self._thread.start()

    def _run(self):
        buf = ctypes.create_string_buffer(self.BUFFER)
        got = wintypes.DWORD(0)
        try:
            while not self._stop.is_set():
                handle = self._handle
                if not handle:
                    break
                ok = self._Read(handle, buf, self.BUFFER, False, self.FILTER,
                                ctypes.byref(got), None, None)
                if self._stop.is_set():
                    break
                if not ok:
                    err = ctypes.get_last_error()
                    if err != 995:  # 995 = cancelled because we are stopping
                        self.on_error("Windows stopped reporting changes for this folder "
                                      "(error %d)." % err)
                    break
                if got.value == 0:  # too many changes at once (buffer overflow)
                    if not os.path.isdir(self.folder):
                        self.on_error("The watched folder was moved or deleted.")
                        break
                    self.on_change("overflow", None)
                    continue
                data = buf.raw[: got.value]
                offset = 0
                while True:
                    nxt, action, length = struct.unpack_from("<III", data, offset)
                    name = data[offset + 12: offset + 12 + length].decode("utf-16-le", "replace")
                    self.on_change(action, name)
                    if not nxt:
                        break
                    offset += nxt
        except Exception:
            log_exception("watcher thread")
            if not self._stop.is_set():
                self.on_error("Watching stopped because of an unexpected error.")
        finally:
            self._close()

    def _close(self):
        with self._lock:
            handle, self._handle = self._handle, None
        if handle:
            self._CloseHandle(handle)

    def stop(self):
        self._stop.set()
        handle = self._handle
        if handle:
            try:
                self._CancelIoEx(handle, None)  # wakes up the blocked read
            except Exception:
                pass
        if threading.current_thread() is not self._thread and self._thread.is_alive():
            self._thread.join(2.0)
        self._close()


class Sorter:
    """Collects 'this file changed' signals and moves a file once it is
    complete: no new event for a moment, size stable, no partial download
    next to it, and Windows lets us move it (i.e. nobody is still writing)."""

    SETTLE = 1.0        # seconds of quiet before a file counts as finished
    EMPTY_GRACE = 10.0  # 0-byte files are often placeholders or being renamed
    TICK = 0.3
    MAX_TRIES = 60

    def __init__(self, engine, folder):
        self.engine = engine
        self.folder = folder
        self._pending = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._announced = set()
        self._thread = threading.Thread(target=self._run, name="TiDY-sorter", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if threading.current_thread() is not self._thread and self._thread.is_alive():
            self._thread.join(2.0)

    def poke(self, name):
        if not name or os.sep in name or "/" in name:
            return
        now = time.monotonic()
        key = name.lower()
        with self._lock:
            item = self._pending.get(key)
            if item is None:
                item = {"first": now, "size": -1, "mtime": -1, "tries": 0, "next": 0.0, "ver": 0}
                self._pending[key] = item
            item["name"] = name
            item["last"] = now
            item["ver"] += 1
        self._wake.set()

    def forget(self, name):
        if name:
            with self._lock:
                self._pending.pop(name.lower(), None)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(self.TICK)
            self._wake.clear()
            if self._stop.is_set():
                break
            now = time.monotonic()
            with self._lock:
                items = list(self._pending.items())
            for key, item in items:
                if self._stop.is_set():
                    break
                version = item["ver"]
                try:
                    verdict = self._consider(item, now)
                except Exception:
                    log_exception("sorting " + repr(item.get("name")))
                    verdict = "drop"
                if verdict != "wait":
                    with self._lock:
                        if self._pending.get(key) is item and item["ver"] == version:
                            del self._pending[key]

    def _consider(self, item, now):
        if now < item["next"]:
            return "wait"
        name = item["name"]
        engine = self.engine
        if partial_ext(name):
            shown = strip_partial(name)
            if not shown.lower().startswith("unconfirmed ") and shown not in self._announced:
                self._announced.add(shown)
                engine.emit("incoming", shown, "downloading")
            return "drop"  # the browser renames it when it is done -> a new event
        rule, path, st = engine.classify(self.folder, name)
        if rule is None:
            return "drop"
        for ext in (".part", ".crdownload", ".download", ".partial", ".opdownload"):
            if os.path.exists(path + ext):  # e.g. Firefox's empty placeholder file
                item["next"] = now + 1.0
                return "wait"
        if now - item["last"] < self.SETTLE:
            return "wait"
        signature = (st.st_size, st.st_mtime_ns)
        if signature != (item["size"], item["mtime"]):
            item["size"], item["mtime"] = signature
            item["next"] = now + 0.45
            if name not in self._announced:
                self._announced.add(name)
                engine.emit("incoming", name, "settling")
            return "wait"
        if st.st_size == 0 and now - item["first"] < self.EMPTY_GRACE:
            item["next"] = now + 0.5
            return "wait"
        try:
            engine.move(self.folder, name, rule, st)
        except FileNotFoundError:
            self._announced.discard(name)
            return "drop"
        except OSError as exc:
            item["tries"] += 1
            if item["tries"] >= self.MAX_TRIES:
                self._announced.discard(name)
                engine.emit("skipped", name, short_error(exc))
                return "drop"
            item["next"] = now + min(5.0, 0.4 * item["tries"])
            return "wait"
        self._announced.discard(name)
        return "done"


class Engine:
    """Owns the watcher + sorter and reports everything through `events`."""

    def __init__(self, store):
        self.store = store
        self.events = queue.Queue()
        self.folder = None
        self._watcher = None
        self._sorter = None
        self._since = time.time()
        self._kept = set()
        self._io_lock = threading.Lock()
        self.set_rules(store.settings["rules"])
        self.set_kept(store.settings.get("kept", []))

    # ---- configuration -------------------------------------------------------
    def set_rules(self, rules):
        self.rulebook = Rulebook(json.loads(json.dumps(rules)))

    def set_kept(self, paths):
        self._kept = set(norm(p) for p in paths)

    def is_kept(self, path):
        return norm(path) in self._kept

    def emit(self, *event):
        self.events.put(event)

    @property
    def running(self):
        return self._sorter is not None

    # ---- start / stop --------------------------------------------------------
    def start(self, folder):
        self.stop(quiet=True)
        if not folder or not os.path.isdir(folder):
            return False, "That folder can't be found. Please choose it again."
        reason = risky_folder_reason(folder)
        if reason:
            return False, reason
        self.folder = folder
        sorter = Sorter(self, folder)
        sorter.start()
        self._sorter = sorter
        watcher = None
        if IS_WIN:
            try:
                watcher = WindowsWatcher(folder, self._on_change, self._on_error)
                watcher.start()
            except Exception:
                log_exception("native watcher")
                watcher = None
        if watcher is None:
            try:
                watcher = PollingWatcher(folder, self._on_change, self._on_error)
                watcher.start()
            except Exception as exc:
                sorter.stop()
                self._sorter = None
                return False, "TiDY can't watch that folder (%s)." % short_error(exc)
        self._watcher = watcher
        self._since = time.time()
        self.emit("started", folder)
        return True, ""

    def stop(self, quiet=False):
        watcher, sorter = self._watcher, self._sorter
        self._watcher = self._sorter = None
        if watcher:
            watcher.stop()
        if sorter:
            sorter.stop()
        if (watcher or sorter) and not quiet:
            self.emit("stopped", None)

    def _on_change(self, action, name):
        sorter = self._sorter
        if sorter is None:
            return
        if action == "overflow":
            # Too many events at once: look at everything that appeared since we started.
            try:
                with os.scandir(sorter.folder) as it:
                    for entry in it:
                        try:
                            if entry.stat().st_ctime >= self._since - 1:
                                sorter.poke(entry.name)
                        except OSError:
                            pass
            except OSError:
                pass
        elif action in (ACTION_REMOVED, ACTION_RENAMED_OLD):
            sorter.forget(name)
        else:
            sorter.poke(name)

    def _on_error(self, message):
        self.emit("failed", message)

    # ---- the actual decision + move -------------------------------------------
    def classify(self, folder, name):
        """(rule, path, stat) when *name* should be tidied, else (None, reason, None)."""
        if is_junk(name) or partial_ext(name):
            return None, "ignored", None
        path = os.path.join(folder, name)
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return None, "gone", None
        if not stat.S_ISREG(st.st_mode):
            return None, "not a file", None
        if is_hidden(st):
            return None, "hidden", None
        if self.is_kept(path):
            return None, "kept", None
        rule = self.rulebook.match(name)
        if rule is None:
            return None, "no rule", None
        return rule, path, st

    def move(self, folder, name, rule, st):
        src = os.path.join(folder, name)
        with self._io_lock:
            dst = move_file(src, os.path.join(folder, rule["name"]), name)
        entry = {
            "id": "%x%04x" % (time.time_ns(), random.randint(0, 0xFFFF)),
            "time": time.time(),
            "name": os.path.basename(dst),
            "orig": name,
            "src": src,
            "dst": dst,
            "rule": rule["id"],
            "folder": rule["name"],
            "size": int(st.st_size),
            "undone": False,
        }
        self.emit("moved", entry)
        return entry

    # ---- one-off jobs -----------------------------------------------------------
    def loose_files(self, folder):
        """Files already sitting in the folder that a rule would tidy."""
        names = []
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    rule, _, _ = self.classify(folder, entry.name)
                    if rule is not None:
                        names.append(entry.name)
        except OSError:
            pass
        return names

    def count_loose_async(self, folder):
        def job():
            self.emit("loose", folder, len(self.loose_files(folder)))
        threading.Thread(target=job, name="TiDY-count", daemon=True).start()

    def tidy_existing(self, folder):
        def job():
            moved = failed = 0
            now = time.time()
            for name in self.loose_files(folder):
                rule, path, st = self.classify(folder, name)
                if rule is None or now - st.st_mtime < 3:  # maybe still being written
                    continue
                try:
                    self.move(folder, name, rule, st)
                    moved += 1
                    time.sleep(0.04)
                except OSError as exc:
                    failed += 1
                    log("tidy existing: could not move", name, exc)
            self.emit("tidied", moved, failed)
            self.emit("loose", folder, len(self.loose_files(folder)))
        threading.Thread(target=job, name="TiDY-tidy", daemon=True).start()

    def undo(self, entry):
        """Put a tidied file back where it came from (and keep it there)."""
        current = entry.get("dst")
        if not current or not os.path.exists(current):
            return False, "That file isn't in its folder any more - it was moved or deleted."
        home = os.path.dirname(entry["src"])
        if not os.path.isdir(home):
            return False, "Its original folder doesn't exist any more."
        target = unique_target(home, entry.get("orig") or entry["name"])
        self._kept.add(norm(target))  # before moving, so the watcher leaves it alone
        try:
            with self._io_lock:
                os.rename(current, target)
        except OSError as exc:
            self._kept.discard(norm(target))
            return False, "Couldn't move it back: %s." % short_error(exc)
        return True, target

    def drop_samples(self, folder, kinds):
        """Write little sample files into the folder - just like a real download."""
        def job():
            for kind in kinds:
                try:
                    name, data = sample_file(kind)
                    final = unique_target(folder, name)
                    temp = final + ".tmp"           # looks like a download in progress...
                    with open(temp, "wb") as fh:
                        fh.write(data)
                    os.replace(temp, final)         # ...that has just finished
                except Exception:
                    log_exception("sample " + kind)
                time.sleep(0.55)
        threading.Thread(target=job, name="TiDY-samples", daemon=True).start()


# =============================================================================
#  Sample files (real, valid files - generated with the standard library)
# =============================================================================

SAMPLE_KINDS = ["png", "txt", "wav", "zip", "py", "csv"]


def sample_file(kind):
    if kind == "png":
        size = 256
        return "TiDY sample picture.png", png_bytes(size, size, render_logo_rgba(size))
    if kind == "txt":
        text = ("Hello from TiDY!\n\nThis little note was dropped into your folder so you could "
                "watch TiDY tidy it.\nIt went to the Documents folder because it is a .txt file.\n\n"
                "You can delete it any time.\n")
        return "TiDY sample note.txt", text.encode("utf-8")
    if kind == "wav":
        return "TiDY sample chime.wav", chime_wav_bytes()
    if kind == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("readme.txt", "A tiny zip made by TiDY. Archives go to the Archives folder.\n")
        return "TiDY sample bundle.zip", buf.getvalue()
    if kind == "py":
        return "TiDY sample script.py", b'print("Hello from TiDY - code files go to the Code folder!")\n'
    if kind == "csv":
        return "TiDY sample table.csv", b"fruit,amount\napple,3\nmango,5\nbanana,2\n"
    raise ValueError(kind)


def chime_wav_bytes():
    """A soft two-note chime, synthesised sample by sample."""
    rate = 22050
    length = int(rate * 0.6)
    notes = ((0.0, 1318.51, 0.55), (0.085, 1975.53, 0.35))  # E6 then B6
    frames = bytearray()
    two_pi = 2 * math.pi
    for i in range(length):
        t = i / rate
        v = 0.0
        for start, freq, amp in notes:
            if t >= start:
                tt = t - start
                env = math.exp(-tt * 8.5) * min(1.0, tt * 500.0)
                v += amp * env * (math.sin(two_pi * freq * tt) + 0.15 * math.sin(2 * two_pi * freq * tt))
        frames += struct.pack("<h", int(clamp(v * 0.3, -1.0, 1.0) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class Chime:
    def __init__(self):
        self.path = os.path.join(CACHE_DIR, "chime-v1.wav")
        self._last = 0.0

    def play(self):
        if not winsound:
            return
        now = time.monotonic()
        if now - self._last < 0.7:
            return
        self._last = now
        try:
            if not os.path.exists(self.path):
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(self.path, "wb") as fh:
                    fh.write(chime_wav_bytes())
            winsound.PlaySound(self.path, winsound.SND_FILENAME | winsound.SND_ASYNC |
                               winsound.SND_NODEFAULT)
        except Exception:
            pass

# =============================================================================
#  Colour, easing and the animation clock
# =============================================================================

def hex_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_hex(r, g, b):
    return "#%02x%02x%02x" % (int(clamp(r, 0, 255) + 0.5), int(clamp(g, 0, 255) + 0.5),
                              int(clamp(b, 0, 255) + 0.5))


def mix(a, b, t):
    """Blend two '#rrggbb' colours (t=0 -> a, t=1 -> b)."""
    if t <= 0:
        return a
    if t >= 1:
        return b
    ra, ga, ba = hex_rgb(a)
    rb, gb, bb = hex_rgb(b)
    return rgb_hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def hex01(h):
    r, g, b = hex_rgb(h)
    return r / 255.0, g / 255.0, b / 255.0


def ease_linear(t):
    return t


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def ease_in_cubic(t):
    return t * t * t


def ease_in_out_cubic(t):
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_back(t):
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_out_quint(t):
    return 1 - (1 - t) ** 5


def lerp(a, b, t):
    return a + (b - a) * t


def bezier(p0, p1, p2, t):
    u = 1 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


class Animator:
    """One shared clock for every animation in the app.

    tween(): runs fn(progress 0..1) over a duration with an easing curve.
    loop():  runs fn(now) every frame until it returns False (ambient motion).
    The clock sleeps completely when nothing is moving."""

    def __init__(self, root):
        self.root = root
        self.enabled = True
        self._tweens = {}
        self._loops = {}
        self._job = None
        self._slow = False
        self._in_tick = False
        self._n = 0
        self._hires = False

    def tween(self, duration, fn, ease=ease_out_cubic, done=None, delay=0.0, key=None):
        if key is None:
            self._n += 1
            key = ("tween", self._n)
        old = self._tweens.pop(key, None)
        if not self.enabled:
            try:
                fn(1.0)
            except tk.TclError:
                pass
            if done:
                done()
            return key
        if old is not None and old[0] > time.perf_counter():
            pass  # replaced before it started
        self._tweens[key] = [time.perf_counter() + delay, max(0.001, duration), fn, ease, done]
        self._kick()
        return key

    def cancel(self, key):
        self._tweens.pop(key, None)

    def running(self, key):
        return key in self._tweens

    def loop(self, key, fn):
        self._loops[key] = fn
        self._kick()

    def unloop(self, key):
        self._loops.pop(key, None)

    def _set_hires(self, on):
        if IS_WIN and on != self._hires:
            try:
                (ctypes.windll.winmm.timeBeginPeriod if on else ctypes.windll.winmm.timeEndPeriod)(1)
                self._hires = on
            except Exception:
                pass

    def _kick(self):
        if self._tweens:
            self._set_hires(True)
        if self._in_tick:
            return  # the running frame schedules the next one when it ends
        if self._tweens and self._job is not None and self._slow:
            self.root.after_cancel(self._job)  # don't let a new tween wait for a slow tick
            self._job = None
        if self._job is None:
            self._slow = False
            self._job = self.root.after(1, self._tick)

    def _tick(self):
        self._job = None
        self._in_tick = True
        try:
            hidden = self._frame()
        except tk.TclError:
            return  # the window is gone
        finally:
            self._in_tick = False
        if self._tweens:
            self._slow = False
            self._job = self.root.after(15, self._tick)
        elif self._loops:
            # Ambient motion (radar, pulses) needs far fewer frames, and even
            # fewer while TiDY isn't the active window - keeps the CPU calm.
            self._set_hires(False)
            if hidden:
                delay = 400
            else:
                try:
                    focused = self.root.focus_displayof() is not None
                except (tk.TclError, KeyError):
                    focused = True
                delay = 50 if focused else 100
            self._slow = True
            self._job = self.root.after(delay, self._tick)
        else:
            self._set_hires(False)

    def _frame(self):
        now = time.perf_counter()
        for key, tw in list(self._tweens.items()):
            start, duration, fn, ease, done = tw
            if now < start:
                continue
            t = min(1.0, (now - start) / duration)
            try:
                fn(ease(t))
            except tk.TclError:
                t, done = 1.0, None
            except Exception:
                log_exception("animation")
                t, done = 1.0, None
            if t >= 1.0:
                if self._tweens.get(key) is tw:
                    del self._tweens[key]
                if done:
                    try:
                        done()
                    except Exception:
                        log_exception("animation done")
        hidden = self.root.state() in ("withdrawn", "iconic")
        if not hidden:
            for key, fn in list(self._loops.items()):
                try:
                    keep = fn(now)
                except tk.TclError:
                    keep = False
                except Exception:
                    log_exception("loop")
                    keep = False
                if keep is False:
                    self._loops.pop(key, None)
        return hidden


# =============================================================================
#  A tiny anti-aliased vector renderer (signed distance fields -> PNG)
#  Tk can't anti-alias shapes, so TiDY draws its own crisp graphics.
# =============================================================================

def png_bytes(w, h, rgba):
    stride = w * 4
    mv = memoryview(rgba)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += mv[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def ico_bytes(pngs):
    """Windows .ico file holding PNG images: pngs = [(size, png_bytes), ...]."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in pngs:
        b = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


# ---- distance functions (negative = inside) ---------------------------------

def sd_circle(cx, cy, r):
    def f(x, y):
        return math.hypot(x - cx, y - cy) - r
    return f


def sd_rrect(x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    hx, hy = (x1 - x0) / 2.0 - r, (y1 - y0) / 2.0 - r

    def f(x, y):
        qx = abs(x - cx) - hx
        qy = abs(y - cy) - hy
        if qx > 0 and qy > 0:
            return math.hypot(qx, qy) - r
        return (qx if qx > qy else qy) - r
    return f


def sd_superellipse(cx, cy, a, n=5.0):
    inv = 1.0 / n

    def f(x, y):
        u = abs(x - cx) / a
        v = abs(y - cy) / a
        return ((u ** n + v ** n) ** inv - 1.0) * a
    return f


def sd_segment(ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy or 1e-9

    def f(x, y):
        t = ((x - ax) * dx + (y - ay) * dy) / l2
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
        return math.hypot(x - ax - t * dx, y - ay - t * dy)
    return f


def sd_polygon(pts):
    n = len(pts)

    def f(x, y):
        d = (x - pts[0][0]) ** 2 + (y - pts[0][1]) ** 2
        s = 1.0
        j = n - 1
        for i in range(n):
            xi, yi = pts[i]
            xj, yj = pts[j]
            ex, ey = xj - xi, yj - yi
            wx, wy = x - xi, y - yi
            l2 = ex * ex + ey * ey or 1e-9
            t = (wx * ex + wy * ey) / l2
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            bx, by = wx - ex * t, wy - ey * t
            dd = bx * bx + by * by
            if dd < d:
                d = dd
            c1, c2, c3 = y >= yi, y < yj, ex * wy > ey * wx
            if (c1 and c2 and c3) or (not c1 and not c2 and not c3):
                s = -s
            j = i
        return s * math.sqrt(d)
    return f


def sd_arc(cx, cy, r, start, sweep):
    """Distance to a circular arc; angles in degrees, clockwise on screen."""
    a0, a1 = math.radians(start), math.radians(start + sweep)
    p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
    p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))

    def f(x, y):
        ang = math.degrees(math.atan2(y - cy, x - cx))
        if (ang - start) % 360.0 <= sweep:
            return abs(math.hypot(x - cx, y - cy) - r)
        return min(math.hypot(x - p0[0], y - p0[1]), math.hypot(x - p1[0], y - p1[1]))
    return f


def sparkle_points(cx, cy, radius, k=0.14, n=40):
    """A four-pointed sparkle with softly curved sides (a rounded astroid)."""
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + radius * (k * c + (1 - k) * c * c * c),
                    cy + radius * (k * s + (1 - k) * s * s * s)))
    return pts


def rotated(f, cx, cy, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))

    def g(x, y):
        dx, dy = x - cx, y - cy
        return f(cx + dx * c + dy * s, cy - dx * s + dy * c)
    return g


def union(*fs):
    def g(x, y):
        best = fs[0](x, y)
        for f in fs[1:]:
            d = f(x, y)
            if d < best:
                best = d
        return best
    return g


def linear_gradient(x0, y0, x1, y1, stops):
    cols = [(t, hex01(c)) for t, c in stops]
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy or 1e-9

    def f(u, v):
        t = clamp(((u - x0) * dx + (v - y0) * dy) / l2)
        for i in range(1, len(cols)):
            if t <= cols[i][0]:
                t0, c0 = cols[i - 1]
                t1, c1 = cols[i]
                k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return (c0[0] + (c1[0] - c0[0]) * k, c0[1] + (c1[1] - c0[1]) * k,
                        c0[2] + (c1[2] - c0[2]) * k)
        return cols[-1][1]
    return f


class Painter:
    """Layered RGBA canvas (premultiplied floats) for multi-colour artwork."""

    def __init__(self, size, design=100.0):
        self.n = size
        self.s = size / float(design)
        self.buf = [0.0] * (size * size * 4)

    def fill(self, sdf, color, alpha=1.0, bbox=None):
        n, s, buf = self.n, self.s, self.buf
        if bbox:
            x0 = max(0, int(bbox[0] * s) - 1)
            y0 = max(0, int(bbox[1] * s) - 1)
            x1 = min(n, int(math.ceil(bbox[2] * s)) + 1)
            y1 = min(n, int(math.ceil(bbox[3] * s)) + 1)
        else:
            x0 = y0 = 0
            x1 = y1 = n
        const_col = not callable(color)
        const_a = not callable(alpha)
        if const_col:
            cr, cg, cb = color
        for y in range(y0, y1):
            v = (y + 0.5) / s
            row = y * n
            for x in range(x0, x1):
                u = (x + 0.5) / s
                d = sdf(u, v) * s
                if d >= 0.5:
                    continue
                cov = 1.0 if d <= -0.5 else 0.5 - d
                a = cov * (alpha if const_a else alpha(u, v))
                if a <= 0.0:
                    continue
                if not const_col:
                    cr, cg, cb = color(u, v)
                i = (row + x) * 4
                ia = 1.0 - a
                buf[i] = cr * a + buf[i] * ia
                buf[i + 1] = cg * a + buf[i + 1] * ia
                buf[i + 2] = cb * a + buf[i + 2] * ia
                buf[i + 3] = a + buf[i + 3] * ia

    def rgba(self):
        buf = self.buf
        out = bytearray(len(buf))
        for i in range(0, len(buf), 4):
            a = buf[i + 3]
            if a <= 0.002:
                continue
            out[i] = int(clamp(buf[i] / a) * 255 + 0.5)
            out[i + 1] = int(clamp(buf[i + 1] / a) * 255 + 0.5)
            out[i + 2] = int(clamp(buf[i + 2] / a) * 255 + 0.5)
            out[i + 3] = int(clamp(a) * 255 + 0.5)
        return bytes(out)


def render_logo_rgba(size):
    """The TiDY logo: a friendly folder full of colourful files, on a
    violet-to-sky squircle, with a little sparkle of tidiness."""
    p = Painter(size, 100.0)
    white = (1.0, 1.0, 1.0)
    bg = sd_superellipse(50, 50, 47, 5.0)
    p.fill(bg, linear_gradient(10, 4, 90, 96, [(0, "#B47CFF"), (0.52, "#6C63FF"), (1, "#2CC5F2")]))
    p.fill(bg, white, alpha=lambda u, v: 0.22 * clamp(1 - v / 60.0) ** 1.5)  # glassy light
    back = union(sd_rrect(21, 31, 79, 77, 7), sd_rrect(21, 26, 47, 38, 5))
    p.fill(back, white, alpha=0.36, bbox=(19, 24, 81, 79))
    for cx, cy, deg, col in ((37.5, 45.5, -13, "#FFD166"), (50.5, 40.0, 3, "#FF8FB8"),
                             (62.5, 45.5, 16, "#6EF0C8")):
        paper = rotated(sd_rrect(cx - 11, cy - 15, cx + 11, cy + 15, 3.4), cx, cy, deg)
        p.fill(paper, hex01(col), bbox=(cx - 20, cy - 20, cx + 20, cy + 20))
    p.fill(sd_rrect(15, 50, 85, 83, 9),
           linear_gradient(0, 50, 0, 83, [(0, "#FFFFFF"), (1, "#E8E6FF")]), bbox=(13, 48, 87, 85))
    ink = hex01("#6259F0")
    p.fill(sd_circle(41.5, 62.5, 2.7), ink, bbox=(37, 58, 46, 67))
    p.fill(sd_circle(58.5, 62.5, 2.7), ink, bbox=(54, 58, 63, 67))
    smile = sd_arc(50, 65.0, 6.6, 28, 124)
    p.fill(lambda x, y: smile(x, y) - 1.75, ink, bbox=(40, 63, 60, 75))
    p.fill(sd_polygon(sparkle_points(79.5, 20.5, 11.5)), white, bbox=(67, 8, 92, 33))
    p.fill(sd_polygon(sparkle_points(89.0, 35.5, 4.6)), white, alpha=0.9, bbox=(83, 30, 95, 41))
    p.fill(sd_circle(68.5, 12.5, 1.8), white, alpha=0.75, bbox=(65, 9, 72, 16))
    return p.rgba()


# ---- line icons (24 x 24 design grid, round caps, like modern icon sets) ----

def _star_points(cx, cy, R, r):
    pts = []
    for i in range(10):
        a = math.radians(-90 + i * 36)
        rad = R if i % 2 == 0 else r
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def _rays(cx, cy, r0, r1, count=8, start=0.0):
    out = []
    for k in range(count):
        a = math.radians(start + k * 360.0 / count)
        out.append(("l", cx + r0 * math.cos(a), cy + r0 * math.sin(a),
                    cx + r1 * math.cos(a), cy + r1 * math.sin(a)))
    return out


ICONS = {
    "home": [("pl", [(3.5, 11), (12, 4), (20.5, 11)]), ("pl", [(5.5, 9.5), (5.5, 20), (18.5, 20), (18.5, 9.5)]),
             ("pl", [(10, 20), (10, 14.5), (14, 14.5), (14, 20)])],
    "activity": [("c", 12, 12, 9), ("pl", [(12, 7.2), (12, 12), (15.3, 14.2)])],
    "rules": [("l", 4, 7, 20, 7), ("l", 4, 12, 20, 12), ("l", 4, 17, 20, 17),
              ("fc", 9, 7, 2.7), ("fc", 15.5, 12, 2.7), ("fc", 8, 17, 2.7)],
    "settings": [("c", 12, 12, 3.2), ("c", 12, 12, 7.2)] + [
        ("l3",) + r[1:] for r in _rays(12, 12, 7.6, 9.9, 8, 22.5)],
    "help": [("c", 12, 12, 9.5), ("a", 12, 9.7, 2.7, 180, 235),
             ("pl", [(13.55, 11.9), (12, 13.1), (12, 14.2)]), ("fc", 12, 17.2, 1.3)],
    "info": [("c", 12, 12, 9.5), ("l", 12, 11, 12, 16.5), ("fc", 12, 7.8, 1.3)],
    "folder": [("pg", [(3.5, 6.5), (9.2, 6.5), (11.2, 8.6), (20.5, 8.6), (20.5, 18.5), (3.5, 18.5)])],
    "play": [("fp", [(9.6, 7.4), (9.6, 16.6), (17.2, 12)], 1.4)],
    "pause": [("frr", 7, 5.5, 10.6, 18.5, 1.4), ("frr", 13.4, 5.5, 17, 18.5, 1.4)],
    "undo": [("pl", [(9, 14), (4, 9), (9, 4)]), ("l", 4, 9, 14.5, 9), ("a", 14.5, 14.5, 5.5, 270, 180),
             ("l", 14.5, 20, 10.5, 20)],
    "plus": [("l", 12, 5, 12, 19), ("l", 5, 12, 19, 12)],
    "x": [("l", 6.5, 6.5, 17.5, 17.5), ("l", 17.5, 6.5, 6.5, 17.5)],
    "check": [("pl", [(5, 12.5), (10, 17.5), (19, 7)])],
    "trash": [("l", 4, 6.5, 20, 6.5), ("pl", [(9, 6.5), (9, 4), (15, 4), (15, 6.5)]),
              ("pl", [(6, 6.5), (7, 20), (17, 20), (18, 6.5)])],
    "edit": [("pg", [(15.5, 4.5), (19.5, 8.5), (8.5, 19.5), (4.5, 19.5), (4.5, 15.5)]), ("l", 13, 7, 17, 11)],
    "sparkle": [("sp", 10.5, 13, 8.6), ("sp", 19, 5, 3.4)],
    "wand": [("l", 4.5, 19.5, 13.5, 10.5), ("sp", 17.4, 6.6, 4.4), ("sp", 9, 4.8, 2.4), ("sp", 19.8, 14.2, 2.4)],
    "sun": [("c", 12, 12, 4)] + _rays(12, 12, 7, 9.4),
    "moon": [("cres", 12.5, 12, 8.3, 17.2, 7.6, 6.8)],
    "monitor": [("rr", 3, 4.5, 21, 16, 2), ("l", 12, 16, 12, 19.5), ("l", 8, 19.5, 16, 19.5)],
    "chev_r": [("pl", [(9.5, 6), (15.5, 12), (9.5, 18)])],
    "chev_l": [("pl", [(14.5, 6), (8.5, 12), (14.5, 18)])],
    "arrow_r": [("l", 5, 12, 19, 12), ("pl", [(13, 6), (19, 12), (13, 18)])],
    "download": [("l", 12, 4, 12, 15), ("pl", [(7, 10), (12, 15), (17, 10)]),
                 ("pl", [(4, 16), (4, 20), (20, 20), (20, 16)])],
    "shield": [("pg", [(12, 3), (19.5, 5.8), (19.5, 11.5), (17.5, 16.5), (12, 21), (6.5, 16.5), (4.5, 11.5),
                       (4.5, 5.8)]), ("pl", [(8.8, 12), (11.2, 14.4), (15.4, 10)])],
    "search": [("c", 10.5, 10.5, 6.5), ("l", 15.5, 15.5, 20, 20)],
    "power": [("a", 12, 13, 7.5, 300, 300), ("l", 12, 3.5, 12, 11)],
    # categories
    "image": [("rr", 3.5, 4.5, 20.5, 19.5, 3), ("fc", 9, 9.8, 1.9),
              ("pl", [(4, 17.6), (9.6, 12.6), (13.2, 15.8), (15.6, 13.8), (20, 17.6)])],
    "video": [("rr", 3, 5.5, 21, 18.5, 3.2), ("fp", [(10.6, 9.8), (10.6, 14.2), (14.4, 12)], 0.9)],
    "music": [("pl", [(9, 17.5), (9, 5.5), (20, 3.5), (20, 15.5)]), ("c", 6.5, 17.5, 2.6), ("c", 17.5, 15.5, 2.6)],
    "doc": [("pg", [(6, 3), (14, 3), (19, 8), (19, 21), (6, 21)]), ("pl", [(14, 3), (14, 8), (19, 8)]),
            ("l", 9, 13, 16, 13), ("l", 9, 17, 14, 17)],
    "archive": [("rr", 3, 3.5, 21, 8.5, 1.6),
                ("pl", [(4.6, 8.5), (4.6, 18.5), (6.5, 20.5), (17.5, 20.5), (19.4, 18.5), (19.4, 8.5)]),
                ("l", 10, 12.5, 14, 12.5)],
    "app": [("rr", 2.8, 4, 21.2, 20, 2.5), ("l", 2.8, 8.8, 21.2, 8.8), ("fc", 5.9, 6.4, 0.95),
            ("fc", 8.7, 6.4, 0.95)],
    "code": [("pl", [(8.5, 7), (3.5, 12), (8.5, 17)]), ("pl", [(15.5, 7), (20.5, 12), (15.5, 17)]),
             ("l", 13.6, 4.8, 10.4, 19.2)],
    "other": [("c", 7.5, 7.5, 3.6), ("rr", 13.4, 3.9, 20.6, 11.1, 1.6),
              ("pg", [(7.5, 13.6), (11.4, 20.3), (3.6, 20.3)]), ("c", 17, 16.8, 3.6)],
    "font": [("pl", [(4, 7.5), (4, 4.5), (20, 4.5), (20, 7.5)]), ("l", 12, 4.5, 12, 19.5), ("l", 9, 19.5, 15, 19.5)],
    "book": [("pg", [(3.5, 5), (9.5, 5), (12, 7.5), (12, 20), (9.5, 18), (3.5, 18)]),
             ("pg", [(20.5, 5), (14.5, 5), (12, 7.5), (12, 20), (14.5, 18), (20.5, 18)])],
    "star": [("pg", _star_points(12, 12.8, 9.6, 4.3))],
    "keyboard": [("rr", 2.5, 6, 21.5, 18, 2.5), ("fc", 6.6, 10, 1), ("fc", 10.2, 10, 1), ("fc", 13.8, 10, 1),
                 ("fc", 17.4, 10, 1), ("l", 8, 14.2, 16, 14.2)],
}
RULE_ICONS = ["folder", "image", "video", "music", "doc", "archive", "app", "code", "font", "book", "star", "other"]


def icon_sdf(name, stroke=2.0):
    hw = stroke / 2.0
    parts = []
    for item in ICONS.get(name, ICONS["other"]):
        kind = item[0]
        if kind in ("l", "l3"):
            f = sd_segment(*item[1:5])
            w = 1.5 if kind == "l3" else hw
            parts.append(lambda x, y, f=f, w=w: f(x, y) - w)
        elif kind in ("pl", "pg"):
            pts = list(item[1])
            if kind == "pg":
                pts.append(pts[0])
            segs = [sd_segment(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]
            parts.append(lambda x, y, segs=segs: min(g(x, y) for g in segs) - hw)
        elif kind == "c":
            f = sd_circle(*item[1:4])
            parts.append(lambda x, y, f=f: abs(f(x, y)) - hw)
        elif kind == "fc":
            parts.append(sd_circle(*item[1:4]))
        elif kind == "rr":
            f = sd_rrect(*item[1:6])
            parts.append(lambda x, y, f=f: abs(f(x, y)) - hw)
        elif kind == "frr":
            parts.append(sd_rrect(*item[1:6]))
        elif kind == "a":
            f = sd_arc(*item[1:6])
            parts.append(lambda x, y, f=f: f(x, y) - hw)
        elif kind == "fp":
            f = sd_polygon(item[1])
            rr = item[2]
            parts.append(lambda x, y, f=f, rr=rr: f(x, y) - rr)
        elif kind == "sp":
            parts.append(sd_polygon(sparkle_points(item[1], item[2], item[3])))
        elif kind == "cres":
            a = sd_circle(item[1], item[2], item[3])
            b = sd_circle(item[4], item[5], item[6])
            parts.append(lambda x, y, a=a, b=b: abs(max(a(x, y), -b(x, y))) - hw)
    return union(*parts)


def render_mask(sdf, w, h, scale):
    """Coverage (0..255) of a shape, one byte per pixel, analytically anti-aliased."""
    out = bytearray(w * h)
    for y in range(h):
        v = (y + 0.5) / scale
        row = y * w
        for x in range(w):
            d = sdf((x + 0.5) / scale, v) * scale
            if d < 0.5:
                out[row + x] = 255 if d <= -0.5 else int((0.5 - d) * 255 + 0.5)
    return out


def colorize(mask, color, alpha=1.0):
    r, g, b = hex_rgb(color)
    out = bytearray(bytes((r, g, b, 0)) * len(mask))
    out[3::4] = mask if alpha >= 1.0 else bytes(int(a * alpha + 0.5) for a in mask)
    return out


def blend_over(dst, dw, src, sw, sh, ox, oy):
    """Alpha-composite RGBA `src` onto RGBA `dst` at (ox, oy)."""
    for y in range(sh):
        dy = y + oy
        for x in range(sw):
            si = (y * sw + x) * 4
            sa = src[si + 3]
            if sa == 0:
                continue
            di = (dy * dw + x + ox) * 4
            da = dst[di + 3]
            if sa == 255 or da == 0:
                dst[di:di + 4] = src[si:si + 4]
                continue
            a = sa / 255.0
            b = da / 255.0 * (1 - a)
            oa = a + b
            dst[di] = int((src[si] * a + dst[di] * b) / oa + 0.5)
            dst[di + 1] = int((src[si + 1] * a + dst[di + 1] * b) / oa + 0.5)
            dst[di + 2] = int((src[si + 2] * a + dst[di + 2] * b) / oa + 0.5)
            dst[di + 3] = int(oa * 255 + 0.5)


def rrect_rgba(w, h, r, fill, border=None, fill2=None, alpha=1.0, bw=1):
    """Fast anti-aliased rounded rectangle. Only the corners are computed
    pixel by pixel; straight parts are built from repeated byte runs."""
    w, h = max(1, int(w)), max(1, int(h))
    r = max(0.0, min(float(r), w / 2.0, h / 2.0))
    bw = int(bw) if border else 0
    r = max(r, float(bw))
    F0 = hex_rgb(fill)
    F1 = hex_rgb(fill2) if fill2 else F0
    B = hex_rgb(border) if border else F0
    A = int(round(clamp(alpha) * 255))
    outer = sd_rrect(0, 0, w, h, r)
    inner = sd_rrect(bw, bw, w - bw, h - bw, max(0.0, r - bw)) if bw else None
    rc = min(int(math.ceil(r)), w // 2)
    full = w < 2 * rc + 2
    bpx = bytes((B[0], B[1], B[2], A))
    rows = []
    straight_cache = {}
    for y in range(h):
        t = y / (h - 1.0) if h > 1 else 0.0
        fc = F0 if F1 == F0 else (F0[0] + (F1[0] - F0[0]) * t, F0[1] + (F1[1] - F0[1]) * t,
                                  F0[2] + (F1[2] - F0[2]) * t)
        fpx = bytes((int(fc[0] + 0.5), int(fc[1] + 0.5), int(fc[2] + 0.5), A))
        cy = y + 0.5
        if r <= cy <= h - r and not full:
            row = straight_cache.get(fpx)
            if row is None:
                row = (bpx * bw + fpx * (w - 2 * bw) + bpx * bw) if bw else fpx * w
                straight_cache[fpx] = row
            rows.append(row)
            continue
        span = w if full else rc
        part = bytearray()
        for x in range(span):
            cx = x + 0.5
            co = clamp(0.5 - outer(cx, cy))
            if co <= 0:
                part += b"\0\0\0\0"
                continue
            if bw:
                ci = clamp(0.5 - inner(cx, cy))
                k = ci / co
                col = (fc[0] * k + B[0] * (1 - k), fc[1] * k + B[1] * (1 - k), fc[2] * k + B[2] * (1 - k))
            else:
                col = fc
            part += bytes((int(col[0] + 0.5), int(col[1] + 0.5), int(col[2] + 0.5), int(A * co + 0.5)))
        if full:
            rows.append(bytes(part))
            continue
        mid = bpx if (bw and (y < bw or y >= h - bw)) else fpx
        right = bytearray()
        for i in range(rc - 1, -1, -1):
            right += part[i * 4:i * 4 + 4]
        rows.append(bytes(part) + mid * (w - 2 * rc) + bytes(right))
    return b"".join(rows)


def shadow_rgba(w, h, r, blur, color, alpha):
    """A soft drop shadow for a w x h rounded card; the image is larger by
    `m` pixels on every side (m = 2 * blur)."""
    m = int(blur * 2)
    W, H = w + 2 * m, h + 2 * m
    f = sd_rrect(m, m, m + w, m + h, r)
    R, G, B = hex_rgb(color)

    def a_at(d):
        t = clamp((d + blur * 0.6) / (blur * 2.0))
        t = t * t * (3 - 2 * t)
        return int(alpha * (1 - t) * 255 + 0.5)

    zone = min(W // 2, int(m + r + blur) + 2)
    rows = []
    band_cache = None
    for y in range(H):
        cy = y + 0.5
        in_band = m + r + blur * 1.5 < cy < H - (m + r + blur * 1.5)
        if in_band and band_cache is not None:
            rows.append(band_cache)
            continue
        part = bytearray()
        for x in range(zone):
            part += bytes((R, G, B, a_at(f(x + 0.5, cy))))
        mid = bytes((R, G, B, a_at(f(W / 2.0, cy))))
        right = bytearray()
        for i in range(zone - 1, -1, -1):
            right += part[i * 4:i * 4 + 4]
        row = bytes(part) + mid * (W - 2 * zone) + bytes(right)
        if in_band:
            band_cache = row
        rows.append(row)
    return W, H, b"".join(rows), m


class Gfx:
    """Creates (and caches) every image the interface uses."""

    def __init__(self, app):
        self.app = app
        self.cache = {}

    def clear(self):
        self.cache.clear()

    def _photo(self, w, h, rgba):
        data = base64.b64encode(png_bytes(w, h, rgba)).decode("ascii")
        return tk.PhotoImage(master=self.app.root, data=data, format="png")

    def get(self, key, build):
        img = self.cache.get(key)
        if img is None:
            img = build()
            self.cache[key] = img
        return img

    def rrect(self, w, h, r, fill, border=None, fill2=None, alpha=1.0):
        w, h = int(round(w)), int(round(h))
        key = ("rr", w, h, round(r, 1), fill, border, fill2, round(alpha, 3))
        return self.get(key, lambda: self._photo(w, h, rrect_rgba(w, h, r, fill, border, fill2, alpha,
                                                                   bw=max(1, int(self.app.S)))))

    def card_bands(self, w, h, r, fill, border=None, fill2=None):
        """Top and bottom strips of a rounded card (only they need anti-aliasing)."""
        key = ("bands", w, h, r, fill, border, fill2)

        def build():
            rb = min(int(math.ceil(r)), h // 2)
            rgba = rrect_rgba(w, h, r, fill, border, fill2, bw=max(1, int(self.app.S)))
            stride = w * 4
            return (self._photo(w, rb, rgba[:rb * stride]), self._photo(w, rb, rgba[(h - rb) * stride:]))
        return self.get(key, build)

    def shadow_pieces(self, w, h, r, blur, color, alpha, dy, rb):
        """A soft shadow cut into four pieces that surround the card, so nothing
        semi-transparent sits underneath the card's middle."""
        key = ("shp", w, h, r, blur, color, alpha, dy, rb)

        def build():
            W, H, rgba, m = shadow_rgba(w, h, r, blur, color, alpha)
            top, bottom = m - dy + rb, m - dy + h - rb

            def crop(x0, y0, x1, y1):
                if x1 <= x0 or y1 <= y0:
                    return None
                rows = [rgba[(yy * W + x0) * 4:(yy * W + x1) * 4] for yy in range(y0, y1)]
                return self._photo(x1 - x0, y1 - y0, b"".join(rows))
            pieces = [(crop(0, 0, W, top), -m, -m + dy),
                      (crop(0, bottom, W, H), -m, -m + dy + bottom),
                      (crop(0, top, m, bottom), -m, rb),
                      (crop(m + w, top, W, bottom), w, rb)]
            return [p for p in pieces if p[0] is not None]
        return self.get(key, build)

    def shadow(self, w, h, r, blur, color, alpha):
        w, h = int(round(w)), int(round(h))
        key = ("sh", w, h, r, blur, color, alpha)

        def build():
            W, H, rgba, m = shadow_rgba(w, h, r, blur, color, alpha)
            img = self._photo(W, H, rgba)
            img.margin = m
            return img
        return self.get(key, build)

    def mask(self, name, size, stroke=2.0):
        key = ("mask", name, size, stroke)
        m = self.cache.get(key)
        if m is None:
            m = render_mask(icon_sdf(name, stroke), size, size, size / 24.0)
            self.cache[key] = m
        return m

    def icon(self, name, size, color, stroke=2.0):
        size = int(round(size))
        key = ("ic", name, size, color, stroke)
        return self.get(key, lambda: self._photo(size, size, colorize(self.mask(name, size, stroke), color)))

    def badge(self, name, size, glyph, tint, radius=None, glyph_size=None, alpha=1.0):
        size = int(round(size))
        key = ("bd", name, size, glyph, tint, radius, glyph_size, alpha)

        def build():
            rad = size * 0.3 if radius is None else radius
            base = bytearray(rrect_rgba(size, size, rad, tint, alpha=alpha))
            gs = int(round(glyph_size or size * 0.54))
            mark = colorize(self.mask(name, gs), glyph)
            off = (size - gs) // 2
            blend_over(base, size, mark, gs, gs, off, off)
            return self._photo(size, size, base)
        return self.get(key, build)

    def core(self, d, top, bottom, icon, icon_color, bg, border=None):
        """A round badge with an icon, pre-blended onto a solid background so the
        result is fully opaque (opaque images are much cheaper to redraw)."""
        d = int(round(d))
        key = ("core", d, top, bottom, icon, icon_color, bg, border)

        def build():
            disc = bytearray(rrect_rgba(d, d, d / 2.0, top, border, bottom, bw=max(1, int(self.app.S))))
            gs = int(round(d * 0.44))
            off = (d - gs) // 2
            blend_over(disc, d, colorize(self.mask(icon, gs, 1.8), icon_color), gs, gs, off, off)
            r, g, b = hex_rgb(bg)
            flat = bytearray(bytes((r, g, b, 255)) * (d * d))
            blend_over(flat, d, disc, d, d, 0, 0)
            return self._photo(d, d, flat)
        return self.get(key, build)

    def disc(self, d, fill, border=None):
        d = int(round(d))
        return self.rrect(d, d, d / 2.0, fill, border)

    def overlay(self, w, h, color, alpha):
        w, h = int(w), int(h)
        key = ("ov", w, h, color, alpha)
        r, g, b = hex_rgb(color)
        return self.get(key, lambda: self._photo(w, h, bytes((r, g, b, int(alpha * 255))) * (w * h)))

    def fade(self, w, h, color, downwards=True):
        """Vertical strip fading from solid `color` to transparent."""
        w, h = int(w), int(h)
        key = ("fd", w, h, color, downwards)

        def build():
            r, g, b = hex_rgb(color)
            rows = []
            for y in range(h):
                t = y / max(1.0, h - 1.0)
                a = (1 - t) if downwards else t
                a = a * a * (3 - 2 * a)
                rows.append(bytes((r, g, b, int(a * 255))) * w)
            return self._photo(w, h, b"".join(rows))
        return self.get(key, build)

    def logo(self, size):
        size = int(round(size))
        key = ("logo", size)

        def build():
            path = os.path.join(CACHE_DIR, "logo-v3-%d.png" % size)
            try:
                if os.path.exists(path):
                    return tk.PhotoImage(master=self.app.root, file=path)
            except tk.TclError:
                pass
            png = png_bytes(size, size, render_logo_rgba(size))
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(png)
            except OSError:
                pass
            return tk.PhotoImage(master=self.app.root, data=base64.b64encode(png).decode("ascii"),
                                 format="png")
        return self.get(key, build)

    def logo_png(self, size):
        path = os.path.join(CACHE_DIR, "logo-v3-%d.png" % size)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()
        png = png_bytes(size, size, render_logo_rgba(size))
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(png)
        except OSError:
            pass
        return png

# =============================================================================
#  Themes and fonts
# =============================================================================

class Palette(dict):
    __getattr__ = dict.__getitem__


THEMES = {
    "dark": Palette(
        dark=True, bg="#0F1117", side="#0B0C11", side_line="#171A22", card="#171A22", card2="#1D212B",
        line="#242935", line2="#2F3542", text="#ECEEF5", dim="#A3A9BA", faint="#6C7385",
        accent="#7C6CFF", accent_deep="#5B4BEE", accent_hi="#8F81FF", on_accent="#FFFFFF",
        accent_soft="#232046", accent_ink="#B4AAFF", good="#34D399", good_soft="#11291F",
        warn="#FBBF24", warn_soft="#2E2410", bad="#F87171", bad_soft="#341A1E", input="#12151C",
        hover="#232836", nav_active="#1D2030", toggle_off="#3A4050", knob="#FFFFFF",
        overlay="#04050A", overlay_a=0.66, shadow=None, glow="#2A2560"),
    "light": Palette(
        dark=False, bg="#F4F5F9", side="#FFFFFF", side_line="#E8EAF0", card="#FFFFFF", card2="#F7F8FB",
        line="#E7E9F0", line2="#D8DBE4", text="#151823", dim="#596075", faint="#9AA1B2",
        accent="#6A5CFF", accent_deep="#5141F0", accent_hi="#7D70FF", on_accent="#FFFFFF",
        accent_soft="#EEECFF", accent_ink="#5345E6", good="#0F9F6E", good_soft="#E2F6EE",
        warn="#C2700A", warn_soft="#FDF3E2", bad="#E5484D", bad_soft="#FDECEC", input="#F7F8FB",
        hover="#F0F1F6", nav_active="#F0EEFF", toggle_off="#D3D6E0", knob="#FFFFFF",
        overlay="#141726", overlay_a=0.42, shadow="#1B2240", glow="#E4E0FF"),
}


class Fonts:
    def __init__(self, app):
        S = app.S
        try:
            fams = set(tkfont.families(app.root))
        except tk.TclError:
            fams = set()

        def pick(*names):
            for n in names:
                if n in fams:
                    return n
            return None

        regular = pick("Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue",
                       "Ubuntu", "Cantarell", "DejaVu Sans") or "TkDefaultFont"
        display = pick("Segoe UI Variable Display", "Segoe UI", "Inter", "SF Pro Display",
                       "Helvetica Neue", "Ubuntu", "DejaVu Sans") or regular
        semi = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold")
        dsemi = pick("Segoe UI Variable Display Semib", "Segoe UI Semibold")
        heading = (dsemi, "normal") if dsemi else (display, "bold")
        strong = (semi, "normal") if semi else (regular, "bold")
        plain = (regular, "normal")
        spec = {
            "splash": (heading, 46), "display": (heading, 27), "h1": (heading, 21), "brand": (heading, 21),
            "stat": (heading, 21), "h2": (strong, 15), "h3": (strong, 13.5), "body": (plain, 13),
            "body_b": (strong, 13), "btn": (strong, 13), "small": (plain, 11.5), "small_b": (strong, 11.5),
            "tiny": (strong, 10), "tour_title": (heading, 23), "tour_body": (plain, 13.5),
        }
        self.f = {}
        for key, ((family, weight), size) in spec.items():
            self.f[key] = tkfont.Font(root=app.root, family=family, size=-int(round(size * S)),
                                      weight=weight)

    def __getitem__(self, key):
        return self.f[key]

    def fit(self, key, text, width):
        """Shorten text with an ellipsis so it fits in `width` pixels."""
        font = self.f[key]
        if font.measure(text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid].rstrip() + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + "…"

    def fit_middle(self, key, text, width):
        """Shorten in the middle (good for paths and file names)."""
        font = self.f[key]
        if font.measure(text) <= width:
            return text
        keep_end = min(len(text) // 2, 14)
        tail = text[-keep_end:]
        head = text[:-keep_end]
        while head and font.measure(head + "…" + tail) > width:
            head = head[:-1]
        return (head + "…" + tail) if head else self.fit(key, text, width)


# =============================================================================
#  Hit testing (hover / click for everything drawn on the canvas)
# =============================================================================

LAYERS = {"page": 0, "side": 0, "toast": 1, "modal": 2, "tour": 3, "splash": 4}


class Hit:
    __slots__ = ("x0", "y0", "x1", "y1", "layer", "owner", "click", "enter", "leave", "press",
                 "release", "cursor", "scroll", "enabled", "drag")


class HitMap:
    def __init__(self, app):
        self.app = app
        self.cv = app.cv
        self.hits = []
        self.hover = None
        self.pressed = None
        self.block = 0
        self._cursor = ""
        self.press_xy = (0, 0)
        cv = self.cv
        cv.bind("<Motion>", lambda e: self.update(e.x, e.y))
        cv.bind("<Leave>", lambda e: self._set_hover(None) if self.pressed is None else None)
        cv.bind("<ButtonPress-1>", self._press)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<B1-Motion>", self._drag)

    def add(self, x0, y0, x1, y1, click=None, enter=None, leave=None, layer="page", owner=None,
            cursor="hand2", scroll=None, press=None, release=None, drag=None):
        h = Hit()
        h.x0, h.y0, h.x1, h.y1 = x0, y0, x1, y1
        h.layer, h.owner, h.click, h.enter, h.leave = layer, owner, click, enter, leave
        h.press, h.release, h.cursor, h.scroll, h.enabled = press, release, cursor, scroll, True
        h.drag = drag
        self.hits.append(h)
        return h

    def _drag(self, e):
        h = self.pressed
        if h is not None and h.drag:
            h.drag(e.x, e.y)

    def remove(self, hit):
        try:
            self.hits.remove(hit)
        except ValueError:
            pass
        if self.hover is hit:
            self.hover = None
            self._apply_cursor("")
        if self.pressed is hit:
            self.pressed = None

    def remove_owner(self, owner):
        self.hits = [h for h in self.hits if h.owner is not owner]
        if self.hover is not None and self.hover.owner is owner:
            self.hover = None
            self._apply_cursor("")
        if self.pressed is not None and self.pressed.owner is owner:
            self.pressed = None

    def find(self, x, y):
        best, rank = None, -1
        for h in reversed(self.hits):
            if not h.enabled:
                continue
            r = LAYERS[h.layer]
            if r < self.block or r <= rank:
                continue
            dy = h.scroll.offset if h.scroll is not None else 0
            if h.x0 <= x < h.x1 and h.y0 - dy <= y < h.y1 - dy:
                if h.scroll is not None and not h.scroll.contains(x, y):
                    continue
                best, rank = h, r
        return best

    def _apply_cursor(self, cursor):
        if cursor != self._cursor:
            self._cursor = cursor
            try:
                self.cv.configure(cursor=cursor)
            except tk.TclError:
                pass

    def _set_hover(self, h):
        if h is self.hover:
            return
        old, self.hover = self.hover, h
        if old is not None and old.leave:
            old.leave()
        if h is not None and h.enter:
            h.enter()
        self._apply_cursor(h.cursor if h is not None else "")

    def update(self, x, y):
        self._set_hover(self.find(x, y))

    def refresh(self):
        try:
            x = self.cv.winfo_pointerx() - self.cv.winfo_rootx()
            y = self.cv.winfo_pointery() - self.cv.winfo_rooty()
        except tk.TclError:
            return
        if 0 <= x < self.cv.winfo_width() and 0 <= y < self.cv.winfo_height():
            self.update(x, y)
        else:
            self._set_hover(None)

    def _press(self, e):
        h = self.find(e.x, e.y)
        self.pressed = h
        self.press_xy = (e.x, e.y)
        if h is not None and h.press:
            h.press()

    def _release(self, e):
        h, self.pressed = self.pressed, None
        if h is None:
            return
        if h.release:
            h.release()
        if h in self.hits and self.find(e.x, e.y) is h and h.click:
            h.click()


# =============================================================================
#  Canvas widgets
# =============================================================================

_uid = [0]


def new_tag(prefix="w"):
    _uid[0] += 1
    return "%s%d" % (prefix, _uid[0])


class Card:
    """A rounded card drawn in pieces: smooth (anti-aliased) images only for the
    rounded top and bottom strips, plain rectangles for the middle.  Anything
    animated on top of a card then stays very cheap for Tk to redraw."""

    def __init__(self, app, x, y, w, h, r, fill, border=None, tags=(), shadow=False, fill2=None,
                 shadow_alpha=0.075, blur=None):
        cv, P = app.cv, app.px
        self.app, self.cv = app, cv
        x, y, w, h = int(round(x)), int(round(y)), int(round(w)), int(round(h))
        self.x, self.y, self.w, self.h, self.r = x, y, w, h, r
        self.fill, self.fill2, self.border = fill, fill2, border
        rb = self.rb = min(int(math.ceil(r)), h // 2)
        if shadow and app.c.shadow:
            for img, dx, dy in app.gfx.shadow_pieces(w, h, r, blur or P(14), app.c.shadow, shadow_alpha, P(5), rb):
                cv.create_image(x + dx, y + dy, anchor="nw", image=img, tags=tags)
        top, bottom = app.gfx.card_bands(w, h, r, fill, border, fill2)
        self.top = cv.create_image(x, y, anchor="nw", image=top, tags=tags)
        self.bottom = cv.create_image(x, y + h - rb, anchor="nw", image=bottom, tags=tags)
        y0, y1 = y + rb, y + h - rb
        if y1 > y0:
            if fill2:  # vertical gradient, drawn as thin stripes
                step = max(2, int(app.S * 2))
                for sy in range(y0, y1, step):
                    t = (sy - y) / max(1.0, h - 1.0)
                    cv.create_rectangle(x, sy, x + w, min(sy + step, y1), fill=mix(fill, fill2, t), outline="",
                                        width=0, tags=tags)
            else:
                cv.create_rectangle(x, y0, x + w, y1, fill=fill, outline="", width=0, tags=tags)
        self.sides = []
        if border and y1 > y0:
            bw = max(1, int(app.S))
            for sx in (x, x + w - bw):
                self.sides.append(cv.create_rectangle(sx, y0, sx + bw, y1, fill=border, outline="", width=0,
                                                      tags=tags))

    def set_border(self, color):
        top, bottom = self.app.gfx.card_bands(self.w, self.h, self.r, self.fill, color, self.fill2)
        self.cv.itemconfigure(self.top, image=top)
        self.cv.itemconfigure(self.bottom, image=bottom)
        for item in self.sides:
            self.cv.itemconfigure(item, fill=color)


class Button:
    """A rounded button with a smooth hover glow and a tiny press dip."""

    def __init__(self, app, x, y, text="", command=None, kind="primary", icon=None, w=None, h=38,
                 layer="page", owner=None, tags=(), scroll=None, surface=None, anchor="nw",
                 radius=None, font="btn", tip=None, enabled=True):
        self.app, self.cv = app, app.cv
        c, P = app.c, app.px
        self.kind, self.command, self.layer, self.owner, self.scroll = kind, command, layer, owner, scroll
        self.text, self.icon_name, self.tip = text, icon, tip
        self.h = P(h)
        f = app.fonts[font]
        tw = f.measure(text) if text else 0
        isz = P(18) if icon else 0
        gap = P(8) if (icon and text) else 0
        pad = P(16) if text else (self.h - isz) // 2
        self.w = P(w) if w else tw + isz + gap + 2 * pad
        if anchor in ("ne", "e", "se"):
            x -= self.w
        elif anchor in ("n", "center", "s"):
            x -= self.w // 2
        if anchor in ("center", "e", "w"):
            y -= self.h // 2
        elif anchor in ("sw", "s", "se"):
            y -= self.h
        self.x, self.y = int(x), int(y)
        self.radius = P(radius) if radius is not None else self.h / 2.0
        surface = surface or c.bg
        self.surface = surface
        styles = {
            "primary": (c.accent_hi, c.accent_deep, None, mix(c.accent_hi, "#FFFFFF", 0.12),
                        mix(c.accent_deep, "#FFFFFF", 0.06), c.on_accent, c.on_accent),
            "secondary": (c.card2, None, c.line2, c.hover, None, c.text, c.text),
            "ghost": (surface, None, None, c.hover, None, c.dim, c.text),
            "soft": (c.accent_soft, None, None, mix(c.accent_soft, c.accent, 0.14), None, c.accent_ink,
                     c.accent_ink),
            "danger": (c.bad_soft, None, None, mix(c.bad_soft, c.bad, 0.16), None, c.bad, c.bad),
            "link": (surface, None, None, surface, None, c.accent_ink, mix(c.accent_ink, c.text, 0.35)),
        }
        (self.c0, self.c0b, self.border, self.c1, self.c1b, self.t0, self.t1) = styles[kind]
        self.tag = new_tag("btn")
        tg = (self.tag,) + tuple(tags)
        self.t = 0.0
        self.enabled = enabled
        self.bg = self.cv.create_image(self.x, self.y, anchor="nw", image=self._image(0.0), tags=tg)
        cx = self.x + self.w // 2
        cy = self.y + self.h // 2
        content = isz + gap + tw
        left = cx - content // 2
        self.icon_item = None
        if icon:
            self.icon_item = self.cv.create_image(left + isz // 2, cy, image=self._icon(self.t0), tags=tg)
        self.text_item = None
        if text:
            self.text_item = self.cv.create_text(left + isz + gap, cy, text=text, anchor="w", font=f,
                                                 fill=self.t0, tags=tg)
        self.hit = app.hits.add(self.x, self.y, self.x + self.w, self.y + self.h, click=self._click,
                                enter=lambda: self._hover(True), leave=lambda: self._hover(False),
                                press=self._press, release=self._release, layer=layer, owner=owner,
                                scroll=scroll)
        self._pressed = False
        self._tip_job = None
        if not enabled:
            self.set_enabled(False)

    def _image(self, t):
        q = round(t * 6) / 6.0
        fill = mix(self.c0, self.c1, q)
        fill2 = mix(self.c0b, self.c1b or self.c0b, q) if self.c0b else None
        if self.kind == "ghost" and q == 0:
            fill = self.surface
        if self.kind == "link":
            fill = self.surface
        return self.app.gfx.rrect(self.w, self.h, self.radius, fill, self.border, fill2)

    def _icon(self, color):
        return self.app.gfx.icon(self.icon_name, self.app.px(18), color)

    def _paint(self, t):
        self.t = t
        self.cv.itemconfigure(self.bg, image=self._image(t))
        col = mix(self.t0, self.t1, t) if self.enabled else self.app.c.faint
        if self.text_item:
            self.cv.itemconfigure(self.text_item, fill=col)
        if self.icon_item and (t in (0.0, 1.0) or self.t0 != self.t1):
            q = 1.0 if t > 0.5 else 0.0
            self.cv.itemconfigure(self.icon_item, image=self._icon(
                (self.t1 if q else self.t0) if self.enabled else self.app.c.faint))

    def _hover(self, on):
        if not self.enabled:
            return
        start, end = self.t, (1.0 if on else 0.0)
        self.app.anim.tween(0.16, lambda k: self._paint(start + (end - start) * k), key=(self.tag, "h"))
        if self.tip:
            if on:
                self._tip_job = self.cv.after(450, self._show_tip)
            else:
                self._hide_tip()

    def _show_tip(self):
        self._tip_job = None
        c, P = self.app.c, self.app.px
        f = self.app.fonts["small"]
        tw = f.measure(self.tip) + P(20)
        th = P(28)
        dy = self.scroll.offset if self.scroll else 0
        x = self.x + self.w // 2 - tw // 2
        x = max(P(8), min(x, self.cv.winfo_width() - tw - P(8)))
        y = self.y + self.h + P(6) - dy
        bg = "#202433" if c.dark else "#1E2130"
        self.cv.create_image(x, y, anchor="nw", image=self.app.gfx.rrect(tw, th, P(8), bg), tags=("tip",))
        self.cv.create_text(x + tw // 2, y + th // 2, text=self.tip, font=f, fill="#F3F4F8", tags=("tip",))
        self.cv.tag_raise("tip")

    def _hide_tip(self):
        if self._tip_job:
            self.cv.after_cancel(self._tip_job)
            self._tip_job = None
        self.cv.delete("tip")

    def _press(self):
        if self.enabled:
            self._pressed = True
            self.cv.move(self.tag, 0, self.app.px(1))

    def _release(self):
        if self._pressed:
            self._pressed = False
            self.cv.move(self.tag, 0, -self.app.px(1))

    def _click(self):
        self._hide_tip()
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, on):
        self.enabled = on
        self.hit.cursor = "hand2" if on else ""
        if not on:
            self.t = 0.0
            self.cv.itemconfigure(self.bg, image=self.app.gfx.rrect(
                self.w, self.h, self.radius, self.app.c.card2 if self.kind != "link" else self.surface,
                self.app.c.line if self.kind not in ("ghost", "link") else None))
        self._paint(self.t if on else 0.0)

    def destroy(self):
        self._hide_tip()
        self.cv.delete(self.tag)
        self.app.hits.remove(self.hit)


class Toggle:
    """An iOS-style switch whose knob glides across."""

    def __init__(self, app, x, y, value, on_change, layer="page", owner=None, scroll=None, tags=(),
                 enabled=True):
        self.app, self.cv = app, app.cv
        P = app.px
        self.w, self.h = P(42), P(24)
        self.k = P(18)
        self.x, self.y = int(x), int(y)
        self.value = bool(value)
        self.on_change = on_change
        self.enabled = enabled
        self.tag = new_tag("tg")
        tg = (self.tag,) + tuple(tags)
        self.t = 1.0 if self.value else 0.0
        self.track = self.cv.create_image(self.x, self.y, anchor="nw", image=self._track(self.t), tags=tg)
        self.knob = self.cv.create_image(self._kx(self.t), self.y + self.h // 2,
                                         image=app.gfx.disc(self.k, app.c.knob), tags=tg)
        self.hit = app.hits.add(self.x - P(4), self.y - P(4), self.x + self.w + P(4), self.y + self.h + P(4),
                                click=self.toggle, layer=layer, owner=owner, scroll=scroll)

    def _kx(self, t):
        pad = (self.h - self.k) / 2.0
        return self.x + pad + self.k / 2.0 + t * (self.w - 2 * pad - self.k)

    def _track(self, t):
        q = round(t * 8) / 8.0
        c = self.app.c
        on = c.accent if self.enabled else mix(c.toggle_off, c.accent, 0.4)
        return self.app.gfx.rrect(self.w, self.h, self.h / 2.0, mix(c.toggle_off, on, q))

    def _paint(self, t):
        self.t = t
        self.cv.itemconfigure(self.track, image=self._track(t))
        dy = self.scroll_offset()
        self.cv.coords(self.knob, self._kx(t), self.y + self.h // 2 - dy)

    def scroll_offset(self):
        return self.hit.scroll.offset if self.hit.scroll is not None else 0

    def set(self, value, animate=True):
        value = bool(value)
        self.value = value
        start, end = self.t, (1.0 if value else 0.0)
        if animate:
            self.app.anim.tween(0.26, lambda k: self._paint(start + (end - start) * k),
                                ease=ease_out_back if value else ease_out_cubic, key=(self.tag, "t"))
        else:
            self._paint(end)

    def toggle(self):
        if not self.enabled:
            return
        self.set(not self.value)
        if self.on_change:
            self.on_change(self.value)


class Segmented:
    """A pill with options; the highlight slides to the chosen one."""

    def __init__(self, app, x, y, options, value, on_change, layer="page", owner=None, scroll=None,
                 tags=(), seg_w=104, surface=None):
        self.app, self.cv = app, app.cv
        c, P = app.c, app.px
        self.options = options
        self.on_change = on_change
        self.value = value
        self.pad = P(4)
        self.sw = P(seg_w)
        self.h = P(38)
        self.w = self.sw * len(options) + 2 * self.pad
        self.x, self.y = int(x), int(y)
        self.tag = new_tag("sg")
        tg = (self.tag,) + tuple(tags)
        self.cv.create_image(self.x, self.y, anchor="nw",
                             image=app.gfx.rrect(self.w, self.h, self.h / 2.0, c.input, c.line), tags=tg)
        idx = self._index(value)
        self.pill = self.cv.create_image(self._px(idx), self.y + self.pad, anchor="nw",
                                         image=app.gfx.rrect(self.sw, self.h - 2 * self.pad,
                                                             (self.h - 2 * self.pad) / 2.0,
                                                             c.card if not c.dark else c.hover,
                                                             c.line2 if not c.dark else None), tags=tg)
        self.labels = []
        f = app.fonts["small_b"]
        for i, (val, label, icon) in enumerate(options):
            cx = self.x + self.pad + i * self.sw + self.sw // 2
            cy = self.y + self.h // 2
            tw = f.measure(label)
            iw = P(16)
            left = cx - (tw + iw + P(6)) // 2
            col = c.text if val == value else c.dim
            ic = self.cv.create_image(left + iw // 2, cy, image=app.gfx.icon(icon, iw, col), tags=tg)
            tx = self.cv.create_text(left + iw + P(6), cy, text=label, anchor="w", font=f, fill=col, tags=tg)
            self.labels.append((val, icon, ic, tx))
            app.hits.add(self.x + self.pad + i * self.sw, self.y, self.x + self.pad + (i + 1) * self.sw,
                         self.y + self.h, click=lambda v=val: self.select(v), layer=layer, owner=owner,
                         scroll=scroll)

    def _index(self, value):
        for i, opt in enumerate(self.options):
            if opt[0] == value:
                return i
        return 0

    def _px(self, idx):
        return self.x + self.pad + idx * self.sw

    def select(self, value, notify=True):
        if value == self.value:
            return
        c = self.app.c
        start = self.cv.coords(self.pill)[0]
        end = self._px(self._index(value))
        y = self.cv.coords(self.pill)[1]
        self.app.anim.tween(0.32, lambda k: self.cv.coords(self.pill, start + (end - start) * k, y),
                            ease=ease_out_back, key=(self.tag, "s"))
        self.value = value
        for val, icon, ic, tx in self.labels:
            col = c.text if val == value else c.dim
            self.cv.itemconfigure(tx, fill=col)
            self.cv.itemconfigure(ic, image=self.app.gfx.icon(icon, self.app.px(16), col))
        if notify and self.on_change:
            self.on_change(value)


class ScrollArea:
    """Vertical scrolling for one region of the canvas.  Items tagged with
    `tag` move; masks drawn by the page hide what scrolls out of view."""

    def __init__(self, app, x0, y0, x1, y1, tag, owner):
        self.app, self.cv = app, app.cv
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.tag, self.owner = tag, owner
        self.offset = 0.0
        self.target = 0.0
        self.content = 0
        self.thumb = None
        self._key = new_tag("sc")
        self._wide = False
        self._grab = None
        app.scroll_areas.append(self)
        # the scroll bar can be dragged, and clicking its track jumps a page
        P = app.px
        app.hits.add(x1 - P(16), y0, x1 + P(2), y1, owner=owner, cursor="",
                     press=self._track_press, release=self._track_release, drag=self._track_drag,
                     enter=lambda: self._hover_thumb(True), leave=lambda: self._hover_thumb(False))

    def contains(self, x, y):
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1

    def _thumb_geometry(self):
        view = self.y1 - self.y0
        th = max(self.app.px(36), view * view / max(1.0, self.content))
        ty = self.y0 + (view - th) * (self.offset / self.max_offset() if self.max_offset() else 0)
        return view, th, ty

    def _hover_thumb(self, on):
        self._wide = on or self._grab is not None
        self._draw_thumb()

    def _track_press(self):
        if self.max_offset() <= 0:
            return
        y = self.app.hits.press_xy[1]
        view, th, ty = self._thumb_geometry()
        if ty <= y <= ty + th:
            self._grab = y - ty  # start dragging the thumb
        else:
            self.scroll_by((view * 0.9) * (1 if y > ty else -1))

    def _track_drag(self, x, y):
        if self._grab is None:
            return
        view, th, _ = self._thumb_geometry()
        k = clamp((y - self._grab - self.y0) / max(1.0, view - th))
        self.scroll_to(k * self.max_offset(), animate=False)

    def _track_release(self):
        self._grab = None
        self._draw_thumb()

    def max_offset(self):
        return max(0.0, self.content - (self.y1 - self.y0))

    def set_content(self, height):
        self.content = height
        if self.offset > self.max_offset():
            self.scroll_to(self.max_offset(), animate=False)
        self._draw_thumb()

    def scroll_by(self, delta):
        self.scroll_to(self.target + delta)

    def scroll_to(self, value, animate=True):
        value = clamp(value, 0.0, self.max_offset())
        self.target = value
        start = self.offset
        if not animate or abs(value - start) < 1:
            self._apply(value)
            return
        self.app.anim.tween(0.24, lambda k: self._apply(start + (value - start) * k), key=self._key)

    def _apply(self, value):
        delta = value - self.offset
        if delta:
            self.cv.move(self.tag, 0, -delta)
            self.offset = value
            self._draw_thumb()
            self.app.hits.refresh()

    def _draw_thumb(self):
        c, P = self.app.c, self.app.px
        if self.thumb is not None:
            self.cv.delete(self.thumb)
            self.thumb = None
        view = self.y1 - self.y0
        if self.content <= view + 1:
            return
        view, th, ty = self._thumb_geometry()
        wide = self._wide or self._grab is not None
        x = self.x1 - P(5 if wide else 3)
        self.thumb = self.cv.create_line(x, ty + P(3), x, ty + th - P(3), width=P(8 if wide else 4),
                                         capstyle="round", fill=c.faint if wide else c.line2,
                                         tags=(self.owner.tag, "thumb"))
        self.cv.tag_raise(self.thumb)

    def destroy(self):
        if self in self.app.scroll_areas:
            self.app.scroll_areas.remove(self)


# =============================================================================
#  Toasts (inside the window) and desktop pop-ups (when TiDY is hidden)
# =============================================================================

class Toasts:
    def __init__(self, app):
        self.app = app
        self.current = None

    def show(self, title, sub="", icon="check", glyph=None, tint=None, action=None, duration=4.2):
        app, cv, c, P = self.app, self.app.cv, self.app.c, self.app.px
        self.hide(fast=True)
        tag = new_tag("toast")
        W, H = cv.winfo_width(), cv.winfo_height()
        w = P(380)
        text_w = w - P(16 + 38 + 14 + 16) - (P(74) if action else 0)
        two_lines = app.fonts["small"].measure(sub) > text_w
        h = P(90) if two_lines else P(70)
        x = W - w - P(26)
        y_end = H - h - P(26)
        y0 = H + P(12)
        tags = (tag, "toast")
        if c.shadow:
            sh = app.gfx.shadow(w, h, P(16), P(14), c.shadow, 0.16)
            cv.create_image(x - sh.margin, y0 - sh.margin + P(4), anchor="nw", image=sh, tags=tags)
        cv.create_image(x, y0, anchor="nw", image=app.gfx.rrect(w, h, P(16), c.card, c.line2 if c.dark else c.line),
                        tags=tags)
        glyph = glyph or c.good
        tint = tint or c.good_soft
        cv.create_image(x + P(16), y0 + h // 2, anchor="w", image=app.gfx.badge(icon, P(38), glyph, tint),
                        tags=tags)
        if two_lines:
            cv.create_text(x + P(68), y0 + P(16), anchor="nw", text=app.fonts.fit("body_b", title, text_w),
                           font=app.fonts["body_b"], fill=c.text, tags=tags)
            words, lines = sub.split(), ["", ""]
            for word in words:  # wrap into at most two lines
                line = 0 if app.fonts["small"].measure((lines[0] + " " + word).strip()) <= text_w and \
                    not lines[1] else 1
                lines[line] = (lines[line] + " " + word).strip()
            lines[1] = app.fonts.fit("small", lines[1], text_w)
            cv.create_text(x + P(68), y0 + P(39), anchor="nw", text="\n".join(lines), font=app.fonts["small"],
                           fill=c.dim, tags=tags)
        else:
            cv.create_text(x + P(68), y0 + h // 2 - P(9), anchor="w", text=app.fonts.fit("body_b", title, text_w),
                           font=app.fonts["body_b"], fill=c.text, tags=tags)
            cv.create_text(x + P(68), y0 + h // 2 + P(11), anchor="w", text=sub, font=app.fonts["small"],
                           fill=c.dim, tags=tags)
        bar = cv.create_line(x + P(16), y0 + h - P(2), x + w - P(16), y0 + h - P(2), fill=glyph,
                             width=max(1, P(2)), capstyle="round", tags=tags)
        state = {"tag": tag, "y": y0, "bar": bar, "x": x, "w": w, "h": h, "buttons": [], "hits": []}
        state["hits"].append(app.hits.add(x, y0, x + w, y0 + h, click=lambda: self.hide(), layer="toast",
                                          owner=self, cursor="hand2"))
        if action:
            label, fn = action

            def run(fn=fn):
                self.hide()
                fn()
            b = Button(app, x + w - P(14), y0 + h // 2, label, run, kind="soft", h=30, anchor="e",
                       layer="toast", owner=self, tags=tags, surface=c.card)
            state["buttons"].append(b)
        cv.tag_raise("toast")
        app.raise_overlays()
        self.current = state

        def slide(k):
            ny = y0 + (y_end - y0) * k
            dy = ny - state["y"]
            state["y"] = ny
            cv.move(tag, 0, dy)
            for hit in app.hits.hits:
                if hit.owner is self:
                    hit.y0 += dy
                    hit.y1 += dy
        app.anim.tween(0.5, slide, ease=ease_out_back, key=(tag, "in"))
        start = time.perf_counter()

        def countdown(now):
            if self.current is not state:
                return False
            k = clamp((now - start) / duration)
            x1 = x + P(16) + (w - P(32)) * (1 - k)
            try:
                cv.coords(bar, x + P(16), state["y"] + h - P(2), max(x + P(16), x1), state["y"] + h - P(2))
            except tk.TclError:
                return False
            if k >= 1:
                self.hide()
                return False
            return True
        app.anim.loop((tag, "cd"), countdown)

    def hide(self, fast=False):
        state, self.current = self.current, None
        if state is None:
            return
        app, cv = self.app, self.app.cv
        app.hits.remove_owner(self)
        tag = state["tag"]
        if fast or not app.anim.enabled:
            cv.delete(tag)
            return
        H = cv.winfo_height()
        y0 = state["y"]

        def slide(k):
            ny = y0 + (H + app.px(20) - y0) * k
            cv.move(tag, 0, ny - state["y"])
            state["y"] = ny
        app.anim.tween(0.28, slide, ease=ease_in_cubic, done=lambda: cv.delete(tag), key=(tag, "out"))


class DesktopPopup:
    """A small card in the corner of the screen, used while TiDY's window is
    hidden or in the background."""

    def __init__(self, app):
        self.app = app
        self.win = None
        self.cv = None
        self._job = None
        self._alpha = 0.0

    def _ensure(self):
        if self.win is not None:
            return
        app = self.app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
            self.win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(self.win, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<ButtonRelease-1>", lambda e: self._clicked())

    def show(self, title, sub, icon="check", glyph=None, tint=None):
        app, c, P = self.app, self.app.c, self.app.px
        self._ensure()
        w, h = P(360), P(84)
        cv = self.cv
        cv.delete("all")
        cv.configure(bg=c.card, width=w, height=h, cursor="hand2")
        cv.create_rectangle(0, 0, w - 1, h - 1, outline=c.line2, fill=c.card)
        cv.create_image(P(16), h // 2, anchor="w", image=app.gfx.logo(P(40)))
        cv.create_text(P(70), P(22), anchor="w", text="TiDY", font=app.fonts["small_b"], fill=c.accent_ink)
        cv.create_text(P(70), P(43), anchor="w", text=app.fonts.fit("body_b", title, w - P(90)),
                       font=app.fonts["body_b"], fill=c.text)
        cv.create_text(P(70), P(63), anchor="w", text=app.fonts.fit("small", sub, w - P(90)),
                       font=app.fonts["small"], fill=c.dim)
        area = work_area() or (0, 0, app.root.winfo_screenwidth(), app.root.winfo_screenheight() - P(48))
        x = area[2] - w - P(18)
        y = area[3] - h - P(18)
        self.win.geometry("%dx%d+%d+%d" % (w, h, x, y + P(16)))
        self.win.deiconify()
        self.win.lift()
        style_native_window(self.win, None, None, c.dark)
        start_alpha = self._alpha

        def fade_in(k):
            self._alpha = start_alpha + (1.0 - start_alpha) * k
            self.win.attributes("-alpha", self._alpha)
            self.win.geometry("+%d+%d" % (x, int(y + P(16) * (1 - k))))
        app.anim.tween(0.35, fade_in, key=("popup", "fade"))
        if self._job:
            app.root.after_cancel(self._job)
        self._job = app.root.after(4800, self.hide)

    def hide(self):
        self._job = None
        if self.win is None:
            return
        start = self._alpha

        def fade_out(k):
            self._alpha = start * (1 - k)
            self.win.attributes("-alpha", self._alpha)
        self.app.anim.tween(0.4, fade_out, key=("popup", "fade"), done=lambda: self.win.withdraw())

    def _clicked(self):
        self.hide()
        self.app.restore(page="activity")

    def destroy(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


# =============================================================================
#  System tray icon + single-instance handshake (Windows, plain ctypes)
# =============================================================================

INSTANCE_KEY = "%08x" % (zlib.crc32(norm(DATA_DIR).encode("utf-8")) & 0xFFFFFFFF)
TRAY_CLASS = "TiDYTrayWindow-" + INSTANCE_KEY
WM_TRAY = 0x8000 + 21
WM_SHOWME = 0x8000 + 22
_MUTEX = []


def acquire_single_instance():
    """True if we are the only TiDY; otherwise wake the running one up."""
    if not IS_WIN:
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = k32.CreateMutexW(None, False, "Local\\TiDY-" + INSTANCE_KEY)
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            _MUTEX.append(handle)
            return True
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        hwnd = user32.FindWindowW(TRAY_CLASS, None)
        if hwnd:
            user32.PostMessageW(hwnd, WM_SHOWME, 0, 0)
        return False
    except Exception:
        return True


class TrayIcon:
    def __init__(self, app):
        self.app = app
        self.ok = False
        self.hwnd = None
        self._added = False
        self.actions = []
        self.taskbar_created = -1  # messages can arrive while the window is being created
        if not IS_WIN:
            return
        try:
            self._create()
            self.ok = True
        except Exception:
            log_exception("tray icon")

    def _create(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32, self.shell32 = user32, shell32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]

        self.NID = NOTIFYICONDATAW
        k32.GetModuleHandleW.restype = wintypes.HMODULE
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        hinst = k32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)  # must stay referenced
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = TRAY_CLASS
        user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        user32.RegisterClassW(ctypes.byref(wc))
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        self.hwnd = user32.CreateWindowExW(0, TRAY_CLASS, "TiDY", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, wintypes.UINT]
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        size = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
        path = os.path.join(CACHE_DIR, "tray-v3-%d.ico" % size)
        if not os.path.exists(path):
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(ico_bytes([(size, self.app.gfx.logo_png(size)),
                                    (size * 2, self.app.gfx.logo_png(size * 2))]))
        self.hicon = user32.LoadImageW(None, path, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        for name, args, res in (("CreatePopupMenu", [], wintypes.HMENU),
                                ("AppendMenuW", [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                                                 wintypes.LPCWSTR], wintypes.BOOL),
                                ("TrackPopupMenu", [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                                    ctypes.c_int, wintypes.HWND, wintypes.LPVOID], ctypes.c_int),
                                ("DestroyMenu", [wintypes.HMENU], wintypes.BOOL),
                                ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
                                ("PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                                  wintypes.LPARAM], wintypes.BOOL),
                                ("GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
                                ("DestroyWindow", [wintypes.HWND], wintypes.BOOL)):
            fn = getattr(user32, name)
            fn.argtypes = args
            fn.restype = res
        self._notify(0)  # NIM_ADD

    def _nid(self, tip):
        nid = self.NID()
        nid.cbSize = ctypes.sizeof(self.NID)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = tip[:127]
        return nid

    def _notify(self, action, tip=None):
        if tip is None:
            tip = "TiDY - " + ("watching your folder" if self.app.engine.running else "paused")
        nid = self._nid(tip)
        ok = self.shell32.Shell_NotifyIconW(action, ctypes.byref(nid))
        if action == 0:
            self._added = bool(ok)
        return ok

    def update(self):
        if self.ok and self._added:
            self._notify(1)  # NIM_MODIFY

    def take(self):
        """Actions requested from the tray since the last call (read by the app's timer)."""
        actions, self.actions = self.actions, []
        return actions

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # Windows calls this from inside Tk's event loop.  Calling tkinter from
        # here would corrupt tkinter's thread state, so we only record what was
        # asked for; App.pump() carries it out a moment later.
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (0x0202, 0x0203):  # left click / double click
                    self.actions.append("open")
                elif event in (0x0205, 0x007B):  # right click / context menu
                    self._menu()
                return 0
            if msg == WM_SHOWME:
                self.actions.append("open")
                return 0
            if msg == WM_QUITME:  # the uninstaller asks us to close
                self.actions.append("quit")
                return 0
            if msg == self.taskbar_created:  # Explorer restarted
                self._notify(0)
                return 0
        except Exception:
            log_exception("tray message")
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        u = self.user32
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, 0, 1, "Open TiDY")
        u.AppendMenuW(menu, 0, 2, "Pause watching" if self.app.engine.running else "Start watching")
        u.AppendMenuW(menu, 0x800, 0, None)
        u.AppendMenuW(menu, 0, 3, "Quit TiDY")
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)
        cmd = u.TrackPopupMenu(menu, 0x0100 | 0x0080 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)
        u.PostMessageW(self.hwnd, 0, 0, 0)
        u.DestroyMenu(menu)
        action = {1: "open", 2: "toggle", 3: "quit"}.get(cmd)
        if action:
            self.actions.append(action)

    def remove(self):
        if not self.ok:
            return
        try:
            if self._added:
                self._notify(2)  # NIM_DELETE
            self.user32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.ok = False

# =============================================================================
#  Pages
# =============================================================================

class Page:
    name = ""

    def __init__(self, app):
        self.app, self.cv = app, app.cv
        self.tag = "pg_" + self.name
        self.widgets = []
        self.areas = []
        self.loops = []

    def box(self):
        return self.app.content_box()

    def build(self):
        raise NotImplementedError

    def destroy(self):
        for key in self.loops:
            self.app.anim.unloop(key)
        for area in self.areas:
            area.destroy()
        for w in self.widgets:
            try:
                w._hide_tip()
            except Exception:
                pass
        self.app.hits.remove_owner(self)
        self.cv.delete(self.tag)
        self.cv.delete("tip")

    def on_event(self, event):
        pass

    def loop(self, key, fn):
        k = (self.tag, key)
        self.loops.append(k)
        self.app.anim.loop(k, fn)

    # ---- drawing helpers ------------------------------------------------------
    def T(self, extra=()):
        return (self.tag,) + tuple(extra)

    def text(self, x, y, s, font, fill, anchor="nw", tags=(), **kw):
        return self.cv.create_text(x, y, text=s, font=self.app.fonts[font], fill=fill, anchor=anchor,
                                   tags=self.T(tags), **kw)

    def image(self, x, y, img, anchor="nw", tags=()):
        return self.cv.create_image(x, y, image=img, anchor=anchor, tags=self.T(tags))

    def card(self, x, y, w, h, r=None, fill=None, border=None, tags=(), shadow=True):
        app, c, P = self.app, self.app.c, self.app.px
        r = P(18) if r is None else r
        return Card(app, x, y, w, h, r, fill or c.card, c.line if border is None else border, self.T(tags),
                    shadow=shadow)

    def button(self, x, y, text="", command=None, **kw):
        kw.setdefault("owner", self)
        kw["tags"] = self.T(kw.get("tags", ()))
        b = Button(self.app, x, y, text, command, **kw)
        self.widgets.append(b)
        return b

    def header(self, title, subtitle):
        x0, y0, x1, y1 = self.box()
        P = self.app.px
        self.text(x0, y0 + P(30), title, "display", self.app.c.text)
        self.text(x0, y0 + P(72), subtitle, "body", self.app.c.dim)
        return y0 + P(112)

    def masks(self, top, bottom, fade=True):
        """Cover everything above `top` / below `bottom` in the content column
        so scrolled items vanish cleanly (with a soft fade at the edges)."""
        app, c, P = self.app, self.app.c, self.app.px
        x0, y0, x1, y1 = self.box()
        H = self.cv.winfo_height()
        left = app.side_w + 1
        W = self.cv.winfo_width()
        self.cv.create_rectangle(left, 0, W, top, fill=c.bg, outline="", tags=self.T(("mask",)))
        self.cv.create_rectangle(left, bottom, W, H, fill=c.bg, outline="", tags=self.T(("mask",)))
        if fade:
            self.image(left, top, app.gfx.fade(W - left, P(18), c.bg, True), tags=("mask",))
            self.image(left, bottom - P(18), app.gfx.fade(W - left, P(18), c.bg, False), tags=("mask",))

    def stack(self, list_tag):
        """Z-order for scrolling pages: list items < masks < everything else."""
        self.cv.tag_lower("mask")
        self.cv.tag_lower(list_tag)
        self.cv.tag_raise("thumb")


def greeting():
    h = datetime.datetime.now().hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    if 17 <= h < 22:
        return "Good evening"
    return "Hello, night owl"


STATUS_OWNER = object()  # owner of the hit areas in the sidebar status card


class Sidebar:
    NAV = [("home", "Home", "home"), ("activity", "Activity", "activity"),
           ("rules", "Rules", "rules"), ("settings", "Settings", "settings"), ("guide", "Guide", "book")]

    def __init__(self, app):
        self.app, self.cv = app, app.cv
        self.tag = "side"
        self.items = {}
        self.active = None

    def destroy(self):
        self.app.anim.unloop(("side", "pulse"))
        self.app.hits.remove_owner(self)
        self.cv.delete(self.tag)

    def build(self, active):
        self.destroy()
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        H = cv.winfo_height()
        w = app.side_w
        tg = (self.tag,)
        cv.create_rectangle(0, 0, w, H, fill=c.side, outline="", tags=tg)
        cv.create_line(w - 1, 0, w - 1, H, fill=c.side_line, tags=tg)
        # brand: logo + "TiDY" word mark (the little i wears the accent colour)
        self.logo_item = cv.create_image(P(22), P(24), anchor="nw", image=app.gfx.logo(P(40)), tags=tg)
        f = F["brand"]
        x = P(74)
        y = P(44)
        for part, col in (("T", c.text), ("i", c.accent), ("DY", c.text)):
            cv.create_text(x, y, text=part, anchor="w", font=f, fill=col, tags=tg)
            x += f.measure(part)
        cv.create_text(P(75), P(65), text="tidy downloads", anchor="w", font=F["small"], fill=c.faint, tags=tg)
        # navigation
        self.nav_y = {}
        y = P(110)
        iw = w - P(32)
        self.pill = cv.create_image(P(16), y, anchor="nw", image=app.gfx.rrect(iw, P(42), P(12), c.nav_active),
                                    tags=tg, state="hidden")
        self.hover_pill = cv.create_image(P(16), y, anchor="nw", image=app.gfx.rrect(iw, P(42), P(12), c.hover),
                                          tags=tg, state="hidden")
        for name, label, icon in self.NAV:
            ic = cv.create_image(P(42), y + P(21), image=app.gfx.icon(icon, P(20), c.dim), tags=tg)
            tx = cv.create_text(P(62), y + P(21), text=label, anchor="w", font=F["body_b"], fill=c.dim, tags=tg)
            self.items[name] = (ic, tx, icon)
            self.nav_y[name] = y
            app.hits.add(P(16), y, P(16) + iw, y + P(42), click=lambda n=name: app.show_page(n),
                         enter=lambda yy=y, n=name: self._hover(yy, n), leave=lambda: self._hover(None, None),
                         layer="side", owner=self)
            y += P(48)
        # tour
        y += P(10)
        cv.create_line(P(28), y, w - P(28), y, fill=c.side_line, tags=tg)
        y += P(14)
        ic = cv.create_image(P(42), y + P(21), image=app.gfx.icon("help", P(20), c.dim), tags=tg)
        tx = cv.create_text(P(62), y + P(21), text="Quick tour", anchor="w", font=F["body_b"], fill=c.dim, tags=tg)
        self.items["tour"] = (ic, tx, "help")
        self.tour_y = y
        app.hits.add(P(16), y, P(16) + iw, y + P(42), click=app.open_tour,
                     enter=lambda yy=y: self._hover(yy, "tour"), leave=lambda: self._hover(None, None),
                     layer="side", owner=self)
        self.active = None
        self.set_active(active, animate=False)
        self.build_status()

    def _hover(self, y, name):
        if y is None or name == self.active:
            self.cv.itemconfigure(self.hover_pill, state="hidden")
            for n, (ic, tx, icon) in self.items.items():
                if n != self.active:
                    self.cv.itemconfigure(tx, fill=self.app.c.dim)
            return
        self.cv.coords(self.hover_pill, self.app.px(16), y)
        self.cv.itemconfigure(self.hover_pill, state="normal")
        self.cv.tag_lower(self.hover_pill, self.pill)
        self.cv.itemconfigure(self.items[name][1], fill=self.app.c.text)

    def set_active(self, name, animate=True):
        app, c, P = self.app, self.app.c, self.app.px
        if name not in self.nav_y:
            return
        prev = self.active
        self.active = name
        for n, (ic, tx, icon) in self.items.items():
            on = n == name
            self.cv.itemconfigure(ic, image=app.gfx.icon(icon, P(20), c.accent if on else c.dim))
            self.cv.itemconfigure(tx, fill=c.text if on else c.dim)
        self.cv.itemconfigure(self.hover_pill, state="hidden")
        y_end = self.nav_y[name]
        self.cv.itemconfigure(self.pill, state="normal")
        if not animate or prev is None:
            self.cv.coords(self.pill, P(16), y_end)
            return
        y_start = self.cv.coords(self.pill)[1]
        app.anim.tween(0.38, lambda k: self.cv.coords(self.pill, P(16), y_start + (y_end - y_start) * k),
                       ease=ease_out_back, key=("side", "pill"))

    def build_status(self):
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        cv.delete("side_status")
        app.hits.remove_owner(STATUS_OWNER)
        app.anim.unloop(("side", "pulse"))
        H = cv.winfo_height()
        w = app.side_w - P(32)
        h = P(92)
        x, y = P(16), H - h - P(18)
        tg = (self.tag, "side_status")
        on = app.engine.running
        Card(app, x, y, w, h, P(16), c.card2 if c.dark else c.bg, c.line, tg)
        dot_x, dot_y = x + P(22), y + P(30)
        col = c.good if on else c.faint
        halo = cv.create_oval(dot_x - P(5), dot_y - P(5), dot_x + P(5), dot_y + P(5), outline=col, width=P(2),
                              tags=tg, state="normal" if on else "hidden")
        cv.create_oval(dot_x - P(5), dot_y - P(5), dot_x + P(5), dot_y + P(5), fill=col, outline="", tags=tg)
        cv.create_text(dot_x + P(14), dot_y, text="Watching" if on else "Paused", anchor="w", font=F["body_b"],
                       fill=c.text, tags=tg)
        folder = app.settings["folder"] or ""
        name = os.path.basename(folder.rstrip("\\/")) or folder
        cv.create_text(x + P(16), y + P(62), text=app.fonts.fit("small", name, w - P(84)), anchor="w",
                       font=F["small"], fill=c.dim, tags=tg)
        self.toggle = Toggle(app, x + w - P(56), y + P(18), on, lambda v: app.toggle_watching(),
                             layer="side", owner=STATUS_OWNER, tags=tg)
        if on:
            bg = c.card2 if c.dark else c.bg

            def pulse(now):
                k = (now % 1.8) / 1.8
                r = P(5) + P(6) * ease_out_cubic(k)
                cv.coords(halo, dot_x - r, dot_y - r, dot_x + r, dot_y + r)
                cv.itemconfigure(halo, outline=mix(c.good, bg, k))
                return True
            app.anim.loop(("side", "pulse"), pulse)


class HomePage(Page):
    name = "home"

    def build(self):
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        x0, y0, x1, y1 = self.box()
        W = x1 - x0
        s = app.settings
        self.tiles = {}
        self.flying = 0
        folder = s["folder"] or ""
        fname = os.path.basename(folder.rstrip("\\/")) or folder
        on = app.engine.running
        # ---- header -----------------------------------------------------------
        self.text(x0, y0 + P(30), greeting(), "display", c.text)
        sub = ("Here's how your %s folder is doing." % fname) if fname else "Let's pick a folder to keep tidy."
        self.text(x0, y0 + P(72), sub, "body", c.dim)
        self.button(x1, y0 + P(34), "", app.open_tour, kind="ghost", icon="help", w=40, h=40, anchor="ne",
                    tip="Take the quick tour")
        theme_icon = "sun" if c.dark else "moon"
        self.button(x1 - P(48), y0 + P(34), "", app.flip_theme, kind="ghost", icon=theme_icon, w=40, h=40,
                    anchor="ne", tip="Switch light / dark")
        # ---- hero card ----------------------------------------------------------
        hy, hh = y0 + P(112), P(184)
        hr = P(18)
        self.card(x0, hy, W, hh, r=hr)
        rcx, rcy = x0 + P(100), hy + hh // 2
        self.radar_center = (rcx, rcy)
        core = P(86)
        if on:
            core_img = app.gfx.core(core, c.accent_hi, c.accent_deep, "folder", c.on_accent, c.card)
        else:
            core_img = app.gfx.core(core, c.card2, c.card2, "folder", c.faint, c.card, border=c.line2)
        self.image(rcx, rcy, core_img, anchor="center", tags=("core",))
        # the rings stay inside the card's solid middle, where redrawing is cheap
        self.radar_room = hh / 2.0 - int(math.ceil(hr)) - P(2)
        if on:
            self._start_radar(rcx, rcy, core)
        else:
            cv.create_oval(rcx - P(58), rcy - P(58), rcx + P(58), rcy + P(58), outline=c.line, width=P(2),
                           dash=(P(3), P(5)), tags=self.T())
            cv.tag_raise("core")
        tx = x0 + P(198)
        btn_w = 186
        text_w = x1 - P(28) - P(btn_w) - P(28) - tx
        pill_text = "WATCHING" if on else "PAUSED"
        pf = F["tiny"]
        pw = pf.measure(pill_text) + P(34)
        self.image(tx, hy + P(26), app.gfx.rrect(pw, P(24), P(12), c.good_soft if on else c.card2,
                                                 None if on else c.line))
        cv.create_oval(tx + P(11), hy + P(35), tx + P(17), hy + P(41), fill=c.good if on else c.faint, outline="",
                       tags=self.T())
        self.text(tx + P(23), hy + P(38), pill_text, "tiny", c.good if on else c.dim, anchor="w")
        self.text(tx, hy + P(62), F.fit("h1", fname or "No folder yet", text_w), "h1", c.text)
        path_w = text_w - P(70)
        pth = F.fit_middle("small", folder, path_w)
        pid = self.text(tx, hy + P(102), pth, "small", c.dim, anchor="w")
        bx = cv.bbox(pid)[2] + P(6)
        self.button(bx, hy + P(102), "Change", app.choose_folder, kind="link", h=26, anchor="w", font="small_b",
                    surface=c.card)
        self.status_icon = self.image(tx + P(8), hy + P(138), app.gfx.icon("info", P(16), c.dim), anchor="center")
        self.status_text = self.text(tx + P(22), hy + P(138), "", "small", c.dim, anchor="w")
        self.status_w = text_w - P(22)
        self.set_status(app.status_message())
        if on:
            self.button(x1 - P(28), hy + P(40), "Pause", app.toggle_watching, kind="secondary", icon="pause",
                        w=btn_w, h=46, anchor="ne", surface=c.card)
        else:
            self.button(x1 - P(28), hy + P(40), "Start tidying", app.toggle_watching, kind="primary",
                        icon="play", w=btn_w, h=46, anchor="ne", surface=c.card)
        self.button(x1 - P(28), hy + P(98), "Try sample files", app.drop_samples, kind="soft", icon="sparkle",
                    w=btn_w, h=40, anchor="ne", surface=c.card,
                    tip="Drops a few harmless files into the folder")
        # ---- stats ----------------------------------------------------------------
        sy, sh = hy + hh + P(18), P(84)
        gap = P(16)
        sw = (W - 3 * gap) / 4.0
        st = s["stats"]
        stats = [
            ("today", "sparkle", app.store.today_count(), "Tidied today", "violet"),
            ("total", "check", st.get("total", 0), "Tidied in total", "green"),
            ("bytes", "archive", st.get("bytes", 0), "Organised so far", "blue"),
            ("loose", "wand", app.loose_count, "Loose files here", "amber"),
        ]
        self.stat_items = {}
        for i, (key, icon, value, label, col) in enumerate(stats):
            sx = x0 + i * (sw + gap)
            self.card(sx, sy, sw, sh, r=P(18))
            glyph, tint = app.palette_colors(col)
            self.image(sx + P(18), sy + sh // 2, app.gfx.badge(icon, P(42), glyph, tint), anchor="w")
            vid = self.text(sx + P(72), sy + sh // 2 - P(11), "", "stat", c.text, anchor="w")
            self.text(sx + P(72), sy + sh // 2 + P(16), F.fit("small", label, sw - P(84)), "small", c.dim, anchor="w")
            self.stat_items[key] = vid
            self._count_up(vid, key, value)
        if app.loose_count:
            self.loose_btn = self.button(x0 + 3 * (sw + gap) + sw - P(14), sy + P(14), "Tidy", app.tidy_existing,
                                         kind="soft", h=28, anchor="ne", font="small_b", surface=c.card,
                                         tip="Sort the files already in this folder")
        # ---- category tiles ------------------------------------------------------------
        ty = sy + sh + P(32)
        self.text(x0, ty, "Where your files go", "h2", c.text, anchor="w")
        self.button(x1, ty, "Edit rules", lambda: app.show_page("rules"), kind="link", icon="arrow_r", h=30,
                    anchor="e", font="small_b")
        top = ty + P(24)
        rules = [r for r in s["rules"]]
        cols = 4 if W >= P(700) else 3
        tgap = P(14)
        th = P(76)
        tw = (W - (cols - 1) * tgap) / float(cols)
        rows_fit = max(1, int((y1 - P(18) - top + tgap) // (th + tgap)))
        capacity = rows_fit * cols
        shown = rules if len(rules) <= capacity else rules[:capacity - 1]
        rows_used = max(1, -(-(len(shown) + (len(shown) < len(rules))) // cols))
        spare = (y1 - P(26) - top - (rows_used - 1) * tgap) / float(rows_used)
        th = int(clamp(spare, P(76), P(100)))  # grow the tiles into free space
        by = st.get("by_rule", {})
        for i, rule in enumerate(shown):
            r_, col_ = divmod(i, cols)
            tx_ = x0 + col_ * (tw + tgap)
            ty_ = top + r_ * (th + tgap)
            self._tile(rule, tx_, ty_, tw, th, by.get(rule["id"], 0))
        if len(shown) < len(rules):
            i = len(shown)
            r_, col_ = divmod(i, cols)
            tx_ = x0 + col_ * (tw + tgap)
            ty_ = top + r_ * (th + tgap)
            self.card(tx_, ty_, tw, th, r=P(16), shadow=False, fill=c.bg, border=c.line2)
            self.text(tx_ + tw / 2, ty_ + th / 2, "+%d more rules" % (len(rules) - len(shown)), "body_b", c.dim,
                      anchor="center")
            app.hits.add(tx_, ty_, tx_ + tw, ty_ + th, click=lambda: app.show_page("rules"), owner=self)

    # ---- pieces -----------------------------------------------------------------------
    def _tile(self, rule, x, y, w, h, count):
        app, c, P = self.app, self.app.c, self.app.px
        glyph, tint = app.rule_colors(rule)
        card = self.card(x, y, w, h, r=P(16))
        badge = self.image(x + P(16), y + h / 2, app.gfx.badge(rule.get("icon", "folder"), P(42), glyph, tint),
                           anchor="w")
        on = rule.get("on", True)
        name = self.app.fonts.fit("body_b", rule["name"], w - P(84))
        self.text(x + P(70), y + h / 2 - P(10), name, "body_b", c.text if on else c.faint, anchor="w")
        sub = self.text(x + P(70), y + h / 2 + P(11), self._count_text(rule, count), "small",
                        c.dim if on else c.faint, anchor="w")
        self.tiles[rule["id"]] = {"card": card, "badge": badge, "sub": sub, "count": count, "x": x, "y": y,
                                  "w": w, "h": h, "glyph": glyph, "rule": rule}
        app.hits.add(x, y, x + w, y + h, click=lambda r=rule: app.open_rule_folder(r), owner=self,
                     enter=lambda r=rule: self._tile_hover(r["id"], True),
                     leave=lambda r=rule: self._tile_hover(r["id"], False))

    def _count_text(self, rule, count):
        if not rule.get("on", True):
            return "Off"
        return plural(count, "file")

    def _tile_hover(self, rid, on):
        tile = self.tiles.get(rid)
        if not tile:
            return
        c = self.app.c
        tile["card"].set_border(mix(c.line, tile["glyph"], 0.65) if on else c.line)

    def _count_up(self, item, key, value):
        def fmt(v):
            return human_size(v) if key == "bytes" else format(int(v), ",")
        if not value:
            self.cv.itemconfigure(item, text=fmt(0))
            return
        self.app.anim.tween(0.9, lambda k: self.cv.itemconfigure(item, text=fmt(value * k)),
                            ease=ease_out_quint, key=(self.tag, "count", key))

    def set_status(self, msg):
        icon, text, tone = msg
        c, P = self.app.c, self.app.px
        col = {"good": c.good, "busy": c.accent_ink, "warn": c.warn, "bad": c.bad}.get(tone, c.dim)
        try:
            self.cv.itemconfigure(self.status_text, text=self.app.fonts.fit("small", text, self.status_w), fill=col)
            self.cv.itemconfigure(self.status_icon, image=self.app.gfx.icon(icon, P(16), col))
        except (tk.TclError, AttributeError):
            pass

    def _start_radar(self, cx, cy, core):
        cv, c, P = self.cv, self.app.c, self.app.px
        rings = [cv.create_oval(0, 0, 0, 0, outline=c.card, width=P(2), tags=self.T(("ring",)))
                 for _ in range(3)]
        glow = cv.create_oval(cx - core / 2 - P(5), cy - core / 2 - P(5), cx + core / 2 + P(5), cy + core / 2 + P(5),
                              outline=mix(c.card, c.accent, 0.35), width=P(3), tags=self.T(("ring",)))
        cv.tag_raise("core")
        r0, r1 = core / 2.0, min(core / 2.0 + P(36), self.radar_room)

        def frame(now):
            for i, ring in enumerate(rings):
                k = ((now / 2.4) + i / 3.0) % 1.0
                r = r0 + (r1 - r0) * ease_out_cubic(k)
                cv.coords(ring, cx - r, cy - r, cx + r, cy + r)
                cv.itemconfigure(ring, outline=mix(c.accent, c.card, 0.25 + 0.75 * k))
            b = 0.5 + 0.5 * math.sin(now * 2.2)
            g = P(4) + P(3) * b
            cv.coords(glow, cx - r0 - g, cy - r0 - g, cx + r0 + g, cy + r0 + g)
            cv.itemconfigure(glow, outline=mix(c.card, c.accent, 0.18 + 0.25 * b))
            return True
        self.loop("radar", frame)

    # ---- live events ----------------------------------------------------------------------
    def on_event(self, event):
        kind = event[0]
        if kind == "moved":
            self._moved(event[1])
        elif kind == "loose":
            item = self.stat_items.get("loose")
            if item:
                self.cv.itemconfigure(item, text=format(self.app.loose_count, ","))

    def _moved(self, entry):
        app = self.app
        st = app.settings["stats"]
        for key, value in (("today", app.store.today_count()), ("total", st.get("total", 0)),
                           ("bytes", st.get("bytes", 0))):
            item = self.stat_items.get(key)
            if item:
                self.cv.itemconfigure(item, text=human_size(value) if key == "bytes" else format(value, ","))
        tile = self.tiles.get(entry["rule"])
        if tile is None or self.flying >= 6 or not app.anim.enabled or not app.visible() or app.hits.block:
            self._arrive(entry, tile)
            return
        self._fly(entry, tile)
        app.raise_overlays()

    def _fly(self, entry, tile):
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        self.flying += 1
        tag = new_tag("chip")
        glyph = tile["glyph"]
        rule = tile["rule"]
        _, tint = app.rule_colors(rule)
        label = F.fit("small_b", entry["name"], P(150))
        cw = F["small_b"].measure(label) + P(54)
        ch = P(38)
        sx, sy = self.radar_center
        ex, ey = tile["x"] + P(37), tile["y"] + tile["h"] / 2
        tags = self.T((tag,))
        if c.shadow:
            sh = app.gfx.shadow(cw, ch, ch / 2, P(10), c.shadow, 0.14)
            cv.create_image(-sh.margin, -sh.margin + P(4), anchor="nw", image=sh, tags=tags)
        cv.create_image(0, 0, anchor="nw", image=app.gfx.rrect(cw, ch, ch / 2.0, c.card, mix(c.line2, glyph, 0.5)),
                        tags=tags)
        cv.create_image(P(5), ch / 2, anchor="w", image=app.gfx.badge(rule.get("icon", "folder"), P(28), glyph, tint,
                                                                     radius=P(14)), tags=tags)
        cv.create_text(P(40), ch / 2, text=label, anchor="w", font=F["small_b"], fill=c.text, tags=tags)
        pos = [sx - cw / 2.0, sy - ch / 2.0]
        cv.move(tag, pos[0], pos[1])
        ctrl = ((sx + ex) / 2.0, min(sy, ey) - P(70))

        def step(k):
            px_, py_ = bezier((sx, sy), ctrl, (ex, ey), k)
            nx, ny = px_ - cw / 2.0 * (1 - k), py_ - ch / 2.0
            cv.move(tag, nx - pos[0], ny - pos[1])
            pos[0], pos[1] = nx, ny

        def landed():
            cv.delete(tag)
            self.flying -= 1
            self._arrive(entry, tile)
        app.anim.tween(0.95, step, ease=ease_in_out_cubic, done=landed)

    def _arrive(self, entry, tile):
        app, cv, c, P = self.app, self.cv, self.app.c, self.app.px
        if tile is None or app.page is not self or not cv.coords(tile["badge"]):
            return  # the page was closed while the file was flying
        tile["count"] += 1
        try:
            cv.itemconfigure(tile["sub"], text=self._count_text(tile["rule"], tile["count"]))
        except tk.TclError:
            return
        if not app.anim.enabled:
            return
        cx, cy = tile["x"] + P(37), tile["y"] + tile["h"] / 2
        badge = tile["badge"]
        base = cv.coords(badge)
        app.anim.tween(0.55, lambda k: cv.coords(badge, base[0], base[1] - math.sin(math.pi * k) * P(7)),
                       ease=ease_linear, key=(self.tag, "bounce", tile["rule"]["id"]))
        glyph = tile["glyph"]

        def glow(k):
            tile["card"].set_border(mix(glyph, c.line, round(k * 6) / 6.0))
        app.anim.tween(0.9, glow, ease=ease_in_cubic, key=(self.tag, "glow", tile["rule"]["id"]))
        burst(app, cx, cy, glyph, c.card, self.T())
        app.raise_overlays()


def burst(app, cx, cy, color, bg, tags, count=12, dist=46):
    """A little confetti pop."""
    cv, P = app.cv, app.px
    parts = []
    for i in range(count):
        ang = 2 * math.pi * i / count + random.uniform(-0.2, 0.2)
        d = P(dist) * random.uniform(0.6, 1.0)
        r = P(random.choice((2, 2.5, 3)))
        item = cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="", tags=tags)
        parts.append((item, ang, d, r))

    def step(k):
        for item, ang, d, r in parts:
            px_ = cx + math.cos(ang) * d * k
            py_ = cy + math.sin(ang) * d * k + P(10) * k * k
            rr = r * (1 - 0.5 * k)
            cv.coords(item, px_ - rr, py_ - rr, px_ + rr, py_ + rr)
            cv.itemconfigure(item, fill=mix(color, bg, k ** 1.5))

    def done():
        for item, _, _, _ in parts:
            cv.delete(item)
    app.anim.tween(0.7, step, ease=ease_out_cubic, done=done)

class ActivityPage(Page):
    name = "activity"

    def build(self):
        app, c, P, F = self.app, self.app.c, self.app.px, self.app.fonts
        x0, y0, x1, y1 = self.box()
        top = self.header("Activity", "Everything TiDY has tidied for you. Changed your mind? Undo it.")
        history = app.store.history
        self.button(x1, y0 + P(36), "Clear history", self.clear, kind="secondary", icon="trash", h=38,
                    anchor="ne", enabled=bool(history))
        flt = app.activity_filter
        rules = {r["id"]: r for r in app.settings["rules"]}
        counts = {}
        for e in history:
            counts[e["rule"]] = counts.get(e["rule"], 0) + 1
        if flt not in counts:
            flt = app.activity_filter = None
        # filter chips
        cx = x0
        cy = top - P(4)
        chips = [(None, "All", len(history))] + [
            (rid, rules[rid]["name"] if rid in rules else rid.title(), n)
            for rid, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        for rid, label, n in chips:
            text = "%s  %d" % (label, n)
            w = F["small_b"].measure(text) + P(26)
            if cx + w > x1:
                break
            on = rid == flt
            self.image(cx, cy, app.gfx.rrect(w, P(32), P(16), c.accent_soft if on else c.card,
                                             None if on else c.line))
            self.text(cx + w / 2, cy + P(16), text, "small_b", c.accent_ink if on else c.dim, anchor="center")
            app.hits.add(cx, cy, cx + w, cy + P(32), click=lambda r=rid: self.set_filter(r), owner=self)
            cx += w + P(8)
        list_top = top + P(44)
        entries = [e for e in history if flt is None or e["rule"] == flt][:200]
        if not entries:
            self._empty(x0, list_top, x1, y1)
            return
        lt = "act_list"
        area = ScrollArea(app, x0 - P(4), list_top, x1 + P(8), y1 - P(10), lt, self)
        self.areas.append(area)
        self.area = area
        y = list_top + P(14)
        last_day = None
        rh, gap = P(68), P(10)
        for i, e in enumerate(entries):
            day = day_label(e.get("time", 0))
            if day != last_day:
                last_day = day
                self.text(x0 + P(4), y + P(10), day.upper(), "tiny", c.faint, anchor="w", tags=(lt,))
                y += P(28)
            self._row(e, x0, y, x1 - x0 - P(10), rh, rules, lt, "row%d" % i)
            y += rh + gap
        self.masks(list_top, y1 - P(10))
        self.stack(lt)
        area.set_content(y - list_top + P(10))

    def _empty(self, x0, top, x1, y1):
        app, c, P = self.app, self.app.c, self.app.px
        cx = (x0 + x1) / 2
        cy = top + (y1 - top) * 0.36
        self.image(cx, cy - P(40), app.gfx.badge("sparkle", P(84), c.accent, c.accent_soft, radius=P(28)),
                   anchor="center")
        self.text(cx, cy + P(26), "Nothing tidied yet", "h2", c.text, anchor="center")
        self.text(cx, cy + P(54), "New files that land in your folder will show up here.", "body", c.dim,
                  anchor="center")
        self.button(cx, cy + P(98), "Try sample files", app.drop_samples, kind="soft", icon="sparkle", h=40,
                    anchor="center")

    def _row(self, e, x, y, w, h, rules, lt, rtag):
        app, c, P, F = self.app, self.app.c, self.app.px, self.app.fonts
        rule = rules.get(e["rule"]) or {"icon": "folder", "color": "slate"}
        glyph, tint = app.rule_colors(rule)
        undone = e.get("undone")
        tags = (lt, rtag)
        card = self.card(x, y, w, h, r=P(16), tags=tags, shadow=False)
        self.image(x + P(14), y + h / 2, app.gfx.badge(rule.get("icon", "folder"), P(42), glyph, tint), anchor="w",
                   tags=tags)
        right = P(200) if not undone else P(110)
        tw = w - P(72) - right
        self.text(x + P(68), y + h / 2 - P(10), F.fit_middle("body_b", e["name"], tw), "body_b",
                  c.faint if undone else c.text, anchor="w", tags=tags)
        if undone:
            meta = "Put back in %s  ·  %s" % (os.path.basename(os.path.dirname(e["src"])) or "its folder",
                                                  clock_time(e.get("time", 0)))
        else:
            meta = "%s  →  %s  ·  %s  ·  %s" % (
                os.path.basename(os.path.dirname(e["src"])) or "folder", e.get("folder", "?"),
                human_size(e.get("size", 0)), clock_time(e.get("time", 0)))
        self.text(x + P(68), y + h / 2 + P(11), F.fit("small", meta, tw), "small", c.dim, anchor="w", tags=tags)
        hit = app.hits.add(x, y, x + w, y + h, click=lambda: self.show(e), owner=self, scroll=self.area,
                           enter=lambda: card.set_border(mix(c.line, glyph, 0.6)),
                           leave=lambda: card.set_border(c.line))
        hit.cursor = "hand2"
        if undone:
            self.image(x + w - P(16), y + h / 2, app.gfx.rrect(P(80), P(26), P(13), c.card2, c.line), anchor="e",
                       tags=tags)
            self.text(x + w - P(56), y + h / 2, "Put back", "tiny", c.dim, anchor="center", tags=tags)
        else:
            self.button(x + w - P(14), y + h / 2, "Undo", lambda: app.undo(e), kind="soft", icon="undo", h=34,
                        anchor="e", scroll=self.area, surface=c.card, font="small_b", tags=tags,
                        tip="Move it back to where it was")
            self.button(x + w - P(112), y + h / 2, "Show", lambda: self.show(e), kind="secondary", icon="folder",
                        h=34, anchor="e", scroll=self.area, surface=c.card, font="small_b", tags=tags,
                        tip="Open the folder and highlight the file")

    def show(self, e):
        target = e.get("restored") if e.get("undone") else e.get("dst")
        if not target or not reveal_in_explorer(target):
            self.app.toasts.show("Can't find that file", "It may have been moved or deleted.", icon="info",
                                 glyph=self.app.c.warn, tint=self.app.c.warn_soft)

    def set_filter(self, rid):
        self.app.activity_filter = rid
        self.app.refresh_page()

    def clear(self):
        def go():
            self.app.store.history.clear()
            self.app.store.save()
            self.app.refresh_page()
        self.app.confirm("Clear the activity list?", "Your files stay exactly where they are - only this list "
                         "is cleared. You won't be able to undo older moves afterwards.", "Clear list", go,
                         danger=True)

    def on_event(self, event):
        if event[0] in ("moved", "undone"):
            self.app.refresh_page(soon=True, slide_first="act_list" if event[0] == "moved" else None)


class RulesPage(Page):
    name = "rules"

    def build(self):
        app, c, P = self.app, self.app.c, self.app.px
        x0, y0, x1, y1 = self.box()
        top = self.header("Sorting rules", "Which file types go to which folder. TiDY creates the folders for you.")
        b = self.button(x1, y0 + P(36), "New rule", lambda: RuleEditor(app, None), kind="primary", icon="plus",
                        h=38, anchor="ne")
        self.button(b.x - P(10), y0 + P(36), "Reset", self.reset, kind="secondary", icon="undo", h=38,
                    anchor="ne", tip="Go back to the original rules")
        lt = "rules_list"
        list_top = top
        area = ScrollArea(app, x0 - P(4), list_top, x1 + P(8), y1 - P(10), lt, self)
        self.areas.append(area)
        self.area = area
        y = list_top + P(12)
        rh, gap = P(78), P(10)
        rules = app.settings["rules"]
        for rule in rules:
            self._row(rule, x0, y, x1 - x0 - P(10), rh, lt)
            y += rh + gap
        y += P(4)
        self.image(x0, y, app.gfx.badge("info", P(28), c.dim, c.card2, radius=P(9)), tags=(lt,))
        self.text(x0 + P(38), y + P(14), "Click a rule to add file types with  +  or remove them with  × . "
                  "Files that are already sorted stay where they are.", "small", c.dim, anchor="w", tags=(lt,))
        y += P(44)
        self.masks(list_top, y1 - P(10))
        self.stack(lt)
        area.set_content(y - list_top)

    def _row(self, rule, x, y, w, h, lt):
        app, c, P, F = self.app, self.app.c, self.app.px, self.app.fonts
        glyph, tint = app.rule_colors(rule)
        on = rule.get("on", True)
        tags = (lt,)
        card = self.card(x, y, w, h, r=P(16), tags=tags, shadow=False)
        self.image(x + P(16), y + h / 2, app.gfx.badge(rule.get("icon", "folder"), P(44), glyph, tint), anchor="w",
                   tags=tags)
        name_w = P(170)
        self.text(x + P(74), y + h / 2 - P(10), F.fit("body_b", rule["name"], name_w), "body_b",
                  c.text if on else c.faint, anchor="w", tags=tags)
        folder = os.path.basename((app.settings["folder"] or "").rstrip("\\/")) or "folder"
        self.text(x + P(74), y + h / 2 + P(11), F.fit("small", "%s \\ %s" % (folder, rule["name"]), name_w),
                  "small", c.faint, anchor="w", tags=tags)
        cx = x + P(74) + name_w + P(18)
        right_edge = x + w - P(120)
        if rule.get("catch_all"):
            self.text(cx, y + h / 2, "Every file type that isn't listed in a rule above", "small",
                      c.dim if on else c.faint, anchor="w", tags=tags)
        else:
            exts = rule.get("exts", [])
            fs = F["small_b"]
            shown = 0
            for ext in exts:
                label = "." + ext
                cw = fs.measure(label) + P(18)
                more_w = fs.measure("+%d" % (len(exts) - shown)) + P(18)
                if cx + cw + (more_w + P(6) if shown < len(exts) - 1 else 0) > right_edge:
                    break
                self.image(cx, y + h / 2, app.gfx.rrect(cw, P(26), P(13), c.card2 if on else c.card, c.line),
                           anchor="w", tags=tags)
                self.text(cx + cw / 2, y + h / 2, label, "small_b", c.dim if on else c.faint, anchor="center",
                          tags=tags)
                cx += cw + P(6)
                shown += 1
            if shown < len(exts):
                label = "+%d" % (len(exts) - shown)
                cw = fs.measure(label) + P(18)
                self.image(cx, y + h / 2, app.gfx.rrect(cw, P(26), P(13), c.accent_soft), anchor="w", tags=tags)
                self.text(cx + cw / 2, y + h / 2, label, "small_b", c.accent_ink, anchor="center", tags=tags)
            if not exts:
                self.text(cx, y + h / 2, "No file types yet - click to add some", "small", c.warn, anchor="w",
                          tags=tags)
        app.hits.add(x, y, x + w, y + h, click=lambda: RuleEditor(app, rule), owner=self, scroll=self.area,
                     enter=lambda: card.set_border(mix(c.line, glyph, 0.6)),
                     leave=lambda: card.set_border(c.line))
        self.button(x + w - P(14), y + h / 2, "", lambda: RuleEditor(app, rule), kind="ghost", icon="edit", w=36,
                    h=36, anchor="e", scroll=self.area, surface=c.card, tags=tags, tip="Edit this rule")
        t = Toggle(app, x + w - P(106), y + h / 2 - P(12), on, lambda v, r=rule: app.set_rule_on(r, v),
                   owner=self, scroll=self.area, tags=self.T(tags))
        self.widgets.append(t)

    def reset(self):
        def go():
            self.app.settings["rules"] = fresh_default_rules()
            self.app.rules_changed()
            self.app.toasts.show("Rules reset", "Back to the original eight folders.", icon="undo",
                                 glyph=self.app.c.accent, tint=self.app.c.accent_soft)
        self.app.confirm("Reset all rules?", "This brings back the original rules and removes any rules you "
                         "added. Files that are already sorted are not touched.", "Reset rules", go)


class SettingsPage(Page):
    name = "settings"

    def build(self):
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        x0, y0, x1, y1 = self.box()
        top = self.header("Settings", "Make TiDY work the way you like.")
        lt = "set_list"
        area = ScrollArea(app, x0 - P(4), top - P(8), x1 + P(8), y1 - P(10), lt, self)
        self.areas.append(area)
        self.area = area
        W = x1 - x0 - P(10)
        s = app.settings
        y = top + P(14)
        # ---- folder -----------------------------------------------------------------
        y = self._section(x0, y, "Watched folder")
        h = P(84)
        self.card(x0, y, W, h, tags=(lt,))
        self.image(x0 + P(18), y + h / 2, app.gfx.badge("folder", P(44), c.accent, c.accent_soft), anchor="w",
                   tags=(lt,))
        folder = s["folder"] or "No folder chosen"
        self.text(x0 + P(76), y + h / 2 - P(10), F.fit("body_b", os.path.basename(folder.rstrip("\\/")) or folder,
                                                        W - P(330)), "body_b", c.text, anchor="w", tags=(lt,))
        self.text(x0 + P(76), y + h / 2 + P(11), F.fit_middle("small", folder, W - P(330)), "small", c.dim,
                  anchor="w", tags=(lt,))
        b = self.button(x0 + W - P(18), y + h / 2, "Change", app.choose_folder, kind="primary", h=36, anchor="e",
                        scroll=area, surface=c.card, tags=(lt,))
        self.button(b.x - P(10), y + h / 2, "Open", lambda: open_path(s["folder"]) if s["folder"] else None,
                    kind="secondary", icon="folder", h=36, anchor="e", scroll=area, surface=c.card, tags=(lt,))
        y += h + P(26)
        # ---- behaviour -------------------------------------------------------------------
        others = next((r for r in s["rules"] if r.get("catch_all")), None)
        rows = [
            ("Keep watching after a restart", "When TiDY opens, it carries on where it left off.",
             s["resume"], lambda v: app.set_setting("resume", v), True),
            ("Keep running when the window is closed", "TiDY stays in the system tray (by the clock) and "
             "keeps tidying." if app.tray.ok else "Not available on this computer.",
             s["close_to_tray"] and app.tray.ok, lambda v: app.set_setting("close_to_tray", v), app.tray.ok),
            ("Start TiDY when Windows starts", "Opens quietly in the tray each time you sign in.",
             autostart_enabled(), app.set_autostart, IS_WIN),
            ("Tidy files with unknown types", "Files that match no rule go into “%s”." % (
                others["name"] if others else "Others"),
             bool(others and others.get("on", True)), lambda v: app.set_rule_on(others, v) if others else None,
             others is not None),
        ]
        y = self._section(x0, y, "While TiDY is running")
        y = self._toggle_card(x0, y, W, rows, lt)
        rows = [
            ("Pop-up when a file is tidied", "A little card in the corner of your screen while TiDY's window "
             "is hidden.", s["popups"], lambda v: app.set_setting("popups", v), True),
            ("Soft chime", "A gentle sound each time a file finds its home.", s["sound"],
             lambda v: app.set_setting("sound", v), bool(winsound)),
            ("Animations", "Turn off for a calmer, motion-free TiDY.", s["animations"], app.set_animations, True),
        ]
        y = self._section(x0, y, "Feedback")
        y = self._toggle_card(x0, y, W, rows, lt)
        # ---- appearance -------------------------------------------------------------------------
        y = self._section(x0, y, "Appearance")
        h = P(78)
        self.card(x0, y, W, h, tags=(lt,))
        self.text(x0 + P(20), y + h / 2 - P(10), "Theme", "body_b", c.text, anchor="w", tags=(lt,))
        self.text(x0 + P(20), y + h / 2 + P(11), "Follow Windows, or pick light or dark.", "small", c.dim,
                  anchor="w", tags=(lt,))
        seg_total = 3 * P(104) + 2 * P(4)
        seg = Segmented(app, x0 + W - P(18) - seg_total, y + (h - P(38)) / 2,
                        [("system", "System", "monitor"), ("light", "Light", "sun"), ("dark", "Dark", "moon")],
                        s["theme"], app.set_theme, owner=self, scroll=area, tags=self.T((lt,)))
        self.widgets.append(seg)
        y += h + P(26)
        # ---- help ------------------------------------------------------------------------------------
        y = self._section(x0, y, "Help")
        items = [
            ("help", "Quick tour", "A one-minute walk-through of how TiDY works.", "Start tour", app.open_tour),
            ("book", "User guide", "Full instructions for every part of TiDY, step by step.", "Open guide",
             lambda: app.show_page("guide")),
            ("wand", "Tidy what's already there", "Sort the files that were in the folder before TiDY "
             "started watching.", "Tidy now", app.tidy_existing),
            ("folder", "TiDY's data folder", "Settings and history live only on this computer.", "Open",
             lambda: open_path(DATA_DIR)),
            ("image", "The TiDY logo", "Save the logo as a 512 × 512 picture.", "Save PNG", app.export_logo),
        ]
        if IS_WIN:
            if running_installed_copy():
                items.insert(0, ("download", "Installed on this PC", "TiDY is in your Start menu and in Windows "
                                 "Settings → Apps.", "Uninstall", app.uninstall_prompt))
            elif is_installed():
                items.insert(0, ("download", "Update the installed TiDY", "Replace the installed copy with this "
                                 "version of TiDY.py.", "Update", app.install_now, "primary"))
            else:
                items.insert(0, ("download", "Install TiDY on this PC", "Adds TiDY to the Start menu and the "
                                 "desktop. No admin rights needed.", "Install", app.install_now, "primary"))
        rh = P(72)
        h = rh * len(items)
        self.card(x0, y, W, h, tags=(lt,))
        for i, (icon, title, desc, label, fn, *kind) in enumerate(items):
            ry = y + i * rh
            if i:
                cv.create_line(x0 + P(20), ry, x0 + W - P(20), ry, fill=c.line, tags=self.T((lt,)))
            self.image(x0 + P(20), ry + rh / 2, app.gfx.badge(icon, P(36), c.accent, c.accent_soft), anchor="w",
                       tags=(lt,))
            self.text(x0 + P(70), ry + rh / 2 - P(10), title, "body_b", c.text, anchor="w", tags=(lt,))
            self.text(x0 + P(70), ry + rh / 2 + P(11), F.fit("small", desc, W - P(260)), "small", c.dim, anchor="w",
                      tags=(lt,))
            self.button(x0 + W - P(18), ry + rh / 2, label, fn, kind=kind[0] if kind else "secondary", h=34,
                        anchor="e", scroll=area, surface=c.card, font="small_b", tags=(lt,))
        y += h + P(30)
        # ---- about ---------------------------------------------------------------------------------------
        self.image(x0 + W / 2, y + P(26), app.gfx.logo(P(52)), anchor="center", tags=(lt,))
        self.text(x0 + W / 2, y + P(70), "TiDY %s" % APP_VERSION, "body_b", c.text, anchor="center", tags=(lt,))
        self.text(x0 + W / 2, y + P(92), "Made with Python  ·  works 100% offline  ·  never deletes a file",
                  "small", c.faint, anchor="center", tags=(lt,))
        y += P(120)
        self.masks(top - P(8), y1 - P(10))
        self.stack(lt)
        area.set_content(y - top + P(8))

    def _section(self, x, y, title):
        self.text(x + P_(self.app, 2), y, title.upper(), "tiny", self.app.c.faint, anchor="w", tags=("set_list",))
        return y + self.app.px(16)

    def _toggle_card(self, x0, y, W, rows, lt):
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        rh = P(70)
        h = rh * len(rows)
        self.card(x0, y, W, h, tags=(lt,))
        for i, (title, desc, value, fn, enabled) in enumerate(rows):
            ry = y + i * rh
            if i:
                cv.create_line(x0 + P(20), ry, x0 + W - P(20), ry, fill=c.line, tags=self.T((lt,)))
            self.text(x0 + P(20), ry + rh / 2 - P(10), title, "body_b", c.text if enabled else c.faint, anchor="w",
                      tags=(lt,))
            self.text(x0 + P(20), ry + rh / 2 + P(11), F.fit("small", desc, W - P(120)), "small", c.dim,
                      anchor="w", tags=(lt,))
            t = Toggle(app, x0 + W - P(20) - P(42), ry + rh / 2 - P(12), value, fn, owner=self, scroll=self.area,
                       tags=self.T((lt,)), enabled=enabled)
            self.widgets.append(t)
        return y + h + P(26)



# =============================================================================
#  The user guide - shown on the Guide page and used for the Word version.
#  **double stars** mark the names of buttons and labels (shown in bold in Word).
# =============================================================================

GUIDE_DOCX_NAME = "TiDY User Guide.docx"


def _rules_table():
    rows = []
    for r in DEFAULT_RULES:
        if r.get("catch_all"):
            rows.append((r["name"], "Every file type that is not listed above"))
        else:
            rows.append((r["name"], " ".join("." + e for e in r["exts"])))
    return rows


GUIDE = [
    {"id": "welcome", "title": "What TiDY does", "short": "What it does", "icon": "sparkle", "color": "violet",
     "blocks": [
         ("p", "TiDY keeps one folder neat for you \u2014 normally your **Downloads** folder. While TiDY is "
               "watching, Windows tells it the moment a file is created, downloaded, copied or dropped into that "
               "folder. TiDY then moves the file into a sub-folder that matches its type."),
         ("table", ("A new file like \u2026", "\u2026 is moved to"), [
             ("holiday.jpg", "Downloads \\ Images"), ("report.pdf", "Downloads \\ Documents"),
             ("song.mp3", "Downloads \\ Music"), ("setup.exe", "Downloads \\ Programs"),
             ("photos.zip", "Downloads \\ Archives")]),
         ("bullets", ["Works 100% offline \u2014 nothing is ever uploaded.",
                      "Never deletes a file and never overwrites one.",
                      "Every move can be undone.",
                      "Made only with Python \u2014 there is nothing else to install."]),
     ]},
    {"id": "install", "title": "What you need", "short": "Setup", "icon": "monitor", "color": "blue",
     "blocks": [
         ("bullets", ["A Windows 10 or Windows 11 computer. (TiDY also starts on macOS and Linux, without the "
                      "tray icon, the shortcuts and the start-with-Windows option.)",
                      "Python 3.8 or newer from python.org. It already includes everything TiDY uses.",
                      "The file **TiDY.py**, for example downloaded from TiDY\u2019s GitHub page or copied from a "
                      "USB stick."]),
         ("note", "TiDY needs no internet connection and no extra downloads. Once Python is on the computer, "
                  "everything else is inside TiDY.py."),
     ]},
    {"id": "installing", "title": "Installing TiDY on a computer", "short": "Installing", "icon": "download",
     "color": "green",
     "blocks": [
         ("p", "TiDY can run straight from TiDY.py, but installing it is nicer: TiDY gets its own icon in the "
               "Start menu and on the desktop, and it appears in Windows **Settings \u2192 Apps** like any other "
               "program. Installing needs no administrator rights and never touches your files."),
         ("steps", ["If the computer doesn\u2019t have Python yet, install it from **python.org** (Downloads, "
                    "Windows). If the installer shows the option **Add python.exe to PATH**, tick it.",
                    "Copy **TiDY.py** onto the computer \u2014 for example, download it from TiDY\u2019s GitHub "
                    "page (green **Code** button, **Download ZIP**, then unzip it).",
                    "Double-click **TiDY.py**. TiDY opens straight away.",
                    "Go to **Settings**, scroll down to **Help** and click **Install** next to **Install TiDY on "
                    "this PC**.",
                    "That\u2019s it. From now on, open TiDY from the **Start menu** or the **TiDY icon on your "
                    "desktop**. The TiDY.py you downloaded can be deleted."]),
         ("tip", "Prefer typing? Open a terminal in the folder that contains TiDY.py and run  py TiDY.py --install"),
         ("p", "TiDY is copied to %LOCALAPPDATA%\\Programs\\TiDY. To install a newer version, open the new "
               "TiDY.py and click **Update** in the same place."),
         ("p", "**To uninstall:** open Windows **Settings \u2192 Apps \u2192 Installed apps**, find **TiDY** and "
               "choose **Uninstall** \u2014 or click **Uninstall** in TiDY\u2019s own Settings. TiDY asks whether "
               "to keep your settings. Your own files are never touched."),
     ]},
    {"id": "start", "title": "Starting TiDY", "short": "Starting", "icon": "play", "color": "green",
     "blocks": [
         ("steps", ["Double-click **TiDY.py**. (Or open a terminal in its folder, type  py TiDY.py  and press "
                    "Enter.)",
                    "The TiDY window opens with a short animation. The first time, a quick tour explains the "
                    "basics \u2014 click **Next** to go through it, or **Skip tour**.",
                    "TiDY shows your folder on the **Home** page. It does not move anything until you press "
                    "**Start tidying**."]),
         ("shot", "tour"),
         ("tip", "Only one TiDY runs at a time. If you open it again, the window that is already running simply "
                 "comes to the front. You can replay the tour any time with **Quick tour** in the sidebar."),
     ]},
    {"id": "first", "title": "Your first tidy, step by step", "short": "First tidy", "icon": "wand",
     "color": "amber",
     "blocks": [
         ("steps", ["On **Home**, check the folder name. It is your Downloads folder unless you changed it. To "
                    "choose another folder, click **Change**.",
                    "Press **Start tidying**. The label turns green and says **WATCHING**, and the rings around "
                    "the folder start to pulse.",
                    "Save, download or drag any file into that folder.",
                    "About two seconds later the file flies into its tile on the Home page and a message appears "
                    "in the bottom-right corner. Click **Undo** in that message if you change your mind.",
                    "Open the folder in File Explorer: the file is now inside a sub-folder such as **Images** or "
                    "**Documents**."]),
         ("tip", "Want to try it without your own files? Click **Try sample files**. TiDY drops four small, "
                 "harmless files (a picture, a note, a sound and a zip) into the folder and sorts them while you "
                 "watch. You can delete them afterwards."),
     ]},
    {"id": "home", "title": "The Home page", "short": "Home", "icon": "home", "color": "violet",
     "blocks": [
         ("shot", "home"),
         ("bullets", ["**Status card** \u2014 shows whether TiDY is **WATCHING** or **PAUSED**, the folder and its "
                      "path, and a live status line, for example \u201cWaiting for movie.mp4 to finish "
                      "downloading\u201d.",
                      "**Start tidying** / **Pause** \u2014 turns watching on or off. The switch at the bottom of "
                      "the sidebar does the same.",
                      "**Try sample files** \u2014 drops a few sample files so you can see TiDY at work.",
                      "**Tidied today**, **Tidied in total** and **Organised so far** \u2014 your statistics.",
                      "**Loose files here** \u2014 files that were already in the folder before TiDY started. "
                      "Click **Tidy** to sort them as well.",
                      "**Where your files go** \u2014 one tile per folder, with the number of files moved there. "
                      "Click a tile to open that folder; **Edit rules** opens the Rules page.",
                      "The sun / moon button switches between light and dark mode, and the **?** button starts "
                      "the quick tour."]),
     ]},
    {"id": "sorting", "title": "How TiDY decides where a file goes", "short": "Sorting", "icon": "rules",
     "color": "cyan",
     "blocks": [
         ("p", "TiDY looks at the file\u2019s extension \u2014 the letters after the last dot, such as .jpg or "
               ".pdf \u2014 and uses its rules to choose a folder. These are the rules TiDY starts with:"),
         ("table", ("Folder", "File types"), _rules_table()),
         ("bullets", ["Files whose type is not in any rule go to **Others**. You can switch this off in "
                      "Settings.",
                      "TiDY creates each folder inside the watched folder the first time it needs it.",
                      "If a file with the same name is already there, TiDY adds a number, for example "
                      "photo (1).jpg. Nothing is ever overwritten.",
                      "Only files placed directly in the watched folder are tidied. Files inside its sub-folders "
                      "are left alone.",
                      "Hidden and system files, desktop.ini and Office lock files (names that start with ~$) are "
                      "ignored."]),
     ]},
    {"id": "downloads", "title": "Downloads that are still running", "short": "Downloads",
     "icon": "download", "color": "blue",
     "blocks": [
         ("p", "Browsers save unfinished downloads under temporary names, such as file.crdownload (Chrome and "
               "Edge) or file.part (Firefox). TiDY never touches those."),
         ("steps", ["While a download is running, the Home status line says **Waiting for \u2026 to finish "
                    "downloading**.",
                    "When the browser finishes and gives the file its real name, TiDY waits a moment until the "
                    "file stops changing.",
                    "If another program still has the file open, TiDY simply tries again a little later.",
                    "Then the file is moved to its folder."]),
     ]},
    {"id": "existing", "title": "Files that were already there", "short": "Older files", "icon": "wand",
     "color": "amber",
     "blocks": [
         ("p", "TiDY only reacts to new files. Files that were already in the folder before you pressed Start "
               "tidying stay where they are, so nothing surprising happens to your old downloads. To sort them "
               "too:"),
         ("steps", ["On **Home**, look at the **Loose files here** card.",
                    "Click **Tidy** (or go to **Settings**, **Help**, **Tidy now**).",
                    "TiDY tells you how many files it will move. Click **Tidy them** to confirm."]),
         ("tip", "Every one of those moves is listed on the Activity page, so you can undo any of them."),
     ]},
    {"id": "activity", "title": "The Activity page", "short": "Activity", "icon": "activity", "color": "pink",
     "blocks": [
         ("shot", "activity"),
         ("bullets", ["Lists every file TiDY moved, newest first, grouped by day.",
                      "The chips at the top filter the list by folder.",
                      "**Show** opens File Explorer with the file selected. Clicking the row does the same.",
                      "**Undo** moves the file back to where it came from. TiDY then leaves it there, and the row "
                      "says **Put back**.",
                      "**Clear history** empties the list. Your files stay exactly where they are."]),
     ]},
    {"id": "rules", "title": "Changing the rules", "short": "Rules", "icon": "edit", "color": "orange",
     "blocks": [
         ("shot", "rules"),
         ("p", "The **Rules** page lists every folder with its file types. Use the switch on the right to turn a "
               "rule on or off. While a rule is off, its files stay where they land."),
         ("p", "Each file type of a rule is shown as a small coloured chip, like **.jpg**. Files that end in that "
               "type are sorted into the rule’s folder automatically."),
         ("steps", ["To change a rule, click its row (or the pencil button).",
                    "To add a file type, click **+ Add file type**, type it (for example  jpg ) and press "
                    "**Enter** or click **Add**. It appears as a new chip straight away, and TiDY confirms where "
                    "those files will go.",
                    "To remove a file type, click the **×** on its chip.",
                    "You can also rename the folder and pick a **Colour** and an **Icon**. Then click **Save "
                    "rule**."]),
         ("p", "To make your own folder, click **New rule**, type a folder name (for example Ebooks), add its file "
               "types (for example epub and mobi) and click **Save rule**."),
         ("shot", "editor"),
         ("bullets", ["A file type belongs to one folder only. If you add a type that another rule already has, "
                      "TiDY moves it to this rule and tells you so.",
                      "Rules you added can be deleted (the message that appears has an **Undo** button). "
                      "Built-in rules can only be switched off.",
                      "**Reset** brings back the original eight rules.",
                      "Renaming a folder only affects new files; files that were already sorted stay where they "
                      "are."]),
     ]},
    {"id": "settings", "title": "Settings", "short": "Settings", "icon": "settings", "color": "slate",
     "blocks": [
         ("shot", "settings"),
         ("table", ("Setting", "What it does"), [
             ("Watched folder", "Choose the folder TiDY looks after (Change), or open it in File Explorer "
                                "(Open)."),
             ("Keep watching after a restart", "When TiDY opens, it carries on watching if it was watching "
                                               "before."),
             ("Keep running when the window is closed", "Closing the window keeps TiDY tidying in the system "
                                                        "tray."),
             ("Start TiDY when Windows starts", "TiDY opens quietly in the tray each time you sign in, and starts "
                                                "watching."),
             ("Tidy files with unknown types", "Files that match no rule go to Others. Turn it off to leave them "
                                               "where they are."),
             ("Pop-up when a file is tidied", "Shows a small card in the corner of the screen while TiDY\u2019s "
                                              "window is hidden."),
             ("Soft chime", "Plays a gentle sound each time a file is tidied."),
             ("Animations", "Turn them off for a calmer interface without motion."),
             ("Theme", "System (follows Windows), Light or Dark."),
         ]),
         ("p", "The **Help** card at the bottom of Settings opens this guide, replays the quick tour, tidies the "
               "files that are already in the folder, opens TiDY\u2019s data folder and saves the TiDY logo as a "
               "picture."),
     ]},
    {"id": "tray", "title": "Running in the background", "short": "Background", "icon": "app", "color": "green",
     "blocks": [
         ("steps", ["While TiDY is watching, close the window with the **X** button. TiDY keeps working in the "
                    "system tray, next to the clock. (You may need to click the small **^** arrow to see its "
                    "icon.)",
                    "Whenever a file is tidied, a small pop-up appears in the corner of the screen.",
                    "Click the tray icon to open TiDY again.",
                    "Right-click the tray icon for **Open TiDY**, **Pause watching** / **Start watching** and "
                    "**Quit TiDY**."]),
         ("note", "To close TiDY completely, right-click the tray icon and choose **Quit TiDY**. If TiDY is "
                  "paused, closing the window quits it straight away."),
     ]},
    {"id": "keys", "title": "Keyboard shortcuts", "short": "Shortcuts", "icon": "keyboard", "color": "cyan",
     "blocks": [
         ("keys", [("Ctrl + 1 \u2026 5", "Home, Activity, Rules, Settings, Guide"),
                   ("Right arrow or Enter", "Next step of the quick tour"),
                   ("Left arrow", "Previous step of the quick tour"),
                   ("Esc", "Close the tour or a dialog"),
                   ("Enter", "Save or confirm in a dialog"),
                   ("Mouse wheel", "Scroll long pages and lists")]),
     ]},
    {"id": "safety", "title": "Safety and privacy", "short": "Safety", "icon": "shield", "color": "green",
     "blocks": [
         ("bullets", ["TiDY never deletes a file and never overwrites one.",
                      "It works completely offline and never connects to the internet.",
                      "For safety it refuses to watch drive roots (like C:\\), Windows and program folders, "
                      "AppData, and your whole user folder.",
                      "Its settings and history are kept only on this computer, in %APPDATA%\\TiDY "
                      "(settings.json, history.json and tidy.log).",
                      "Every move is listed on the Activity page and can be undone."]),
     ]},
    {"id": "trouble", "title": "Troubleshooting", "short": "Troubleshooting", "icon": "help", "color": "red",
     "blocks": [
         ("table", ("Problem", "What to do"), [
             ("A new file was not moved",
              "Check that Home says WATCHING and that the rule for that file type is switched on (Rules page). "
              "For unusual file types, Tidy files with unknown types must be on. If the file is still open in "
              "another program, close that program \u2014 TiDY tries again by itself."),
             ("My older files were not moved",
              "That is on purpose. Click Tidy on the Loose files card, or use Settings, Help, Tidy now."),
             ("\u201cTiDY can\u2019t watch this folder\u201d",
              "The folder was moved or deleted, or it is a protected system folder. Click Change and choose "
              "another folder."),
             ("\u201cCouldn\u2019t tidy \u2026\u201d",
              "Another program kept the file open for a long time. Close that program, then use Tidy now."),
             ("I can\u2019t find a file TiDY moved", "Open Activity and click Show next to the file."),
             ("Undo says the file isn\u2019t there", "The file was moved or deleted after TiDY sorted it."),
             ("I can\u2019t see the tray icon",
              "Click the ^ arrow next to the clock. You can drag the TiDY icon onto the taskbar to keep it "
              "visible."),
             ("Nothing happens when I open TiDY",
              "TiDY is probably already running in the tray. Click its tray icon."),
             ("Double-clicking TiDY.py does nothing",
              "Install Python 3 from python.org, or open a terminal in TiDY\u2019s folder and run  py TiDY.py  to "
              "see the error message."),
             ("The Start menu shortcut stopped working",
              "Python was probably updated or moved. Open TiDY.py once more and click Install (or Update) in "
              "Settings → Help to refresh the shortcuts."),
             ("I want to start over",
              "Quit TiDY, then delete the folder %APPDATA%\\TiDY. Your own files are not affected."),
         ]),
     ]},
    {"id": "faq", "title": "Questions and answers", "short": "Q & A", "icon": "info", "color": "violet",
     "blocks": [
         ("qa", [("Does TiDY need the internet?", "No. Everything happens on your own computer."),
                 ("Can TiDY tidy my Desktop instead?",
                  "Yes. In Settings, click Change under Watched folder and choose your Desktop."),
                 ("Will TiDY sort the files that are already in Downloads?",
                  "Only when you ask it to, with Tidy now."),
                 ("Does TiDY look inside sub-folders?",
                  "No, only at files placed directly in the watched folder."),
                 ("Can I rename or delete the folders TiDY created?",
                  "Yes, they are ordinary folders. TiDY creates a folder again the next time it needs it."),
                 ("Can I put TiDY on another computer?",
                  "Yes. Install Python there, copy TiDY.py over (for example from TiDY’s GitHub page) and "
                  "follow the steps in Installing TiDY on a computer."),
                 ("How do I remove TiDY?",
                  "Open Windows Settings → Apps → Installed apps, find TiDY and choose Uninstall. If you "
                  "never installed it, just delete TiDY.py (and, if you like, the folder %APPDATA%\\TiDY).")]),
     ]},
]


def plain(text):
    """Guide text without the **bold** markers."""
    return text.replace("**", "")


def wrap_lines(font, text, width, cache):
    """Greedy word wrap using real font measurements."""
    def measure(s):
        w = cache.get(s)
        if w is None:
            w = cache[s] = font.measure(s)
        return w
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            trial = (line + " " + word) if line else word
            if measure(trial) <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
            while measure(line) > width and len(line) > 1:  # a single very long word
                cut = len(line)
                while cut > 1 and measure(line[:cut]) > width:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
        lines.append(line)
    return lines


def find_word_guide(app):
    """Where is the Word version of the guide? (TiDY may have tidied it into Documents.)"""
    folders = [INSTALL_DIR, os.path.dirname(os.path.abspath(__file__))]
    for base in (known_downloads_dir(), app.settings.get("folder")):
        if base:
            folders.append(base)
            rule = app.engine.rulebook.match(GUIDE_DOCX_NAME)
            if rule:
                folders.append(os.path.join(base, rule["name"]))
    return locate_word_guide(folders)


class GuidePage(Page):
    name = "guide"

    def build(self):
        app, c, P, F = self.app, self.app.c, self.app.px, self.app.fonts
        x0, y0, x1, y1 = self.box()
        top = self.header("User guide", "Everything TiDY can do, explained step by step.")
        b = self.button(x1, y0 + P(36), "Quick tour", app.open_tour, kind="soft", icon="help", h=38, anchor="ne")
        word = find_word_guide(app)
        if word:
            self.button(b.x - P(10), y0 + P(36), "Word version", lambda: open_path(word), kind="secondary",
                        icon="doc", h=38, anchor="ne", tip="Open the printable guide in Word")
        lt = "guide_list"
        area = ScrollArea(app, x0 - P(4), top - P(8), x1 + P(8), y1 - P(10), lt, self)
        self.areas.append(area)
        self.area = area
        self.lt = lt
        self._wcache = {}
        W = x1 - x0 - P(10)
        y = top + P(14)
        # ---- contents ----------------------------------------------------------------
        chips = []
        cx, cy = x0 + P(20), y + P(52)
        fs = F["small_b"]
        for i, sec in enumerate(GUIDE):
            label = "%d  %s" % (i + 1, sec["short"])
            w = fs.measure(label) + P(24)
            if cx + w > x0 + W - P(20):
                cx, cy = x0 + P(20), cy + P(40)
            chips.append((sec, label, cx, cy, w))
            cx += w + P(8)
        toc_h = cy + P(32) + P(20) - y
        self.card(x0, y, W, toc_h, tags=(lt,))
        self.image(x0 + P(20), y + P(26), app.gfx.badge("book", P(30), c.accent, c.accent_soft, radius=P(10)),
                   anchor="w", tags=(lt,))
        self.text(x0 + P(60), y + P(26), "On this page", "h3", c.text, anchor="w", tags=(lt,))
        self.text(x0 + W - P(20), y + P(26), "Click a topic to jump to it", "small", c.faint, anchor="e",
                  tags=(lt,))
        self.section_y = {}
        for sec, label, cx, cy, w in chips:
            glyph, tint = app.palette_colors(sec["color"])
            self.image(cx, cy, app.gfx.rrect(w, P(30), P(15), c.card2 if c.dark else c.bg, c.line), tags=(lt,))
            self.text(cx + w / 2, cy + P(15), label, "small_b", c.dim, anchor="center", tags=(lt,))
            app.hits.add(cx, cy, cx + w, cy + P(30), click=lambda s=sec["id"]: self.jump(s), owner=self,
                         scroll=area)
        y += toc_h + P(18)
        # ---- sections --------------------------------------------------------------------
        for i, sec in enumerate(GUIDE):
            y = self._section(i, sec, x0, y, W) + P(18)
        self.text(x0 + W / 2, y + P(8), "Still stuck? Replay the quick tour, or look at TiDY\u2019s log file in "
                  "Settings \u2192 Help \u2192 Data folder.", "small", c.faint, anchor="n", tags=(lt,))
        y += P(48)
        self.masks(top - P(8), y1 - P(10))
        self.stack(lt)
        area.set_content(y - top + P(8))
        if app.guide_jump:  # opened from somewhere that wants a particular topic
            section, app.guide_jump = app.guide_jump, None
            self.jump(section, animate=False)

    def jump(self, sid, animate=True):
        y = self.section_y.get(sid)
        if y is not None:
            self.area.scroll_to(y - self.area.y0 - self.app.px(10), animate=animate)

    # ---- layout helpers ---------------------------------------------------------------------
    def _lines(self, font, text, width):
        return wrap_lines(self.app.fonts[font], plain(text), width, self._wcache.setdefault(font, {}))

    def _measure(self, blocks, width):
        """Return (height, ops) for a list of blocks laid out in `width` pixels."""
        P, F = self.app.px, self.app.fonts
        ops = []
        y = 0
        lh = F["body"].metrics("linespace") + P(5)
        for kind, *data in blocks:
            if kind == "shot":
                continue
            if y:
                y += P(14)
            if kind == "p":
                for line in self._lines("body", data[0], width):
                    ops.append(("text", 0, y, line, "body", "text"))
                    y += lh
            elif kind in ("steps", "bullets"):
                for n, item in enumerate(data[0]):
                    indent = P(38) if kind == "steps" else P(22)
                    lines = self._lines("body", item, width - indent)
                    ops.append(("num", 0, y, str(n + 1)) if kind == "steps" else ("dot", 0, y))
                    for line in lines:
                        ops.append(("text", indent, y, line, "body", "text"))
                        y += lh
                    y += P(6)
                y -= P(6)
            elif kind in ("tip", "note"):
                inner = width - P(64)
                lines = self._lines("body", data[0], inner)
                h = P(16) + P(22) + len(lines) * lh + P(12)
                ops.append(("box", 0, y, width, h, kind))
                ops.append(("text", P(52), y + P(14), "Tip" if kind == "tip" else "Good to know", "body_b",
                            "accent" if kind == "tip" else "warn"))
                yy = y + P(14) + P(24)
                for line in lines:
                    ops.append(("text", P(52), yy, line, "body", "text"))
                    yy += lh
                y += h
            elif kind in ("table", "keys"):
                rows = data[-1]
                head = data[0] if kind == "table" else None
                left_w = int(width * (0.36 if kind == "table" else 0.3))
                if head:
                    ops.append(("text", P(14), y, head[0].upper(), "tiny", "faint"))
                    ops.append(("text", left_w + P(14), y, head[1].upper(), "tiny", "faint"))
                    y += P(22)
                for left, right in rows:
                    ops.append(("rule", 0, y, width))
                    y += P(10)
                    lcol = self._lines("body_b", left, left_w - P(24))
                    rcol = self._lines("body", right, width - left_w - P(20))
                    if kind == "keys":
                        ops.append(("key", P(14), y, plain(left)))
                        lcol = [""]
                    else:
                        for k, line in enumerate(lcol):
                            ops.append(("text", P(14), y + k * lh, line, "body_b", "text"))
                    for k, line in enumerate(rcol):
                        ops.append(("text", left_w + P(14), y + k * lh, line, "body", "dim"))
                    y += max(len(lcol), len(rcol)) * lh + (P(8) if kind == "keys" else P(4))
            elif kind == "qa":
                for q, a in data[0]:
                    for line in self._lines("body_b", q, width):
                        ops.append(("text", 0, y, line, "body_b", "text"))
                        y += lh
                    for line in self._lines("body", a, width):
                        ops.append(("text", 0, y, line, "body", "dim"))
                        y += lh
                    y += P(10)
                y -= P(10)
        return y, ops

    def _section(self, i, sec, x0, y, W):
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        lt = self.lt
        glyph, tint = app.palette_colors(sec["color"])
        pad = P(26)
        inner_x = x0 + pad + P(56)
        inner_w = W - pad - P(56) - pad
        body_h, ops = self._measure(sec["blocks"], inner_w)
        head_h = P(58)
        h = head_h + body_h + P(26)
        self.section_y[sec["id"]] = y
        self.card(x0, y, W, h, tags=(lt,))
        self.image(x0 + pad, y + P(24), app.gfx.badge(sec["icon"], P(42), glyph, tint), anchor="nw", tags=(lt,))
        num = "%d." % (i + 1)
        self.text(inner_x, y + P(32), num, "h2", glyph, anchor="w", tags=(lt,))
        self.text(inner_x + F["h2"].measure(num) + P(8), y + P(32), sec["title"], "h2", c.text, anchor="w",
                  tags=(lt,))
        by = y + head_h + P(6)
        colors = {"text": mix(c.text, c.dim, 0.25), "dim": c.dim, "faint": c.faint, "accent": c.accent_ink,
                  "warn": c.warn}
        for op in ops:
            kind = op[0]
            if kind == "text":
                _, dx, dy, s, font, col = op
                self.text(inner_x + dx, by + dy, s, font, colors[col], tags=(lt,))
            elif kind == "num":
                _, dx, dy, s = op
                self.image(inner_x + dx, by + dy - P(2), app.gfx.disc(P(26), tint), tags=(lt,))
                self.text(inner_x + dx + P(13), by + dy + P(11), s, "small_b", glyph, anchor="center", tags=(lt,))
            elif kind == "dot":
                _, dx, dy = op
                self.image(inner_x + dx + P(4), by + dy + P(7), app.gfx.disc(P(7), glyph), tags=(lt,))
            elif kind == "box":
                _, dx, dy, w, hh, style = op
                fill = c.accent_soft if style == "tip" else c.warn_soft
                Card(app, inner_x + dx, by + dy, w, hh, P(14), fill, None, self.T((lt,)))
                self.image(inner_x + dx + P(16), by + dy + P(13),
                           app.gfx.icon("sparkle" if style == "tip" else "info", P(22),
                                        c.accent_ink if style == "tip" else c.warn), tags=(lt,))
            elif kind == "rule":
                _, dx, dy, w = op
                cv.create_line(inner_x + dx, by + dy, inner_x + dx + w, by + dy, fill=c.line, tags=self.T((lt,)))
            elif kind == "key":
                _, dx, dy, s = op
                kw = F["small_b"].measure(s) + P(20)
                self.image(inner_x + dx, by + dy - P(3), app.gfx.rrect(kw, P(28), P(8), c.card2, c.line2),
                           tags=(lt,))
                self.text(inner_x + dx + kw / 2, by + dy + P(11), s, "small_b", c.text, anchor="center", tags=(lt,))
        return y + h


def P_(app, v):
    return app.px(v)


# =============================================================================
#  Dialogs (drawn on the canvas, above everything else)
# =============================================================================

class Modal:
    layer = "modal"

    def __init__(self, app, w, h):
        self.app, self.cv = app, app.cv
        P = app.px
        self.tag = new_tag("modal")
        self.entries = []
        self.widgets = []
        app.close_modal()
        app.modal = self
        app.hits.block = LAYERS[self.layer]
        app.hits._set_hover(None)
        self.w, self.h = P(w), P(h)
        self._bind = app.root.bind("<Escape>", lambda e: self.close(), add="+")
        self._draw(animate=True)

    def _draw(self, animate):
        app, c, P = self.app, self.app.c, self.app.px
        W, H = self.cv.winfo_width(), self.cv.winfo_height()
        self.x = (W - self.w) // 2
        self.y = max(P(8), (H - self.h) // 2)
        tg = (self.tag, "modal")
        self.cv.create_image(0, 0, anchor="nw", image=app.gfx.overlay(W, H, c.overlay, c.overlay_a), tags=tg)
        app.hits.add(0, 0, W, H, click=self.close, layer=self.layer, owner=self, cursor="")
        Card(app, self.x, self.y, self.w, self.h, P(22), c.card, c.line2 if c.dark else None, tg, shadow=True,
             shadow_alpha=0.2, blur=P(22))
        app.hits.add(self.x, self.y, self.x + self.w, self.y + self.h, layer=self.layer, owner=self, cursor="")
        self.build()
        self.cv.tag_raise(self.tag)
        app.raise_overlays()
        if animate and app.anim.enabled:
            off = [P(26)]
            self.cv.move(self.tag + "_body", 0, off[0])

            def slide(k):
                target = P(26) * (1 - k)
                self.cv.move(self.tag + "_body", 0, target - off[0])
                off[0] = target
            app.anim.tween(0.34, slide, ease=ease_out_back)

    def T(self):
        return (self.tag, self.tag + "_body", "modal")

    def text(self, x, y, s, font, fill, anchor="nw", **kw):
        return self.cv.create_text(self.x + x, self.y + y, text=s, font=self.app.fonts[font], fill=fill,
                                   anchor=anchor, tags=self.T(), **kw)

    def button(self, x, y, text, command, **kw):
        kw.setdefault("layer", self.layer)
        kw.setdefault("owner", self)
        kw.setdefault("surface", self.app.c.card)
        kw["tags"] = self.T()
        b = Button(self.app, self.x + x, self.y + y, text, command, **kw)
        self.widgets.append(b)
        return b

    def entry(self, x, y, w, value):
        app, c, P = self.app, self.app.c, self.app.px
        frame = self.cv.create_image(self.x + x, self.y + y, anchor="nw",
                                     image=app.gfx.rrect(w, P(42), P(12), c.input, c.line2), tags=self.T())
        e = tk.Entry(self.cv, relief="flat", bd=0, highlightthickness=0, bg=c.input, fg=c.text,
                     insertbackground=c.text, font=app.fonts["body"], selectbackground=c.accent,
                     selectforeground="#FFFFFF", disabledbackground=c.input)
        e.insert(0, value)
        self.cv.create_window(self.x + x + P(14), self.y + y + P(21), anchor="w", window=e, width=w - P(28),
                              height=P(26), tags=self.T())
        e.bind("<FocusIn>", lambda ev: self.cv.itemconfigure(frame, image=app.gfx.rrect(w, P(42), P(12), c.input,
                                                                                         c.accent)))
        e.bind("<FocusOut>", lambda ev: self.cv.itemconfigure(frame, image=app.gfx.rrect(w, P(42), P(12), c.input,
                                                                                          c.line2)))
        self.entries.append(e)
        return e

    def build(self):
        pass

    def redraw(self, h=None):
        """Lay the dialog out again in place (for example after it grew)."""
        for e in self.entries:
            try:
                e.destroy()
            except tk.TclError:
                pass
        self.entries = []
        for b in self.widgets:
            try:
                b._hide_tip()
            except Exception:
                pass
        self.widgets = []
        self.app.hits.remove_owner(self)
        self.cv.delete(self.tag)
        if h is not None:
            self.h = self.app.px(h)
        self._draw(animate=False)
        self.app.hits.refresh()

    def close(self):
        if self.app.modal is not self:
            return
        self.app.modal = None
        for e in self.entries:
            try:
                e.destroy()
            except tk.TclError:
                pass
        for b in self.widgets:
            try:
                b._hide_tip()
            except Exception:
                pass
        try:
            self.app.root.unbind("<Escape>", self._bind)
        except tk.TclError:
            pass
        self.app.hits.remove_owner(self)
        self.cv.delete(self.tag)
        self.app.hits.block = LAYERS["tour"] if self.app.tour else 0
        self.app.rebind_keys()
        self.app.hits.refresh()


class Confirm(Modal):
    def __init__(self, app, title, message, ok_label, on_ok, danger=False, cancel_label="Cancel", on_cancel=None):
        self.title, self.message, self.ok_label, self.on_ok, self.danger = title, message, ok_label, on_ok, danger
        self.cancel_label, self.on_cancel = cancel_label, on_cancel
        super().__init__(app, 460, 250)

    def build(self):
        c, P = self.app.c, self.app.px
        icon = "trash" if self.danger else "info"
        self.cv.create_image(self.x + P(28), self.y + P(30), anchor="nw", tags=self.T(),
                             image=self.app.gfx.badge(icon, P(44), c.bad if self.danger else c.accent,
                                                      c.bad_soft if self.danger else c.accent_soft))
        self.text(P(86), P(36), self.title, "h2", c.text)
        self.text(P(86), P(66), self.message, "body", c.dim, width=self.w - P(114))
        ok = self.button(self.w - P(28), self.h - P(26), self.ok_label, self._ok,
                         kind="danger" if self.danger else "primary", h=40, anchor="se")
        self.button(ok.x - self.x - P(10), self.h - P(26), self.cancel_label, self._cancel, kind="secondary", h=40,
                    anchor="se")
        self.app.root.bind("<Return>", lambda e: self._ok())

    def _ok(self):
        self.close()
        self.on_ok()

    def _cancel(self):
        self.close()
        if self.on_cancel:
            self.on_cancel()


class RuleEditor(Modal):
    """Edit or create a rule.  File types are shown as chips: click "+ Add file
    type", type an extension like jpg, press Enter - and it becomes a chip.
    Click a chip's x to remove it."""

    WIDTH = 720
    CHIP_H = 30

    def __init__(self, app, rule):
        self.app = app  # needed by _height() before Modal sets everything up
        self.rule = rule
        self.is_new = rule is None
        self.catch = bool(rule and rule.get("catch_all"))
        base = rule or {"name": "", "exts": [], "icon": "folder", "color": random.choice(PALETTE_ORDER)}
        self.base = base
        self.color = base.get("color", "slate")
        self.icon = base.get("icon", "folder")
        self.name_text = base.get("name", "")
        self.exts = list(base.get("exts", []))
        self.adding = self.is_new and not self.catch
        self.add_text = ""
        self.note = None
        self.fresh = None
        self.focus = "name"
        self.name_entry = self.add_entry = None
        self.compact = False
        super().__init__(app, self.WIDTH, self._height())

    # ---- layout ----------------------------------------------------------------------------------
    def _chip_w(self, ext):
        return self.app.fonts["small_b"].measure("." + ext) + self.app.px(14 + 24)

    def _chips_layout(self):
        """Positions (relative to the chip box) of every chip and of the add control."""
        P = self.app.px
        inner = P(self.WIDTH - 56 - 24)
        h, gap = P(self.CHIP_H), P(8)
        items = [("chip", e, self._chip_w(e)) for e in self.exts]
        add_w = P(260) if self.adding else self.app.fonts["small_b"].measure("Add file type") + P(52)
        items.append(("add", None, add_w))
        x = y = 0
        placed = []
        for kind, ext, w in items:
            if x and x + w > inner:
                x, y = 0, y + h + gap
            placed.append((kind, ext, x, y, w))
            x += w + gap
        return placed, y + h + 2 * P(12)

    def _height(self):
        """Dialog height in design units (mirrors the layout in build)."""
        S = self.app.S
        box = 40 if self.catch else self._chips_layout()[1] / S
        total = 184 + box + 38 + (24 + 26 + 22) + (0 if self.compact else 24 + 36) + 24 + 40 + 26
        if not self.compact and total > self.app.cv.winfo_height() / S - 16:
            self.compact = True  # small window: leave out the icon picker row
            return self._height()
        return total

    def _sync(self):
        """Remember what was typed before the dialog is laid out again."""
        if self.name_entry is not None:
            try:
                self.name_text = self.name_entry.get()
            except tk.TclError:
                pass
        if self.add_entry is not None:
            try:
                self.add_text = self.add_entry.get()
            except tk.TclError:
                pass

    def refresh(self):
        self._sync()
        self.compact = False
        self.redraw(self._height())

    # ---- drawing --------------------------------------------------------------------------------------
    def build(self):
        app, cv, c, P = self.app, self.cv, self.app.c, self.app.px
        glyph, tint = app.palette_colors(self.color)
        title = "New rule" if self.is_new else "Edit “%s”" % self.base["name"]
        self.text(P(28), P(28), title, "h1", c.text)
        self.button(self.w - P(20), P(22), "", self.close, kind="ghost", icon="x", w=36, h=36, anchor="ne")
        y = P(80)
        self.text(P(28), y, "Folder name", "small_b", c.dim)
        self.text(self.w - P(28), y, "the sub-folder files are moved into", "small", c.faint, anchor="ne")
        self.name_entry = self.entry(P(28), y + P(22), self.w - P(56), self.name_text)
        self.name_entry.bind("<FocusIn>", lambda e: setattr(self, "focus", "name"), add="+")
        y += P(80)
        self.text(P(28), y, "File types", "small_b", c.dim)
        if self.catch:
            self.text(P(28), y + P(26), "This rule catches every file type that no other rule lists.", "body",
                      c.dim)
            y += P(24) + P(40)
        else:
            self.text(self.w - P(28), y, "click  +  to add a type,  ×  to remove one", "small", c.faint,
                      anchor="ne")
            y = self._draw_chips(P(28), y + P(24), glyph, tint)
        self.note_item = self.text(P(28), y + P(8), "", "small_b", c.dim)
        self._show_note()
        y += P(38)
        self.text(P(28), y, "Colour", "small_b", c.dim)
        y += P(24)
        self.swatches = {}
        for i, name in enumerate(PALETTE_ORDER):
            sx, sy = self.x + P(28) + i * P(36), self.y + y
            light, dark = PALETTE[name]
            col = dark if c.dark else light
            ring = cv.create_oval(sx - P(3), sy - P(3), sx + P(29), sy + P(29), width=P(2), tags=self.T(),
                                  outline=col if name == self.color else c.card)
            cv.create_image(sx, sy, anchor="nw", image=app.gfx.disc(P(26), col), tags=self.T())
            self.swatches[name] = ring
            app.hits.add(sx, sy, sx + P(26), sy + P(26), click=lambda n=name: self.pick_color(n), layer="modal",
                         owner=self)
        y += P(26) + P(22)
        if not self.compact:
            self.text(P(28), y, "Icon", "small_b", c.dim)
            y += P(24)
            for i, name in enumerate(RULE_ICONS):
                ix, iy = self.x + P(28) + i * P(42), self.y + y
                on = name == self.icon
                cv.create_image(ix, iy, anchor="nw", tags=self.T(), image=app.gfx.badge(
                    name, P(36), glyph if on else c.dim, tint if on else c.card2, radius=P(11)))
                app.hits.add(ix, iy, ix + P(36), iy + P(36), click=lambda n=name: self.pick_icon(n),
                             layer="modal", owner=self)
        if not self.is_new and not self.rule.get("builtin"):
            self.button(P(28), self.h - P(26), "Delete rule", self.delete, kind="danger", icon="trash", h=40,
                        anchor="sw")
        save = self.button(self.w - P(28), self.h - P(26), "Save rule", self.save, kind="primary", icon="check",
                           h=40, anchor="se")
        self.button(save.x - self.x - P(10), self.h - P(26), "Cancel", self.close, kind="secondary", h=40,
                    anchor="se")
        app.root.bind("<Return>", lambda e: self.save())
        target = self.add_entry if (self.focus == "add" and self.add_entry is not None) else self.name_entry
        target.focus_set()
        target.icursor("end")
        if self.fresh:
            self._pop_fresh()

    def _draw_chips(self, x, y, glyph, tint):
        """The chip box: one chip per file type plus the add button (or the add box)."""
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        placed, box_h = self._chips_layout()
        bx, by = self.x + x, self.y + y
        Card(app, bx, by, self.w - P(56), box_h, P(16), c.input, c.line2, self.T())
        pad = P(12)
        h = P(self.CHIP_H)
        self.add_entry = None
        for kind, ext, cx, cy, w in placed:
            X, Y = bx + pad + cx, by + pad + cy
            if kind == "chip":
                tags = self.T() + ((self.tag + "_fresh",) if ext == self.fresh else ())
                chip = cv.create_image(X, Y, anchor="nw", image=app.gfx.rrect(w, h, h / 2.0, tint), tags=tags)
                cv.create_text(X + P(14), Y + h / 2, text="." + ext, anchor="w", font=F["small_b"], fill=glyph,
                               tags=tags)
                cross = cv.create_image(X + w - P(15), Y + h / 2, image=app.gfx.icon("x", P(13), glyph), tags=tags)
                app.hits.add(X, Y, X + w - P(26), Y + h, layer="modal", owner=self,
                             click=lambda e=ext: self.explain(e),
                             enter=lambda i=chip, ww=w: cv.itemconfigure(i, image=app.gfx.rrect(
                                 ww, h, h / 2.0, tint, mix(tint, glyph, 0.55))),
                             leave=lambda i=chip, ww=w: cv.itemconfigure(i, image=app.gfx.rrect(ww, h, h / 2.0, tint)))
                app.hits.add(X + w - P(26), Y, X + w, Y + h, layer="modal", owner=self,
                             click=lambda e=ext: self.remove(e),
                             enter=lambda i=cross: cv.itemconfigure(i, image=app.gfx.icon("x", P(13), c.bad)),
                             leave=lambda i=cross: cv.itemconfigure(i, image=app.gfx.icon("x", P(13), glyph)))
            elif not self.adding:
                add = cv.create_image(X, Y, anchor="nw", tags=self.T(),
                                      image=app.gfx.rrect(w, h, h / 2.0, c.card, mix(c.line2, c.accent, 0.5)))
                cv.create_image(X + P(18), Y + h / 2, image=app.gfx.icon("plus", P(15), c.accent_ink),
                                tags=self.T())
                cv.create_text(X + P(32), Y + h / 2, text="Add file type", anchor="w", font=F["small_b"],
                               fill=c.accent_ink, tags=self.T())
                app.hits.add(X, Y, X + w, Y + h, layer="modal", owner=self, click=self.start_adding,
                             enter=lambda i=add, ww=w: cv.itemconfigure(i, image=app.gfx.rrect(
                                 ww, h, h / 2.0, c.accent_soft, c.accent)),
                             leave=lambda i=add, ww=w: cv.itemconfigure(i, image=app.gfx.rrect(
                                 ww, h, h / 2.0, c.card, mix(c.line2, c.accent, 0.5))))
            else:
                ew = w - P(70)
                cv.create_image(X, Y, anchor="nw", tags=self.T(), image=app.gfx.rrect(ew, h, h / 2.0, c.card, c.accent))
                cv.create_text(X + P(14), Y + h / 2, text=".", anchor="w", font=F["body_b"], fill=c.faint,
                               tags=self.T())
                e = tk.Entry(cv, relief="flat", bd=0, highlightthickness=0, bg=c.card, fg=c.text,
                             insertbackground=c.text, font=F["small_b"], selectbackground=c.accent,
                             selectforeground="#FFFFFF")
                e.insert(0, self.add_text)
                cv.create_window(X + P(24), Y + h / 2, anchor="w", window=e, width=ew - P(38), height=P(22),
                                 tags=self.T())
                e.bind("<Return>", lambda ev: (self.commit_add(), "break")[1])
                e.bind("<KP_Enter>", lambda ev: (self.commit_add(), "break")[1])
                e.bind("<Escape>", lambda ev: (self.stop_adding(), "break")[1])
                e.bind("<FocusIn>", lambda ev: setattr(self, "focus", "add"), add="+")
                self.entries.append(e)
                self.add_entry = e
                self.button(X - self.x + ew + P(8), Y - self.y, "Add", self.commit_add, kind="primary",
                            icon="plus", h=self.CHIP_H, w=62, font="small_b")
        return y + box_h

    def _show_note(self):
        c = self.app.c
        if self.note:
            text, tone = self.note
        elif self.adding and not self.catch:
            text, tone = "Type a file type, for example  jpg  or  pdf , and press Enter.", "info"
        elif not self.exts and not self.catch:
            text, tone = "No file types yet — click  + Add file type  to add one.", "warn"
        else:
            text, tone = "", "info"
        col = {"good": c.good, "bad": c.bad, "warn": c.warn, "info": c.dim}[tone]
        self.cv.itemconfigure(self.note_item, text=self.app.fonts.fit("small_b", text, self.w - self.app.px(56)),
                              fill=col)

    def _pop_fresh(self):
        tag = self.tag + "_fresh"
        P = self.app.px
        moved = [0.0]

        def bounce(k):
            target = -math.sin(math.pi * k) * P(6)
            self.cv.move(tag, 0, target - moved[0])
            moved[0] = target
        self.app.anim.tween(0.4, bounce, ease=ease_out_cubic)
        self.fresh = None

    # ---- actions --------------------------------------------------------------------------------------------
    def folder_label(self):
        name = (self.name_entry.get() if self.name_entry is not None else self.name_text).strip()
        return "“%s”" % name if name else "this folder"

    def owner_of(self, ext):
        for other in self.app.settings["rules"]:
            if other is not self.rule and not other.get("catch_all") and ext in other.get("exts", []):
                return other
        return None

    def start_adding(self):
        self.adding = True
        self.focus = "add"
        self.note = None
        self.refresh()

    def stop_adding(self):
        self.adding = False
        self.add_text = ""
        self.focus = "name"
        self.note = None
        self.refresh()

    def commit_add(self):
        text = self.add_entry.get() if self.add_entry is not None else ""
        added, problems, invalid = [], [], []
        for tok in re.split(r"[\s,;]+", text.lower()):
            tok = tok.strip().lstrip("*").strip(".")
            if not tok:
                continue
            if not re.match(r"^[a-z0-9][a-z0-9_.+!~-]*$", tok):
                problems.append("“%s” isn't a file type" % tok)
                invalid.append(tok)
            elif tok in self.exts:
                problems.append(".%s is already in this rule" % tok)
            else:
                self.exts.append(tok)
                added.append(tok)
        self.add_text = ""
        if self.add_entry is not None:
            self.add_entry.delete(0, "end")  # ready for the next one
        if added:
            self.fresh = added[-1]
            names = ", ".join("." + a for a in added)
            moved = [self.owner_of(a) for a in added]
            moved = sorted(set(o["name"] for o in moved if o))
            note = "✓ %s files will now be sorted into %s automatically." % (names, self.folder_label())
            if moved:
                note += " (Moved out of %s.)" % ", ".join(moved)
            self.note = (note, "good")
        elif problems:
            self.note = ("; ".join(problems) + ".", "bad")
        else:
            self.note = None
        self.focus = "add"
        self.refresh()
        return invalid

    def remove(self, ext):
        if ext in self.exts:
            self.exts.remove(ext)
        self.note = ("Removed .%s — those files won't be moved to %s any more." % (ext, self.folder_label()),
                     "info")
        self.refresh()

    def explain(self, ext):
        self.note = ("Files that end in .%s are sorted into %s automatically. Click × to remove it."
                     % (ext, self.folder_label()), "info")
        self._sync()
        self._show_note()

    def pick_color(self, name):
        self.color = name
        self.refresh()

    def pick_icon(self, name):
        self.icon = name
        self.refresh()

    def _err(self, msg):
        self.note = (msg, "bad")
        self._show_note()
        return None

    def save(self):
        app = self.app
        if self.adding and self.add_entry is not None and self.add_entry.get().strip():
            if self.commit_add():  # something typed but not added yet - add it first
                return None  # (only real typos stop the save; duplicates are simply ignored)
        name = (self.name_entry.get() if self.name_entry is not None else self.name_text).strip()
        bad = '\\/:*?"<>|'
        rules = app.settings["rules"]
        if not name:
            return self._err("Please give the folder a name.")
        if any(ch in name for ch in bad) or name in (".", "..") or name.endswith("."):
            return self._err("Folder names can't contain  \\ / : * ? \" < > |")
        if any(r is not self.rule and r["name"].lower() == name.lower() for r in rules):
            return self._err("Another rule already uses a folder with that name.")
        if not self.catch and not self.exts:
            return self._err("Add at least one file type — click  + Add file type.")
        moved = []
        if not self.catch:  # a file type belongs to one folder only: take it away from other rules
            for other in rules:
                if other is self.rule or other.get("catch_all"):
                    continue
                taken = [e for e in other.get("exts", []) if e in self.exts]
                if taken:
                    other["exts"] = [e for e in other["exts"] if e not in self.exts]
                    moved.append(other["name"])
        if self.is_new:
            rule = {"id": "rule%x" % random.randint(0x10000, 0xFFFFFF), "name": name, "exts": list(self.exts),
                    "icon": self.icon, "color": self.color, "on": True, "builtin": False, "catch_all": False}
            rules.insert(0, rule)
        else:
            rule = self.rule
            rule.update(name=name, icon=self.icon, color=self.color)
            if not self.catch:
                rule["exts"] = list(self.exts)
        self.close()
        app.rules_changed()
        glyph, tint = app.rule_colors(rule)
        if self.catch:
            sub = "Files that match no other rule go to “%s”." % name
        else:
            shown = ", ".join("." + e for e in rule["exts"][:4]) + (" …" if len(rule["exts"]) > 4 else "")
            sub = "%s files go to “%s”." % (shown, name)
            if moved:
                sub += " (Moved from %s.)" % ", ".join(moved)
        app.toasts.show("Rule saved", sub, icon=rule.get("icon", "folder"), glyph=glyph, tint=tint)

    def delete(self):
        app = self.app
        rules = app.settings["rules"]
        idx = rules.index(self.rule)
        rule = rules.pop(idx)
        self.close()
        app.rules_changed()

        def undo():
            rules.insert(min(idx, len(rules) - 1), rule)
            app.rules_changed()
        app.toasts.show("Rule deleted", "“%s” won't be used any more." % rule["name"], icon="trash",
                        glyph=app.c.bad, tint=app.c.bad_soft, action=("Undo", undo))


# =============================================================================
#  The tutorial: six animated steps
# =============================================================================

def rounded_poly(x0, y0, x1, y1, r):
    """Points for a smooth rounded rectangle polygon (for outlines/glows)."""
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


class Tour:
    STEPS = [
        ("Welcome to TiDY",
         "TiDY keeps your Downloads folder neat — all by itself. This quick tour shows how it works. "
         "It takes about a minute.", "welcome"),
        ("It watches one folder",
         "TiDY looks after one folder — your Downloads folder is picked for you. While it is watching, "
         "Windows tells TiDY the moment a new file appears there.", "watch"),
        ("Every file finds its home",
         "TiDY checks the end of the file name (.jpg, .pdf, .mp3 …) and moves the file into a matching "
         "folder like Images, Documents or Music.", "sort"),
        ("Downloads are safe",
         "TiDY waits until a download has completely finished before it touches it. It never deletes or "
         "overwrites anything — if a name is taken, it adds a number.", "safe"),
        ("You're in charge",
         "Edit the rules any time, switch folders on or off, and undo any move from the Activity page.",
         "control"),
        ("Ready when you are",
         "Press Start tidying, then save something into your folder — or try the sample files. "
         "Whenever you need help, the Guide page (in the sidebar) explains everything step by step.", "ready"),
    ]

    def __init__(self, app, step=0):
        self.app, self.cv = app, app.cv
        self.tag = "tour"
        self.i = step
        self.widgets = []
        app.close_modal()
        app.tour = self
        app.hits.block = LAYERS["tour"]
        app.hits._set_hover(None)
        self.build()
        app.rebind_keys()

    def build(self):
        app, cv, c, P = self.app, self.cv, self.app.c, self.app.px
        cv.delete(self.tag)
        app.hits.remove_owner(self)
        W, H = cv.winfo_width(), cv.winfo_height()
        self.w, self.h = P(640), P(528)
        if self.h > H - P(30):
            self.h = H - P(30)
        self.x, self.y = (W - self.w) // 2, (H - self.h) // 2
        tg = (self.tag,)
        cv.create_image(0, 0, anchor="nw", image=app.gfx.overlay(W, H, c.overlay, c.overlay_a), tags=tg)
        app.hits.add(0, 0, W, H, layer="tour", owner=self, cursor="")
        Card(app, self.x, self.y, self.w, self.h, P(26), c.card, c.line2 if c.dark else None, tg, shadow=True,
             shadow_alpha=0.22, blur=P(24))
        self.pw, self.ph = self.w - P(32), P(282)
        self.px0, self.py0 = self.x + P(16), self.y + P(16)
        top = mix(c.card, c.accent, 0.10 if c.dark else 0.08)
        bottom = mix(c.card, c.accent, 0.20 if c.dark else 0.17)
        self.panel = top
        self.panel_mid = mix(top, bottom, 0.5)
        Card(app, self.px0, self.py0, self.pw, self.ph, P(18), top, None, tg, fill2=bottom)
        b = Button(app, self.px0 + self.pw - P(12), self.py0 + P(12), "Skip tour", self.close, kind="ghost",
                   h=30, anchor="ne", layer="tour", owner=self, tags=tg, surface=top, font="small_b")
        self.widgets.append(b)
        self.show(self.i, first=True)

    def show(self, i, first=False):
        app, cv, c, P, F = self.app, self.cv, self.app.c, self.app.px, self.app.fonts
        self.i = i
        cv.delete("tour_dyn")
        for w in list(self.widgets[1:]):
            w.destroy()
        self.widgets = self.widgets[:1]
        app.anim.unloop(("tour", "scene"))
        title, body, scene = self.STEPS[i]
        tg = (self.tag, "tour_dyn")
        tx = self.x + P(40)
        ty = self.py0 + self.ph + P(26)
        items = [
            (cv.create_text(tx, ty, text="STEP %d OF %d" % (i + 1, len(self.STEPS)), anchor="nw",
                            font=F["tiny"], fill=c.card, tags=tg), c.accent_ink),
            (cv.create_text(tx, ty + P(22), text=title, anchor="nw", font=F["tour_title"], fill=c.card, tags=tg),
             c.text),
            (cv.create_text(tx, ty + P(62), text=body, anchor="nw", font=F["tour_body"], fill=c.card,
                            width=self.w - P(80), tags=tg), c.dim),
        ]

        def fade(k):
            for item, col in items:
                cv.itemconfigure(item, fill=mix(c.card, col, k))
        app.anim.tween(0.45, fade, key=("tour", "fade"))
        # progress dots (the current one is a stretched pill)
        fy = self.y + self.h - P(44)
        dx = tx
        for j in range(len(self.STEPS)):
            w = P(22) if j == i else P(8)
            col = c.accent if j == i else (c.line2 if not c.dark else c.toggle_off)
            cv.create_image(dx, fy, anchor="w", image=app.gfx.rrect(w, P(8), P(4), col), tags=tg)
            dx += w + P(6)
        last = i == len(self.STEPS) - 1
        right = self.x + self.w - P(28)
        if last:
            nb = Button(app, right, fy, "Start tidying", self.finish, kind="primary", icon="play", h=42,
                        anchor="e", layer="tour", owner=self, tags=tg, surface=c.card)
            sb = Button(app, nb.x - P(10), fy, "Try sample files", self.samples, kind="soft", icon="sparkle",
                        h=42, anchor="e", layer="tour", owner=self, tags=tg, surface=c.card)
            self.widgets += [nb, sb]
        else:
            nb = Button(app, right, fy, "Next", self.next, kind="primary", icon="arrow_r", h=42, w=112, anchor="e",
                        layer="tour", owner=self, tags=tg, surface=c.card)
            self.widgets.append(nb)
            if i > 0:
                bb = Button(app, nb.x - P(10), fy, "Back", self.back, kind="ghost", h=42, anchor="e", layer="tour",
                            owner=self, tags=tg, surface=c.card)
                self.widgets.append(bb)
        start = time.perf_counter()
        draw = getattr(self, "scene_" + scene)

        def frame(now):
            cv.delete("tour_scene")
            draw(now - start)
            cv.tag_raise("tour_scene", "tour")
            for w in self.widgets:
                cv.tag_raise(w.tag)
            return True
        frame(start)
        app.anim.loop(("tour", "scene"), frame)
        app.hits.refresh()

    # ---- navigation ---------------------------------------------------------------------
    def next(self):
        if self.i < len(self.STEPS) - 1:
            self.show(self.i + 1)

    def back(self):
        if self.i > 0:
            self.show(self.i - 1)

    def close(self):
        app = self.app
        if app.tour is not self:
            return
        app.tour = None
        app.anim.unloop(("tour", "scene"))
        for w in self.widgets:
            w.destroy()
        app.hits.remove_owner(self)
        self.cv.delete(self.tag)
        self.cv.delete("tour_scene")
        app.hits.block = 0
        app.rebind_keys()
        app.hits.refresh()
        if not app.settings.get("tutorial_done"):
            app.settings["tutorial_done"] = True
            app.save_soon()

    def finish(self):
        self.close()
        if not self.app.engine.running:
            self.app.start_watching()
        self.app.show_page("home")

    def samples(self):
        self.close()
        self.app.show_page("home")
        self.app.drop_samples()

    # ---- scene helpers (everything is redrawn every frame) -------------------------------------
    def _img(self, x, y, img, anchor="center"):
        return self.cv.create_image(x, y, image=img, anchor=anchor, tags=("tour_scene", self.tag))

    def _oval(self, x, y, r, **kw):
        return self.cv.create_oval(x - r, y - r, x + r, y + r, tags=("tour_scene", self.tag), **kw)

    def _text(self, x, y, s, font, fill, anchor="center"):
        return self.cv.create_text(x, y, text=s, font=self.app.fonts[font], fill=fill, anchor=anchor,
                                   tags=("tour_scene", self.tag))

    def _sparkle(self, x, y, r, fill):
        if r < 1:
            return
        pts = []
        for px_, py_ in sparkle_points(x, y, r, k=0.18, n=16):
            pts += [px_, py_]
        self.cv.create_polygon(pts, fill=fill, outline="", tags=("tour_scene", self.tag))

    def _rings(self, cx, cy, r0, r1, period=2.4, n=3, t=0.0, color=None):
        c, P = self.app.c, self.app.px
        color = color or c.accent
        for i in range(n):
            k = ((t / period) + i / float(n)) % 1.0
            r = r0 + (r1 - r0) * ease_out_cubic(k)
            self._oval(cx, cy, r, outline=mix(color, self.panel_mid, 0.3 + 0.7 * k), width=P(2))

    def _core(self, cx, cy, d, icon="folder"):
        app, c = self.app, self.app.c
        self._img(cx, cy, app.gfx.rrect(d, d, d / 2.0, c.accent_hi, fill2=c.accent_deep))
        self._img(cx, cy, app.gfx.icon(icon, d * 0.44, "#FFFFFF", stroke=1.8))

    def _badge(self, rid, x, y, size=44):
        app, P = self.app, self.app.px
        rule = app.rule_by_id(rid) or {"icon": "folder", "color": "slate"}
        glyph, tint = app.rule_colors(rule)
        self._img(x, y, app.gfx.badge(rule.get("icon", "folder"), P(size), glyph, tint))
        return glyph, tint

    def _chip(self, x, y, label, rid):
        app, c, P, F = self.app, self.app.c, self.app.px, self.app.fonts
        rule = app.rule_by_id(rid) or {"icon": "folder", "color": "slate"}
        glyph, tint = app.rule_colors(rule)
        w = F["small_b"].measure(label) + P(46)
        h = P(32)
        self._img(x, y, app.gfx.rrect(w, h, h / 2.0, c.card, mix(c.line2, glyph, 0.5)))
        self._img(x - w / 2 + P(17), y, app.gfx.badge(rule.get("icon", "folder"), P(24), glyph, tint, radius=P(12)))
        self._text(x - w / 2 + P(34), y, label, "small_b", c.text, anchor="w")

    # ---- the six scenes ------------------------------------------------------------------------
    def scene_welcome(self, t):
        app, c, P = self.app, self.app.c, self.app.px
        cx, cy = self.px0 + self.pw / 2, self.py0 + self.ph / 2 + P(4)
        self._rings(cx, cy, P(60), P(132), period=3.2, t=t)
        k = clamp(t / 0.8)
        ly = cy - (1 - ease_out_back(k)) * P(70)
        orbit = [("images", 0.0), ("music", 2.1), ("documents", 4.2)]
        for rid, phase in orbit:
            a = t * 0.6 + phase
            ox = cx + math.cos(a) * P(150)
            oy = cy + math.sin(a) * P(78)
            if k > 0.5:
                self._badge(rid, ox, oy, 40)
        self._img(cx, ly, app.gfx.logo(P(116)))
        for i, (ang, dist, size) in enumerate(((-40, 92, 9), (210, 100, 7), (150, 86, 6), (20, 104, 5),
                                               (260, 78, 5), (95, 112, 6))):
            a = math.radians(ang)
            tw = 0.5 + 0.5 * math.sin(t * 3.0 + i * 1.7)
            self._sparkle(cx + math.cos(a) * P(dist), cy + math.sin(a) * P(dist) * 0.8,
                          P(size) * (0.4 + 0.6 * tw) * k, mix(self.panel_mid, c.accent_hi, 0.35 + 0.65 * tw))

    def scene_watch(self, t):
        c, P = self.app.c, self.app.px
        cx, cy = self.px0 + self.pw / 2, self.py0 + self.ph / 2 + P(14)
        self._rings(cx, cy, P(44), P(120), t=t)
        period = 2.6
        k = (t % period) / period
        files = [("report.pdf", "documents"), ("photo.jpg", "images"), ("song.mp3", "music")]
        name, rid = files[int(t // period) % len(files)]
        if k < 0.62:
            q = ease_in_cubic(clamp(k / 0.62))
            fy = self.py0 + P(40) + (cy - self.py0 - P(40)) * q
            self._chip(cx, fy, name, rid)
        pulse = math.sin(math.pi * clamp((k - 0.62) / 0.25)) if 0.62 <= k < 0.87 else 0
        d = P(78) + P(10) * pulse
        self._core(cx, cy, d)
        dot = 0.5 + 0.5 * math.sin(t * 4)
        self._oval(cx - P(70), self.py0 + self.ph - P(22), P(4), fill=mix(c.good, self.panel, 0.4 * dot), outline="")
        self._text(cx - P(58), self.py0 + self.ph - P(22), "Watching “Downloads” … new file spotted!"
                   if 0.55 < k < 0.95 else "Watching “Downloads”", "small_b", c.accent_ink, anchor="w")

    def scene_sort(self, t):
        c, P = self.app.c, self.app.px
        sx, sy = self.px0 + P(120), self.py0 + self.ph / 2
        self._rings(sx, sy, P(34), P(70), period=2.0, n=2, t=t)
        self._core(sx, sy, P(64))
        self._text(sx, sy + P(84), "Downloads", "small_b", c.dim)
        targets = [("images", "Images", ".jpg"), ("music", "Music", ".mp3"), ("documents", "Documents", ".pdf")]
        bx = self.px0 + self.pw - P(190)
        period = 1.7
        n = int(t // period)
        k = (t % period) / period
        active = n % 3
        for j, (rid, label, ext) in enumerate(targets):
            by = sy + (j - 1) * P(72)
            bounce = math.sin(math.pi * clamp((k - 0.72) / 0.28)) * P(6) if (j == active and k > 0.72) else 0
            glyph, _ = self._badge(rid, bx, by - bounce, 46)
            self._text(bx + P(34), by, label, "body_b", c.text, anchor="w")
            if j == active and k > 0.72:
                kk = clamp((k - 0.72) / 0.28)
                for a in range(8):
                    ang = a * math.pi / 4
                    r = P(4) * (1 - kk)
                    self._oval(bx + math.cos(ang) * P(36) * kk, by + math.sin(ang) * P(36) * kk, r,
                               fill=mix(glyph, self.panel_mid, kk), outline="")
        rid, label, ext = targets[active]
        if k < 0.75:
            q = ease_in_out_cubic(clamp(k / 0.75))
            by = sy + (active - 1) * P(72)
            px_, py_ = bezier((sx + P(40), sy), ((sx + bx) / 2, min(sy, by) - P(60)), (bx - P(30), by), q)
            self._chip(px_, py_, "file" + ext, rid)

    def scene_safe(self, t):
        app, c, P, F = self.app, self.app.c, self.app.px, self.app.fonts
        period = 4.6
        k = t % period
        cx = self.px0 + P(40)
        cy = self.py0 + self.ph / 2 + P(16)
        w, h = P(330), P(84)
        bx = self.px0 + self.pw - P(90)
        glyph, _ = self._badge("videos", bx, cy, 50)
        self._text(bx, cy + P(42), "Videos", "small_b", c.dim)
        prog = clamp(k / 2.6)
        if k < 3.2:
            x = cx
            y = cy
        else:
            q = ease_in_cubic(clamp((k - 3.2) / 0.8))
            x = cx + (bx - P(30) - cx - w / 2) * q
            y = cy - math.sin(math.pi * q) * P(30)
        if k < 4.0:
            self._img(x, y, app.gfx.rrect(w, h, P(16), c.card, c.line), anchor="w")
            self._img(x + P(16), y, app.gfx.badge("video", P(42), glyph, mix(c.card, glyph, 0.16)), anchor="w")
            done = prog >= 1
            name = "holiday-video.mp4" if done else "holiday-video.mp4.crdownload"
            self._text(x + P(70), y - P(16), F.fit("small_b", name, w - P(90)), "small_b", c.text, anchor="w")
            bar_w = w - P(90)
            self.cv.create_line(x + P(70), y + P(4), x + P(70) + bar_w, y + P(4), fill=c.line, width=P(6),
                                capstyle="round", tags=("tour_scene", self.tag))
            self.cv.create_line(x + P(70), y + P(4), x + P(70) + max(1, bar_w * prog), y + P(4),
                                fill=c.good if done else c.accent, width=P(6), capstyle="round",
                                tags=("tour_scene", self.tag))
            msg = "Finished — safe to tidy" if done else "Downloading … %d%%  (TiDY waits)" % (prog * 100)
            self._text(x + P(70), y + P(24), msg, "small", c.good if done else c.dim, anchor="w")
        self._img(self.px0 + P(40), self.py0 + P(36), app.gfx.badge("shield", P(30), c.good, c.good_soft, radius=P(10)),
                  anchor="w")
        self._text(self.px0 + P(78), self.py0 + P(36), "Never deletes. Never overwrites.", "small_b", c.good, anchor="w")

    def scene_control(self, t):
        app, c, P = self.app, self.app.c, self.app.px
        x = self.px0 + P(70)
        w, h = self.pw - P(140), P(62)
        y1 = self.py0 + P(70)
        y2 = y1 + h + P(14)
        period = 5.0
        k = t % period
        flip = 1.0 if 1.2 <= k < 3.6 else 0.0
        if 1.0 <= k < 1.4:
            flip = ease_out_back(clamp((k - 1.0) / 0.4))
        elif 3.4 <= k < 3.8:
            flip = 1 - ease_out_cubic(clamp((k - 3.4) / 0.4))
        for j, (rid, label, exts, val) in enumerate((("images", "Images", ".jpg .png .gif", 1.0),
                                                     ("music", "Music", ".mp3 .wav .flac", 1 - flip))):
            yy = y1 if j == 0 else y2
            self._img(x, yy, app.gfx.rrect(w, h, P(14), c.card, c.line), anchor="nw")
            self._badge(rid, x + P(34), yy + h / 2, 38)
            self._text(x + P(62), yy + h / 2 - P(9), label, "body_b", c.text if val > 0.5 else c.faint, anchor="w")
            self._text(x + P(62), yy + h / 2 + P(11), exts, "small", c.dim, anchor="w")
            tx_, ty_ = x + w - P(62), yy + h / 2
            q = round(val * 8) / 8.0
            self._img(tx_, ty_, app.gfx.rrect(P(42), P(24), P(12), mix(c.toggle_off, c.accent, q)), anchor="w")
            self._img(tx_ + P(12) + P(18) * val, ty_, app.gfx.disc(P(18), c.knob))
        uy = y2 + h + P(40)
        ux = self.px0 + self.pw / 2
        pulse = math.sin(math.pi * clamp((k - 4.1) / 0.35)) if 4.1 <= k < 4.45 else 0
        self._img(ux, uy - pulse * P(2), app.gfx.rrect(P(150), P(38), P(19), mix(c.accent_soft, c.accent, 0.12 * pulse)))
        self._img(ux - P(38), uy - pulse * P(2), app.gfx.icon("undo", P(18), c.accent_ink))
        self._text(ux + P(10), uy - pulse * P(2), "Undo move", "small_b", c.accent_ink)
        # a little mouse pointer that does the clicking
        toggle_pt = (x + w - P(30), y2 + h / 2 + P(4))
        undo_pt = (ux + P(30), uy + P(6))
        rest = (self.px0 + self.pw - P(60), self.py0 + self.ph - P(30))
        if k < 1.0:
            p = bezier(rest, (rest[0], toggle_pt[1]), toggle_pt, ease_in_out_cubic(k))
        elif k < 3.6:
            p = toggle_pt if k < 2.2 else bezier(toggle_pt, (undo_pt[0] + P(60), toggle_pt[1]), undo_pt,
                                                 ease_in_out_cubic(clamp((k - 2.2) / 1.6)))
        elif k < 4.6:
            p = undo_pt
        else:
            p = bezier(undo_pt, (rest[0], undo_pt[1]), rest, ease_in_out_cubic(clamp((k - 4.6) / 0.4)))
        if 0.95 <= k < 1.3 or 4.05 <= k < 4.4:
            kk = clamp(((k - 0.95) if k < 2 else (k - 4.05)) / 0.35)
            self._oval(p[0], p[1], P(6) + P(14) * kk, outline=mix(c.accent, self.panel_mid, kk), width=P(2))
        px_, py_ = p
        pts = [px_, py_, px_, py_ + P(17), px_ + P(4.5), py_ + P(13), px_ + P(8), py_ + P(20), px_ + P(10.5), py_ + P(19),
               px_ + P(7.2), py_ + P(12), px_ + P(12.5), py_ + P(12)]
        self.cv.create_polygon(pts, fill="#FFFFFF", outline="#1B1E2B", width=max(1, P(1.4)), joinstyle="round",
                               tags=("tour_scene", self.tag))

    def scene_ready(self, t):
        app, c, P = self.app, self.app.c, self.app.px
        cx, cy = self.px0 + self.pw / 2, self.py0 + self.ph / 2
        w, h = P(232), P(58)
        for i in range(2):
            k = ((t / 2.0) + i * 0.5) % 1.0
            g = P(4) + P(26) * ease_out_cubic(k)
            self.cv.create_polygon(rounded_poly(cx - w / 2 - g, cy - h / 2 - g, cx + w / 2 + g, cy + h / 2 + g,
                                                h / 2 + g), smooth=True, fill="",
                                   outline=mix(c.accent, self.panel_mid, 0.2 + 0.8 * k), width=P(2),
                                   tags=("tour_scene", self.tag))
        bob = math.sin(t * 2.4) * P(3)
        self._img(cx, cy + bob, app.gfx.rrect(w, h, h / 2.0, c.accent_hi, fill2=c.accent_deep))
        self._img(cx - P(62), cy + bob, app.gfx.icon("play", P(22), "#FFFFFF"))
        self._text(cx + P(14), cy + bob, "Start tidying", "h3", "#FFFFFF")
        for i in range(7):
            a = math.radians(i * 51 + t * 25)
            d = P(150) + P(10) * math.sin(t * 2 + i)
            tw = 0.5 + 0.5 * math.sin(t * 3.2 + i * 1.3)
            self._sparkle(cx + math.cos(a) * d, cy + math.sin(a) * d * 0.55, P(4 + (i % 3) * 2) * (0.4 + 0.6 * tw),
                          mix(self.panel_mid, c.accent_hi, 0.3 + 0.7 * tw))


# =============================================================================
#  Launch splash
# =============================================================================

class Splash:
    def __init__(self, app, done):
        self.app, self.cv, self.done = app, app.cv, done
        app.splash = self
        app.hits.block = LAYERS["splash"]
        c, P, F = app.c, app.px, app.fonts
        cv = self.cv
        W, H = cv.winfo_width(), cv.winfo_height()
        self.tag = "splash"
        tg = (self.tag,)
        cv.create_rectangle(0, 0, W, H, fill=c.bg, outline="", tags=tg)
        cx, cy = W / 2, H / 2 - P(40)
        self.rings = [cv.create_oval(0, 0, 0, 0, outline=c.bg, width=P(2), tags=tg) for _ in range(2)]
        self.logo = cv.create_image(cx, cy - P(60), image=app.gfx.logo(P(124)), tags=tg)
        f = F["splash"]
        word = [("T", c.text), ("i", c.accent), ("D", c.text), ("Y", c.text)]
        total = sum(f.measure(s) for s, _ in word)
        x = cx - total / 2
        self.letters = []
        for s, col in word:
            item = cv.create_text(x, cy + P(108), text=s, anchor="w", font=f, fill=c.bg, tags=tg)
            self.letters.append((item, col, x))
            x += f.measure(s)
        self.tagline = cv.create_text(cx, cy + P(156), text=APP_TAGLINE, font=F["body"], fill=c.bg, tags=tg)
        app.hits.add(0, 0, W, H, click=self.finish, layer="splash", owner=self, cursor="")
        self.cx, self.cy, self.H = cx, cy, H
        self.start = time.perf_counter()
        self.lifted = False
        app.anim.loop(("splash", "f"), self.frame)

    def frame(self, now):
        cv, c, P = self.cv, self.app.c, self.app.px
        t = now - self.start
        cx, cy = self.cx, self.cy
        k = clamp(t / 0.75)
        cv.coords(self.logo, cx, cy - (1 - ease_out_back(k)) * P(70))
        for i, ring in enumerate(self.rings):
            kk = clamp((t - 0.35 - i * 0.18) / 1.1)
            r = P(60) + P(110) * ease_out_cubic(kk)
            cv.coords(ring, cx - r, cy - r, cx + r, cy + r)
            cv.itemconfigure(ring, outline=mix(c.accent, c.bg, 0.25 + 0.75 * kk) if kk > 0 else c.bg)
        for i, (item, col, x) in enumerate(self.letters):
            kk = clamp((t - 0.35 - i * 0.09) / 0.4)
            cv.coords(item, x, cy + P(108) + (1 - ease_out_back(kk)) * P(18))
            cv.itemconfigure(item, fill=mix(c.bg, col, kk))
        kk = clamp((t - 0.8) / 0.5)
        cv.itemconfigure(self.tagline, fill=mix(c.bg, c.dim, kk))
        if t > 1.75 and not self.lifted:
            self.finish()
            return False
        return True

    def finish(self):
        if self.lifted:
            return
        self.lifted = True
        app = self.app
        app.anim.unloop(("splash", "f"))
        app.hits.remove_owner(self)
        moved = [0.0]

        def lift(k):
            target = -self.H * k
            self.cv.move(self.tag, 0, target - moved[0])
            moved[0] = target

        def gone():
            self.cv.delete(self.tag)
            app.splash = None
            app.hits.block = LAYERS["tour"] if app.tour else 0
            app.hits.refresh()
            self.done()
        app.anim.tween(0.55, lift, ease=ease_in_out_cubic, done=gone)

# =============================================================================
#  The application
# =============================================================================

PAGES = {"home": HomePage, "activity": ActivityPage, "rules": RulesPage, "settings": SettingsPage,
         "guide": GuidePage}


class App:
    def __init__(self, background=False):
        self.background = background
        self.store = Store()
        self.settings = self.store.settings
        self.engine = Engine(self.store)
        self.root = tk.Tk(className="TiDY")
        self.root.withdraw()
        self.root.title("TiDY")
        self.root.report_callback_exception = self._tk_error
        forced = os.environ.get("TIDY_SCALE")
        try:
            self.S = float(forced) if forced else max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except (tk.TclError, ValueError):
            self.S = 1.0
        self.fonts = Fonts(self)
        self.anim = Animator(self.root)
        self.anim.enabled = bool(self.settings.get("animations", True))
        self.gfx = Gfx(self)
        self.c = THEMES[self.resolve_theme()]
        self.side_w = self.px(236)
        self.cv = tk.Canvas(self.root, bg=self.c.bg, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.hits = HitMap(self)
        self.scroll_areas = []
        self.modal = None
        self.tour = None
        self.splash = None
        self.page = None
        self.page_name = "home"
        self.activity_filter = None
        self.guide_jump = None
        self.loose_count = 0
        self.status = None
        self._status_job = None
        self._resize_job = None
        self._refresh_job = None
        self._save_job = None
        self._loose_job = None
        self._size = None
        self._burst = []
        self._burst_job = None
        self._sample_i = 0
        self._told_tray = False
        self._quitting = False
        self._no_save = False
        self.toasts = Toasts(self)
        self.popup = DesktopPopup(self)
        self.chime = Chime()
        self.sidebar = Sidebar(self)
        self.tray = TrayIcon(self)
        self._setup_window()
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", lambda e: self._wheel(e, -120))
        self.cv.bind("<Button-5>", lambda e: self._wheel(e, 120))
        try:
            self.cv.bind("<TouchpadScroll>", self._touchpad)  # Tk 9+: laptop touchpads
        except tk.TclError:
            pass  # older Tk sends touchpad scrolling as normal mouse-wheel events
        self.cv.bind("<Configure>", self._configure)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.rebind_keys()

    # ---- basics --------------------------------------------------------------------
    def px(self, v):
        return int(round(v * self.S))

    def resolve_theme(self):
        mode = self.settings.get("theme", "system")
        if mode in ("light", "dark"):
            return mode
        return "dark" if system_uses_dark_theme() else "light"

    def content_box(self):
        W, H = self.cv.winfo_width(), self.cv.winfo_height()
        return self.side_w + self.px(40), 0, W - self.px(40), H

    def palette_colors(self, name):
        light, dark = PALETTE.get(name, PALETTE["slate"])
        c = self.c
        if c.dark:
            return dark, mix(c.card, dark, 0.16)
        return light, mix(c.card, dark, 0.2)

    def rule_colors(self, rule):
        return self.palette_colors(rule.get("color", "slate"))

    def rule_by_id(self, rid):
        for r in self.settings["rules"]:
            if r["id"] == rid:
                return r
        return None

    def visible(self):
        try:
            return self.root.state() == "normal"
        except tk.TclError:
            return False

    def focused(self):
        try:
            return self.visible() and self.root.focus_displayof() is not None
        except (tk.TclError, KeyError):
            return False

    def save_soon(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(700, self._save_now)

    def _save_now(self):
        self._save_job = None
        if not self._no_save:
            self.store.save()

    def _tk_error(self, exc, val, tb):
        log("UI error:", "".join(traceback.format_exception(exc, val, tb)))
        try:
            self.toasts.show("Oops - something went wrong", "Details were written to TiDY's log file.",
                             icon="info", glyph=self.c.bad, tint=self.c.bad_soft)
        except Exception:
            pass

    # ---- window ---------------------------------------------------------------------
    def _setup_window(self):
        root, P = self.root, self.px
        try:
            icons = [self.gfx.logo(s) for s in (16, 24, 32, 48, 64, 128)]
            root.iconphoto(True, *icons)
        except tk.TclError:
            pass
        area = work_area() or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        aw, ah = area[2] - area[0], area[3] - area[1]
        # never ask for a window bigger than the screen (small laptops, big zoom levels)
        root.minsize(min(P(1000), aw - P(20)), min(P(640), ah - P(50)))
        w = min(P(1180), aw - P(40))
        h = min(P(770), ah - P(70))
        geo = self.settings.get("geometry")
        placed = False
        if geo:
            try:
                size, x, y = geo.split("+")[0], int(geo.split("+")[1]), int(geo.split("+")[2])
                gw, gh = (int(v) for v in size.split("x"))
                if area[0] - 50 <= x <= area[2] - 200 and area[1] - 10 <= y <= area[3] - 120 and \
                        gw <= aw + 20 and gh <= ah + 20:
                    root.geometry("%dx%d+%d+%d" % (gw, gh, x, y))
                    placed = True
            except (ValueError, IndexError):
                pass
        if not placed:
            x = area[0] + (aw - w) // 2
            y = area[1] + max(0, (ah - h) // 2 - P(16))
            root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def style_titlebar(self):
        c = self.c
        style_native_window(self.root, c.side if c.dark else c.side, c.text, c.dark)

    def _configure(self, event):
        if event.widget is not self.cv:
            return
        size = (self.cv.winfo_width(), self.cv.winfo_height())
        if size == self._size or size[0] < 50:
            return
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(90, self.layout)

    def layout(self, animate=False):
        self._resize_job = None
        self._size = (self.cv.winfo_width(), self.cv.winfo_height())
        if self._size[0] < 300 or self._size[1] < 200:  # not on screen yet
            return
        self.toasts.hide(fast=True)
        self.sidebar.build(self.page_name)
        self.show_page(self.page_name, animate=animate)
        if self.modal is not None:
            self.modal.close()
        if self.tour is not None:
            self.tour.build()
        self.raise_overlays()

    def raise_overlays(self):
        for tag in ("toast", "modal", "tour", "tour_scene", "splash", "tip"):
            self.cv.tag_raise(tag)

    def rebind_keys(self):
        r = self.root
        r.bind("<Escape>", lambda e: self._key("esc"))
        r.bind("<Return>", lambda e: self._key("enter"))
        r.bind("<Right>", lambda e: self._key("right"))
        r.bind("<Left>", lambda e: self._key("left"))
        for i, name in enumerate(("home", "activity", "rules", "settings", "guide")):
            r.bind("<Control-Key-%d>" % (i + 1), lambda e, n=name: self.show_page(n) if not (
                self.tour or self.modal) else None)
        for seq, kind in (("<Up>", "up"), ("<Down>", "down"), ("<Prior>", "pgup"), ("<Next>", "pgdn"),
                          ("<Home>", "home"), ("<End>", "end")):
            r.bind(seq, lambda e, k=kind: self._key_scroll(k))

    def _key(self, key):
        if self.splash is not None:
            if key in ("esc", "enter"):
                self.splash.finish()
            return
        if self.tour is not None:
            if key == "esc":
                self.tour.close()
            elif key in ("right", "enter"):
                if self.tour.i == len(Tour.STEPS) - 1 and key == "enter":
                    self.tour.finish()
                else:
                    self.tour.next()
            elif key == "left":
                self.tour.back()
        elif self.modal is not None and key == "esc":
            self.modal.close()

    def _area_at(self, x=None, y=None):
        """The scrollable list under the pointer - or the current page's list."""
        if self.hits.block:
            return None
        if x is not None:
            for area in self.scroll_areas:
                if area.contains(x, y):
                    return area
        return getattr(self.page, "area", None)

    def _wheel(self, event, delta=None):
        area = self._area_at(event.x, event.y)
        if area is not None:
            delta = event.delta if delta is None else delta
            area.scroll_by(-delta / 120.0 * self.px(90))

    def _touchpad(self, event):
        """Two-finger scrolling on a laptop touchpad (Tk 9 reports it separately)."""
        area = self._area_at(event.x, event.y)
        if area is None:
            return
        low = event.delta & 0xFFFF
        dy = low if low < 0x8000 else low - 0x10000
        if dy:
            area.scroll_by(-dy * self.S * 1.5)

    def _key_scroll(self, kind):
        if self.modal is not None or self.tour is not None or self.splash is not None:
            return
        area = self._area_at()
        if area is None:
            return
        page = (area.y1 - area.y0) * 0.9
        if kind == "home":
            area.scroll_to(0)
        elif kind == "end":
            area.scroll_to(area.max_offset())
        else:
            area.scroll_by({"up": -self.px(70), "down": self.px(70), "pgup": -page, "pgdn": page}[kind])

    # ---- pages ----------------------------------------------------------------------------
    def show_page(self, name, animate=True):
        if self.tour is not None or self.modal is not None:
            if self.modal is not None:
                self.modal.close()
        old_offset = None
        if self.page is not None:
            if self.page.name == name and not animate:
                old_offset = getattr(getattr(self.page, "area", None), "offset", None)
            self.page.destroy()
        self.page_name = name
        self.page = PAGES[name](self)
        self.page.build()
        if old_offset:
            area = getattr(self.page, "area", None)
            if area is not None:
                area.scroll_to(old_offset, animate=False)
        self.sidebar.set_active(name, animate=animate)
        self.raise_overlays()
        if animate and self.anim.enabled:
            tag = self.page.tag
            moved = [self.px(18)]
            self.cv.move(tag, 0, moved[0])

            def slide(k):
                target = self.px(18) * (1 - k)
                self.cv.move(tag, 0, target - moved[0])
                moved[0] = target
            self.anim.tween(0.38, slide, ease=ease_out_cubic, key=("page", "in"))
        self.hits.refresh()

    def refresh_page(self, soon=False, slide_first=None):
        if soon:
            if self._refresh_job:
                self.root.after_cancel(self._refresh_job)
            self._refresh_job = self.root.after(140, lambda: self.refresh_page(slide_first=slide_first))
            return
        self._refresh_job = None
        if self.modal is not None and isinstance(self.page, (RulesPage, SettingsPage)):
            return
        self.show_page(self.page_name, animate=False)
        if slide_first and self.anim.enabled:
            area = getattr(self.page, "area", None)
            if area is not None and area.offset < 1:
                moved = [-self.px(40)]
                self.cv.move("row0", moved[0], 0)

                def slide(k):
                    target = -self.px(40) * (1 - k)
                    self.cv.move("row0", target - moved[0], 0)
                    moved[0] = target
                self.anim.tween(0.5, slide, ease=ease_out_back)

    def open_tour(self, step=0):
        if self.tour is None:
            Tour(self, step)

    def confirm(self, title, message, ok_label, on_ok, danger=False):
        Confirm(self, title, message, ok_label, on_ok, danger)

    def close_modal(self):
        if self.modal is not None:
            self.modal.close()

    # ---- status line ----------------------------------------------------------------------------
    def status_message(self):
        now = time.time()
        if self.status and self.status[3] > now:
            return self.status[:3]
        folder = self.settings.get("folder")
        if not folder or not os.path.isdir(folder):
            return ("info", "This folder can't be found — please choose another one.", "warn")
        if self.engine.running:
            return ("check", "Watching for new files — drop something in and watch it move.", "good")
        return ("info", "Paused. Press Start tidying and TiDY will sort new files for you.", None)

    def set_status(self, icon, text, tone, seconds):
        self.status = (icon, text, tone, time.time() + seconds)
        if isinstance(self.page, HomePage):
            self.page.set_status(self.status_message())
        if self._status_job:
            self.root.after_cancel(self._status_job)
        self._status_job = self.root.after(int(seconds * 1000) + 50, self._status_expired)

    def _status_expired(self):
        self._status_job = None
        if isinstance(self.page, HomePage):
            self.page.set_status(self.status_message())

    # ---- watching ------------------------------------------------------------------------------
    def start_watching(self):
        folder = self.settings.get("folder")
        ok, msg = self.engine.start(folder)
        if not ok:
            self.toasts.show("TiDY can't watch this folder", msg, icon="info", glyph=self.c.bad,
                             tint=self.c.bad_soft)
            return False
        self.settings["was_watching"] = True
        self.save_soon()
        self._watch_changed()
        return True

    def stop_watching(self):
        self.engine.stop(quiet=True)
        self.settings["was_watching"] = False
        self.save_soon()
        self._watch_changed()

    def toggle_watching(self):
        if self.engine.running:
            self.stop_watching()
            self.toasts.show("Paused", "New files will stay where they land until you press Start.", icon="pause",
                             glyph=self.c.dim, tint=self.c.card2)
        elif self.start_watching():
            folder = os.path.basename(self.settings["folder"].rstrip("\\/"))
            self.toasts.show("TiDY is watching “%s”" % folder, "New files will be sorted automatically.",
                             icon="check")

    def _watch_changed(self):
        self.status = None
        self.sidebar.build_status()
        self.tray.update()
        if isinstance(self.page, (HomePage, SettingsPage)):
            self.refresh_page()
        self.raise_overlays()
        self.count_loose()

    def count_loose(self, delay=0):
        if self._loose_job:
            self.root.after_cancel(self._loose_job)
        folder = self.settings.get("folder")

        def go():
            self._loose_job = None
            if folder and os.path.isdir(folder):
                self.engine.count_loose_async(folder)
        self._loose_job = self.root.after(delay, go)

    def choose_folder(self):
        current = self.settings.get("folder") or known_downloads_dir()
        path = filedialog.askdirectory(parent=self.root, initialdir=current if os.path.isdir(current) else None,
                                       title="Choose the folder TiDY should keep tidy", mustexist=True)
        if not path:
            return
        path = os.path.normpath(path)
        reason = risky_folder_reason(path)
        if reason:
            self.toasts.show("Let's pick a different folder", reason, icon="info", glyph=self.c.warn,
                             tint=self.c.warn_soft, duration=6)
            return
        was = self.engine.running
        self.engine.stop(quiet=True)
        self.settings["folder"] = path
        self.save_soon()
        if was:
            self.start_watching()
        else:
            self._watch_changed()
        name = os.path.basename(path.rstrip("\\/")) or path
        self.toasts.show("Folder changed", "TiDY now looks after “%s”." % name, icon="folder",
                         glyph=self.c.accent, tint=self.c.accent_soft)

    def drop_samples(self):
        if not self.engine.running:
            if not self.start_watching():
                return
        folder = self.settings["folder"]
        kinds = [SAMPLE_KINDS[(self._sample_i + i) % len(SAMPLE_KINDS)] for i in range(4)]
        self._sample_i += 4
        if self.page_name != "home":
            self.show_page("home")
        self.set_status("sparkle", "Dropping 4 sample files into “%s” …" % os.path.basename(folder),
                        "busy", 3)
        self.engine.drop_samples(folder, kinds)

    def tidy_existing(self):
        folder = self.settings.get("folder")
        if not folder or not os.path.isdir(folder):
            return
        names = self.engine.loose_files(folder)
        if not names:
            self.toasts.show("Already tidy", "There are no loose files in this folder right now.", icon="sparkle",
                             glyph=self.c.accent, tint=self.c.accent_soft)
            return

        def go():
            if self.page_name != "home":
                self.show_page("home")
            self.set_status("wand", "Tidying %s …" % plural(len(names), "file"), "busy", 4)
            self.engine.tidy_existing(folder)
        name = os.path.basename(folder.rstrip("\\/"))
        self.confirm("Tidy %s now?" % plural(len(names), "file"),
                     "TiDY will move the files already in “%s” into their folders. You can undo any "
                     "of them from the Activity page." % name, "Tidy them", go)

    def undo(self, entry):
        ok, info = self.engine.undo(entry)
        if not ok:
            self.toasts.show("Couldn't undo that", info, icon="info", glyph=self.c.warn, tint=self.c.warn_soft)
            return
        entry["undone"] = True
        entry["restored"] = info
        self.settings.setdefault("kept", []).append(info)
        self.store.record_undo(entry)
        self.save_soon()
        self.toasts.show("Put back", "“%s” is back where it was, and TiDY will leave it there." %
                         os.path.basename(info), icon="undo", glyph=self.c.accent, tint=self.c.accent_soft)
        if self.page is not None:
            self.page.on_event(("undone", entry))
        self.count_loose(300)

    def open_rule_folder(self, rule):
        folder = self.settings.get("folder") or ""
        path = os.path.join(folder, rule["name"])
        if os.path.isdir(path):
            open_path(path)
        else:
            glyph, tint = self.rule_colors(rule)
            self.toasts.show("No “%s” folder yet" % rule["name"],
                             "It appears as soon as the first matching file arrives.", icon=rule.get("icon", "folder"),
                             glyph=glyph, tint=tint)

    # ---- settings -------------------------------------------------------------------------------
    def set_setting(self, key, value):
        self.settings[key] = value
        self.save_soon()

    def set_animations(self, value):
        self.settings["animations"] = bool(value)
        self.anim.enabled = bool(value)
        self.save_soon()

    def set_autostart(self, value):
        if not set_autostart(value):
            self.toasts.show("Couldn't change that", "Windows didn't allow TiDY to change its start-up "
                             "setting.", icon="info", glyph=self.c.warn, tint=self.c.warn_soft)

    # ---- installing -------------------------------------------------------------------------------
    def install_now(self):
        self.toasts.show("Installing TiDY …", "Adding it to the Start menu and the desktop.", icon="download",
                         glyph=self.c.accent, tint=self.c.accent_soft)
        source = os.path.abspath(__file__)
        threading.Thread(target=lambda: self.engine.emit("installed", *install_tidy(source)),
                         name="TiDY-install", daemon=True).start()

    def uninstall_prompt(self):
        def ask_about_settings():
            Confirm(self, "Keep your settings?", "TiDY can keep your settings and history in case you install it "
                    "again later.", "Delete them", lambda: self._uninstall(True), danger=True,
                    cancel_label="Keep them", on_cancel=lambda: self._uninstall(False))
        self.confirm("Uninstall TiDY?", "TiDY will be removed from the Start menu, the desktop and Windows "
                     "Settings → Apps. Your own files stay exactly where they are.", "Uninstall",
                     ask_about_settings, danger=True)

    def _uninstall(self, remove_data):
        self.engine.stop(quiet=True)
        self.tray.remove()
        if not remove_data:
            self.settings["was_watching"] = False
            self.store.save()
        self._no_save = True  # don't recreate what is about to be removed
        ok, message = uninstall_tidy(remove_data=remove_data, stop_running=False)
        from tkinter import messagebox
        (messagebox.showinfo if ok else messagebox.showwarning)("TiDY", message, parent=self.root)
        self.quit()

    def set_rule_on(self, rule, value):
        rule["on"] = bool(value)
        self.engine.set_rules(self.settings["rules"])
        self.save_soon()
        self.count_loose(200)

    def rules_changed(self):
        self.engine.set_rules(self.settings["rules"])
        self.save_soon()
        self.count_loose(200)
        if isinstance(self.page, (RulesPage, HomePage)):
            self.refresh_page()

    def set_theme(self, mode):
        self.settings["theme"] = mode
        self.save_soon()
        self.root.after(260 if self.anim.enabled else 0, self.apply_theme)

    def flip_theme(self):
        self.settings["theme"] = "light" if self.c.dark else "dark"
        self.save_soon()
        self.apply_theme()

    def apply_theme(self):
        name = self.resolve_theme()
        if THEMES[name] is self.c:
            return
        self.c = THEMES[name]
        self.cv.configure(bg=self.c.bg)
        self.popup.destroy()
        self.style_titlebar()
        self.layout()

    def export_logo(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="Save the TiDY logo",
                                            initialfile="TiDY-logo.png", defaultextension=".png",
                                            filetypes=[("PNG picture", "*.png")])
        if not path:
            return
        self.toasts.show("Drawing the logo …", "This takes a few seconds.", icon="image", glyph=self.c.accent,
                         tint=self.c.accent_soft)

        def job():
            try:
                with open(path, "wb") as fh:
                    fh.write(png_bytes(512, 512, render_logo_rgba(512)))
                self.engine.emit("exported", path, None)
            except Exception as exc:
                self.engine.emit("exported", path, short_error(exc))
        threading.Thread(target=job, name="TiDY-logo", daemon=True).start()

    # ---- engine events ---------------------------------------------------------------------------
    def pump(self):
        if self._quitting:
            return
        try:
            for _ in range(300):
                event = self.engine.events.get_nowait()
                try:
                    self.handle(event)
                except Exception:
                    log_exception("handling %r" % (event[0],))
        except queue.Empty:
            pass
        for action in self.tray.take():  # clicks on the tray icon / its menu
            {"open": self.restore, "toggle": self.toggle_watching, "quit": self.quit}[action]()
            if self._quitting:
                return
        self.root.after(90, self.pump)

    def handle(self, event):
        kind = event[0]
        c = self.c
        if kind == "moved":
            entry = event[1]
            self.store.record_move(entry)
            self.save_soon()
            rule = self.rule_by_id(entry["rule"]) or {"icon": "folder", "color": "slate", "name": entry["folder"]}
            glyph, tint = self.rule_colors(rule)
            self.set_status(rule.get("icon", "folder"), "Moved “%s” to %s" % (entry["name"], entry["folder"]),
                            "good", 4)
            if self.page is not None:
                self.page.on_event(event)
            self._announce(entry, rule, glyph, tint)
            if self.settings.get("sound"):
                self.chime.play()
            self.count_loose(1500)
        elif kind == "incoming":
            name, why = event[1], event[2]
            if why == "downloading":
                self.set_status("download", "Waiting for “%s” to finish downloading …" % name, "busy", 60)
            else:
                self.set_status("sparkle", "New file spotted: “%s” …" % name, "busy", 6)
        elif kind == "skipped":
            self.toasts.show("Couldn't tidy “%s”" % event[1], "TiDY skipped it because %s." % event[2],
                             icon="info", glyph=c.warn, tint=c.warn_soft, duration=6)
        elif kind == "failed":
            self.engine.stop(quiet=True)
            self.settings["was_watching"] = False
            self._watch_changed()
            self.toasts.show("Watching stopped", event[1], icon="info", glyph=c.bad, tint=c.bad_soft, duration=8)
        elif kind == "loose":
            if event[1] == self.settings.get("folder"):
                changed = (self.loose_count == 0) != (event[2] == 0)
                self.loose_count = event[2]
                if isinstance(self.page, HomePage):
                    if changed:
                        self.refresh_page()
                    else:
                        self.page.on_event(event)
        elif kind == "tidied":
            moved, failed = event[1], event[2]
            if failed:
                self.toasts.show("Tidied %s" % plural(moved, "file"), "%s couldn't be moved (in use?)." % plural(
                    failed, "file"), icon="info", glyph=c.warn, tint=c.warn_soft)
        elif kind == "installed":
            ok, message = event[1], event[2]
            if ok:
                self.toasts.show("TiDY is installed", "Open it from the Start menu or the desktop icon.",
                                 icon="check", duration=6)
                if isinstance(self.page, SettingsPage):
                    self.refresh_page()
            else:
                self.toasts.show("Install didn't work", message, icon="info", glyph=c.bad, tint=c.bad_soft,
                                 duration=8)
        elif kind == "exported":
            if event[2]:
                self.toasts.show("Couldn't save the logo", event[2], icon="info", glyph=c.bad, tint=c.bad_soft)
            else:
                self.toasts.show("Logo saved", os.path.basename(event[1]), icon="image", glyph=c.accent,
                                 tint=c.accent_soft, action=("Show", lambda p=event[1]: reveal_in_explorer(p)))

    def _announce(self, entry, rule, glyph, tint):
        """Toast (or desktop pop-up) - grouped when many files arrive at once."""
        self._burst.append(entry)
        n = len(self._burst)
        if self._burst_job:
            self.root.after_cancel(self._burst_job)
        self._burst_job = self.root.after(2500, self._burst.clear)
        if n == 1:
            title = "Tidied “%s”" % entry["name"]
            sub = "Moved to %s" % entry["folder"]
            action = ("Undo", lambda e=entry: self.undo(e))
        else:
            title = "Tidied %d files" % n
            sub = "Latest: %s → %s" % (entry["name"], entry["folder"])
            action = ("View", lambda: self.show_page("activity"))
        if self.focused() or (self.visible() and not self.settings.get("popups")):
            self.toasts.show(title, sub, icon=rule.get("icon", "folder"), glyph=glyph, tint=tint, action=action)
        elif self.settings.get("popups"):
            self.popup.show(title, sub)

    # ---- lifecycle -------------------------------------------------------------------------------
    def restore(self, page=None):
        root = self.root
        try:
            first = self.page is None
            root.deiconify()
            if root.state() == "iconic":
                root.state("normal")
            if first:  # started in the background: build the interface now
                root.update()
                self.style_titlebar()
                self.layout()
            root.lift()
            root.attributes("-topmost", True)
            root.after(150, lambda: root.attributes("-topmost", False))
            root.focus_force()
        except tk.TclError:
            pass
        if page and page != self.page_name and self.tour is None:
            self.show_page(page)

    def on_close(self):
        if self.engine.running and self.settings.get("close_to_tray") and self.tray.ok:
            self.root.withdraw()
            if not self._told_tray:
                self._told_tray = True
                self.popup.show("TiDY is still tidying", "Find it in the system tray, by the clock.")
            return
        self.quit()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        try:
            if self.root.state() == "normal":
                self.settings["geometry"] = self.root.geometry()
        except tk.TclError:
            pass
        was = self.engine.running
        self.engine.stop(quiet=True)
        self.settings["was_watching"] = was
        if not self._no_save:
            self.store.save()
        self.tray.remove()
        self.popup.destroy()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        root = self.root
        if not self.background:
            root.deiconify()
            root.update()
            self.style_titlebar()
            self.layout()
        if self.background or (self.settings.get("resume") and self.settings.get("was_watching")):
            self.start_watching()
        else:
            self.count_loose()
        self.root.after(90, self.pump)

        def after_splash():
            self.show_page("home", animate=True)
            if not self.settings.get("tutorial_done"):
                self.root.after(350, self.open_tour)
        if not self.background:
            if self.anim.enabled:
                Splash(self, after_splash)
            elif not self.settings.get("tutorial_done"):
                self.open_tour()
        root.mainloop()


# =============================================================================
#  Installing TiDY on this PC (Start menu, desktop shortcut, Settings > Apps)
#  Everything goes into the user's own folders - no administrator rights.
# =============================================================================

INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Programs", APP_NAME)
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\TiDY"
WM_QUITME = 0x8000 + 23


def installed_script():
    return os.path.join(INSTALL_DIR, "TiDY.py")


def is_installed():
    if not IS_WIN or not os.path.isfile(installed_script()):
        return False
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY))
        return True
    except OSError:
        return False


def running_installed_copy():
    return norm(os.path.abspath(__file__)) == norm(installed_script())


def install_paths():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    programs = known_folder("{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
                            os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
    desktop = known_folder("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
                           os.path.join(os.path.expanduser("~"), "Desktop"))
    return {"dir": INSTALL_DIR, "script": installed_script(), "icon": os.path.join(INSTALL_DIR, "TiDY.ico"),
            "start": os.path.join(programs, "TiDY.lnk"), "desktop": os.path.join(desktop, "TiDY.lnk")}


def locate_word_guide(extra_folders=()):
    folders = list(extra_folders)
    downloads = known_downloads_dir()
    folders += [downloads, os.path.join(downloads, "Documents"), os.path.dirname(os.path.abspath(__file__)),
                INSTALL_DIR]
    for folder in folders:
        path = os.path.join(folder, GUIDE_DOCX_NAME)
        if folder and os.path.isfile(path):
            return path
    return None


def _com_call(obj, index, argtypes, *args, restype=None):
    """Call method number `index` of a COM interface pointer (plain ctypes)."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype or ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])(obj, *args)


def make_shortcut(lnk_path, target, arguments, workdir, icon, description, app_id=None):
    """Create a Windows .lnk shortcut through the Shell's IShellLink interface."""
    ole32 = ctypes.windll.ole32
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.HRESULT
    initialized = ole32.CoInitializeEx(None, 2) == 0  # apartment threaded
    link = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(GUID.parse("{00021401-0000-0000-C000-000000000046}")), None, 1,
                               ctypes.byref(GUID.parse("{000214F9-0000-0000-C000-000000000046}")),
                               ctypes.byref(link))
        wstr = [ctypes.c_wchar_p]
        _com_call(link, 20, wstr, target)                                # SetPath
        _com_call(link, 11, wstr, arguments)                             # SetArguments
        _com_call(link, 9, wstr, workdir)                                # SetWorkingDirectory
        _com_call(link, 17, [ctypes.c_wchar_p, ctypes.c_int], icon, 0)   # SetIconLocation
        _com_call(link, 7, wstr, description)                            # SetDescription
        query = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        if app_id:  # lets Windows group the running window with this shortcut (pinning works)
            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

            class PROPVARIANT(ctypes.Structure):
                _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                            ("r3", ctypes.c_ushort), ("value", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]
            store = ctypes.c_void_p()
            try:
                _com_call(link, 0, query, ctypes.byref(GUID.parse("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")),
                          ctypes.byref(store))
                key = PROPERTYKEY(GUID.parse("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)
                value = PROPVARIANT(vt=31, value=app_id)  # VT_LPWSTR
                _com_call(store, 6, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
                          ctypes.byref(key), ctypes.byref(value))       # SetValue
                _com_call(store, 7, [])                                  # Commit
            except OSError:
                pass
            finally:
                if store:
                    _com_call(store, 2, [], restype=ctypes.c_ulong)      # Release
        persist = ctypes.c_void_p()
        _com_call(link, 0, query, ctypes.byref(GUID.parse("{0000010B-0000-0000-C000-000000000046}")),
                  ctypes.byref(persist))
        try:
            os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
            _com_call(persist, 6, [ctypes.c_wchar_p, ctypes.c_int], lnk_path, 1)  # Save
        finally:
            _com_call(persist, 2, [], restype=ctypes.c_ulong)
    finally:
        if link:
            _com_call(link, 2, [], restype=ctypes.c_ulong)
        if initialized:
            ole32.CoUninitialize()


def install_tidy(source=None):
    """Copy TiDY to %LOCALAPPDATA%\\Programs\\TiDY and add shortcuts + an Apps entry."""
    if not IS_WIN:
        return False, "Installing is only available on Windows. On this computer, simply run TiDY.py."
    p = install_paths()
    source = os.path.abspath(source or __file__)
    try:
        os.makedirs(p["dir"], exist_ok=True)
        if norm(source) != norm(p["script"]):
            shutil.copyfile(source, p["script"])
        sizes = (16, 24, 32, 48, 64, 256)
        with open(p["icon"], "wb") as fh:
            fh.write(ico_bytes([(s, png_bytes(s, s, render_logo_rgba(s))) for s in sizes]))
        guide = locate_word_guide([os.path.dirname(source)])
        if guide and norm(os.path.dirname(guide)) != norm(p["dir"]):
            shutil.copyfile(guide, os.path.join(p["dir"], GUIDE_DOCX_NAME))
        exe = pythonw_executable()
        for lnk in (p["start"], p["desktop"]):
            make_shortcut(lnk, exe, '"%s"' % p["script"], p["dir"], p["icon"],
                          "TiDY - keeps your Downloads folder tidy", APP_USER_MODEL_ID)
        size_kb = sum(os.path.getsize(os.path.join(p["dir"], f)) for f in os.listdir(p["dir"])) // 1024
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE)
        for name, kind, value in (
                ("DisplayName", winreg.REG_SZ, APP_NAME),
                ("DisplayVersion", winreg.REG_SZ, APP_VERSION),
                ("Publisher", winreg.REG_SZ, "TiDY"),
                ("Comments", winreg.REG_SZ, "Folder monitor and automatic file sorter"),
                ("DisplayIcon", winreg.REG_SZ, p["icon"]),
                ("InstallLocation", winreg.REG_SZ, p["dir"]),
                ("InstallDate", winreg.REG_SZ, datetime.date.today().strftime("%Y%m%d")),
                ("UninstallString", winreg.REG_SZ, '"%s" "%s" --uninstall' % (exe, p["script"])),
                ("QuietUninstallString", winreg.REG_SZ, '"%s" "%s" --uninstall --quiet' % (exe, p["script"])),
                ("NoModify", winreg.REG_DWORD, 1), ("NoRepair", winreg.REG_DWORD, 1),
                ("EstimatedSize", winreg.REG_DWORD, int(size_kb))):
            winreg.SetValueEx(key, name, 0, kind, value)
        winreg.CloseKey(key)
        if autostart_enabled():  # keep "Start with Windows" pointing at the installed copy
            set_autostart(True, p["script"])
    except OSError as exc:
        log_exception("install")
        return False, "TiDY couldn't be installed: %s." % short_error(exc)
    return True, "TiDY is installed. Open it from the Start menu or with the TiDY icon on your desktop."


def ask_running_tidy_to_quit(timeout=6.0):
    """Politely ask a running TiDY to close (used before uninstalling)."""
    if not IS_WIN:
        return
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        hwnd = user32.FindWindowW(TRAY_CLASS, None)
        if not hwnd:
            return
        user32.PostMessageW(hwnd, WM_QUITME, 0, 0)
        end = time.time() + timeout
        while time.time() < end and user32.FindWindowW(TRAY_CLASS, None):
            time.sleep(0.2)
    except Exception:
        pass


def uninstall_tidy(remove_data=False, stop_running=True):
    """Remove the installed copy, shortcuts, Apps entry and start-up entry.
    The user's own files are never touched; settings only if asked."""
    if not IS_WIN:
        return False, "Nothing to uninstall on this computer."
    if stop_running:
        ask_running_tidy_to_quit()
    p = install_paths()
    problems = []
    for lnk in (p["start"], p["desktop"]):
        try:
            if os.path.exists(lnk):
                os.remove(lnk)
        except OSError as exc:
            problems.append(short_error(exc))
    set_autostart(False)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    try:
        os.chdir(os.path.expanduser("~"))  # can't delete a folder we are standing in
    except OSError:
        pass
    for folder in ([p["dir"], DATA_DIR] if remove_data else [p["dir"]]):
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            if os.path.isdir(folder):
                problems.append("some files in %s are in use" % folder)
    if problems:
        return False, "TiDY was mostly removed, but: %s. You can delete what is left by hand." % "; ".join(problems)
    return True, "TiDY was removed from this computer. Your own files were not touched."


def _dialog_root():
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    try:
        root.iconphoto(True, tk.PhotoImage(master=root, data=base64.b64encode(
            png_bytes(32, 32, render_logo_rgba(32))).decode("ascii"), format="png"))
    except tk.TclError:
        pass
    return root


def run_installer(quiet=False):
    ok, message = install_tidy()
    print(message)
    if quiet:
        return
    from tkinter import messagebox
    root = _dialog_root()
    if ok:
        if messagebox.askyesno("TiDY", message + "\n\nOpen TiDY now?", parent=root):
            subprocess.Popen([pythonw_executable(), installed_script()], cwd=INSTALL_DIR)
    else:
        messagebox.showerror("TiDY", message, parent=root)
    root.destroy()


def run_uninstaller(quiet=False):
    remove_data = False
    if not quiet:
        from tkinter import messagebox
        root = _dialog_root()
        if not messagebox.askyesno("Uninstall TiDY", "Remove TiDY from this computer?\n\n"
                                   "Your own files stay exactly where they are.", parent=root):
            root.destroy()
            return
        remove_data = messagebox.askyesno(
            "Uninstall TiDY", "Also delete TiDY's settings and history?\n\n"
            "Choose No to keep them, in case you install TiDY again later.", default="no", parent=root)
    ok, message = uninstall_tidy(remove_data=remove_data)
    print(message)
    if not quiet:
        (messagebox.showinfo if ok else messagebox.showwarning)("Uninstall TiDY", message, parent=root)
        root.destroy()


def main():
    args = sys.argv[1:]
    if "--install" in args:
        run_installer(quiet="--quiet" in args)
        return
    if "--uninstall" in args:
        run_uninstaller(quiet="--quiet" in args)
        return
    if "--export-logo" in args:
        i = args.index("--export-logo")
        out = args[i + 1] if len(args) > i + 1 else "TiDY-logo.png"
        size = 512
        with open(out, "wb") as fh:
            fh.write(png_bytes(size, size, render_logo_rgba(size)))
        print("Saved", os.path.abspath(out))
        return
    if not acquire_single_instance():
        return
    hide_stray_console()
    enable_dpi_awareness()
    set_app_user_model_id()
    app = App(background="--background" in args)
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_exception("startup")
        try:
            from tkinter import messagebox
            messagebox.showerror("TiDY", "TiDY couldn't start:\n\n%s\nDetails are in:\n%s" % (
                traceback.format_exc(limit=1), LOG_FILE))
        except Exception:
            traceback.print_exc()
