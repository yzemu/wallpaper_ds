"""
Wallpaper Injector for Discord — любые обои (картинка / GIF / видео) на весь Discord.

Запуск: двойной клик по wallpaper_injector.pyw (без консоли).
При первом запуске сам доустановит нужные библиотеки.
Ошибки пишутся в injector.log рядом с файлом.
"""
import base64
import importlib
import json
import mimetypes
import os
import queue
import random
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NOWIN = 0x08000000 if os.name == "nt" else 0  # не показывать окна консоли у дочерних процессов
BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "wallpaper_config.json"
CFG_BAK = BASE / "wallpaper_config.json.bak"
CFG_TMP = BASE / "wallpaper_config.json.tmp"
CFG_LOCK = threading.Lock()
LOG = BASE / "injector.log"
QUIET = "--hidden" in sys.argv  # запуск из автозагрузки Windows: никаких окон и сообщений об ошибках
if sys.stderr is None:
    sys.stderr = open(LOG, "a", encoding="utf-8")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")


def log_exc():
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{traceback.format_exc()}")
    except Exception:
        pass


def log_msg(text: str):
    """Короткая строка в injector.log — чтобы было видно, что программа решила и почему."""
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


def ensure_deps():
    need = []
    for mod, pkg in (("websocket", "websocket-client"), ("customtkinter", "customtkinter"),
                     ("pystray", "pystray"), ("PIL", "pillow")):
        try:
            importlib.import_module(mod)
        except ImportError:
            need.append(pkg)
    if not need:
        return
    if QUIET:  # при входе в Windows сети может ещё не быть — тихо пробуем несколько раз
        err = ""
        for _ in range(5):
            r = subprocess.run([sys.executable, "-m", "pip", "install", *need],
                               capture_output=True, text=True, creationflags=NOWIN)
            if r.returncode == 0:
                importlib.invalidate_caches()
                return
            err = (r.stderr or "")[-300:]
            time.sleep(15)
        raise RuntimeError("Не удалось установить компоненты: " + err)
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.title("Wallpaper Injector")
    root.geometry("360x90")
    tk.Label(root, text="Первый запуск: устанавливаю компоненты…\nЭто займёт около минуты.",
             pady=22).pack()
    root.update()
    r = subprocess.run([sys.executable, "-m", "pip", "install", *need],
                       capture_output=True, text=True, creationflags=NOWIN)
    root.destroy()
    importlib.invalidate_caches()
    if r.returncode != 0:
        messagebox.showerror("Wallpaper Injector",
                             "Не удалось установить компоненты:\n" + (r.stderr or "")[-600:])
        sys.exit(1)


try:
    ensure_deps()
    import websocket  # noqa: E402
    import customtkinter as ctk  # noqa: E402
    import pystray  # noqa: E402
    from PIL import Image, ImageDraw  # noqa: E402
    from tkinter import filedialog, messagebox  # noqa: E402
except Exception:
    log_exc()
    if not QUIET:
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk()
            _r.withdraw()
            _mb.showerror("Wallpaper Injector", "Не удалось запустить. Подробности в injector.log")
        except Exception:
            pass
    sys.exit(1)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

PORT = 9222
IPC_PORT = 47653
RUN_VALUE = "DiscordWallpaperInjector"
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}

DEFAULT = {
    "v": 4,
    "enabled": True,
    "wallpaper": "",
    "library": [],
    "full": True,
    "panel_color": "16,16,24",
    "panel_alpha": 0.25,
    "blur": 0,
    "brightness": 0.85,
    "fit": "cover",
    "pause_hidden": True,
    "slideshow": False,
    "interval": 15,
    "shuffle": True,
    "watch": True,
    "autostart": False,
    "patch_launchers": True,
    "tray": True,
    "discord": "Discord",
    "extra_css": "",
    "show_with_discord": True,
    "geometry": "",
    "tab": "",
}
NO_APPLY = {"slideshow", "interval", "shuffle", "watch", "show_with_discord", "autostart", "patch_launchers", "tray", "discord"}

VARS = [
    "--background-primary", "--background-secondary", "--background-secondary-alt",
    "--background-tertiary", "--background-mobile-primary", "--background-mobile-secondary",
    "--chat-background", "--chat-background-default", "--channeltextarea-background",
    "--background-base-lowest", "--background-base-lower", "--background-base-low",
    "--background-surface-high", "--background-surface-higher",
    "--background-surface-highest", "--app-frame-background",
    "--bg-overlay-chat", "--bg-overlay-app-frame",
    "--bg-overlay-1", "--bg-overlay-2", "--bg-overlay-3",
    "--bg-overlay-4", "--bg-overlay-5", "--bg-overlay-6",
]
FLOAT_VARS = [
    "--background-floating", "--background-nested-floating",
    "--modal-background", "--modal-footer-background",
]

JS = r"""
(() => {
  const CFG = __CFG__;
  window.__wp?.stop?.();
  if (window.__wpVis) document.removeEventListener('visibilitychange', window.__wpVis);
  window.__wpVis = null;
  if (CFG.pause) {
    window.__wpVis = () => {
      const v = document.querySelector('#wp-bg video');
      if (v) { document.hidden ? v.pause() : v.play().catch(() => {}); }
    };
    document.addEventListener('visibilitychange', window.__wpVis);
  }
  const SKIP = '[class*="layerContainer"],[role="dialog"],[role="menu"],[role="tooltip"],' +
               '[class*="modal"],[class*="popout"],[class*="tooltip"],[class*="contextMenu"]';

  const startSweep = () => {
    const sweep = () => {
      const root = document.getElementById('app-mount');
      if (!root) return;
      for (const el of root.querySelectorAll('div,section,nav,aside,main,header,ul')) {
        if (el.hasAttribute('data-wp-t')) continue;
        const bg = getComputedStyle(el).backgroundColor;
        if (!bg || bg === 'transparent') continue;
        const m = bg.match(/[\d.]+/g);
        if (!m || m.length < 3) continue;
        const r = +m[0], g = +m[1], b = +m[2];
        const a = m.length > 3 ? +m[3] : 1;
        if (a < 0.5 || Math.max(r, g, b) - Math.min(r, g, b) > 30) continue;
        if (el.closest(SKIP)) continue;
        const rc = el.getBoundingClientRect();
        if (rc.width < 120 || rc.height < 100) continue;
        el.style.setProperty('background-color', CFG.tint, 'important');
        el.setAttribute('data-wp-t', '1');
      }
    };
    let t = null;
    const mo = new MutationObserver(() => {
      if (t) return;
      t = setTimeout(() => { t = null; sweep(); }, 300);
    });
    mo.observe(document.documentElement, { childList: true, subtree: true });
    const iv = setInterval(sweep, 2000);
    sweep();
    window.__wp = { stop() {
      mo.disconnect(); clearInterval(iv); clearTimeout(t);
      document.querySelectorAll('[data-wp-t]').forEach(e => {
        e.style.removeProperty('background-color'); e.removeAttribute('data-wp-t');
      });
    } };
  };

  const apply = () => {
    for (const id of ['wp-style', 'wp-bg']) document.getElementById(id)?.remove();
    const st = document.createElement('style');
    st.id = 'wp-style'; st.textContent = CFG.css;
    document.documentElement.appendChild(st);
    const bg = document.createElement('div'); bg.id = 'wp-bg';
    if (CFG.kind === 'video') {
      const v = document.createElement('video');
      v.src = CFG.url; v.autoplay = true; v.loop = true; v.muted = true;
      v.playsInline = true; bg.appendChild(v); v.play().catch(() => {});
    } else {
      const i = document.createElement('img'); i.src = CFG.url; bg.appendChild(i);
    }
    document.documentElement.appendChild(bg);
    if (CFG.full) startSweep();
  };
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', apply, { once: true });
  else apply();
})();
"""
# -1 — страница ещё грузится, 1 — обои на месте, 0 — страница готова, но обоев нет
CHECK_JS = "document.readyState!=='complete'?-1:(document.getElementById('wp-bg')?1:0)"
CLEAN_JS = ("window.__wp?.stop?.(); "
            "if (window.__wpVis) document.removeEventListener('visibilitychange', window.__wpVis); "
            "for (const id of ['wp-style','wp-bg']) document.getElementById(id)?.remove();")


# ---------- локальный сервер для файла обоев (с Range для видео) ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.server.file
        if not p or not p.is_file():
            self.send_error(404)
            return
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            if a == "":
                start = max(0, size - int(b))
            else:
                start = int(a)
                if b:
                    end = min(int(b), size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def start_server() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.file = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------- конфиг и сборка скрипта ----------
def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULT))
    old_v = DEFAULT["v"]
    for p in (CFG_PATH, CFG_BAK):  # основной файл повреждён -> берём резервную копию
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
                old_v = int(data.get("v", 0) or 0)
                break
        except Exception:
            if p.exists():
                log_exc()
    cfg.pop("start_discord", None)  # старая опция, её больше нет
    if old_v < 4:  # старый конфиг: включаем порт в ярлыках и автоподхват обоев
        cfg["patch_launchers"] = True
        cfg["watch"] = True
    cfg["v"] = 4
    if old_v < 4 and CFG_PATH.exists():
        save_cfg(cfg)
    return cfg


def save_cfg(cfg: dict):
    """Атомарная запись: сначала во временный файл, прежняя рабочая версия остаётся как .bak."""
    with CFG_LOCK:
        try:
            CFG_TMP.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                old = CFG_PATH.read_bytes()
                json.loads(old.decode("utf-8"))
                CFG_BAK.write_bytes(old)
            except Exception:
                pass
            os.replace(CFG_TMP, CFG_PATH)
        except Exception:
            log_exc()


def is_usable(w: str) -> bool:
    return w.startswith(("http://", "https://")) or Path(w).is_file()


def build_css(c: dict) -> str:
    rgba = f"rgba({c['panel_color']},{c['panel_alpha']})"
    sel = ".theme-dark,.theme-light,.theme-darker,.theme-midnight,:root"
    vars_css = "".join(f"{v}:{rgba}!important;" for v in VARS)
    fl = f"rgba({c['panel_color']},{max(float(c['panel_alpha']), 0.9)})"
    float_css = "".join(f"{v}:{fl}!important;" for v in FLOAT_VARS)
    shadow = "#app-mount{text-shadow:0 1px 3px rgba(0,0,0,.6)}" if c["full"] else ""
    return (
        "html,body{background:#000!important}"
        "#wp-bg{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden;background:#000}"
        f"#wp-bg video,#wp-bg img{{width:100%;height:100%;object-fit:{c['fit']};"
        f"filter:blur({c['blur']}px) brightness({c['brightness']});transform:scale(1.05)}}"
        "#app-mount{position:relative;z-index:1}"
        f"{sel}{{{vars_css}{float_css}}}" + shadow + c["extra_css"]
    )


def build_js(cfg: dict, srv: ThreadingHTTPServer) -> str:
    w = cfg["wallpaper"].strip().strip('"')
    if not cfg.get("enabled", True) or not w:
        return CLEAN_JS
    if not is_usable(w):  # файл переехал или удалён — берём первые доступные обои из библиотеки
        w = next((x for x in cfg["library"] if is_usable(x)), "")
        if not w:
            return CLEAN_JS
    if w.startswith(("http://", "https://")):
        url, ext = w, Path(w.split("?")[0]).suffix.lower()
    else:
        p = Path(w)
        if not p.is_file():
            return CLEAN_JS
        srv.file, ext = p, p.suffix.lower()
        url = f"http://127.0.0.1:{srv.server_port}/wp{ext}?v={int(p.stat().st_mtime)}"
    payload = {
        "css": build_css(cfg), "url": url,
        "kind": "video" if ext in VIDEO_EXT else "image",
        "full": bool(cfg["full"]), "pause": bool(cfg["pause_hidden"]),
        "tint": f"rgba({cfg['panel_color']},{cfg['panel_alpha']})",
    }
    return JS.replace("__CFG__", json.dumps(payload))


# ---------- Discord и CDP ----------
class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=15)
        self.n = 0
        self.script_id = None

    def call(self, method: str, **params):
        self.n += 1
        i = self.n
        self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == i:
                return msg.get("result", {})

    def inject(self, js: str, run_now: bool):
        if self.script_id:
            self.call("Page.removeScriptToEvaluateOnNewDocument", identifier=self.script_id)
        self.script_id = self.call("Page.addScriptToEvaluateOnNewDocument", source=js).get("identifier")
        if run_now:
            self.call("Runtime.evaluate", expression=js)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def cdp_targets():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def _process_names():
    """Имена всех процессов через Toolhelp32 — мгновенно, без запуска tasklist."""
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k = ctypes.WinDLL("kernel32")
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE)]
    k.Process32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, 0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        raise OSError("snapshot failed")
    names = []
    try:
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        ok = k.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            names.append(pe.szExeFile.lower())
            ok = k.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k.CloseHandle(snap)
    return names


def discord_hide_windows(name: str):
    """Мгновенно прячет окна Discord без обоев, чтобы не мелькали во время перезапуска."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        target = ("\\" + name + ".exe").lower()

        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = kernel32.OpenProcess(0x1000, False, pid.value)
            if not h:
                return True
            try:
                buf = ctypes.create_unicode_buffer(520)
                size = wintypes.DWORD(520)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) \
                        and buf.value.lower().endswith(target):
                    user32.ShowWindow(hwnd, 0)  # SW_HIDE
            finally:
                kernel32.CloseHandle(h)
            return True

        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proto(cb), 0)
    except Exception:
        log_exc()


def discord_running(name: str) -> bool:
    if os.name == "nt":
        try:
            return f"{name}.exe".lower() in _process_names()
        except Exception:
            pass
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"],
                             capture_output=True, text=True, encoding="cp866", errors="ignore",
                             creationflags=NOWIN).stdout
        return f"{name}.exe".lower() in out.lower()
    except Exception:
        return False


def discord_started_minimized(name: str) -> bool:
    """Запущен ли Discord тихо в трей (с --start-minimized, как при автозапуске Windows).
    Смотрим командную строку главного процесса — это работает сразу после старта, ещё до появления окна."""
    ps = ("$p=@(Get-CimInstance Win32_Process -Filter \"Name='" + name + ".exe'\" -ErrorAction SilentlyContinue |"
          "Where-Object{$_.CommandLine -and $_.CommandLine -notmatch '--type='});"
          "if(@($p|Where-Object{$_.CommandLine -match 'start-minimized'}).Count -gt 0){'1'}else{'0'}")
    try:
        return run_ps(ps).stdout.strip().endswith("1")
    except Exception:
        return False


def start_discord(name: str, kill: bool, minimized: bool = False):
    if kill:
        subprocess.run(["taskkill", "/F", "/T", "/IM", f"{name}.exe"],
                       capture_output=True, creationflags=NOWIN)
        for _ in range(60):  # ждём, пока старый Discord реально закроется (иначе новый сразу выходит)
            if not discord_running(name):
                break
            time.sleep(0.1)
        time.sleep(0.5)
    update = Path(os.environ.get("LOCALAPPDATA", "")) / name / "Update.exe"
    if not update.exists():
        raise RuntimeError(f"не найден {update}")
    args = FLAGS + (" --start-minimized" if minimized else "")  # тихий старт остаётся тихим
    subprocess.Popen([
        str(update), "--processStart", f"{name}.exe",
        "--process-start-args", args,
    ], creationflags=NOWIN)


def wait_target() -> str:
    for _ in range(360):
        for t in cdp_targets() or []:
            if t.get("type") == "page" and "discord.com" in t.get("url", ""):
                return t["webSocketDebuggerUrl"]
        time.sleep(0.25)
    raise RuntimeError("не дождался окна Discord")


# ---------- система: автозапуск, ярлык, один экземпляр ----------
def launcher_cmd():
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else exe), str(Path(__file__).resolve())


def set_autostart(on: bool):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    try:
        if on:
            exe, script = launcher_cmd()
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}" "{script}" --hidden')
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def run_ps(script: str):
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                          capture_output=True, text=True, errors="ignore", creationflags=NOWIN)


def discord_info(name: str):
    """(возраст самого старого процесса Discord в секундах или -1, если не запущен; есть ли видимое окно).
    При ошибке опроса возвращает (None, False)."""
    ps = (f"$p=@(Get-Process -Name '{name}' -ErrorAction SilentlyContinue);"
          "if(-not $p){'-1 0'}else{"
          "$a=[int]((Get-Date)-($p|Sort-Object StartTime|Select-Object -First 1).StartTime).TotalSeconds;"
          "$v=@($p|Where-Object{$_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -notmatch 'Updat'}).Count;"
          "\"$a $([int]($v -gt 0))\"}")
    try:
        a, v = run_ps(ps).stdout.strip().splitlines()[-1].split()
        return float(a), v == "1"
    except Exception:
        return None, False


def discord_window_visible(name: str) -> bool:
    """Есть ли на экране видимое окно Discord (не скрытое в трей и не окно обновления)."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")      # отдельные экземпляры, чтобы не трогать общие настройки
        kernel32 = ctypes.WinDLL("kernel32")  # функций у tkinter и pystray
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        target = ("\\" + name + ".exe").lower()
        found = []

        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return True
            try:
                buf = ctypes.create_unicode_buffer(520)
                size = wintypes.DWORD(520)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) \
                        and buf.value.lower().endswith(target):
                    title = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, title, 256)
                    if "updat" not in title.value.lower():
                        found.append(hwnd)
                        return False
            finally:
                kernel32.CloseHandle(h)
            return True

        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proto(cb), 0)
        return bool(found)
    except Exception:
        return discord_info(name)[1]  # запасной способ через PowerShell


def wait_for_taskbar(timeout: int = 120):
    """При автозапуске Windows программа стартует раньше панели задач — без ожидания иконка в трее не появится."""
    if os.name != "nt":
        return
    try:
        import ctypes
        end = time.time() + timeout
        while time.time() < end:
            if ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None):
                break
            time.sleep(1)
        time.sleep(3)
    except Exception:
        log_exc()


def create_shortcut(discord_name: str):
    exe, script = launcher_cmd()
    icons = list((Path(os.environ.get("LOCALAPPDATA", "")) / discord_name).glob("app-*/Discord.exe"))
    icon = ""
    if icons:
        best = max(icons, key=lambda p: [int(x) for x in re.findall(r"\d+", p.parent.name)])
        icon = f"$s.IconLocation='{best}';"
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -COM WScript.Shell).CreateShortcut($d+'\\Discord Wallpaper.lnk');"
          f"$s.TargetPath='{exe}';$s.Arguments='\"{script}\" --launch';"
          f"$s.WorkingDirectory='{BASE}';{icon}$s.Save()")
    r = run_ps(ps)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:300] or "PowerShell вернул ошибку")


FLAGS = f"--remote-debugging-port={PORT} --remote-allow-origins=*"
# Аргументы запуска Discord (--process-start-args) аккуратно объединяются с уже имеющимися
# (например, --start-minimized в автозапуске Discord), а не дописываются вторым разом.
PATCH_PS = r"""
$name = '__NAME__'
$flags = '__FLAGS__'
$on = __ON__
$global:n = 0
function Fix-Args([string]$orig) {
  $m = [regex]::Match($orig, '--process-start-args\s+(?:"([^"]*)"|(\S+))')
  $inner = ''
  $rest = $orig
  if ($m.Success) {
    if ($m.Groups[1].Success) { $inner = $m.Groups[1].Value } else { $inner = $m.Groups[2].Value }
    $rest = $orig.Remove($m.Index, $m.Length)
  }
  $had = $inner -match 'remote-debugging-port'
  if ((-not $on) -and (-not $had)) { return $orig }
  $inner = ($inner -replace '--remote-debugging-port=\d+', '' -replace '--remote-allow-origins=\S+', '').Trim()
  if ($on) { $inner = ($inner + ' ' + $flags).Trim() }
  $rest = $rest.Trim()
  if ($inner) { $rest = ($rest + ' --process-start-args "' + $inner + '"').Trim() }
  return $rest
}
$dirs = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'),
          [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('CommonPrograms'),
          "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar") |
        Where-Object { $_ -and (Test-Path $_) }
$sh = New-Object -COM WScript.Shell
foreach ($d in $dirs) {
  Get-ChildItem -Path $d -Filter '*.lnk' -Recurse -Depth 2 -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $s = $sh.CreateShortcut($_.FullName)
      if ($s.TargetPath -like "*\$name\Update.exe") {
        $a = Fix-Args $s.Arguments
        if ($a -ne $s.Arguments) { $s.Arguments = $a; $s.Save(); $global:n++ }
      }
    } catch {}
  }
}
$k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$v = (Get-ItemProperty -Path $k -Name $name -ErrorAction SilentlyContinue).$name
if ($v -and $v -like '*Update.exe*') {
  $nv = Fix-Args $v
  if ($nv -ne $v) { Set-ItemProperty -Path $k -Name $name -Value $nv; $global:n++ }
}
Write-Output $global:n
"""


def patch_launchers(name: str, on: bool) -> int:
    """Добавляет (или убирает) отладочный порт в ярлыках и автозапуске Discord."""
    ps = (PATCH_PS.replace("__NAME__", name).replace("__FLAGS__", FLAGS)
          .replace("__ON__", "$true" if on else "$false"))
    r = run_ps(ps)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:300] or "PowerShell вернул ошибку")
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except Exception:
        return 0


def ipc_client(cmd: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=1) as s:
            s.sendall(cmd.encode())
        return True
    except OSError:
        return False


def ipc_server(on_cmd):
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", IPC_PORT))
    except OSError:
        return
    srv.listen(4)
    while True:
        try:
            c, _ = srv.accept()
            data = c.recv(32).decode(errors="ignore").strip()
            c.close()
            if data:
                on_cmd(data)
        except OSError:
            break


# ---------- интерфейс ----------
ACCENT, ACCENT_H = "#5865F2", "#4752c4"
CARD, MUTED, DIM = "#232428", "#b5bac1", "#80848e"
STATUS_COLORS = {"ok": "#23a559", "wait": "#f0b232", "off": "#80848e", "err": "#da373c"}
PRESETS = {
    "🪟 Стекло": {"panel_alpha": 0.18, "blur": 0, "brightness": 0.9},
    "🖼 Чистые обои": {"panel_alpha": 0.0, "blur": 0, "brightness": 1.0},
    "📖 Читаемо": {"panel_alpha": 0.45, "blur": 6, "brightness": 0.7},
    "🌙 Ночь": {"panel_alpha": 0.3, "blur": 0, "brightness": 0.4},
}
FIT = {"Заполнить": "cover", "Вписать": "contain", "Растянуть": "fill"}
INTERVALS = {"1 мин": 1, "5 мин": 5, "15 мин": 15, "30 мин": 30, "1 час": 60, "3 часа": 180}
FONT = "Segoe UI"


def make_tray_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=16, fill="#5865F2")
    d.polygon([(18, 44), (32, 18), (46, 44)], fill="white")
    return img


class App(ctk.CTk):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.hidden = "--hidden" in args or "--launch" in args
        if self.hidden:
            self.withdraw()  # прячем сразу, чтобы окно не мелькало при старте
        self.cfg = load_cfg()
        self.srv = start_server()
        self.cdp = None
        self.lock = threading.Lock()
        self.conn_lock = threading.Lock()
        self.q = queue.Queue()
        self.thumbs = {}
        self.sliders = {}
        self._job = None
        self.tray = None
        self.notified = False
        self.last_launch = 0.0
        self.last_change = time.time()

        self.title("Wallpaper Injector for Discord")
        self.geometry(self.saved_geometry())
        self.minsize(880, 640)
        self.configure(fg_color="#1e1f22")
        self._build_header()
        self._build_tabs()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.hidden:
            for ms in (300, 1200, 3000):  # CTk на Windows иногда сам показывает окно после старта
                self.after(ms, self._keep_hidden)
        self.after(120, self._pump)
        self.after(20000, self._tick)
        for target in (self._tray_boot, self._heartbeat, self._watch, self._startup, self._repatch):
            threading.Thread(target=target, daemon=True).start()
        threading.Thread(target=ipc_server, args=(lambda c: self.post(lambda: self.on_ipc(c)),),
                         daemon=True).start()

    # --- память окна: размер, позиция, последняя вкладка ---
    def saved_geometry(self):
        m = re.fullmatch(r"(\d+)x(\d+)(?:(\+-?\d+|-\d+)(\+-?\d+|-\d+))?", str(self.cfg.get("geometry", "")))
        if not m:
            return "960x720"
        w, h = max(int(m[1]), 880), max(int(m[2]), 640)
        if m[3] is None:
            return f"{w}x{h}"
        x, y = int(m[3].replace("+-", "-")), int(m[4].replace("+-", "-"))
        if -50 <= x <= self.winfo_screenwidth() - 150 and 0 <= y <= self.winfo_screenheight() - 150:
            return f"{w}x{h}+{x}+{y}"  # окно не уедет за пределы экрана, если монитор отключили
        return f"{w}x{h}"

    def save_window(self):
        try:
            if self.state() == "normal":
                self.cfg["geometry"] = self.geometry()
            self.cfg["tab"] = self.tabs.get()
            save_cfg(self.cfg)
        except Exception:
            log_exc()

    # --- потокобезопасная очередь в UI ---
    def post(self, fn):
        self.q.put(fn)

    def _pump(self):
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log_exc()
        self.after(120, self._pump)

    def set_status(self, text, kind="ok"):
        def _do():
            self.dot.configure(text_color=STATUS_COLORS[kind])
            self.status_lbl.configure(text=text)
        self.post(_do)

    # --- шапка ---
    def _build_header(self):
        head = ctk.CTkFrame(self, corner_radius=0, fg_color="#17181b")
        head.pack(fill="x")
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", padx=20, pady=14)
        ctk.CTkLabel(left, text="Wallpaper Injector", font=(FONT, 22, "bold")).pack(anchor="w")
        st = ctk.CTkFrame(left, fg_color="transparent")
        st.pack(anchor="w", pady=(2, 0))
        self.dot = ctk.CTkLabel(st, text="●", text_color=STATUS_COLORS["off"], font=(FONT, 14))
        self.dot.pack(side="left")
        self.status_lbl = ctk.CTkLabel(st, text="Discord не подключён", text_color=MUTED)
        self.status_lbl.pack(side="left", padx=6)
        right = ctk.CTkFrame(head, fg_color="transparent")
        right.pack(side="right", padx=20)
        self.sw_enabled = ctk.CTkSwitch(right, text="Обои вкл", progress_color=ACCENT,
                                        command=lambda: self.set_enabled(bool(self.sw_enabled.get())))
        if self.cfg["enabled"]:
            self.sw_enabled.select()
        self.sw_enabled.pack(side="left", padx=(0, 14))
        ctk.CTkButton(right, text="🚀  Запустить Discord с обоями", height=40, font=(FONT, 14, "bold"),
                      fg_color=ACCENT, hover_color=ACCENT_H, command=self.launch).pack(side="left")

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self, fg_color="#1e1f22", segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color=ACCENT_H)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        self._tab_gallery(self.tabs.add("🖼  Обои"))
        self._tab_look(self.tabs.add("🎨  Вид"))
        self._tab_auto(self.tabs.add("⚡  Авто"))
        self._tab_settings(self.tabs.add("⚙  Настройки"))
        if self.cfg.get("tab"):
            try:
                self.tabs.set(self.cfg["tab"])
            except Exception:
                pass

    # --- вкладка «Обои» ---
    def _tab_gallery(self, tab):
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        for text, cmd in (("＋  Файл", self.add_files), ("＋  Ссылка", self.add_url),
                          ("🎲  Случайные", self.random_wallpaper)):
            ctk.CTkButton(bar, text=text, width=130, height=34, fg_color=CARD, hover_color="#2e3035",
                          command=cmd).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(bar, text="Картинки, GIF, видео mp4/webm", text_color=DIM).pack(side="right")
        self.gallery = ctk.CTkScrollableFrame(tab, fg_color="#1e1f22")
        self.gallery.pack(fill="both", expand=True)
        self.render_gallery()

    def thumb(self, w):
        if w in self.thumbs:
            return self.thumbs[w]
        img = None
        ext = Path(w.split("?")[0]).suffix.lower()
        if not w.startswith("http") and ext not in VIDEO_EXT:
            try:
                im = Image.open(w).convert("RGB")
                im.thumbnail((480, 270))
                img = ctk.CTkImage(light_image=im, dark_image=im, size=(im.width // 2, im.height // 2))
            except Exception:
                img = None
        self.thumbs[w] = img
        return img

    def render_gallery(self):
        for child in self.gallery.winfo_children():
            child.destroy()
        lib = self.cfg["library"]
        if not lib:
            ctk.CTkLabel(self.gallery, text="Пока пусто.\nДобавь обои кнопкой «＋ Файл» или «＋ Ссылка».",
                         text_color=DIM, font=(FONT, 15)).pack(pady=80)
            return
        for i, w in enumerate(lib):
            sel = (w == self.cfg["wallpaper"])
            card = ctk.CTkFrame(self.gallery, corner_radius=12, border_width=2, fg_color=CARD,
                                border_color=ACCENT if sel else "#2b2d31")
            card.grid(row=i // 3, column=i % 3, padx=7, pady=7)
            img = self.thumb(w)
            icon = "" if img else ("🔗" if w.startswith("http") else "🎬")
            lbl = ctk.CTkLabel(card, text=icon, image=img, width=240, height=135, font=(FONT, 36))
            lbl.pack(padx=8, pady=(8, 2))
            name = w if w.startswith("http") else Path(w).name
            cap = ctk.CTkLabel(card, text=(name[:30] + "…") if len(name) > 30 else name,
                               text_color=MUTED, font=(FONT, 12))
            cap.pack(pady=(0, 8))
            for wd in (card, lbl, cap):
                wd.bind("<Button-1>", lambda e, w=w: self.select(w))
            ctk.CTkButton(card, text="✕", width=26, height=26, fg_color="#da373c", hover_color="#a12d2f",
                          command=lambda w=w: self.remove(w)).place(relx=1, x=-12, y=12, anchor="ne")

    def set_wallpaper(self, w):
        self.cfg["wallpaper"] = w
        self.last_change = time.time()
        save_cfg(self.cfg)
        self.render_gallery()
        self.apply_async()

    def select(self, w):
        self.set_wallpaper(w)

    def add(self, w):
        if w not in self.cfg["library"]:
            self.cfg["library"].append(w)
        self.set_wallpaper(w)

    def add_files(self):
        paths = filedialog.askopenfilenames(filetypes=[
            ("Обои", "*.png *.jpg *.jpeg *.webp *.gif *.mp4 *.webm *.mov"), ("Все файлы", "*.*")])
        for p in paths:
            self.add(p)

    def add_url(self):
        u = ctk.CTkInputDialog(text="Прямая ссылка на картинку / GIF / видео:", title="Ссылка").get_input()
        if u and u.strip().startswith("http"):
            self.add(u.strip())

    def remove(self, w):
        if w in self.cfg["library"]:
            self.cfg["library"].remove(w)
        if w == self.cfg["wallpaper"]:
            self.cfg["wallpaper"] = ""
        save_cfg(self.cfg)
        self.render_gallery()
        self.apply_async()

    def usable(self):
        return [w for w in self.cfg["library"] if w.startswith("http") or Path(w).is_file()]

    def random_wallpaper(self):
        lib = [w for w in self.usable() if w != self.cfg["wallpaper"]]
        if lib:
            self.set_wallpaper(random.choice(lib))

    def next_wallpaper(self):
        lib = self.usable()
        if len(lib) < 2:
            return
        cur = self.cfg["wallpaper"]
        if self.cfg["shuffle"]:
            self.set_wallpaper(random.choice([w for w in lib if w != cur]))
        else:
            i = lib.index(cur) if cur in lib else -1
            self.set_wallpaper(lib[(i + 1) % len(lib)])

    # --- вкладка «Вид» ---
    def _slider(self, parent, text, key, lo, hi, steps, fmt):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(10, 0))
        top = ctk.CTkFrame(row, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=text).pack(side="left")
        val = ctk.CTkLabel(top, text=fmt(self.cfg[key]), text_color=MUTED)
        val.pack(side="right")
        s = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, progress_color=ACCENT,
                          button_color=ACCENT, button_hover_color=ACCENT_H,
                          command=lambda v: (val.configure(text=fmt(v)), self.changed(key, round(float(v), 2))))
        s.set(self.cfg[key])
        s.pack(fill="x", pady=(4, 0))
        self.sliders[key] = (s, val, fmt)

    def _tab_look(self, tab):
        box = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        box.pack(fill="both", expand=True)
        ctk.CTkLabel(box, text="Пресеты", font=(FONT, 15, "bold")).pack(anchor="w", padx=16, pady=(6, 0))
        pr = ctk.CTkFrame(box, fg_color="transparent")
        pr.pack(fill="x", padx=16, pady=8)
        for name, vals in PRESETS.items():
            ctk.CTkButton(pr, text=name, width=140, height=34, fg_color=CARD, hover_color="#2e3035",
                          command=lambda v=vals: self.preset(v)).pack(side="left", padx=(0, 8))
        pct = lambda v: f"{int(round(float(v) * 100))}%"
        self._slider(box, "Затемнение панелей (0% = чистые обои)", "panel_alpha", 0, 1, 100, pct)
        self._slider(box, "Размытие обоев", "blur", 0, 30, 30, lambda v: f"{int(float(v))} px")
        self._slider(box, "Яркость обоев", "brightness", 0.1, 1.5, 28, pct)
        self.switch(box, "Фулл-режим: обои на весь Discord, везде", "full",
                    "Делает прозрачными все большие панели: чат, списки, профиль.")
        self.switch(box, "Ставить видео на паузу, когда Discord свёрнут", "pause_hidden",
                    "Экономит ресурсы процессора и видеокарты.")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(16, 0))
        ctk.CTkLabel(row, text="Масштаб обоев").pack(side="left")
        cur = next((k for k, v in FIT.items() if v == self.cfg["fit"]), "Заполнить")
        ctk.CTkOptionMenu(row, values=list(FIT), width=150, fg_color=CARD, button_color=ACCENT,
                          button_hover_color=ACCENT_H, command=lambda v: self.changed("fit", FIT[v])
                          ).pack(side="left", padx=12)
        row.winfo_children()[-1].set(cur)
        ctk.CTkLabel(row, text="Цвет панелей").pack(side="left", padx=(24, 0))
        self.swatch = ctk.CTkFrame(row, width=30, height=30, corner_radius=8,
                                   fg_color="#%02x%02x%02x" % tuple(int(x) for x in self.cfg["panel_color"].split(",")))
        self.swatch.pack(side="left", padx=10)
        ctk.CTkButton(row, text="Выбрать…", width=100, fg_color=CARD, hover_color="#2e3035",
                      command=self.pick_color).pack(side="left")

    def preset(self, vals):
        for k, v in vals.items():
            s, lbl, fmt = self.sliders[k]
            s.set(v)
            lbl.configure(text=fmt(v))
            self.cfg[k] = v
        save_cfg(self.cfg)
        self.apply_async()

    def pick_color(self):
        from tkinter import colorchooser
        c = colorchooser.askcolor(title="Цвет панелей")
        if c and c[0]:
            r, g, b = (int(x) for x in c[0])
            self.swatch.configure(fg_color="#%02x%02x%02x" % (r, g, b))
            self.changed("panel_color", f"{r},{g},{b}")

    # --- вкладка «Авто» ---
    def _tab_auto(self, tab):
        box = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        box.pack(fill="both", expand=True)
        ctk.CTkLabel(box, text="Слайдшоу", font=(FONT, 15, "bold")).pack(anchor="w", padx=16, pady=(6, 0))
        self.switch(box, "Менять обои автоматически (из библиотеки)", "slideshow")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(row, text="Каждые").pack(side="left")
        cur = next((k for k, v in INTERVALS.items() if v == self.cfg["interval"]), "15 мин")
        om = ctk.CTkOptionMenu(row, values=list(INTERVALS), width=110, fg_color=CARD, button_color=ACCENT,
                               button_hover_color=ACCENT_H,
                               command=lambda v: self.changed("interval", INTERVALS[v]))
        om.set(cur)
        om.pack(side="left", padx=10)
        ctk.CTkButton(row, text="⏭  Следующие сейчас", width=170, fg_color=CARD, hover_color="#2e3035",
                      command=self.next_wallpaper).pack(side="left", padx=8)
        self.switch(box, "Случайный порядок", "shuffle")
        ctk.CTkLabel(box, text="Запуск вместе с Discord", font=(FONT, 15, "bold")
                     ).pack(anchor="w", padx=16, pady=(28, 0))
        self.switch(box, "Сама ставить обои, когда ты открываешь Discord", "watch",
                    "Обои подхватываются сразу, как только запустился Discord. Если он стартовал без "
                    "обоев-порта, программа в первую же секунду перезапустит его с обоями. Если Discord "
                    "тихо стартует в трее при включении компьютера, он и перезапустится тихо, в трее, "
                    "без окна. Сама Discord никогда не запускает.")
        self.switch(box, "Открывать окно программы вместе с Discord", "show_with_discord",
                    "Когда ты открываешь Discord, окно программы тоже появляется (поверх, без кражи фокуса).")
        self.switch(box, "Всегда запускать Discord с обоями (правит ярлыки Discord)", "patch_launchers",
                    "Рекомендуется: в ярлыки и автозапуск Discord добавляется отладочный порт, и обои "
                    "ставятся сразу, без перезапуска Discord. Порт доступен только на этом компьютере. "
                    "После обновления Discord ярлыки могут сброситься — программа чинит их при каждом "
                    "запуске и каждые 10 минут.",
                    extra=self.on_patch)

    # --- вкладка «Настройки» ---
    def _tab_settings(self, tab):
        box = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        box.pack(fill="both", expand=True)
        self.switch(box, "Запускать программу вместе с Windows (в трее)", "autostart",
                    "Окно не откроется — программа тихо сидит в трее. Discord она сама не запускает.",
                    extra=set_autostart)
        ctk.CTkLabel(box, text="Крестик окна не выключает программу — она продолжает работать в трее.\n"
                               "Полностью выйти можно только через меню иконки в трее («Выход»).",
                     text_color=DIM, font=(FONT, 12), justify="left").pack(anchor="w", padx=18, pady=(16, 0))
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(18, 0))
        ctk.CTkLabel(row, text="Версия Discord").pack(side="left")
        om = ctk.CTkOptionMenu(row, values=["Discord", "DiscordPTB", "DiscordCanary"], width=150,
                               fg_color=CARD, button_color=ACCENT, button_hover_color=ACCENT_H,
                               command=lambda v: self.changed("discord", v))
        om.set(self.cfg["discord"])
        om.pack(side="left", padx=12)
        ctk.CTkButton(box, text="🔗  Создать ярлык «Discord Wallpaper» на рабочем столе", height=36,
                      fg_color=ACCENT, hover_color=ACCENT_H, command=self.make_shortcut
                      ).pack(anchor="w", padx=16, pady=(18, 0))
        ctk.CTkLabel(box, text="Запускаешь Discord через этот ярлык — он сразу открывается с обоями.",
                     text_color=DIM, font=(FONT, 12)).pack(anchor="w", padx=18)
        ctk.CTkLabel(box, text="Свой CSS (для продвинутых)", font=(FONT, 15, "bold")
                     ).pack(anchor="w", padx=16, pady=(24, 4))
        self.css_box = ctk.CTkTextbox(box, height=120, fg_color=CARD)
        self.css_box.pack(fill="x", padx=16)
        self.css_box.insert("1.0", self.cfg["extra_css"])
        ctk.CTkButton(box, text="Применить CSS", width=140, fg_color=CARD, hover_color="#2e3035",
                      command=lambda: self.changed("extra_css", self.css_box.get("1.0", "end").strip())
                      ).pack(anchor="w", padx=16, pady=8)
        ctk.CTkLabel(box, text=f"Конфиг и лог: {BASE}", text_color=DIM, font=(FONT, 11)
                     ).pack(anchor="w", padx=16, pady=(12, 0))

    def switch(self, parent, text, key, hint=None, extra=None):
        sw = ctk.CTkSwitch(parent, text=text, progress_color=ACCENT,
                           command=lambda: self._sw(key, sw, extra))
        if self.cfg[key]:
            sw.select()
        sw.pack(anchor="w", padx=16, pady=(14, 0))
        if hint:
            ctk.CTkLabel(parent, text=hint, text_color=DIM, font=(FONT, 12), justify="left",
                         wraplength=760).pack(anchor="w", padx=58, pady=(2, 0))
        return sw

    def _sw(self, key, sw, extra):
        v = bool(sw.get())
        self.changed(key, v)
        if extra:
            try:
                extra(v)
            except Exception as e:
                messagebox.showerror("Ошибка", str(e))

    def make_shortcut(self):
        try:
            create_shortcut(self.cfg["discord"])
            messagebox.showinfo("Готово", "Ярлык «Discord Wallpaper» создан на рабочем столе.")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    # --- изменение настроек ---
    def changed(self, key, value):
        self.cfg[key] = value
        save_cfg(self.cfg)
        if key not in NO_APPLY:
            if self._job:
                self.after_cancel(self._job)
            self._job = self.after(250, self.apply_async)

    def set_enabled(self, v):
        self.cfg["enabled"] = v
        save_cfg(self.cfg)
        (self.sw_enabled.select if v else self.sw_enabled.deselect)()
        self.apply_async()

    def _tick(self):
        try:
            mins = int(self.cfg["interval"])
            if self.cfg["slideshow"] and time.time() - self.last_change >= mins * 60:
                self.next_wallpaper()
        except Exception:
            log_exc()
        self.after(20000, self._tick)

    # --- связь с Discord ---
    def launch(self, confirm=True):
        name = self.cfg["discord"]
        if confirm and cdp_targets() is None and discord_running(name):
            if not messagebox.askokcancel(
                    "Discord", "Discord будет перезапущен (если ты в звонке — он прервётся). Продолжить?"):
                return
        threading.Thread(target=self._connect, kwargs={"kill": True, "allow_start": True},
                         daemon=True).start()

    def _connect(self, kill=False, allow_start=False, minimized=False):
        """allow_start=True ставится только по явной команде пользователя (кнопка, трей, ярлык).
        Без неё функция лишь подключается к уже запущенному Discord и ничего не запускает и не убивает."""
        if not self.conn_lock.acquire(blocking=False):
            return
        try:
            name = self.cfg["discord"]
            self.last_launch = time.time()
            if cdp_targets() is None:
                if not allow_start:
                    return
                running = discord_running(name)
                if running and not kill:
                    self.set_status("Discord запущен без обоев — нажми «Запустить»", "off")
                    return
                self.set_status("Запускаю Discord…", "wait")
                start_discord(name, kill=running, minimized=minimized)
            else:
                self.set_status("Подключаюсь к Discord…", "wait")
            ws = wait_target()
            with self.lock:
                if self.cdp:
                    self.cdp.close()
                cdp = CDP(ws)
                cdp.call("Page.setBypassCSP", enabled=True)  # чтобы Discord принял локальные обои
                cdp.inject(build_js(self.cfg, self.srv), run_now=False)
                cdp.call("Page.reload")
                self.cdp = cdp
            self.set_status("Подключено — обои работают", "ok")
        except Exception as e:
            log_exc()
            self.set_status(f"Ошибка: {e}", "err")
        finally:
            self.conn_lock.release()

    def apply_async(self):
        threading.Thread(target=self.apply, daemon=True).start()

    def apply(self):
        with self.lock:
            if not self.cdp:
                return
            try:
                self.cdp.inject(build_js(self.cfg, self.srv), run_now=True)
            except Exception:
                self.cdp = None
                self.set_status("Discord закрыт — жду запуска", "off")

    def _heartbeat(self):
        while True:
            time.sleep(2)
            with self.lock:
                if not self.cdp:
                    continue
                try:
                    # самопроверка: страница загрузилась, а обоев нет (перезагрузка Discord, Ctrl+R,
                    # сбой автовставки) — ставим обои заново, без единого клика
                    js = build_js(self.cfg, self.srv)
                    r = self.cdp.call("Runtime.evaluate", expression=CHECK_JS, returnByValue=True)
                    if js != CLEAN_JS and r.get("result", {}).get("value") == 0:
                        self.cdp.call("Runtime.evaluate", expression=js)
                except Exception:
                    self.cdp = None
                    self.set_status("Discord закрыт — жду запуска", "off")

    def notify(self, text):
        try:
            self.tray.notify(text, "Wallpaper Injector")
        except Exception:
            pass

    def on_patch(self, on):
        n = patch_launchers(self.cfg["discord"], on)
        if on:
            msg = (f"Обновлено ярлыков и автозапусков Discord: {n}.\n"
                   "Теперь Discord, запущенный любым способом, подхватывается без перезапуска.")
            if n == 0:
                msg = ("Ярлыки Discord не найдены. Ничего страшного: если открыть Discord без обоев, "
                       "программа сама перезапустит его с обоями.")
        else:
            msg = f"Вернул как было: {n}."
        messagebox.showinfo("Готово", msg)

    def _startup(self):
        time.sleep(1)
        name = self.cfg["discord"]
        for fn in (lambda: set_autostart(True) if self.cfg["autostart"] else None,  # самолечение пути
                   lambda: log_msg(f"ярлыков Discord обновлено: {patch_launchers(name, True)}")
                   if self.cfg["patch_launchers"] else None):
            try:
                fn()
            except Exception:
                log_exc()
        try:
            if "--launch" in self.args:  # ярлык «Discord Wallpaper» — явная команда пользователя
                self._connect(kill=True, allow_start=True)
        except Exception:
            log_exc()

    def _watch(self):
        """Подключается к Discord с отладочным портом и сразу ставит последние обои.
        Если Discord стартовал без порта, перезапускает его с обоями сразу, в первую секунду после старта.
        Тихий старт в трее (автозапуск) остаётся тихим: перезапуск идёт с --start-minimized, окно не
        появляется. Сама программа Discord, которого нет, никогда не запускает."""
        self._wstate = None
        self._restart_at = 0.0  # когда в последний раз сами перезапускали Discord
        self._restart_fails = 0  # сколько перезапусков подряд не дали подключения
        self._follow_done = False  # окно программы уже показано для этого запуска Discord
        self._seen = 0  # сколько проверок подряд процесс Discord уже есть, а порта нет
        last_ps = 0.0
        while True:  # проверка каждые полсекунды, чтобы поймать Discord сразу при старте
            try:
                if self.cdp:
                    self._wstate, self._restart_fails = "ok", 0
                    if self.cfg["show_with_discord"] and not self._follow_done \
                            and discord_window_visible(self.cfg["discord"]):
                        self._follow_done = True  # Discord открыт с обоями — открываем и окно программы
                        self.post(self.show_quiet)
                elif cdp_targets() is not None:
                    if self.cfg["watch"] and time.time() - self.last_launch > 10:
                        self._connect()  # последние сохранённые обои ставятся сразу
                elif time.time() - last_ps > 0.25:
                    last_ps = time.time()
                    self._watch_no_port()
            except Exception:
                log_exc()
            time.sleep(0.2)

    def _wstatus(self, state, text, kind="off"):
        if state != self._wstate:
            self._wstate = state
            self.set_status(text, kind)

    def _watch_no_port(self):
        name = self.cfg["discord"]
        run_msg = "Discord запущен без обоев — нажми «Запустить»"
        if not discord_running(name):
            self._seen = 0
            self._restart_fails = 0
            self._follow_done = False
            self._wstatus("none", "Discord не запущен")
            return
        if not self.cfg["watch"]:
            self._wstatus("run", run_msg)
            return
        # Discord запущен без порта. Перезапускаем сразу, не дожидаясь появления окна.
        if time.time() - self._restart_at < 30:  # только что перезапускали — даём время подключиться
            self._wstatus("run", run_msg)
            return
        if self._restart_fails >= 3:
            if time.time() - self._restart_at < 300:
                self._wstatus("run", run_msg)  # три неудачи подряд — пауза 5 минут, потом пробуем снова
                return
            self._restart_fails = 0
        self._seen += 1
        if self._seen == 1:
            # Первое обнаружение: пока идёт короткая «выдержка», в фоне узнаём, тихий ли это старт
            # (--start-minimized), и сразу прячем окно Discord без обоев, чтобы оно не мелькало.
            self._mini = None

            def _q():
                self._mini = discord_started_minimized(name)
            threading.Thread(target=_q, daemon=True).start()
            discord_hide_windows(name)
            return
        if self._seen < 3:  # ~0.5 с — чтобы не схватить Discord посреди самоперезапуска после обновления
            return
        self._seen = 0
        self._restart_at = time.time()
        self._restart_fails += 1  # сбросится в 0, как только подключение удастся
        self._wstate = "restart"
        for _ in range(40):
            if self._mini is not None:
                break
            time.sleep(0.1)
        mini = bool(self._mini)  # тихий старт в трее перезапускаем тоже тихо
        discord_hide_windows(name)
        log_msg(f"Discord стартовал без порта — перезапускаю с обоями "
                f"(попытка {self._restart_fails}, {'скрыто в трее' if mini else 'с окном'})")
        self.set_status("Перезапускаю Discord с обоями…", "wait")
        self._connect(kill=True, allow_start=True, minimized=mini)
        self._restart_at = time.time()
        if not self.cdp:
            log_msg("перезапуск не дал подключения к Discord")

    def _repatch(self):
        """Обновление Discord сбрасывает ярлыки — периодически возвращаем в них отладочный порт."""
        while True:
            time.sleep(600)
            try:
                if self.cfg["patch_launchers"]:
                    patch_launchers(self.cfg["discord"], True)
            except Exception:
                log_exc()

    # --- трей, окно, выход ---
    def start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda: self.post(self.show), default=True),
            pystray.MenuItem("Обои включены", lambda: self.post(lambda: self.set_enabled(not self.cfg["enabled"])),
                             checked=lambda item: self.cfg["enabled"]),
            pystray.MenuItem("Следующие обои", lambda: self.post(self.next_wallpaper)),
            pystray.MenuItem("Запустить Discord с обоями", lambda: self.post(self.launch)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda: self.post(self.quit)),
        )
        self.tray = pystray.Icon("wallpaper_injector", make_tray_icon(), "Wallpaper Injector", menu)
        self.tray.run_detached()

    def show_quiet(self):
        """Показывает окно программы поверх остальных, не забирая фокус у Discord."""
        if not self.hidden and self.state() == "normal":
            return
        self.hidden = False
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)
        self.after(500, lambda: self.attributes("-topmost", False))

    def _keep_hidden(self):
        if self.hidden and self.state() != "withdrawn":
            self.withdraw()

    def _tray_boot(self):
        if "--hidden" in self.args:
            wait_for_taskbar()
        for _ in range(3):
            try:
                self.start_tray()
                return
            except Exception:
                self.tray = None
                log_exc()
                time.sleep(5)
        if self.hidden:  # без трея скрытая программа была бы невидимкой — показываем окно
            self.post(self.show)

    def show(self):
        self.hidden = False
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_ipc(self, cmd):
        if cmd == "show":
            self.show()
        elif cmd == "launch":
            self.launch(confirm=False)

    def on_close(self):
        self.save_window()
        # крестик окна НЕ закрывает программу: она продолжает работать в фоне
        if self.tray:
            self.withdraw()
            if not self.notified:
                self.notified = True
                try:
                    self.tray.notify("Работаю в трее. Выход — в меню иконки.", "Wallpaper Injector")
                except Exception:
                    pass
        else:
            self.iconify()  # трея нет — сворачиваем в панель задач, программа продолжает работать

    def quit(self):
        self.save_window()
        with self.lock:
            if self.cdp:  # обои в открытом Discord остаются до его перезапуска
                self.cdp.close()
        try:
            self.tray.stop()
        except Exception:
            pass
        self.destroy()
        os._exit(0)


def main():
    args = sys.argv[1:]
    cmd = "launch" if "--launch" in args else ("noop" if "--hidden" in args else "show")
    if ipc_client(cmd):  # программа уже запущена — передаём команду ей и выходим
        return
    App(args).mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_exc()
        if not QUIET:
            try:
                messagebox.showerror("Wallpaper Injector", "Ошибка. Подробности в injector.log")
            except Exception:
                pass
