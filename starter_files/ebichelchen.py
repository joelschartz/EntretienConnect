#!/usr/bin/env python3
# eBichelchenHelper v1.10.30 - lokaler Helfer für individuelle e-Bichelchen-Nachrichten.
# Keine e-Bichelchen-Zugangsdaten. v1.10.16 kann nach Vorschau mehrere individuelle Message-Einträge erstellen und wieder löschen.
# v1.10.17: Browser.close/Profil-Löschung nur noch, wenn KEIN App-Tab (127.0.0.1/localhost) im
# Debug-Browser läuft — sonst verschwand die App mitsamt Fenster beim Verbinden/Aufräumen.

from __future__ import annotations

import base64
import configparser
import json
import mimetypes
import os
import pathlib
import platform
import secrets
import shutil
import sqlite3
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from urllib.parse import parse_qs, urlparse

PORT = 8765
CDP_PORT = 9223
ROOT = pathlib.Path(__file__).resolve().parent

def _user_app_data_dir() -> pathlib.Path:
    """Dauerhafter, benutzerspezifischer Speicher ohne Admin-Rechte."""
    system = platform.system().lower()
    if system == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support"
    elif system == "windows":
        base = pathlib.Path(os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home()))
    else:
        base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home() / ".config"))
    path = base / "EntretienConnect"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        path = ROOT
    return path

DATA_ROOT = _user_app_data_dir()
PROFILE_ROOT = DATA_ROOT / "profiles"
EB_URL = "https://ssl.education.lu/ebichelchen/app/login"

# Eigener Zertifikats-Context für direkte Hintergrund-Requests zu ssl.education.lu.
# Auf manchen macOS-Python-Installationen fehlt sonst die lokale CA-Kette.
CA_FILE = ROOT / "cacert.pem"
try:
    SSL_CONTEXT = ssl.create_default_context(cafile=str(CA_FILE)) if CA_FILE.exists() else ssl.create_default_context()
except Exception:
    SSL_CONTEXT = None

LATEST_DATA = None
LATEST_AT = None
LOCK = threading.Lock()
BROWSER_PROCESSES: dict[str, subprocess.Popen] = {}
BROWSER_LAUNCH_LOCK = threading.Lock()
CREATED_TEST_ENTRIES: list[dict] = []
LATEST_SESSION: dict | None = None
LATEST_SESSION_AT: str | None = None
ACTIVE_BROWSER_MODE = "cdp"  # cdp | firefox-current
ACTIVE_BROWSER_USER_AGENT = ""
FIREFOX_SESSIONSTORE_CACHE: dict[str, tuple[float, int, list[dict], dict]] = {}


def _json_response(handler: BaseHTTPRequestHandler, data, status: int = 200):
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


def sanitize_profile_name(raw: str) -> str:
    raw = (raw or "default").strip()
    out = []
    for ch in raw:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    name = "".join(out).strip("._-") or "default"
    return name[:80]


def find_browser_executable(preferred: str = "auto") -> dict | None:
    """Findet einen Chromium-basierten Browser.
    Reihenfolge bei preferred="auto": Chrome, dann Microsoft Edge.
    Rückgabe: {path, id, name}
    """
    system = platform.system().lower()
    preferred = (preferred or "auto").strip().lower()
    browser_candidates: list[dict] = []

    def add(browser_id: str, name: str, paths: list[str]):
        browser_candidates.append({"id": browser_id, "name": name, "paths": paths})

    if system == "darwin":
        add("chrome", "Google Chrome", [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ])
        add("edge", "Microsoft Edge", [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ])
    elif system == "windows":
        local = os.environ.get("LOCALAPPDATA", "")
        prog = os.environ.get("PROGRAMFILES", "")
        progx86 = os.environ.get("PROGRAMFILES(X86)", "")
        add("chrome", "Google Chrome", [
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(prog, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(progx86, "Google", "Chrome", "Application", "chrome.exe"),
            shutil.which("chrome") or "",
            shutil.which("chrome.exe") or "",
        ])
        add("edge", "Microsoft Edge", [
            os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(prog, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(progx86, "Microsoft", "Edge", "Application", "msedge.exe"),
            shutil.which("msedge") or "",
            shutil.which("msedge.exe") or "",
        ])
    else:
        add("chrome", "Google Chrome / Chromium", [
            shutil.which("google-chrome") or "",
            shutil.which("google-chrome-stable") or "",
            shutil.which("chromium") or "",
            shutil.which("chromium-browser") or "",
        ])
        add("edge", "Microsoft Edge", [
            shutil.which("microsoft-edge") or "",
            shutil.which("microsoft-edge-stable") or "",
            shutil.which("msedge") or "",
        ])

    if preferred in ("chrome", "edge"):
        browser_candidates.sort(key=lambda b: 0 if b["id"] == preferred else 1)

    for browser in browser_candidates:
        for c in browser["paths"]:
            if c and pathlib.Path(c).exists():
                return {"path": c, "id": browser["id"], "name": browser["name"]}
    return None

def read_url_json(url: str, timeout: float = 4.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def debug_browser_running() -> bool:
    if ACTIVE_BROWSER_MODE == "firefox-current":
        return True
    """True only while the isolated Chrome/Edge instance used for e-Bichelchen
    still exposes its DevTools endpoint. A normal Chrome window is intentionally
    not considered here because it cannot be read safely without remote debugging.
    """
    try:
        data = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1.0)
        return isinstance(data, dict)
    except Exception:
        return False


def open_remote_tab(url: str) -> bool:
    """Öffnet einen neuen Tab im bereits kontrollierten Chrome-/Edge-Fenster.

    v302: Zuerst wird ``Target.createTarget`` mit ``newWindow=False`` verwendet.
    Dadurch erscheint e-Bichelchen als zweiter Tab neben EntretienConnect und nicht
    als weiteres Browserfenster. Die ältere HTTP-DevTools-Route bleibt Fallback.
    """
    try:
        version = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2)
        ws = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if ws:
            msg = cdp_call(ws, "Target.createTarget", {
                "url": url,
                "newWindow": False,
                "background": False,
            }, msg_id=906, timeout=4)
            target_id = (((msg or {}).get("result") or {}).get("targetId"))
            if target_id:
                return True
    except Exception:
        pass

    target = f"http://127.0.0.1:{CDP_PORT}/json/new?" + urllib.parse.quote(url, safe="")
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(target, method=method)
            with urllib.request.urlopen(req, timeout=2) as resp:
                resp.read()
            return True
        except Exception:
            continue
    return False



def _activate_browser_app(browser_name: str) -> bool:
    """Best-effort OS activation without changing the e-Bichelchen route.

    v300 (macOS): do *not* call ``open -a Google Chrome`` here. EntretienConnect
    usually runs in the user's normal Chrome instance while e-Bichelchen runs in a
    second isolated Chrome process. ``open -a`` activates the bundle, not that exact
    process, and therefore often brings the EntretienConnect window back to the
    foreground immediately after the white helper window appears. The isolated
    window is focused through its concrete CDP target instead.
    """
    system = platform.system().lower()
    try:
        if system == "darwin":
            return False
        if system == "windows":
            # Beim Python-Starter reicht normalerweise der neue Browserprozess; der
            # Windows-PowerShell-Starter besitzt zusätzlich eine stärkere user32-Fokussierung.
            return True
    except Exception:
        pass
    return False


def _bring_ebichelchen_target_forward(browser_name: str, wait_s: float = 3.0) -> dict:
    """Activate the exact isolated e-Bichelchen tab/window via DevTools.

    This is deliberately target-specific. It never opens or activates the user's
    normal Chrome window and it never navigates to calendar/pinboard.
    """
    deadline = time.time() + max(0.2, float(wait_s))
    last_error = ""
    while time.time() < deadline:
        try:
            target = find_ebichelchen_target()
            _cdp_set_window_state(target, "normal")
            cdp_call(target.get("webSocketDebuggerUrl"), "Page.bringToFront", {}, msg_id=912, timeout=2)
            # On Windows this remains a harmless best-effort activation. On macOS
            # Page.bringToFront is intentionally the only activation mechanism.
            os_active = _activate_browser_app(browser_name)
            return {"focused": True, "method": "cdp", "targetId": target.get("id"), "url": target.get("url"), "osActive": os_active}
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.10)
    return {"focused": False, "method": "cdp", "error": last_error or "Target not ready"}


def launch_app_browser(app_url: str, profile: str = "default", preferred_browser: str = "auto", timeout_s: float = 18.0) -> dict:
    """Startet EntretienConnect selbst im kontrollierten Chromium-Browser.

    v302: App und e-Bichelchen teilen damit denselben Browserprozess, dasselbe
    Fenster und dasselbe isolierte Profil. Beim Verbinden wird nur ein zweiter Tab
    angelegt; ein separater Chrome-Kaltstart entfällt vollständig. Ein beliebiger
    bereits laufender Standardbrowser (insbesondere Firefox) kann nicht nachträglich
    sicher über DevTools übernommen werden, daher wird die App von Beginn an in der
    kontrollierten Chrome-/Edge-Instanz geöffnet.
    """
    browser = find_browser_executable(preferred_browser)
    if not browser:
        raise RuntimeError("Kein unterstützter Browser gefunden. Installiert sein muss Google Chrome oder Microsoft Edge.")

    profile = sanitize_profile_name(profile)
    profile_dir = PROFILE_ROOT / browser["id"] / profile
    profile_dir.mkdir(parents=True, exist_ok=True)

    with BROWSER_LAUNCH_LOCK:
        if debug_browser_running():
            try:
                targets = [t for t in _list_cdp_targets() if _is_app_target(t)]
                if targets:
                    target = targets[0]
                    _cdp_set_window_state(target, "normal")
                    cdp_call(target.get("webSocketDebuggerUrl"), "Page.bringToFront", {}, msg_id=907, timeout=3)
                    return {"opened": True, "alreadyRunning": True, "reusedAppTab": True, "browser": browser.get("name"), "profileDir": str(profile_dir), "url": target.get("url")}
            except Exception:
                pass
            if not open_remote_tab(app_url):
                raise RuntimeError("Der EntretienConnect-Tab konnte im kontrollierten Browser nicht geöffnet werden.")
        else:
            args = [
                browser["path"],
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                app_url,
            ]
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            BROWSER_PROCESSES[profile] = proc

    deadline = time.time() + max(4.0, float(timeout_s))
    last_error = ""
    while time.time() < deadline:
        try:
            targets = [t for t in _list_cdp_targets() if _is_app_target(t)]
            if targets:
                target = targets[0]
                _cdp_set_window_state(target, "normal")
                cdp_call(target.get("webSocketDebuggerUrl"), "Page.bringToFront", {}, msg_id=908, timeout=3)
                return {"opened": True, "alreadyRunning": False, "reusedAppTab": False, "browser": browser.get("name"), "profileDir": str(profile_dir), "url": target.get("url"), "targetId": target.get("id")}
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.12)
    raise RuntimeError("Der kontrollierte Browser wurde gestartet, aber EntretienConnect erschien nicht rechtzeitig." + ((" " + last_error) if last_error else ""))


def prewarm_browser(profile: str = "default", preferred_browser: str = "auto", wait_ready_s: float = 0.0) -> dict:
    """Startet den isolierten Chromium-Prozess ohne sichtbares Fenster.

    Legacy-Prewarm-Fallback: Der Browser-Kaltstart kann im Hintergrund laufen, falls
    EntretienConnect geöffnet wird. Beim Klick auf « Connecter » muss dadurch nur
    noch der direkte Login-Tab angelegt werden. Das normale Chrome-Profil des
    Benutzers bleibt unangetastet; die automatische Klassen-/Schülerlesung behält
    weiterhin ihren kontrollierten DevTools-Kontext.
    """
    with BROWSER_LAUNCH_LOCK:
        if debug_browser_running():
            return {"prewarmed": True, "alreadyRunning": True, "port": CDP_PORT}

        browser = find_browser_executable(preferred_browser)
        if not browser:
            return {"prewarmed": False, "error": "Kein unterstützter Browser gefunden."}

        profile = sanitize_profile_name(profile)
        profile_dir = PROFILE_ROOT / browser["id"] / profile
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            browser["path"],
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-startup-window",
        ]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            BROWSER_PROCESSES[profile] = proc
        except Exception as exc:
            return {"prewarmed": False, "error": str(exc), "browser": browser.get("name")}

    deadline = time.time() + max(0.0, float(wait_ready_s))
    while time.time() < deadline:
        if debug_browser_running():
            return {"prewarmed": True, "alreadyRunning": False, "ready": True, "port": CDP_PORT, "browser": browser.get("name"), "profileDir": str(profile_dir)}
        if proc.poll() is not None:
            return {"prewarmed": False, "ready": False, "error": "Browser-Prozess wurde vorzeitig beendet.", "browser": browser.get("name")}
        time.sleep(0.10)
    return {"prewarmed": True, "alreadyRunning": False, "ready": debug_browser_running(), "port": CDP_PORT, "browser": browser.get("name"), "profileDir": str(profile_dir)}


def launch_browser(profile: str, preferred_browser: str = "auto", user_agent: str = "") -> dict:
    global ACTIVE_BROWSER_MODE, ACTIVE_BROWSER_USER_AGENT
    if str(preferred_browser or "").lower() == "firefox-current":
        # Firefox speichert die education.lu-Sitzung im lokalen Profil. Dadurch kann
        # EntretienConnect denselben bereits geöffneten Firefox verwenden, ohne eine
        # zweite kontrollierte Chromium-Instanz zu starten.
        ACTIVE_BROWSER_MODE = "firefox-current"
        ACTIVE_BROWSER_USER_AGENT = str(user_agent or "Mozilla/5.0 Firefox")[:500]
        return {"alreadyRunning": True, "openedByPage": True, "sameBrowser": True, "browser": "Firefox", "browserId": "firefox-current", "url": EB_URL}
    ACTIVE_BROWSER_MODE = "cdp"
    ACTIVE_BROWSER_USER_AGENT = str(user_agent or "")[:500]
    browser = find_browser_executable(preferred_browser)
    if not browser:
        raise RuntimeError(
            "Kein unterstützter Browser gefunden. Installiert sein muss Google Chrome oder Microsoft Edge. "
            "Auf Schul-PCs sollte normalerweise Microsoft Edge vorhanden sein."
        )

    browser_path = browser["path"]
    browser_id = browser["id"]
    browser_name = browser["name"]

    profile = sanitize_profile_name(profile)
    profile_dir = PROFILE_ROOT / browser_id / profile
    profile_dir.mkdir(parents=True, exist_ok=True)

    # v302: Der Browser läuft bereits, weil EntretienConnect selbst darin geöffnet ist. Falls der Klick
    # sehr schnell erfolgt, kurz auf dessen DevTools-Port warten, statt einen zweiten
    # Kaltstart mit demselben Profil anzustoßen.
    warm_proc = BROWSER_PROCESSES.get(profile)
    if warm_proc is not None and warm_proc.poll() is None and not debug_browser_running():
        for _ in range(35):
            if debug_browser_running():
                break
            time.sleep(0.10)

    # Falls schon ein Browser mit CDP-Port läuft, einen neuen e-Bichelchen-Tab öffnen.
    # v285: Nur Erfolg melden, wenn /json/new den Tab wirklich angelegt hat. Zuvor
    # wurde ein Fehler hier ignoriert; die Oberfläche wartete dann endlos auf einen
    # Tab, der nie geöffnet worden war.
    try:
        version = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
        # v291: Läuft der Browser bereits, zuerst einen vorhandenen e-Bichelchen-Tab
        # WIEDERVERWENDEN, statt jedes Mal einen neuen zu öffnen. Das verhindert die
        # Tab-Flut und nutzt den nach dem Lesen "warm" (minimiert) gehaltenen Tab weiter,
        # sodass kein langsamer Kaltstart nötig ist. Fenster wird dabei wieder normalisiert.
        try:
            existing = find_ebichelchen_target()
        except Exception:
            existing = None
        if existing:
            focus_info = _bring_ebichelchen_target_forward(browser_name, wait_s=1.0)
            return {"alreadyRunning": True, "reusedTab": True, "openedTab": False, "active": bool(focus_info.get("focused")), "focus": focus_info, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}

        # v299: Eine verwaiste IAM-/Zwischenseite aus einem abgebrochenen Login
        # nicht neben einem neuen e-Bichelchen-Fenster stehen lassen. Der Browser am
        # CDP-Port verwendet ausschließlich das isolierte EntretienConnect-Profil.
        all_pages = [t for t in _list_cdp_targets() if t.get("type") == "page"]
        for blank in [t for t in all_pages if str(t.get("url") or "") in ("", "about:blank") or str(t.get("url") or "").startswith(("chrome://newtab", "edge://newtab"))]:
            if blank.get("id"):
                _cdp_close_tab(blank.get("id"))
        stale_pages = [t for t in _list_cdp_targets() if t.get("type") == "page" and not _is_app_target(t) and "/ebichelchen/app/" not in str(t.get("url") or "")]
        if stale_pages:
            ws = stale_pages[0].get("webSocketDebuggerUrl") or (version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None)
            if ws:
                try:
                    cdp_call(ws, "Browser.close", {}, msg_id=929, timeout=2)
                except Exception:
                    pass
            for _ in range(12):
                if not debug_browser_running():
                    break
                time.sleep(0.1)
            raise OSError("Stale helper browser restarted")

        opened = False
        for _ in range(8):
            if open_remote_tab(EB_URL):
                opened = True
                break
            time.sleep(0.25)
        if opened:
            focus_info = _bring_ebichelchen_target_forward(browser_name, wait_s=2.5)
            return {"alreadyRunning": True, "openedTab": True, "active": bool(focus_info.get("focused")), "focus": focus_info, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
        # Fallback: URL über den Browser-Prozess an die bereits laufende Instanz geben.
        # Chrome/Edge leitet dies normalerweise an dieselbe Profilinstanz weiter.
        try:
            subprocess.Popen([browser_path, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile_dir}", EB_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        for _ in range(16):
            if open_remote_tab(EB_URL):
                focus_info = _bring_ebichelchen_target_forward(browser_name, wait_s=2.5)
                return {"alreadyRunning": True, "openedTab": True, "active": bool(focus_info.get("focused")), "focus": focus_info, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
            time.sleep(0.25)
        raise RuntimeError("Le navigateur est ouvert, mais l’onglet e-Bichelchen n’a pas pu être créé. Réessayez une fois.")
    except RuntimeError:
        raise
    except Exception:
        pass

    args = [
        browser_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        EB_URL,
    ]

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    BROWSER_PROCESSES[profile] = proc

    # Ein Kaltstart kann auf verwalteten Macs/Schul-PCs deutlich länger als sechs
    # Sekunden dauern. Wir warten deshalb länger auf DevTools, ohne die Oberfläche mit
    # vorübergehenden "Connection refused"-Meldungen zu belasten.
    for _ in range(48):
        try:
            version = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=0.5)
            focus_info = _bring_ebichelchen_target_forward(browser_name, wait_s=3.0)
            return {"alreadyRunning": False, "active": bool(focus_info.get("focused")), "focus": focus_info, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(f"{browser_name} s’est fermé avant l’ouverture de la fenêtre e-Bichelchen.")
            time.sleep(0.25)

    return {"alreadyRunning": False, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "warning": "Le navigateur a été lancé, mais sa préparation prend plus de temps que prévu."}


class SimpleWebSocket:
    def __init__(self, ws_url: str, timeout: float = 8.0):
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme != "ws":
            raise ValueError("Nur ws:// URLs werden unterstützt")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(req)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        if b" 101 " not in data.split(b"\r\n", 1)[0]:
            raise RuntimeError("WebSocket Handshake fehlgeschlagen: " + data[:200].decode("latin1", "replace"))

    def send_text(self, text: str):
        payload = text.encode("utf-8")
        first = 0x81  # FIN + text
        mask_bit = 0x80
        length = len(payload)
        header = bytearray([first])
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_text(self) -> str:
        chunks = []
        while True:
            head = self._recvn(2)
            b1, b2 = head[0], head[1]
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recvn(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recvn(8))[0]
            mask = self._recvn(4) if masked else b""
            payload = self._recvn(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                raise RuntimeError("WebSocket wurde geschlossen")
            if opcode == 0x9:  # ping -> pong (persistente Firefox-BiDi-Sitzung)
                self.send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x0):
                chunks.append(payload)
                if b1 & 0x80:
                    return b"".join(chunks).decode("utf-8")

    def send_pong(self, payload: bytes = b""):
        payload = bytes(payload or b"")[:125]
        mask = secrets.token_bytes(4)
        header = bytearray([0x8A, 0x80 | len(payload)])
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recvn(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("Socket unerwartet geschlossen")
            data += chunk
        return data

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def cdp_call(ws_url: str, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    ws = SimpleWebSocket(ws_url)
    try:
        ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}, ensure_ascii=False))
        deadline = time.time() + 12
        while time.time() < deadline:
            msg = json.loads(ws.recv_text())
            if msg.get("id") == msg_id:
                return msg
        raise RuntimeError("Keine CDP-Antwort erhalten")
    finally:
        ws.close()


def cdp_eval(ws_url: str, expression: str, await_promise: bool = False, msg_id: int = 1, timeout_ms: int = 15000) -> dict:
    return cdp_call(ws_url, "Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": await_promise,
        "returnByValue": True,
        "timeout": timeout_ms,
    }, msg_id=msg_id)



def build_read_expression(selected_group_id: int | None = None) -> str:
    # Gibt JSON-String zurück, damit Browser DevTools ihn sauber by value liefert.
    selected_literal = "null" if selected_group_id is None else str(int(selected_group_id))
    js = r"""
(async () => {
  const requestedGroupId = __SELECTED_GROUP_ID__;
  const perfStart = performance.now();
  const timing = {};

  async function getJson(url, options) {
    const res = await fetch(url, Object.assign({
      method: "GET",
      credentials: "include",
      headers: { "accept": "application/json, text/plain, */*", "mobileappversion": "web" }
    }, options || {}));
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error("GET " + url + " -> " + res.status + " " + res.statusText + " " + txt.slice(0, 180));
    }
    return await res.json();
  }

  function extractGroupObjects(json) {
    if (Array.isArray(json)) return json;
    if (!json || typeof json !== "object") return [];
    const candidates = [json.objects, json.groups, json.data, json.data && json.data.objects, json.result, json.result && json.result.objects];
    for (const c of candidates) if (Array.isArray(c)) return c;
    return [];
  }

  async function waitMs(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

  // v299: EntretienConnect steuert die Klassenwahl ausschließlich über API-Parameter.
  // Es gibt absichtlich keine DOM-Klicks, Store-Manipulationen oder location.replace()-
  // Navigationen mehr im sichtbaren e-Bichelchen-Tab.

  async function getGroupsFromTeacher() {
    const preferred = "/ebichelchen/app/api/group/get-groups-from-teacher";
    const attempts = [];
    async function tryOne(url, label) {
      try {
        const json = await getJson(url);
        const count = extractGroupObjects(json).length;
        attempts.push({label, url, ok:true, count});
        return { json, url, attempts };
      } catch (e) {
        attempts.push({label, url, ok:false, error:String(e.message || e).slice(0,180)});
        throw e;
      }
    }
    async function tryAll(label) {
      let firstErr = null;
      try {
        const r = await tryOne(preferred, label + ':preferred');
        if (extractGroupObjects(r.json).length) return r;
      } catch (e) { firstErr = firstErr || e; }
      const urls = [...new Set(performance.getEntriesByType("resource")
        .map(e => e.name)
        .filter(u => /get-groups-from-teacher/i.test(u)))];
      for (const url of urls) {
        try {
          const r = await tryOne(url, label + ':resource');
          if (extractGroupObjects(r.json).length) return r;
        } catch (e) { firstErr = firstErr || e; }
      }
      if (firstErr && !attempts.some(a => a.ok)) throw firstErr;
      return { json:{objects:[]}, url: preferred, attempts };
    }

    // 1) normal versuchen.
    let result = await tryAll('initial');
    if (extractGroupObjects(result.json).length) return result;

    // 2) v299: Die App klickt in e-Bichelchen keinerlei Reiter mehr an. Ein sichtbarer
    // Seitenwechsel (Kalender → Pinnwand/Klassen → Kalender) kann den laufenden CDP-
    // JavaScript-Kontext zerstören. Wir warten stattdessen kurz und fragen denselben
    // authentifizierten API-Endpunkt erneut ab.
    await waitMs(650);
    result = await tryAll('after-short-wait');
    if (extractGroupObjects(result.json).length) return result;

    // 3) Letzter stiller Versuch: IAM/e-Bichelchen füllt die Sitzung manchmal verzögert.
    await waitMs(1300);
    result = await tryAll('after-wait');
    result.attempts = attempts;
    return result;
  }

  function parseStore(name) {
    const raw = sessionStorage.getItem(name);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (_) { return null; }
  }

  function mapPerson(p) {
    if (!p || typeof p !== "object") return null;
    const id = Number(p.id ?? p.studentId ?? p.childId);
    const firstName = String(p.firstName ?? p.firstname ?? "");
    const lastName = String(p.lastName ?? p.lastname ?? "");
    const fullName = String(p.fullName ?? p.displayName ?? p.name ?? (firstName + " " + lastName).trim());
    if (!Number.isFinite(id) || !(firstName || lastName || fullName)) return null;
    return { id, firstName, lastName, fullName };
  }

  function uniqPeople(arr) {
    const m = new Map();
    (arr || []).forEach(p => { const x = mapPerson(p); if (x) m.set(x.id, x); });
    return [...m.values()].sort((a,b) => String(a.fullName).localeCompare(String(b.fullName)));
  }

  function mapGroup(g) {
    const id = Number(g.id ?? g.groupId);
    const students = uniqPeople(g.students || g.children || []);
    const teachers = uniqPeople(g.teachers || []);
    const tutors = uniqPeople(g.tutors || []);
    const excludedMap = new Map();
    teachers.concat(tutors).forEach(p => excludedMap.set(p.id, p));
    return {
      id,
      classAlias: String(g.classAlias ?? g.name ?? ""),
      name: String(g.name ?? g.classAlias ?? ""),
      classGrade: String(g.classGrade ?? ""),
      entityCode: String(g.entityCode ?? ""),
      isActivatedByTeacher: !!g.isActivatedByTeacher,
      isInactive: !!g.isInactive,
      isTestClass: !!g.isTestClass,
      numberOfClasses: g.numberOfClasses ?? null,
      lastSynchDate: g.lastSynchDate ?? null,
      students,
      teachers,
      tutors,
      excluded: [...excludedMap.values()].sort((a,b) => String(a.fullName).localeCompare(String(b.fullName)))
    };
  }

  function findSubjectsFromStorage() {
    const subjects = new Map();
    const scannedKeys = [];
    const seen = new WeakSet();
    function add(obj, source) {
      if (!obj || typeof obj !== "object") return;
      const id = obj.id ?? obj.subjectId;
      const labelDeu = obj.labelDeu ?? obj.labelDE ?? obj.label_deu ?? obj.labelDe ?? "";
      const labelFra = obj.labelFra ?? obj.labelFR ?? obj.label_fra ?? obj.labelFr ?? "";
      const icon = obj.icon ?? "";
      const defaultColor = obj.defaultColor ?? obj.colorId ?? null;
      const labelText = String(labelDeu) + " " + String(labelFra);
      if (id !== undefined && (labelDeu || labelFra) && (icon || defaultColor !== null || /deutsch|fran|math|nachricht|message|wochenplan|ausflug|luxemb/i.test(labelText))) {
        subjects.set(Number(id), { id: Number(id), labelDeu: String(labelDeu || ""), labelFra: String(labelFra || ""), icon: String(icon || ""), defaultColor, source });
      }
    }
    function walk(obj, source, depth = 0) {
      if (!obj || typeof obj !== "object") return;
      if (seen.has(obj)) return;
      if (depth > 12) return;
      seen.add(obj);
      add(obj, source);
      if (Array.isArray(obj)) obj.forEach((v,i) => walk(v, source + "[" + i + "]", depth + 1));
      else for (const [k,v] of Object.entries(obj)) if (v && typeof v === "object") walk(v, source + "." + k, depth + 1);
    }
    for (const [storeName, store] of [["sessionStorage", sessionStorage], ["localStorage", localStorage]]) {
      for (let i=0; i<store.length; i++) {
        const key = store.key(i); const raw = store.getItem(key); if (!raw) continue;
        scannedKeys.push(storeName + ":" + key);
        try { walk(JSON.parse(raw), storeName + ":" + key); } catch (_) {}
      }
    }
    return { subjects: [...subjects.values()].filter(s => Number.isFinite(s.id)).sort((a,b) => String(a.labelDeu || a.labelFra).localeCompare(String(b.labelDeu || b.labelFra))), scannedKeys };
  }

  function normalizeSubjectsFromJson(json, source) {
    const subjects = new Map();
    const seen = new WeakSet();
    function looksLikeSubject(obj, text) {
      if (!obj || typeof obj !== "object") return false;
      if (obj.firstName || obj.lastName || obj.fullName || obj.classAlias || obj.students || obj.teachers || obj.tutors) return false;
      if (obj.labelDeu || obj.labelFra || obj.labelDE || obj.labelFR || obj.label_deu || obj.label_fra) return true;
      return /nachricht|message|deutsch|fran[cç]ais|franz|math|luxemb|wochenplan|ausflug|sortie|sport|kunst|science|wissenschaft/i.test(text || "");
    }
    function add(obj, path) {
      if (!obj || typeof obj !== "object") return;
      const id = Number(obj.id ?? obj.subjectId ?? obj.subject?.id);
      const labelDeu = obj.labelDeu ?? obj.labelDE ?? obj.label_deu ?? obj.labelDe ?? obj.nameDeu ?? obj.nameDe ?? "";
      const labelFra = obj.labelFra ?? obj.labelFR ?? obj.label_fra ?? obj.labelFr ?? obj.nameFra ?? obj.nameFr ?? "";
      const label = obj.label ?? obj.name ?? obj.title ?? obj.description ?? "";
      const icon = obj.icon ?? obj.iconName ?? "";
      const defaultColor = obj.defaultColor ?? obj.colorId ?? obj.color ?? null;
      const text = [labelDeu, labelFra, label, icon].map(x => String(x || "")).join(" ");
      if (Number.isFinite(id) && looksLikeSubject(obj, text)) {
        if (!subjects.has(id)) subjects.set(id, {
          id,
          labelDeu: String(labelDeu || (label && !labelFra ? label : "")),
          labelFra: String(labelFra || ""),
          label: String(label || ""),
          icon: String(icon || ""),
          defaultColor,
          source: source + (path ? " " + path : "")
        });
      }
    }
    function walk(obj, path = "$", depth = 0) {
      if (!obj || typeof obj !== "object" || depth > 10) return;
      if (seen.has(obj)) return;
      seen.add(obj);
      add(obj, path);
      if (Array.isArray(obj)) obj.forEach((v,i) => walk(v, path + "[" + i + "]", depth + 1));
      else for (const [k,v] of Object.entries(obj)) if (v && typeof v === "object") walk(v, path + "." + k, depth + 1);
    }
    walk(json);
    return [...subjects.values()].sort((a,b) => String(a.labelDeu || a.labelFra || a.label || a.id).localeCompare(String(b.labelDeu || b.labelFra || b.label || b.id)));
  }

  function detectMessageSubject(subjects) {
    const rx = /(^|\b|\/|\s)(nachricht|message)(\b|\/|\s|$)/i;
    return (subjects || []).find(s => rx.test([s.labelDeu, s.labelFra, s.label, s.icon].map(x => String(x || "")).join(" "))) || null;
  }

  async function tryReadSubjects(group, strictGroup = false) {
    const gid = group && Number(group.id);
    const attempts = [];
    const triedKeys = new Set();
    const knownUrls = [...new Set(performance.getEntriesByType("resource")
      .map(e => e.name)
      .filter(u => /get-subjects-for-groups/i.test(u)))];

    async function tryFetch(label, url, options) {
      const rawOptions = Object.assign({}, options || {});
      const timeoutMs = Math.max(700, Number(rawOptions.timeoutMs) || 2200);
      delete rawOptions.timeoutMs;
      const key = url + " " + (rawOptions.method || "GET") + " " + (rawOptions.body || "");
      if (triedKeys.has(key)) return [];
      triedKeys.add(key);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const opts = Object.assign({
          method: "GET",
          credentials: "include",
          headers: { "accept": "application/json, text/plain, */*", "mobileappversion": "web" },
          signal: controller.signal
        }, rawOptions);
        opts.signal = controller.signal;
        if (opts.body && !opts.headers["content-type"]) opts.headers["content-type"] = "application/json";
        const res = await fetch(url, opts);
        const text = await res.text();
        if (!res.ok) {
          attempts.push({ label, url, method: opts.method || "GET", ok:false, status:res.status, note:text.slice(0,160) });
          return [];
        }
        let json;
        try { json = JSON.parse(text); } catch (_) {
          attempts.push({ label, url, method: opts.method || "GET", ok:false, status:res.status, note:"Antwort war kein JSON" });
          return [];
        }
        const subjects = normalizeSubjectsFromJson(json, label + " " + url);
        attempts.push({ label, url, method: opts.method || "GET", ok:true, status:res.status, subjects:subjects.length, messageSubjectId:(detectMessageSubject(subjects)||{}).id || null });
        return subjects;
      } catch (e) {
        attempts.push({ label, url, method: rawOptions.method || "GET", ok:false, error:String(e.message || e).slice(0,180) });
        return [];
      } finally {
        clearTimeout(timer);
      }
    }

    async function tryBatch(label, specs) {
      const jobs = (specs || []).map((spec, i) => tryFetch(label + ":" + i, spec.url, spec.options)
        .then(subjects => ({ subjects, spec })));
      if (!jobs.length) return null;
      const results = await Promise.all(jobs);
      return results.find(r => r.subjects && r.subjects.length) || null;
    }

    // 1) Den echten Frontend-Aufruf nur dann bevorzugen, wenn keine Klasse explizit
    // in EntretienConnect gewählt wurde. Bei strictGroup könnte die Resource-URL noch
    // zur zuvor in e-Bichelchen sichtbaren Klasse gehören.
    if (!strictGroup) {
      for (const url of knownUrls) {
        const subjects = await tryFetch("known-resource", url, { timeoutMs:1800 });
        if (subjects.length) return { subjects, source:"known-resource", attempts, knownSubjectUrls: knownUrls };
      }
    }

    // 2) v296 Fast-Path: die wahrscheinlichsten GET-Varianten parallel statt Dutzende
    // Kombinationen nacheinander. Ein langsamer/alter Endpoint blockiert dadurch nicht mehr
    // den gesamten Verbindungsaufbau.
    const v6 = "/ebichelchen/app/api/v6/get-subjects-for-groups";
    const groupApi = "/ebichelchen/app/api/group/get-subjects-for-groups";
    const legacy = "/ebichelchen/app/api/get-subjects-for-groups";
    const gidQ = Number.isFinite(gid) ? encodeURIComponent(gid) : "";
    const fastGet = Number.isFinite(gid) ? [
      { url:v6 + "?groupId=" + gidQ },
      { url:v6 + "?groupIds=" + gidQ },
      { url:groupApi + "?groupId=" + gidQ },
      ...(strictGroup ? [] : [{ url:v6 }])
    ] : [{ url:v6 }, { url:groupApi }, { url:legacy }];
    let hit = await tryBatch("fast-get", fastGet);
    if (hit) return { subjects:hit.subjects, source:"fast-get " + hit.spec.url, attempts, knownSubjectUrls: knownUrls };

    // 3) Seltenere GET-Varianten ebenfalls parallel und mit kurzem Timeout prüfen.
    const baseUrls = [v6, groupApi, legacy];
    const queryParts = strictGroup && Number.isFinite(gid) ? [] : [""];
    if (Number.isFinite(gid)) {
      queryParts.unshift("?groupId=" + gidQ, "?groupIds=" + gidQ);
      queryParts.push("?ids=" + gidQ);
      if (group.classGrade) queryParts.push("?groupId=" + gidQ + "&classGrade=" + encodeURIComponent(group.classGrade));
    }
    const fallbackGet = [];
    for (const base of baseUrls) for (const q of queryParts) fallbackGet.push({ url:base + q });
    hit = await tryBatch("fallback-get", fallbackGet);
    if (hit) return { subjects:hit.subjects, source:"fallback-get " + hit.spec.url, attempts, knownSubjectUrls: knownUrls };

    // 4) Falls das Frontend POST verwendet: wahrscheinliche Körper zuerst parallel.
    if (Number.isFinite(gid)) {
      const headers = { "accept":"application/json, text/plain, */*", "mobileappversion":"web", "content-type":"application/json" };
      const fastPostBodies = [
        { groupId:gid },
        { groupIds:[gid] },
        [gid],
        { ids:[gid] }
      ];
      const fastPost = fastPostBodies.map(body => ({ url:v6, options:{ method:"POST", body:JSON.stringify(body), headers, timeoutMs:2200 } }));
      hit = await tryBatch("fast-post", fastPost);
      if (hit) return { subjects:hit.subjects, source:"fast-post " + hit.spec.url, attempts, knownSubjectUrls: knownUrls };

      // Letzter Kompatibilitäts-Fallback für ältere Installationen.
      const bodies = [
        { groupId: gid }, { groupIds: [gid] }, { ids: [gid] }, [gid],
        [{ id: gid }], { groups: [{ id: gid }] }
      ];
      const fallbackPost = [];
      for (const base of baseUrls) for (const body of bodies) {
        fallbackPost.push({ url:base, options:{ method:"POST", body:JSON.stringify(body), headers, timeoutMs:2200 } });
      }
      hit = await tryBatch("fallback-post", fallbackPost);
      if (hit) return { subjects:hit.subjects, source:"fallback-post " + hit.spec.url, attempts, knownSubjectUrls: knownUrls };
    }

    return { subjects: [], source:null, attempts, knownSubjectUrls: knownUrls };
  }

  if (!location.href.includes("/ebichelchen/app/")) throw new Error("Bitte im echten e-Bichelchen-Tab bleiben.");

  const groupsT0 = performance.now();
  const groupsResult = await getGroupsFromTeacher();
  timing.groupsMs = Math.round(performance.now() - groupsT0);
  let groupObjects = extractGroupObjects(groupsResult.json);
  const groups = groupObjects.map(mapGroup).filter(g => Number.isFinite(g.id)).sort((a,b) => String(a.classAlias || a.name).localeCompare(String(b.classAlias || b.name)));
  if (!groups.length) throw new Error("Keine Klassen aus get-groups-from-teacher erhalten. Automatik-Versuche: " + JSON.stringify(groupsResult.attempts || []).slice(0, 900));

  const groupStore = parseStore("groupStore");
  const userStore = parseStore("userStore");
  const selectedFromStore = Number(groupStore?.selectedGroup?.id);

  // v299: EntretienConnect ist bei mehreren Klassen die einzige maßgebliche Auswahl.
  // Die in der sichtbaren e-Bichelchen-Oberfläche zuletzt gewählte Klasse wird bewusst
  // ignoriert. Dadurch erscheint immer zuerst die Klassenwahl in EntretienConnect und
  // ein Klick dort arbeitet direkt mit der angeforderten groupId — ohne DOM-Klick,
  // sessionStorage-Manipulation oder Navigation im e-Bichelchen-Tab.
  let group = null;
  let groupChosenAutomatically = false;
  if (requestedGroupId !== null) {
    group = groups.find(g => Number(g.id) === Number(requestedGroupId)) || null;
    if (!group) throw new Error("Die in EntretienConnect gewählte Klasse wurde in e-Bichelchen nicht gefunden (groupId " + requestedGroupId + ").");
  } else if (groups.length === 1) {
    group = groups[0];
    groupChosenAutomatically = true;
  }

  const subjectsT0 = performance.now();
  const storageSubjects = findSubjectsFromStorage();
  let subjects = storageSubjects.subjects.map(s => ({ id:s.id, labelDeu:s.labelDeu||"", labelFra:s.labelFra||"", label:s.label||"", icon:s.icon||"", defaultColor:s.defaultColor ?? null, source:s.source||"storage" }));
  let subjectsSource = subjects.length ? "storage" : null;
  let subjectAttempts = [];
  let messageSubject = detectMessageSubject(subjects);

  // v299: Nach einem Klick in EntretienConnect wird die Kategorie zuerst mit der
  // ausdrücklich gewählten groupId abgefragt. Die aktuell sichtbare Klasse bzw. ein
  // alter Store in e-Bichelchen darf die Auswahl nicht mehr beeinflussen.
  if (requestedGroupId !== null && group) {
    const explicitApiSubj = await tryReadSubjects(group, true);
    const explicitMessage = detectMessageSubject(explicitApiSubj.subjects);
    subjectAttempts = explicitApiSubj.attempts || [];
    storageSubjects.knownSubjectUrls = explicitApiSubj.knownSubjectUrls || [];
    if (explicitMessage) {
      subjects = explicitApiSubj.subjects;
      subjectsSource = explicitApiSubj.source;
      messageSubject = explicitMessage;
    }
  }
  if (!messageSubject) {
    const apiSubj = await tryReadSubjects(group, requestedGroupId !== null);
    if (apiSubj.subjects.length) {
      subjects = apiSubj.subjects;
      subjectsSource = apiSubj.source;
      messageSubject = detectMessageSubject(subjects);
    }
    subjectAttempts = subjectAttempts.concat(apiSubj.attempts || []);
    storageSubjects.knownSubjectUrls = apiSubj.knownSubjectUrls || storageSubjects.knownSubjectUrls || [];
  }

  timing.subjectsMs = Math.round(performance.now() - subjectsT0);
  timing.totalMs = Math.round(performance.now() - perfStart);

  const loggedInUser = userStore?.loggedInUser ? {
    firstName: userStore.loggedInUser.firstName || "",
    lastName: userStore.loggedInUser.lastName || "",
    fullName: userStore.loggedInUser.fullName || [userStore.loggedInUser.firstName, userStore.loggedInUser.lastName].filter(Boolean).join(" "),
    id: userStore.loggedInUser.id || null
  } : null;

  const payload = {
    version: "1.10.26",
    importedAt: new Date().toISOString(),
    pageUrl: location.href,
    groups,
    needsGroupSelection: !group && groups.length > 1,
    group: group ? { id: group.id, classAlias: group.classAlias, name: group.name, classGrade: group.classGrade, entityCode: group.entityCode } : null,
    loggedInUser,
    students: group ? group.students : [],
    teachers: group ? group.teachers : [],
    tutors: group ? group.tutors : [],
    excluded: group ? group.excluded : [],
    subjects,
    messageSubject: messageSubject ? { id: messageSubject.id, labelDeu: messageSubject.labelDeu || "", labelFra: messageSubject.labelFra || "", label: messageSubject.label || "", source: messageSubject.source || subjectsSource || "" } : null,
    endpoints: { groupsUrl: groupsResult.url, subjectsSource, scannedStorageKeys: storageSubjects.scannedKeys, knownSubjectUrls: storageSubjects.knownSubjectUrls || [], subjectAttempts },
    timing,
    summary: { groups: groups.length, students: group ? group.students.length : 0, teachers: group ? group.teachers.length : 0, tutors: group ? group.tutors.length : 0, excluded: group ? group.excluded.length : 0, subjects: subjects.length, messageSubjectId: messageSubject ? messageSubject.id : null }
  };
  return JSON.stringify(payload);
})()
"""
    return js.replace("__SELECTED_GROUP_ID__", selected_literal)

def find_ebichelchen_target() -> dict:
    targets = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3)
    if not isinstance(targets, list):
        raise RuntimeError("Le navigateur n’a pas répondu (DevTools sans liste d’onglets).")

    # Wichtig: Beim automatischen Einlesen NIEMALS selbst einen neuen e-Bichelchen-Tab öffnen.
    # Während IAM/Login kann der Browser kurz auf einer fremden Login-URL sein.
    # v1.5 hat in diesem Moment irrtümlich wieder EB_URL geöffnet und dadurch den Login unterbrochen.
    pages = [t for t in targets if t.get("type") == "page"]
    candidates = [t for t in pages if "/ebichelchen/app/" in (t.get("url") or "")]

    if not candidates:
        current_urls = [t.get("url") or "" for t in pages[:6]]
        hint = " | ".join(u[:90] for u in current_urls if u)
        raise RuntimeError("En attente d’e-Bichelchen. Terminez la connexion dans la fenêtre ouverte ; aucun nouvel onglet ne sera ouvert automatiquement. Onglets actuels : " + hint)

    # v299: Jede echte e-Bichelchen-Seite ist ausreichend; Pinnwand und andere
    # Bereiche werden nicht auf den Kalender umgeschaltet.
    target = candidates[0]
    if not target.get("webSocketDebuggerUrl"):
        raise RuntimeError("Der e-Bichelchen-Tab hat keine DevTools-WebSocket-URL geliefert.")
    return target




def _firefox_profile_dirs() -> list[pathlib.Path]:
    """Findet Firefox-Profile, ohne den Browser zu steuern oder zu beenden."""
    system = platform.system().lower()
    if system == "darwin":
        root = pathlib.Path.home() / "Library" / "Application Support" / "Firefox"
    elif system == "windows":
        root = pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home()) / "Mozilla" / "Firefox"
    else:
        root = pathlib.Path.home() / ".mozilla" / "firefox"
    profiles_root = root / "Profiles"
    found: list[pathlib.Path] = []

    ini = root / "profiles.ini"
    if ini.exists():
        cp = configparser.RawConfigParser()
        try:
            cp.read(ini, encoding="utf-8")
            for section in cp.sections():
                if not section.lower().startswith("profile"):
                    continue
                raw = cp.get(section, "Path", fallback="").strip()
                if not raw:
                    continue
                is_relative = cp.get(section, "IsRelative", fallback="1").strip() != "0"
                path = (root / raw) if is_relative else pathlib.Path(os.path.expanduser(raw))
                if path.is_dir() and path not in found:
                    found.append(path)
        except Exception:
            pass

    if profiles_root.is_dir():
        try:
            for path in profiles_root.iterdir():
                if path.is_dir() and path not in found:
                    found.append(path)
        except Exception:
            pass

    def score(path: pathlib.Path):
        # v304: Session-Cookies werden von Firefox häufig nicht in cookies.sqlite
        # geschrieben. Das aktive Profil erkennt man zuverlässiger an der neuesten
        # Session-Restore-Datei (recovery.jsonlz4).
        mtimes = []
        for candidate in (
            path / "cookies.sqlite",
            path / "sessionstore-backups" / "recovery.jsonlz4",
            path / "sessionstore-backups" / "recovery.baklz4",
            path / "sessionstore.jsonlz4",
        ):
            try:
                mtimes.append(candidate.stat().st_mtime)
            except Exception:
                pass
        return max(mtimes, default=0)
    found.sort(key=score, reverse=True)
    return found



def _lz4_block_decompress_with_size(data: bytes) -> bytes:
    """Kleiner, dependency-freier Decoder für Mozillas LZ4-Blockformat.

    Nach dem 8-Byte-Header ``mozLz40\0`` folgen vier Bytes mit der
    unkomprimierten Länge und anschließend ein normaler LZ4-Block. Die App darf
    sich nicht darauf verlassen, dass beim Benutzer das optionale Python-Paket
    ``lz4`` installiert ist.
    """
    if len(data) < 4:
        raise ValueError("LZ4-Block ist zu kurz.")
    expected = struct.unpack_from("<I", data, 0)[0]
    src = memoryview(data)[4:]
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = int(src[i]); i += 1
        literal_len = token >> 4
        if literal_len == 15:
            while True:
                if i >= n: raise ValueError("Ungültige LZ4-Literallänge.")
                value = int(src[i]); i += 1
                literal_len += value
                if value != 255: break
        if i + literal_len > n:
            raise ValueError("LZ4-Literale überschreiten den Eingabeblock.")
        out.extend(src[i:i + literal_len])
        i += literal_len
        if i >= n:
            break
        if i + 2 > n:
            raise ValueError("LZ4-Match-Offset fehlt.")
        offset = int(src[i]) | (int(src[i + 1]) << 8)
        i += 2
        if offset <= 0 or offset > len(out):
            raise ValueError("Ungültiger LZ4-Match-Offset.")
        match_len = token & 0x0F
        if match_len == 15:
            while True:
                if i >= n: raise ValueError("Ungültige LZ4-Matchlänge.")
                value = int(src[i]); i += 1
                match_len += value
                if value != 255: break
        match_len += 4
        start = len(out) - offset
        for _ in range(match_len):
            out.append(out[start])
            start += 1
    if expected and len(out) != expected:
        raise ValueError(f"LZ4-Länge stimmt nicht ({len(out)} statt {expected}).")
    return bytes(out)


def _read_mozlz4_json(path: pathlib.Path):
    raw = path.read_bytes()
    magic = b"mozLz40\x00"
    if not raw.startswith(magic):
        raise ValueError("Kein Firefox-JSONLZ4-Format.")
    decoded = _lz4_block_decompress_with_size(raw[len(magic):])
    return json.loads(decoded.decode("utf-8"))


def _firefox_sessionstore_cookie_rows(profile_dir: pathlib.Path) -> tuple[list[dict], dict]:
    """Liest nur die education.lu-Cookies aus Firefox' Session-Restore-Datei.

    Firefox hält echte Session-Cookies im Arbeitsspeicher; sie fehlen daher oft
    in cookies.sqlite. recovery.jsonlz4 enthält diese Cookies für die
    Sitzungswiederherstellung und wird während des Browsens regelmäßig erneuert.
    Alle anderen Sessiondaten werden unmittelbar verworfen.
    """
    paths = [
        profile_dir / "sessionstore-backups" / "recovery.jsonlz4",
        profile_dir / "sessionstore-backups" / "recovery.baklz4",
        profile_dir / "sessionstore.jsonlz4",
    ]
    existing = []
    for path in paths:
        try:
            existing.append((path.stat().st_mtime, path))
        except Exception:
            pass
    existing.sort(reverse=True)
    last_error = ""
    for mtime, path in existing:
        try:
            try:
                size = path.stat().st_size
            except Exception:
                size = -1
            cached = FIREFOX_SESSIONSTORE_CACHE.get(str(path))
            if cached and cached[0] == mtime and cached[1] == size:
                return [dict(r) for r in cached[2]], dict(cached[3])
            data = _read_mozlz4_json(path)
            raw_cookies = data.get("cookies") if isinstance(data, dict) else None
            if not isinstance(raw_cookies, list):
                raw_cookies = []
            rows = []
            for cookie in raw_cookies:
                if not isinstance(cookie, dict):
                    continue
                host = str(cookie.get("host") or "")
                normalized = host.lower().lstrip(".")
                if not (normalized == "education.lu" or normalized.endswith(".education.lu")):
                    continue
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if not name:
                    continue
                origin = cookie.get("originAttributes") or {}
                rows.append({
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": str(cookie.get("path") or "/"),
                    "expiry": int(cookie.get("expiry") or 0),
                    "lastAccessed": int(mtime * 1_000_000),
                    "creationTime": int(mtime * 1_000_000),
                    "originAttributes": origin,
                    "source": "sessionstore",
                })
            meta = {
                "file": str(path),
                "mtime": mtime,
                "ageSeconds": max(0.0, time.time() - mtime),
                "cookieCount": len(rows),
                "error": "",
            }
            FIREFOX_SESSIONSTORE_CACHE[str(path)] = (mtime, size, [dict(r) for r in rows], dict(meta))
            return rows, meta
        except Exception as exc:
            last_error = str(exc)
    return [], {"file": "", "mtime": 0, "ageSeconds": None, "cookieCount": 0, "error": last_error}


def _origin_attr_key(value) -> str:
    if isinstance(value, dict):
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(value)
    return str(value or "")


def _is_default_firefox_context(value) -> bool:
    if not value:
        return True
    if isinstance(value, dict):
        return not any(value.get(k) for k in ("userContextId", "privateBrowsingId", "partitionKey", "firstPartyDomain"))
    text = str(value)
    return not any(marker in text for marker in ("userContextId=", "privateBrowsingId=", "partitionKey=", "firstPartyDomain="))


def _cookie_applies_to_ssl_ebichelchen(row: dict) -> bool:
    target_host = "ssl.education.lu"
    target_path = "/ebichelchen/app/api/"
    domain = str(row.get("domain") or "").lower().lstrip(".")
    if not domain or not (target_host == domain or target_host.endswith("." + domain)):
        return False
    cookie_path = str(row.get("path") or "/")
    return target_path.startswith(cookie_path.rstrip("/") + "/") or target_path == cookie_path or cookie_path == "/"

def _query_firefox_cookie_db(db_path: pathlib.Path) -> list[dict]:
    sql = """
        SELECT name, value, host, path, expiry, lastAccessed, creationTime,
               COALESCE(originAttributes, '')
        FROM moz_cookies
        WHERE lower(host) IN ('education.lu', '.education.lu')
           OR lower(host) LIKE '%.education.lu'
    """
    rows = []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path), safe='/:')}?mode=ro", uri=True, timeout=0.4)
        rows = conn.execute(sql).fetchall()
    except Exception:
        # Firefox nutzt WAL. Wenn ein Virenscanner/eine alte SQLite-Version den direkten
        # Reader blockiert, eine kurze lokale Momentaufnahme inklusive WAL verwenden.
        try:
            with tempfile.TemporaryDirectory(prefix="entretienconnect_ff_") as td:
                dst = pathlib.Path(td) / "cookies.sqlite"
                shutil.copy2(db_path, dst)
                for suffix in ("-wal", "-shm"):
                    src_extra = pathlib.Path(str(db_path) + suffix)
                    if src_extra.exists():
                        try:
                            shutil.copy2(src_extra, pathlib.Path(str(dst) + suffix))
                        except Exception:
                            pass
                conn2 = sqlite3.connect(str(dst), timeout=0.4)
                try:
                    rows = conn2.execute(sql).fetchall()
                finally:
                    conn2.close()
        except Exception:
            rows = []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    out = []
    for row in rows:
        try:
            out.append({
                "name": str(row[0] or ""),
                "value": str(row[1] or ""),
                "domain": str(row[2] or ""),
                "path": str(row[3] or "/"),
                "expiry": int(row[4] or 0),
                "lastAccessed": int(row[5] or 0),
                "creationTime": int(row[6] or 0),
                "originAttributes": str(row[7] or ""),
            })
        except Exception:
            continue
    return out


def capture_firefox_session() -> dict:
    """Erfasst ausschließlich die für ssl.education.lu relevanten Cookies.

    v303 las nur ``cookies.sqlite``. Das ist bei Firefox unzuverlässig, weil
    Session-Cookies dort während eines laufenden Browsers oft überhaupt nicht
    erscheinen. v304 kombiniert deshalb persistente Cookies mit den
    Session-Cookies aus ``recovery.jsonlz4``.
    """
    now = int(time.time())
    candidates = []
    diagnostics = []
    for profile_dir in _firefox_profile_dirs():
        persistent = []
        db = profile_dir / "cookies.sqlite"
        if db.exists():
            persistent = _query_firefox_cookie_db(db)
            persistent = [r for r in persistent if r.get("name") and (not r.get("expiry") or int(r.get("expiry") or 0) > now)]
            for row in persistent:
                row.setdefault("source", "cookies.sqlite")
        session_rows, session_meta = _firefox_sessionstore_cookie_rows(profile_dir)
        rows = persistent + session_rows
        applicable = [r for r in rows if _cookie_applies_to_ssl_ebichelchen(r)]
        diagnostics.append({
            "profile": str(profile_dir),
            "persistent": len(persistent),
            "session": len(session_rows),
            "applicable": len(applicable),
            "sessionFile": session_meta.get("file"),
            "sessionFileAgeSeconds": session_meta.get("ageSeconds"),
            "sessionError": session_meta.get("error"),
        })
        if applicable:
            newest = max([int(r.get("lastAccessed") or 0) for r in applicable] + [int((session_meta.get("mtime") or 0) * 1_000_000)])
            candidates.append((newest, profile_dir, applicable, session_meta))
    if not candidates:
        raise RuntimeError("Noch keine verwendbare e-Bichelchen-Sitzung in Firefox gefunden. Bitte Login abschließen; Firefox kann die Sitzung mit kurzer Verzögerung speichern.")

    _, profile_dir, rows, session_meta = max(candidates, key=lambda item: item[0])

    # Der von EntretienConnect geöffnete Tab läuft normalerweise im Standard-Container.
    # Partitionierte/Container-Cookies nur verwenden, wenn keine Standardkopie existiert.
    default_rows = [r for r in rows if _is_default_firefox_context(r.get("originAttributes"))]
    use_rows = default_rows or rows

    # Gleiche Cookies aus cookies.sqlite und recovery.jsonlz4 zusammenführen. Die
    # Session-Restore-Kopie gewinnt, da sie die aktuelle Login-Sitzung enthält.
    merged: dict[tuple, dict] = {}
    for row in sorted(use_rows, key=lambda r: (0 if r.get("source") == "cookies.sqlite" else 1, int(r.get("lastAccessed") or 0))):
        key = (
            str(row.get("name") or ""),
            str(row.get("domain") or "").lower(),
            str(row.get("path") or "/"),
            _origin_attr_key(row.get("originAttributes")),
        )
        merged[key] = row
    final_rows = list(merged.values())
    final_rows.sort(key=lambda r: (-len(str(r.get("path") or "/")), str(r.get("name") or "")))
    cookie_header = "; ".join(f"{r['name']}={r.get('value','')}" for r in final_rows if r.get("name"))
    if not cookie_header:
        raise RuntimeError("Keine verwendbaren education.lu-Cookies in Firefox gefunden.")

    ua = ACTIVE_BROWSER_USER_AGENT or "Mozilla/5.0 (Macintosh; Intel Mac OS X) Gecko/20100101 Firefox"
    source_counts = {
        "sessionstore": sum(1 for r in final_rows if r.get("source") == "sessionstore"),
        "cookiesSqlite": sum(1 for r in final_rows if r.get("source") == "cookies.sqlite"),
    }
    return {
        "cookieHeader": cookie_header,
        "cookieNames": sorted({str(r.get("name")) for r in final_rows if r.get("name")}),
        "cookieSources": source_counts,
        "userAgent": ua,
        "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "targetUrl": EB_URL,
        "browser": "firefox-current",
        "profileDir": str(profile_dir),
        "sessionStoreFile": session_meta.get("file") or "",
        "sessionStoreAgeSeconds": session_meta.get("ageSeconds"),
        "profileDiagnostics": diagnostics,
    }

def _session_request(session: dict, method: str, path: str, json_body=None, timeout: float = 5.0) -> dict:
    url = path if str(path).startswith("http") else "https://ssl.education.lu" + str(path)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://ssl.education.lu/ebichelchen/app/",
        "Cookie": session.get("cookieHeader") or "",
        "User-Agent": session.get("userAgent") or "Mozilla/5.0",
        "mobileappversion": "web",
    }
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://ssl.education.lu"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    status = 0
    text = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            status = int(resp.getcode() or 0)
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        status = int(getattr(exc, "code", 0) or 0)
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = str(exc)
    try:
        body = json.loads(text) if text else None
    except Exception:
        body = None
    return {"ok": 200 <= status < 300, "status": status, "body": body, "text": text[:700], "url": url}


def _extract_group_objects_py(body) -> list:
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    candidates = [
        body.get("objects"), body.get("groups"), body.get("data"),
        (body.get("data") or {}).get("objects") if isinstance(body.get("data"), dict) else None,
        body.get("result"),
        (body.get("result") or {}).get("objects") if isinstance(body.get("result"), dict) else None,
    ]
    return next((c for c in candidates if isinstance(c, list)), [])


def _map_person_py(person) -> dict | None:
    if not isinstance(person, dict):
        return None
    raw_id = person.get("id", person.get("studentId", person.get("childId")))
    try:
        pid = int(raw_id)
    except Exception:
        return None
    first = str(person.get("firstName", person.get("firstname", "")) or "")
    last = str(person.get("lastName", person.get("lastname", "")) or "")
    full = str(person.get("fullName", person.get("displayName", person.get("name", ""))) or "")
    if not full:
        full = (first + " " + last).strip()
    if not (first or last or full):
        return None
    return {"id": pid, "firstName": first, "lastName": last, "fullName": full}


def _unique_people_py(items) -> list:
    mapped = {}
    for item in items if isinstance(items, list) else []:
        p = _map_person_py(item)
        if p:
            mapped[p["id"]] = p
    return sorted(mapped.values(), key=lambda p: str(p.get("fullName") or "").casefold())


def _map_group_py(group) -> dict | None:
    if not isinstance(group, dict):
        return None
    try:
        gid = int(group.get("id", group.get("groupId")))
    except Exception:
        return None
    students = _unique_people_py(group.get("students") or group.get("children") or [])
    teachers = _unique_people_py(group.get("teachers") or [])
    tutors = _unique_people_py(group.get("tutors") or [])
    excluded = {p["id"]: p for p in teachers + tutors}
    return {
        "id": gid,
        "classAlias": str(group.get("classAlias", group.get("name", "")) or ""),
        "name": str(group.get("name", group.get("classAlias", "")) or ""),
        "classGrade": str(group.get("classGrade") or ""),
        "entityCode": str(group.get("entityCode") or ""),
        "isActivatedByTeacher": bool(group.get("isActivatedByTeacher")),
        "isInactive": bool(group.get("isInactive")),
        "isTestClass": bool(group.get("isTestClass")),
        "numberOfClasses": group.get("numberOfClasses"),
        "lastSynchDate": group.get("lastSynchDate"),
        "students": students,
        "teachers": teachers,
        "tutors": tutors,
        "excluded": sorted(excluded.values(), key=lambda p: str(p.get("fullName") or "").casefold()),
    }


def _normalize_subjects_py(body, source: str) -> list:
    subjects: dict[int, dict] = {}
    seen: set[int] = set()
    rx_words = ("nachricht", "message", "deutsch", "fran", "math", "luxemb", "wochenplan", "ausflug", "sortie", "sport", "kunst", "science", "wissenschaft")

    def walk(obj, path="$", depth=0):
        if depth > 10 or not isinstance(obj, (dict, list)):
            return
        oid = id(obj)
        if oid in seen:
            return
        seen.add(oid)
        if isinstance(obj, dict):
            if not any(k in obj for k in ("firstName", "lastName", "fullName", "classAlias", "students", "teachers", "tutors")):
                raw_id = obj.get("id", obj.get("subjectId"))
                if raw_id is None and isinstance(obj.get("subject"), dict):
                    raw_id = obj["subject"].get("id")
                try:
                    sid = int(raw_id)
                except Exception:
                    sid = None
                label_de = obj.get("labelDeu", obj.get("labelDE", obj.get("label_deu", obj.get("labelDe", obj.get("nameDeu", obj.get("nameDe", ""))))))
                label_fr = obj.get("labelFra", obj.get("labelFR", obj.get("label_fra", obj.get("labelFr", obj.get("nameFra", obj.get("nameFr", ""))))))
                label = obj.get("label", obj.get("name", obj.get("title", obj.get("description", ""))))
                icon = obj.get("icon", obj.get("iconName", ""))
                text = " ".join(str(x or "") for x in (label_de, label_fr, label, icon)).casefold()
                looks = bool(label_de or label_fr or any(w in text for w in rx_words))
                if sid is not None and looks and sid not in subjects:
                    subjects[sid] = {
                        "id": sid,
                        "labelDeu": str(label_de or (label if label and not label_fr else "")),
                        "labelFra": str(label_fr or ""),
                        "label": str(label or ""),
                        "icon": str(icon or ""),
                        "defaultColor": obj.get("defaultColor", obj.get("colorId", obj.get("color"))),
                        "source": source + " " + path,
                    }
            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    walk(value, path + "." + str(key), depth + 1)
        else:
            for i, value in enumerate(obj):
                if isinstance(value, (dict, list)):
                    walk(value, f"{path}[{i}]", depth + 1)

    walk(body)
    return sorted(subjects.values(), key=lambda s: str(s.get("labelDeu") or s.get("labelFra") or s.get("label") or s.get("id")).casefold())


def _detect_message_subject_py(subjects: list) -> dict | None:
    for subject in subjects or []:
        text = " ".join(str(subject.get(k) or "") for k in ("labelDeu", "labelFra", "label", "icon")).casefold()
        if "nachricht" in text or "message" in text:
            return subject
    return None


def _read_subjects_direct(session: dict, group: dict) -> tuple[list, str | None, list]:
    gid = int(group["id"])
    gid_q = urllib.parse.quote(str(gid))
    bases = [
        "/ebichelchen/app/api/v6/get-subjects-for-groups",
        "/ebichelchen/app/api/group/get-subjects-for-groups",
        "/ebichelchen/app/api/get-subjects-for-groups",
    ]
    attempts = []
    get_paths = [
        bases[0] + "?groupId=" + gid_q,
        bases[0] + "?groupIds=" + gid_q,
        bases[1] + "?groupId=" + gid_q,
        bases[1] + "?groupIds=" + gid_q,
        bases[2] + "?groupId=" + gid_q,
    ]
    for path in get_paths:
        res = _session_request(session, "GET", path, timeout=3.0)
        subjects = _normalize_subjects_py(res.get("body"), "direct-get " + path) if res.get("ok") else []
        attempts.append({"url": path, "method": "GET", "status": res.get("status"), "subjects": len(subjects), "messageSubjectId": (_detect_message_subject_py(subjects) or {}).get("id")})
        if _detect_message_subject_py(subjects):
            return subjects, "direct-get " + path, attempts
    bodies = [{"groupId": gid}, {"groupIds": [gid]}, [gid], {"ids": [gid]}]
    for body in bodies:
        res = _session_request(session, "POST", bases[0], json_body=body, timeout=3.2)
        subjects = _normalize_subjects_py(res.get("body"), "direct-post " + bases[0]) if res.get("ok") else []
        attempts.append({"url": bases[0], "method": "POST", "status": res.get("status"), "subjects": len(subjects), "messageSubjectId": (_detect_message_subject_py(subjects) or {}).get("id")})
        if _detect_message_subject_py(subjects):
            return subjects, "direct-post " + bases[0], attempts
    return [], None, attempts


def _firefox_groups(session: dict) -> tuple[list, dict]:
    path = "/ebichelchen/app/api/group/get-groups-from-teacher"
    result = _session_request(session, "GET", path, timeout=4.0)
    raw_groups = _extract_group_objects_py(result.get("body")) if result.get("ok") else []
    groups = []
    for raw in raw_groups:
        mapped = _map_group_py(raw)
        if mapped:
            groups.append(mapped)
    groups.sort(key=lambda g: str(g.get("classAlias") or g.get("name") or g.get("id")).casefold())
    return groups, result


def firefox_check_login_ready() -> dict:
    try:
        session = capture_firefox_session()
        groups, result = _firefox_groups(session)
        if groups:
            global LATEST_SESSION, LATEST_SESSION_AT
            with LOCK:
                LATEST_SESSION = session
                LATEST_SESSION_AT = session.get("capturedAt")
            return {"ok": True, "ready": True, "browserClosed": False, "stage": "ready", "groupCount": len(groups), "status": result.get("status"), "via": "firefox-cookie-api", "lightweight": True}
        cookie_sources = session.get("cookieSources") or {}
        stage = "session-sync" if sum(int(v or 0) for v in cookie_sources.values()) > 0 else "login"
        return {
            "ok": True, "ready": False, "browserClosed": False, "stage": stage,
            "status": result.get("status"), "via": "firefox-session-api",
            "cookieSources": cookie_sources,
            "sessionStoreAgeSeconds": session.get("sessionStoreAgeSeconds"),
            "lightweight": True,
        }
    except Exception as exc:
        return {"ok": True, "ready": False, "browserClosed": False, "stage": "login", "detail": str(exc), "lightweight": True}


def read_from_firefox(selected_group_id: int | None = None) -> dict:
    t0 = time.perf_counter()
    session = capture_firefox_session()
    groups_t0 = time.perf_counter()
    groups, group_result = _firefox_groups(session)
    groups_ms = round((time.perf_counter() - groups_t0) * 1000)
    if not groups:
        raise RuntimeError("Keine Klassen aus dem aktuellen Firefox erhalten. Bitte e-Bichelchen-Login abschließen.")

    group = None
    chosen_automatically = False
    if selected_group_id is not None:
        group = next((g for g in groups if int(g.get("id")) == int(selected_group_id)), None)
        if group is None:
            raise RuntimeError(f"Die in EntretienConnect gewählte Klasse wurde nicht gefunden (groupId {selected_group_id}).")
    elif len(groups) == 1:
        group = groups[0]
        chosen_automatically = True

    subjects = []
    message_subject = None
    subjects_source = None
    subject_attempts = []
    subjects_t0 = time.perf_counter()
    if group is not None:
        subjects, subjects_source, subject_attempts = _read_subjects_direct(session, group)
        message_subject = _detect_message_subject_py(subjects)
        if not message_subject:
            raise RuntimeError("Die Kategorie « Nachricht / Message » konnte für die gewählte Klasse nicht gelesen werden.")
    subjects_ms = round((time.perf_counter() - subjects_t0) * 1000)

    payload = {
        "version": "1.10.28",
        "importedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pageUrl": EB_URL,
        "groups": groups,
        "needsGroupSelection": group is None and len(groups) > 1,
        "group": ({k: group.get(k) for k in ("id", "classAlias", "name", "classGrade", "entityCode")} if group else None),
        "loggedInUser": None,
        "students": group.get("students", []) if group else [],
        "teachers": group.get("teachers", []) if group else [],
        "tutors": group.get("tutors", []) if group else [],
        "excluded": group.get("excluded", []) if group else [],
        "subjects": subjects,
        "messageSubject": ({
            "id": message_subject.get("id"),
            "labelDeu": message_subject.get("labelDeu", ""),
            "labelFra": message_subject.get("labelFra", ""),
            "label": message_subject.get("label", ""),
            "source": message_subject.get("source", subjects_source or ""),
        } if message_subject else None),
        "endpoints": {
            "groupsUrl": group_result.get("url"),
            "subjectsSource": subjects_source,
            "scannedStorageKeys": [],
            "knownSubjectUrls": [],
            "subjectAttempts": subject_attempts,
        },
        "timing": {
            "groupsMs": groups_ms,
            "subjectsMs": subjects_ms,
            "totalMs": round((time.perf_counter() - t0) * 1000),
        },
        "summary": {
            "groups": len(groups),
            "students": len(group.get("students", [])) if group else 0,
            "teachers": len(group.get("teachers", [])) if group else 0,
            "tutors": len(group.get("tutors", [])) if group else 0,
            "excluded": len(group.get("excluded", [])) if group else 0,
            "subjects": len(subjects),
            "messageSubjectId": message_subject.get("id") if message_subject else None,
        },
        "source": {
            "browser": "firefox-current",
            "sessionCaptured": True,
            "sessionCookieNames": session.get("cookieNames", []),
            "selectionAuthority": "EntretienConnect" if selected_group_id is not None else "automatic-only-for-single-group",
            "groupChosenAutomatically": chosen_automatically,
        },
    }
    global LATEST_SESSION, LATEST_SESSION_AT
    with LOCK:
        LATEST_SESSION = session
        LATEST_SESSION_AT = session.get("capturedAt")
    return payload

def check_login_ready() -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-current":
        return firefox_check_login_ready()
    """Sehr leichte Bereitschaftsprüfung für den Login-Polling-Loop.

    v297: weiterhin nur ein DevTools-Listenaufruf pro Poll. Die Freigabe erfolgt aber
    nicht mehr allein anhand alter sessionStorage-Werte: e-Bichelchen darf seine Route
    erst stabilisieren und der echte Klassen-Endpunkt muss antworten. Das verhindert
    eine zu frühe Lesung während e-Bichelchens internem Startvorgang.
    """
    try:
        targets = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json", timeout=0.8)
    except Exception as exc:
        return {"ok": True, "ready": False, "browserClosed": True, "stage": "closed", "detail": str(exc), "lightweight": True}

    pages = [t for t in (targets if isinstance(targets, list) else []) if t.get("type") == "page"]
    real_pages = [t for t in pages if str(t.get("url") or "").startswith(("http://", "https://"))]
    if not real_pages:
        return {"ok": True, "ready": False, "browserClosed": True, "stage": "no-windows", "lightweight": True}
    candidates = [t for t in pages if "/ebichelchen/app/" in str(t.get("url") or "")]
    if not candidates:
        return {"ok": True, "ready": False, "browserClosed": False, "stage": "login", "lightweight": True}

    target = candidates[0]
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        return {"ok": True, "ready": False, "browserClosed": False, "stage": "target", "lightweight": True}

    expr = r"""
(async () => {
  const out = { ready:false, pageUrl:String(location.href || ""), groupCount:0, via:"" };
  if (!out.pageUrl.includes('/ebichelchen/app/')) return JSON.stringify(out);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1200);
  try {
    const res = await fetch('/ebichelchen/app/api/group/get-groups-from-teacher', {
      method:'GET', credentials:'include', signal:controller.signal,
      headers:{'accept':'application/json, text/plain, */*','mobileappversion':'web'}
    });
    out.status = res.status;
    if (!res.ok) return JSON.stringify(out);
    const json = await res.json();
    const candidates = [json, json && json.objects, json && json.groups, json && json.data,
      json && json.data && json.data.objects, json && json.result,
      json && json.result && json.result.objects];
    const groups = candidates.find(Array.isArray) || [];
    out.groupCount = groups.length;
    out.ready = groups.length > 0;
    out.via = 'api';
    return JSON.stringify(out);
  } catch (e) {
    out.error = String(e && (e.message || e) || '');
    return JSON.stringify(out);
  } finally {
    clearTimeout(timer);
  }
})()
"""
    try:
        msg = cdp_eval(ws_url, expr, await_promise=True, msg_id=609, timeout_ms=1900)
        result = msg.get("result", {})
        if result.get("exceptionDetails"):
            return {"ok": True, "ready": False, "browserClosed": False, "stage": "page", "lightweight": True}
        value = result.get("result", {}).get("value")
        data = json.loads(value) if value else {}
        return {
            "ok": True,
            "ready": bool(data.get("ready")),
            "browserClosed": False,
            "stage": "ready" if data.get("ready") else "app-loading",
            "groupCount": int(data.get("groupCount") or 0),
            "status": data.get("status"),
            "via": data.get("via") or "",
            "lightweight": True,
        }
    except Exception as exc:
        return {"ok": True, "ready": False, "browserClosed": False, "stage": "probe", "detail": str(exc), "lightweight": True}


def _is_transient_context_error(text: str) -> bool:
    """Erkennt CDP-Fehler, die bei einer natürlichen Angular-/Ionic-Navigation entstehen.

    e-Bichelchen kann direkt nach dem Login selbst noch einmal von Kalender zu Pinnwand
    (oder umgekehrt) wechseln. Der alte Code gab dann sofort den technischen Fehler
    ``Execution context was destroyed`` an den Nutzer weiter. v299 liest nach dem
    Seitenwechsel automatisch erneut — ohne selbst eine Navigation auszulösen.
    """
    t = str(text or "").lower()
    markers = (
        "execution context was destroyed",
        "cannot find context with specified id",
        "inspected target navigated or closed",
        "target closed",
        "websocket wurde geschlossen",
        "websocket was closed",
        "no frame with given id",
    )
    return any(m in t for m in markers)


def read_from_chrome(selected_group_id: int | None = None) -> dict:
    expr = build_read_expression(selected_group_id)
    last_error: Exception | None = None
    target: dict | None = None
    payload: dict | None = None
    retries_used = 0

    # v299: Ein von e-Bichelchen selbst ausgelöster Route-Wechsel darf die Auswahl in
    # EntretienConnect nicht mehr abbrechen. Bei genau diesen transienten CDP-Fehlern
    # wird das aktuelle Target neu gesucht und die reine API-Lesung wiederholt.
    for attempt in range(3):
        try:
            target = find_ebichelchen_target()
            msg = cdp_eval(target["webSocketDebuggerUrl"], expr, await_promise=True, timeout_ms=18000)
            if "error" in msg:
                err_text = "CDP-Fehler: " + json.dumps(msg["error"], ensure_ascii=False)
                if _is_transient_context_error(err_text) and attempt < 2:
                    retries_used += 1
                    time.sleep(0.35 + 0.25 * attempt)
                    continue
                raise RuntimeError(err_text)
            result = msg.get("result", {})
            if result.get("exceptionDetails"):
                details = result["exceptionDetails"]
                text = details.get("text") or "JavaScript-Fehler"
                exc = details.get("exception", {}).get("description") or details.get("exception", {}).get("value") or ""
                err_text = f"Fehler im e-Bichelchen-Tab: {text} {exc}"
                if _is_transient_context_error(err_text) and attempt < 2:
                    retries_used += 1
                    time.sleep(0.35 + 0.25 * attempt)
                    continue
                raise RuntimeError(err_text)
            remote = result.get("result", {})
            value = remote.get("value")
            if not value:
                err_text = "Keine Daten aus dem e-Bichelchen-Tab erhalten. Ist e-Bichelchen nach dem Login sichtbar?"
                if attempt < 2:
                    retries_used += 1
                    time.sleep(0.35 + 0.25 * attempt)
                    continue
                raise RuntimeError(err_text)
            try:
                payload = json.loads(value)
            except Exception as exc:
                raise RuntimeError(f"Antwort konnte nicht gelesen werden: {exc}; Wert: {str(value)[:300]}")
            break
        except Exception as exc:
            last_error = exc
            if _is_transient_context_error(str(exc)) and attempt < 2:
                retries_used += 1
                time.sleep(0.35 + 0.25 * attempt)
                continue
            raise

    if payload is None or target is None:
        raise last_error or RuntimeError("e-Bichelchen-Daten konnten nicht gelesen werden.")

    payload.setdefault("source", {})
    payload["source"].update({
        "targetTitle": target.get("title"),
        "targetUrl": target.get("url"),
        "contextRetries": retries_used,
        "selectionAuthority": "EntretienConnect" if selected_group_id is not None else "automatic-only-for-single-group",
    })
    # Session-Cookies zwischenspeichern, damit spätere Schreib-/Löschtests unsichtbar direkt
    # vom lokalen Helfer ausgeführt werden können, ohne e-Bichelchen erneut sichtbar zu öffnen.
    try:
        session = capture_browser_session(target)
        global LATEST_SESSION, LATEST_SESSION_AT
        with LOCK:
            LATEST_SESSION = session
            LATEST_SESSION_AT = session.get("capturedAt")
        payload["source"]["sessionCaptured"] = True
        payload["source"]["sessionCookieNames"] = session.get("cookieNames", [])
    except Exception as exc:
        payload["source"]["sessionCaptured"] = False
        payload["source"]["sessionError"] = str(exc)
    return payload


def focus_app_tab() -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-current":
        return {"method": "page-window-reference", "foundExistingTab": True, "openedNewTab": False, "handledByFrontend": True}
    """Bringt den bereits geöffneten App-Tab / das App-Fenster nach vorne, ohne eine neue App-URL zu öffnen.

    v297: Zuerst rein über DevTools nach dem lokalen EntretienConnect-Tab suchen. Das
    funktioniert ohne AppleScript-Abfrage und ist besonders wichtig, wenn App und
    e-Bichelchen als Tabs im selben Chrome-/Edge-Fenster laufen.
    Windows behält zusätzlich den bisherigen Fenster-API-Fallback.
    """
    app_base = f"http://127.0.0.1:{PORT}"
    system = platform.system().lower()

    try:
        app_targets = [t for t in _list_cdp_targets() if _is_app_target(t) and t.get("webSocketDebuggerUrl")]
        app_targets.sort(key=lambda t: ("/graph.html" not in str(t.get("url") or ""), str(t.get("url") or "")))
        if app_targets:
            target = app_targets[0]
            cdp_call(target.get("webSocketDebuggerUrl"), "Page.bringToFront", {}, msg_id=913)
            return {"method": "cdp", "foundExistingTab": True, "targetId": target.get("id"), "url": target.get("url"), "openedNewTab": False}
    except Exception:
        pass

    if system == "darwin":
        # Ne pas utiliser AppleScript ici: cela déclenche la demande macOS
        # « contrôler Google Chrome ». L'app reste utilisable; on ne force
        # simplement plus le retour automatique au tab EntretienConnect.
        return {"method": "none", "foundExistingTab": False, "message": "AppleScript deaktiviert, um macOS-Automationsabfrage zu vermeiden.", "openedNewTab": False}

    if system == "windows":
        # Ohne Zusatzpakete (kein pywin32): Windows-Fenster mit ctypes suchen.
        # Das funktioniert, wenn der App-Tab im normalen Browserfenster aktiv ist. Genau das ist beim Standardablauf der Fall.
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            titles = []
            found = {"hwnd": None, "title": ""}

            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowTextW.restype = ctypes.c_int

            def callback(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value or ""
                if title:
                    titles.append(title)
                # Chrome/Edge/Firefox-Fenstertitel enthalten normalerweise den Titel des aktiven Tabs.
                if "EntretienConnect" in title and "e-Bichelchen" not in title:
                    found["hwnd"] = hwnd
                    found["title"] = title
                    return False
                return True

            user32.EnumWindows(EnumWindowsProc(callback), 0)
            hwnd = found["hwnd"]
            if hwnd:
                SW_RESTORE = 9
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.BringWindowToTop(hwnd)
                ok = bool(user32.SetForegroundWindow(hwnd))
                return {"method": "windows-user32", "foundExistingTab": True, "setForegroundOk": ok, "title": found["title"], "openedNewTab": False}
            return {"method": "windows-user32", "foundExistingTab": False, "message": "Kein vorhandenes EntretienConnect-Fenster gefunden.", "sampleTitles": titles[:8], "openedNewTab": False}
        except Exception as exc:
            raise RuntimeError("Windows-Fokus fehlgeschlagen: " + str(exc))

    # Linux/Fallback: nicht automatisch öffnen, um Duplikate zu vermeiden.
    return {"method": "none", "foundExistingTab": False, "message": "Automatisches Fokussieren ist für dieses Betriebssystem noch nicht umgesetzt.", "openedNewTab": False}

def _list_cdp_targets() -> list:
    try:
        targets = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2)
        return targets if isinstance(targets, list) else []
    except Exception:
        return []


def _is_app_target(t: dict) -> bool:
    """App-Tab der EntretienConnect-Oberfläche (läuft auf 127.0.0.1/localhost).
    v1.10.17: LaunchServices kann die App-URL in den Debug-Browser geöffnet haben —
    dieser Tab darf beim Aufräumen niemals mit geschlossen werden."""
    url = str((t or {}).get("url") or "")
    return (t or {}).get("type") == "page" and ("//127.0.0.1" in url or "//localhost" in url)


def _cdp_close_tab(target_id: str) -> bool:
    close_url = f"http://127.0.0.1:{CDP_PORT}/json/close/{urllib.parse.quote(str(target_id), safe='')}"
    try:
        req = urllib.request.Request(close_url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
        return True
    except Exception:
        return False


def close_ebichelchen_target() -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-current":
        return {"closed": False, "method": "page-window-reference", "handledByFrontend": True, "sameBrowser": True}
    """Schließt nur den e-Bichelchen-Tab, niemals den vorgewärmten Browserprozess.

    v302: Der kontrollierte Browser bleibt durch den EntretienConnect-App-Tab geöffnet
    bereit. Dadurch ist die nächste Verbindung ebenso schnell wie das Öffnen eines
    neuen Tabs in einem bereits laufenden Chrome. Beim echten Beenden der App räumt
    ``force_close_launched_browser`` den Prozess weiterhin vollständig auf.
    """
    targets = [t for t in _list_cdp_targets() if t.get("type") == "page" and "/ebichelchen/app/" in str(t.get("url") or "")]
    if not targets:
        raise RuntimeError("Kein e-Bichelchen-Target zum Schließen gefunden.")
    closed = 0
    details = []
    for target in targets:
        target_id = target.get("id")
        url = target.get("url") or ""
        title = target.get("title") or ""
        ws_url = target.get("webSocketDebuggerUrl")
        ok = False
        if target_id:
            ok = _cdp_close_tab(target_id)
        if not ok and ws_url:
            try:
                cdp_call(ws_url, "Page.close", {}, msg_id=700)
                ok = True
            except Exception:
                ok = False
        if ok:
            closed += 1
        details.append({"targetId": target_id, "url": url, "title": title, "closed": ok})
    if not closed:
        raise RuntimeError("Der e-Bichelchen-Tab konnte nicht geschlossen werden.")
    return {"closed": True, "method": "tabs-only", "closedTabs": closed, "browserKeptWarm": True, "targets": details}

def force_close_launched_browser(force: bool = False) -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-current":
        return {"closed": False, "method": "none", "normalBrowserProtected": True}
    """v155: Schließt den vom Helfer gestarteten Browser komplett – egal welche Seite
    gerade offen ist (auch IAM-/EduKey-Login). Betrifft ausschließlich den Browser am
    lokalen CDP-Port mit App-Profil; der normale Browser des Benutzers bleibt unberührt."""
    result = {"closed": False, "method": None}
    targets = _list_cdp_targets()
    if any(_is_app_target(t) for t in targets) and not force:
        # Während eines Session-Resets bleibt der App-Tab geschützt. Beim echten
        # Programmende darf die dedizierte v302-Browserinstanz dagegen komplett schließen.
        closed_tabs = 0
        for t in targets:
            if t.get("type") == "page" and not _is_app_target(t) and t.get("id"):
                if _cdp_close_tab(t.get("id")):
                    closed_tabs += 1
        BROWSER_PROCESSES.clear()
        return {"closed": closed_tabs > 0, "method": "tabs-only", "appTabProtected": True, "closedTabs": closed_tabs}
    try:
        for t in targets:
            ws = t.get("webSocketDebuggerUrl")
            if not ws:
                continue
            try:
                cdp_call(ws, "Browser.close", {}, msg_id=702)
                result = {"closed": True, "method": "Browser.close"}
                break
            except Exception:
                continue
    except Exception:
        pass
    for key, proc in list(BROWSER_PROCESSES.items()):
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except Exception:
                    proc.kill()
                result = {"closed": True, "method": result.get("method") or "terminate"}
        except Exception:
            pass
        BROWSER_PROCESSES.pop(key, None)
    for _ in range(20):
        try:
            read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
            time.sleep(0.25)
        except Exception:
            break
    return result


def _clear_saved_login_data() -> list[str]:
    """Entfernt gezielt nur Login-/Sitzungsdaten aus den App-Browserprofilen.

    v1.10.21: Frühere Versionen liefen mit ``PROFILE_ROOT.rglob("*")`` durch den
    gesamten Chromium-Cache. Nach mehreren Starts konnte allein dieser Scan mehrere
    Sekunden dauern, obwohl nur wenige feste Cookie-/Session-Pfade relevant sind.
    Jetzt werden ausschließlich die bekannten Profilordner und Dateien geprüft.
    Cache, Browserinitialisierung und ``Web Data`` bleiben erhalten.
    """
    removed: list[str] = []
    if not PROFILE_ROOT.exists():
        return removed

    # --user-data-dir legt die eigentlichen Chromium-Profile normalerweise in
    # "Default" (oder "Profile X") an. Einige ältere Builds schrieben einzelne
    # Datenbanken direkt in den user-data-dir; deshalb wird auch dieser geprüft.
    profile_roots: list[pathlib.Path] = []
    try:
        for browser_dir in PROFILE_ROOT.iterdir():
            if not browser_dir.is_dir():
                continue
            for user_data_dir in browser_dir.iterdir():
                if not user_data_dir.is_dir():
                    continue
                profile_roots.append(user_data_dir)
                default_dir = user_data_dir / "Default"
                if default_dir.is_dir():
                    profile_roots.append(default_dir)
                try:
                    profile_roots.extend(
                        d for d in user_data_dir.glob("Profile *") if d.is_dir()
                    )
                except Exception:
                    pass
    except Exception:
        return removed

    file_relpaths = (
        pathlib.Path("Cookies"),
        pathlib.Path("Cookies-journal"),
        pathlib.Path("Network") / "Cookies",
        pathlib.Path("Network") / "Cookies-journal",
    )
    dir_relpaths = (
        pathlib.Path("Sessions"),
        pathlib.Path("Session Storage"),
    )

    seen: set[str] = set()
    for base in profile_roots:
        for rel in dir_relpaths:
            path = base / rel
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed.append(str(path.relative_to(PROFILE_ROOT)))
            except Exception:
                pass
        for rel in file_relpaths:
            path = base / rel
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
                    removed.append(str(path.relative_to(PROFILE_ROOT)))
            except Exception:
                pass
    return removed


def soft_reset_login() -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-current":
        try:
            clear_current()
        except Exception:
            pass
        return {"softReset": True, "browserRunning": True, "cookiesCleared": False, "navigated": False, "normalBrowserProtected": True}
    """v292: Verwirft eine halbfertige/abgebrochene IAM-Sitzung, OHNE den Browser zu
    schließen. Die Cookies werden per DevTools gelöscht (browserweit) und der
    e-Bichelchen-Tab frisch geladen, sodass ein sauberer Login startet. Der Browser
    bleibt "warm": Der nächste Connect verwendet ihn weiter, statt einen langsamen
    neuen Browser zu starten. Läuft kein Debug-Browser, passiert nichts (open-browser
    macht dann ohnehin einen Kaltstart)."""
    if not debug_browser_running():
        try:
            clear_current()
        except Exception:
            pass
        return {"softReset": True, "browserRunning": False, "cookiesCleared": False, "navigated": False}
    cleared = False
    for t in _list_cdp_targets():
        ws = t.get("webSocketDebuggerUrl")
        if not ws:
            continue
        try:
            cdp_call(ws, "Network.clearBrowserCookies", {}, msg_id=720)
            cleared = True
            break
        except Exception:
            continue
    navigated = False
    try:
        target = find_ebichelchen_target()
        ws = target.get("webSocketDebuggerUrl")
        if ws:
            cdp_call(ws, "Page.navigate", {"url": EB_URL}, msg_id=721)
            navigated = True
    except Exception:
        pass
    try:
        clear_current()
    except Exception:
        pass
    return {"softReset": True, "browserRunning": True, "cookiesCleared": cleared, "navigated": navigated}


def reset_login_session(profile: str = "default", preserve_profile: bool = False) -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-current":
        try:
            clear_current()
        except Exception:
            pass
        return {"closed": False, "profilesRemoved": [], "sessionDataRemoved": [], "cookiesCleared": False, "profilePreserved": True, "normalBrowserProtected": True}
    """v155: Kompletter Neustart der e-Bichelchen-Anmeldung. Schließt den App-Browser
    und löscht die App-Browserprofile (Cookies / halbfertige IAM-Sitzung). Ohne diesen
    Reset blockiert eine abgebrochene Anmeldung (falsches Passwort, Fenster zu früh
    geschlossen) die nächste EduKey-Sicherheitsanfrage."""
    closed = force_close_launched_browser()
    removed = []
    cookies_cleared = False
    if closed.get("appTabProtected"):
        # v1.10.17: Browser läuft weiter (App-Tab drin) — das Profil eines laufenden
        # Browsers nicht löschen. Stattdessen die Cookies (IAM-/EduKey-Sitzung) über
        # DevTools leeren; das hat denselben Effekt für einen sauberen Neu-Login.
        for t in _list_cdp_targets():
            ws = t.get("webSocketDebuggerUrl")
            if not ws:
                continue
            try:
                cdp_call(ws, "Network.clearBrowserCookies", {}, msg_id=703)
                cookies_cleared = True
                break
            except Exception:
                continue
    elif preserve_profile:
        # v285: schneller Reset für den normalen Verbindungsfluss. Nur Login- und
        # Sitzungsdaten entfernen, das bereits initialisierte Browserprofil behalten.
        removed = _clear_saved_login_data()
    else:
        prof_root = PROFILE_ROOT
        try:
            if prof_root.exists():
                for sub in prof_root.iterdir():
                    try:
                        shutil.rmtree(sub, ignore_errors=True)
                        removed.append(sub.name)
                    except Exception:
                        pass
        except Exception:
            pass
    try:
        clear_current()
    except Exception:
        pass
    return {"closed": closed, "profilesRemoved": removed if not preserve_profile else [], "sessionDataRemoved": removed if preserve_profile else [], "cookiesCleared": cookies_cleared, "profilePreserved": bool(preserve_profile)}


def _cdp_set_window_state(target: dict, state: str) -> bool:
    """v291: Fenster eines CDP-Targets minimieren/normalisieren – rein über DevTools,
    ohne AppleScript (Mac) oder Fenster-API (Windows). state: "minimized" | "normal"."""
    ws = (target or {}).get("webSocketDebuggerUrl")
    tid = (target or {}).get("id")
    if not ws or not tid:
        return False
    try:
        win = cdp_call(ws, "Browser.getWindowForTarget", {"targetId": tid}, msg_id=915)
        window_id = (((win or {}).get("result") or {}).get("windowId"))
        if window_id is None:
            return False
        cdp_call(ws, "Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": state}}, msg_id=916)
        return True
    except Exception:
        return False


def park_ebichelchen_browser() -> dict:
    """v297 Kompatibilitäts-Alias für alte, eventuell noch gecachte v296-Oberflächen.

    Die frühere Park-Funktion minimierte das gesamte Browserfenster und damit unter
    Umständen auch EntretienConnect. Sie minimiert deshalb nie wieder. Stattdessen wird
    nur der e-Bichelchen-Tab sauber geschlossen; ein vorhandener App-Tab hält den
    Debug-Browser weiterhin warm.
    """
    try:
        closed = close_ebichelchen_target()
        return {"parked": False, "minimized": False, "closedInstead": True, "closedEbichelchen": closed, "keptOpenForPublishing": True}
    except Exception as exc:
        return {"parked": False, "minimized": False, "closedInstead": False, "keptOpenForPublishing": True, "reason": str(exc)}


def park_after_read(focus_app: bool = True) -> dict:
    result = {"parkedEbichelchen": park_ebichelchen_browser(), "focusedApp": None}
    if focus_app:
        try:
            result["focusedApp"] = focus_app_tab()
        except Exception as exc:
            result["focusedApp"] = {"ok": False, "error": str(exc)}
    return result


def cleanup_after_read(close_eb: bool = True, focus_app: bool = True) -> dict:
    result = {"closedEbichelchen": None, "focusedApp": None, "prewarmScheduled": False, "sharedBrowser": True}
    if close_eb:
        try:
            result["closedEbichelchen"] = close_ebichelchen_target()
        except Exception as exc:
            result["closedEbichelchen"] = {"closed": False, "error": str(exc)}
    if focus_app:
        try:
            result["focusedApp"] = focus_app_tab()
        except Exception as exc:
            result["focusedApp"] = {"ok": False, "error": str(exc)}
    # v302: Der App-Tab selbst hält den kontrollierten Browser bereits offen.
    # Ein separater Prewarm-/Hintergrundprozess ist deshalb weder nötig noch erwünscht.
    return result



def capture_browser_session(target: dict) -> dict:
    """Speichert die e-Bichelchen-Cookies aus dem eingeloggten Browser.
    Danach können save-entry/delete-entry direkt vom lokalen Helfer ausgeführt werden,
    ohne das e-Bichelchen-Fenster sichtbar erneut öffnen zu müssen.
    """
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Keine DevTools-Verbindung zum Speichern der Sitzung gefunden.")
    cookies_msg = cdp_call(ws_url, "Network.getAllCookies", {}, msg_id=880)
    cookies = (((cookies_msg or {}).get("result") or {}).get("cookies") or [])
    eb_cookies = []
    for c in cookies:
        domain = str(c.get("domain") or "")
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if not name:
            continue
        # Wichtig sind die Cookies für ssl.education.lu / education.lu.
        if "education.lu" in domain or domain in ("", "ssl.education.lu"):
            eb_cookies.append((name, value, domain))
    if not eb_cookies:
        raise RuntimeError("Keine e-Bichelchen-Cookies im Browser gefunden. Bitte neu verbinden.")
    # Doppelte Cookie-Namen vermeiden; letztes Vorkommen gewinnt.
    m = {}
    for name, value, domain in eb_cookies:
        m[name] = value
    cookie_header = "; ".join(f"{k}={v}" for k, v in m.items())
    try:
        ver = cdp_call(ws_url, "Browser.getVersion", {}, msg_id=881)
        ua = (((ver or {}).get("result") or {}).get("userAgent") or "Mozilla/5.0")
    except Exception:
        ua = "Mozilla/5.0"
    return {
        "cookieHeader": cookie_header,
        "cookieNames": sorted(m.keys()),
        "userAgent": ua,
        "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "targetUrl": target.get("url") or "",
    }


def get_saved_session() -> dict:
    with LOCK:
        session = dict(LATEST_SESSION or {})
    if not session.get("cookieHeader"):
        raise RuntimeError("Keine gespeicherte e-Bichelchen-Sitzung gefunden. Bitte einmal neu verbinden/einlesen.")
    return session


def encode_multipart_form(fields: dict) -> tuple[bytes, str]:
    boundary = "----ebichelchenhelper" + secrets.token_hex(12)
    chunks: list[bytes] = []
    for key, val in fields.items():
        if isinstance(val, bool):
            text = "true" if val else "false"
        elif isinstance(val, (list, dict)):
            text = json.dumps(val, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(val)
        chunks.append((f"--{boundary}\r\n").encode("utf-8"))
        chunks.append((f'Content-Disposition: form-data; name="{key}"\r\n\r\n').encode("utf-8"))
        chunks.append(text.encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append((f"--{boundary}--\r\n").encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def direct_ebichelchen_request(method: str, path: str, fields: dict | None = None) -> dict:
    session = get_saved_session()
    url = "https://ssl.education.lu" + path
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://ssl.education.lu",
        "Referer": "https://ssl.education.lu/ebichelchen/app/",
        "Cookie": session["cookieHeader"],
        "User-Agent": session.get("userAgent") or "Mozilla/5.0",
        "mobileappversion": "web",
    }
    data = None
    if fields is not None:
        data, content_type = encode_multipart_form(fields)
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as resp:
            status = resp.getcode()
            status_text = getattr(resp, "reason", "") or ""
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        status_text = getattr(exc, "reason", "") or ""
        text = exc.read().decode("utf-8", "replace")
    try:
        body = json.loads(text) if text else None
    except Exception:
        body = text[:1000]
    return {"ok": 200 <= int(status) < 300, "status": status, "statusText": status_text, "body": body, "direct": True}


def direct_save_entry(payload: dict) -> dict:
    fields = {
        "groupId": payload["groupId"],
        "startDate": payload["startDate"],
        "title": payload["title"],
        "subjectId": payload["subjectId"],
        "type": payload.get("type", 0),
        "description": payload["description"],
        "scope": payload["scope"],
        "recurrenceEndDate": payload.get("recurrenceEndDate") or payload["startDate"],
        "selectedRecurrenceDays": payload.get("selectedRecurrenceDays") or [],
        "skipHolidays": payload.get("skipHolidays", True),
        "weekInterval": payload.get("weekInterval", 1),
    }
    return direct_ebichelchen_request("POST", "/ebichelchen/app/api/v6/save-entry", fields)


def direct_delete_entry(entry_id: int) -> dict:
    path = f"/ebichelchen/app/api/delete-entry?id={int(entry_id)}&deleteFollowingRecurrence=false"
    return direct_ebichelchen_request("DELETE", path, None)

def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("JSON-Anfrage konnte nicht gelesen werden: " + str(exc))


def ensure_ebichelchen_target(profile: str = "default", preferred_browser: str = "auto", timeout_s: float = 24.0) -> dict:
    """Stellt sicher, dass ein e-Bichelchen-Tab mit DevTools existiert.
    Wird für den echten Test-Eintrag genutzt, nachdem das e-Bichelchen-Fenster nach dem Lesen evtl. geschlossen wurde.
    """
    try:
        return find_ebichelchen_target()
    except Exception:
        pass
    launch_browser(profile or "default", preferred_browser=preferred_browser or "auto")
    deadline = time.time() + timeout_s
    last_error = None
    while time.time() < deadline:
        try:
            return find_ebichelchen_target()
        except Exception as exc:
            last_error = exc
            time.sleep(0.4)
    raise RuntimeError("e-Bichelchen-Tab konnte nicht für den Test-Eintrag geöffnet werden: " + str(last_error))


def _as_int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _entry_object_from_response(resp) -> dict | None:
    """Sucht in einer save-entry-Antwort den tatsächlich gespeicherten Entry.

    Wichtig für v1.10.16: Wir wollen nicht nur die entryId, sondern auch `isIndividual`
    und `scope` serverseitig prüfen. Ohne diese Prüfung ist Mehrfachversand zu riskant.
    """
    def looks_like_entry(d: dict) -> bool:
        return isinstance(d, dict) and _as_int_or_none(d.get("id")) is not None and (
            "isIndividual" in d or "scope" in d or "groupId" in d or "subjectId" in d
        )

    def scan(value, depth=0):
        if value is None or depth > 8:
            return None
        if isinstance(value, dict):
            for key in ("objects", "object", "entry", "savedEntry"):
                if key in value:
                    found = scan(value.get(key), depth + 1)
                    if found is not None:
                        return found
            if looks_like_entry(value):
                return value
            for child in value.values():
                found = scan(child, depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = scan(item, depth + 1)
                if found is not None:
                    return found
        return None

    return scan(resp)


def _entry_id_from_response(resp) -> int | None:
    obj = _entry_object_from_response(resp)
    if obj:
        return _as_int_or_none(obj.get("id"))

    # Sehr vorsichtiger Fallback für alte/unerwartete Antwortformen.
    def scan(value, depth=0):
        if value is None or depth > 8:
            return None
        if isinstance(value, dict):
            for key in ("objects", "object", "entry", "savedEntry"):
                if key in value:
                    found = scan(value.get(key), depth + 1)
                    if found is not None:
                        return found
            for key in ("entryId", "id"):
                found = _as_int_or_none(value.get(key))
                if found is not None:
                    return found
            for child in value.values():
                found = scan(child, depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = scan(item, depth + 1)
                if found is not None:
                    return found
        return None
    return scan(resp)


def _normalize_scope(value) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [int(x) for x in value if _as_int_or_none(x) is not None]
    if isinstance(value, tuple):
        return [int(x) for x in value if _as_int_or_none(x) is not None]
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return []
        try:
            parsed = json.loads(v)
            return _normalize_scope(parsed)
        except Exception:
            i = _as_int_or_none(v)
            return [i] if i is not None else []
    i = _as_int_or_none(value)
    return [i] if i is not None else []


def verify_saved_entry_response(resp_body, payload: dict) -> dict:
    """Prüft die e-Bichelchen-Antwort nach dem Speichern hart.

    Ein Eintrag gilt nur als erfolgreich, wenn der Server selbst zurückmeldet:
    - isIndividual == true
    - scope enthält genau die erwartete Schüler-ID
    - groupId und subjectId passen weiterhin
    """
    obj = _entry_object_from_response(resp_body)
    if not obj:
        raise RuntimeError("Serverantwort enthält keine prüfbaren Eintragsdetails. Aus Sicherheitsgründen wird nicht weiter erstellt.")

    entry_id = _as_int_or_none(obj.get("id"))
    if entry_id is None:
        raise RuntimeError("Serverantwort enthält keine prüfbare entryId.")

    expected_scope = int(payload.get("scope"))
    returned_scope = _normalize_scope(obj.get("scope"))
    is_individual = obj.get("isIndividual")
    returned_group = _as_int_or_none(obj.get("groupId"))
    returned_subject = _as_int_or_none(obj.get("subjectId"))

    problems = []
    if is_individual is not True:
        problems.append(f"isIndividual ist {is_individual!r}, nicht true")
    if returned_scope != [expected_scope]:
        problems.append(f"scope ist {returned_scope}, erwartet [{expected_scope}]")
    if returned_group is not None and returned_group != int(payload.get("groupId")):
        problems.append(f"groupId ist {returned_group}, erwartet {payload.get('groupId')}")
    if returned_subject is not None and returned_subject != int(payload.get("subjectId")):
        problems.append(f"subjectId ist {returned_subject}, erwartet {payload.get('subjectId')}")

    if problems:
        raise RuntimeError(f"Eintrag {entry_id} wurde vom Server nicht als individueller Eintrag bestätigt: " + "; ".join(problems))

    return {
        "entryId": entry_id,
        "isIndividual": True,
        "scope": returned_scope,
        "groupId": returned_group,
        "subjectId": returned_subject,
        "serverEntry": obj,
    }


def validate_single_entry_payload(payload: dict) -> dict:
    """Harter Sicherheitscheck: in v1.10.16 darf nur ein individueller Message-Testeintrag erstellt werden."""
    if not isinstance(payload, dict):
        raise RuntimeError("Payload fehlt oder ist ungültig.")
    with LOCK:
        data = LATEST_DATA
    if not data or not isinstance(data, dict):
        raise RuntimeError("Es wurden noch keine e-Bichelchen-Daten eingelesen. Erst verbinden und Daten lesen.")
    group = data.get("group") or {}
    message_subject = data.get("messageSubject") or {}
    students = data.get("students") or []
    allowed_student_ids = {int(s.get("id")) for s in students if isinstance(s, dict) and s.get("id") is not None}

    def as_int(name):
        try:
            return int(payload.get(name))
        except Exception:
            raise RuntimeError(f"{name} fehlt oder ist keine gültige Zahl.")

    group_id = as_int("groupId")
    subject_id = as_int("subjectId")
    scope = as_int("scope")
    expected_group_id = int(group.get("id")) if group.get("id") is not None else None
    expected_subject_id = int(message_subject.get("id")) if message_subject.get("id") is not None else None

    if expected_group_id is None or group_id != expected_group_id:
        raise RuntimeError(f"Sicherheitsstopp: groupId {group_id} passt nicht zur eingelesenen Klasse {expected_group_id}.")
    if expected_subject_id is None or subject_id != expected_subject_id:
        raise RuntimeError(f"Sicherheitsstopp: subjectId {subject_id} ist nicht die dynamisch erkannte Nachricht/Message-ID {expected_subject_id}.")
    if scope not in allowed_student_ids:
        raise RuntimeError(f"Sicherheitsstopp: scope {scope} gehört nicht zu den eingelesenen Schülern.")

    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    start_date = str(payload.get("startDate") or "").strip()
    if not title:
        raise RuntimeError("Titel fehlt.")
    if not description:
        raise RuntimeError("Beschreibung/Nachricht fehlt.")
    if not start_date or not start_date[:10].count("-") == 2:
        raise RuntimeError("startDate fehlt oder hat ein unerwartetes Format.")

    safe = {
        "groupId": group_id,
        "startDate": start_date,
        "title": title[:500],
        "subjectId": subject_id,
        "type": 0,
        "description": description[:5000],
        "scope": scope,
        "recurrenceEndDate": str(payload.get("recurrenceEndDate") or start_date),
        "selectedRecurrenceDays": [],
        "skipHolidays": True,
        "weekInterval": 1,
    }
    return safe


def build_save_entry_expression(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"""
(async () => {{
  const payload = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(\"{b64}\"), c => c.charCodeAt(0))));
  if (!location.href.includes('/ebichelchen/app/')) throw new Error('Nicht im e-Bichelchen-Kontext.');
  const fd = new FormData();
  fd.append('groupId', String(payload.groupId));
  fd.append('startDate', String(payload.startDate));
  fd.append('title', String(payload.title));
  fd.append('subjectId', String(payload.subjectId));
  fd.append('type', String(payload.type ?? 0));
  fd.append('description', String(payload.description));
  fd.append('scope', String(payload.scope));
  fd.append('recurrenceEndDate', String(payload.recurrenceEndDate || payload.startDate));
  fd.append('selectedRecurrenceDays', JSON.stringify(payload.selectedRecurrenceDays || []));
  fd.append('skipHolidays', String(payload.skipHolidays ?? true));
  fd.append('weekInterval', String(payload.weekInterval ?? 1));
  const res = await fetch('/ebichelchen/app/api/v6/save-entry', {{
    method: 'POST',
    credentials: 'include',
    headers: {{ 'accept': 'application/json, text/plain, */*', 'mobileappversion': 'web' }},
    body: fd
  }});
  const text = await res.text();
  let body = null;
  try {{ body = JSON.parse(text); }} catch (_) {{ body = text.slice(0, 1000); }}
  return JSON.stringify({{ ok: res.ok, status: res.status, statusText: res.statusText, body }});
}})()
"""


def build_delete_entry_expression(entry_id: int) -> str:
    entry_id = int(entry_id)
    return f"""
(async () => {{
  if (!location.href.includes('/ebichelchen/app/')) throw new Error('Nicht im e-Bichelchen-Kontext.');
  const url = '/ebichelchen/app/api/delete-entry?id={entry_id}&deleteFollowingRecurrence=false';
  const res = await fetch(url, {{
    method: 'DELETE',
    credentials: 'include',
    headers: {{ 'accept': 'application/json, text/plain, */*', 'mobileappversion': 'web' }}
  }});
  const text = await res.text();
  let body = null;
  try {{ body = JSON.parse(text); }} catch (_) {{ body = text.slice(0, 1000); }}
  return JSON.stringify({{ ok: res.ok, status: res.status, statusText: res.statusText, body }});
}})()
"""


def eval_in_ebichelchen(expression: str, profile: str = "default", preferred_browser: str = "auto", timeout_ms: int = 22000) -> dict:
    target = ensure_ebichelchen_target(profile=profile, preferred_browser=preferred_browser)
    msg = cdp_eval(target["webSocketDebuggerUrl"], expression, await_promise=True, timeout_ms=timeout_ms)
    if "error" in msg:
        raise RuntimeError("CDP-Fehler: " + json.dumps(msg["error"], ensure_ascii=False))
    result = msg.get("result", {})
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        text = details.get("text") or "JavaScript-Fehler"
        exc = details.get("exception", {}).get("description") or details.get("exception", {}).get("value") or ""
        raise RuntimeError(f"Fehler im e-Bichelchen-Tab: {text} {exc}")
    remote = result.get("result", {})
    value = remote.get("value")
    if not value:
        raise RuntimeError("Keine Antwort vom e-Bichelchen-Tab erhalten.")
    try:
        return json.loads(value)
    except Exception as exc:
        raise RuntimeError(f"e-Bichelchen-Antwort konnte nicht gelesen werden: {exc}; Wert: {str(value)[:300]}")


def create_single_test_entry(body: dict) -> dict:
    if not body.get("confirmSingle"):
        raise RuntimeError("Sicherheitsbestätigung fehlt.")
    payload = validate_single_entry_payload(body.get("payload") or {})
    profile = sanitize_profile_name(body.get("profile") or "default")
    browser = body.get("browser") or "auto"
    # v1.10.16: direkt mit der gespeicherten Sitzung schreiben; kein sichtbares e-Bichelchen-Fenster nötig.
    resp = direct_save_entry(payload)
    if not resp.get("ok"):
        raise RuntimeError("e-Bichelchen hat den Eintrag nicht gespeichert: " + json.dumps(resp, ensure_ascii=False)[:1200])
    try:
        verification = verify_saved_entry_response(resp.get("body"), payload)
    except Exception as exc:
        maybe_id = _entry_id_from_response(resp.get("body"))
        if maybe_id is not None:
            try:
                direct_delete_entry(int(maybe_id))
            except Exception:
                pass
        raise RuntimeError(str(exc) + (f" Der verdächtige Eintrag {maybe_id} wurde vorsorglich gelöscht." if maybe_id else ""))
    entry_id = verification["entryId"]
    record = {"entryId": entry_id, "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"), "payload": payload, "response": resp.get("body"), "verification": verification}
    with LOCK:
        CREATED_TEST_ENTRIES.append(record)
    print("\n🟠 Test-Eintrag erstellt:")
    print(f"   entryId: {entry_id or 'unbekannt'}")
    print(f"   scope: {payload.get('scope')} · subjectId: {payload.get('subjectId')} · groupId: {payload.get('groupId')}")
    return record


def create_bulk_entries(body: dict) -> dict:
    """Erstellt mehrere individuell adressierte Nachricht/Message-Einträge.

    Sicherheitsprinzip: Jeder Payload wird mit denselben harten Checks validiert wie der einzelne
    Test-Eintrag. Wenn unterwegs ein Fehler auftritt, werden bereits erstellte Einträge trotzdem
    in der Antwort zurückgegeben, damit sie sofort wieder gelöscht werden können.
    """
    if not body.get("confirmBulk"):
        raise RuntimeError("Sicherheitsbestätigung für Mehrfach-Erstellung fehlt.")
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise RuntimeError("Keine vorbereiteten Nachrichten erhalten.")
    expected = body.get("expectedCount")
    try:
        expected = int(expected)
    except Exception:
        expected = len(items)
    if expected != len(items):
        raise RuntimeError(f"Sicherheitsstopp: erwartete Anzahl {expected}, erhalten {len(items)}.")
    if len(items) > 60:
        raise RuntimeError("Sicherheitsstopp: mehr als 60 Einträge auf einmal sind blockiert.")

    created: list[dict] = []
    error = None
    for idx, item in enumerate(items, start=1):
        try:
            payload = validate_single_entry_payload((item or {}).get("payload") or {})
            student = (item or {}).get("student") or {}
            resp = direct_save_entry(payload)
            if not resp.get("ok"):
                raise RuntimeError("e-Bichelchen hat nicht gespeichert: " + json.dumps(resp, ensure_ascii=False)[:900])
            try:
                verification = verify_saved_entry_response(resp.get("body"), payload)
            except Exception as exc:
                maybe_id = _entry_id_from_response(resp.get("body"))
                if maybe_id is not None:
                    try:
                        direct_delete_entry(int(maybe_id))
                    except Exception:
                        pass
                raise RuntimeError(str(exc) + (f" Der verdächtige Eintrag {maybe_id} wurde vorsorglich gelöscht." if maybe_id else ""))
            entry_id = verification["entryId"]
            record = {
                "entryId": entry_id,
                "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "index": idx,
                "student": {
                    "id": student.get("id") or payload.get("scope"),
                    "fullName": student.get("fullName") or student.get("name") or "",
                },
                "payload": payload,
                "response": resp.get("body"),
                "verification": verification,
            }
            with LOCK:
                CREATED_TEST_ENTRIES.append(record)
            created.append(record)
            print(f"   ✓ {idx}/{len(items)} entryId: {entry_id or 'unbekannt'} · scope: {payload.get('scope')} · server: individuel bestätigt")
            time.sleep(0.35)
        except Exception as exc:
            name = ((item or {}).get("student") or {}).get("fullName") or f"Eintrag {idx}"
            error = f"Abbruch bei {name}: {exc}"
            break

    print("\n🟠 Mehrfach-Erstellung:")
    print(f"   erstellt: {len(created)} von {len(items)}")
    if error:
        print("   Fehler: " + error)
        return {"ok": False, "created": created, "error": error, "requested": len(items)}
    return {"ok": True, "created": created, "requested": len(items)}


def delete_created_entries(body: dict) -> dict:
    ids = body.get("entryIds") or []
    if not isinstance(ids, list) or not ids:
        raise RuntimeError("Keine entryIds zum Löschen erhalten.")
    entry_ids = []
    for x in ids:
        try:
            n = int(x)
            if n and n not in entry_ids:
                entry_ids.append(n)
        except Exception:
            pass
    if not entry_ids:
        raise RuntimeError("Keine gültigen entryIds zum Löschen erhalten.")

    with LOCK:
        known_ids = {int(r.get("entryId") or 0) for r in CREATED_TEST_ENTRIES if r.get("entryId")}
    unknown = [n for n in entry_ids if n not in known_ids]
    if unknown:
        raise RuntimeError("Sicherheitsstopp: Diese entryIds wurden nicht in dieser App-Sitzung erstellt: " + ", ".join(map(str, unknown)))

    deleted = []
    errors = []
    for entry_id in entry_ids:
        try:
            resp = direct_delete_entry(entry_id)
            if not resp.get("ok"):
                raise RuntimeError(json.dumps(resp, ensure_ascii=False)[:900])
            rec = {"entryId": entry_id, "deletedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "response": resp.get("body")}
            deleted.append(rec)
            with LOCK:
                for r in CREATED_TEST_ENTRIES:
                    if int(r.get("entryId") or 0) == entry_id:
                        r["deletedAt"] = rec["deletedAt"]
                        r["deleteResponse"] = resp.get("body")
            print(f"   🗑️ gelöscht: {entry_id}")
        except Exception as exc:
            errors.append({"entryId": entry_id, "error": str(exc)})

    print("\n🗑️ Sammel-Löschen:")
    print(f"   gelöscht: {len(deleted)} von {len(entry_ids)}")
    return {"ok": not errors, "deleted": deleted, "errors": errors, "requested": len(entry_ids)}


def delete_created_test_entry(body: dict) -> dict:
    entry_id = int(body.get("entryId") or 0)
    if not entry_id:
        raise RuntimeError("entryId fehlt.")
    with LOCK:
        known = any(int(r.get("entryId") or 0) == entry_id for r in CREATED_TEST_ENTRIES)
    if not known:
        raise RuntimeError("Sicherheitsstopp: Diese entryId wurde nicht in dieser App-Sitzung als Test-Eintrag erstellt.")
    profile = sanitize_profile_name(body.get("profile") or "default")
    browser = body.get("browser") or "auto"
    # v1.10.16: direkt mit der gespeicherten Sitzung löschen; kein sichtbares e-Bichelchen-Fenster nötig.
    resp = direct_delete_entry(entry_id)
    if not resp.get("ok"):
        raise RuntimeError("e-Bichelchen hat den Test-Eintrag nicht gelöscht: " + json.dumps(resp, ensure_ascii=False)[:1200])
    with LOCK:
        for r in CREATED_TEST_ENTRIES:
            if int(r.get("entryId") or 0) == entry_id:
                r["deletedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                r["deleteResponse"] = resp.get("body")
    print("\n🗑️ Test-Eintrag gelöscht:")
    print(f"   entryId: {entry_id}")
    return {"entryId": entry_id, "deletedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "response": resp.get("body")}


# ===================================================================
# Modul-Wrapper für die zusammengeführte EntretienConnect-App.
# Diese Funktionen werden direkt von server.py aufgerufen (kein eigener
# HTTP-Server mehr). Die harten Sicherheitschecks oben bleiben unverändert.
# ===================================================================

READ_BROWSER_LOCK = threading.Lock()


def read_browser_and_store(selected_group_id=None) -> dict:
    """Liest Klassen/Schüler/Message-subjectId aus dem e-Bichelchen-Tab und
    merkt sie als LATEST_DATA (Grundlage der serverseitigen Sicherheitsprüfung).
    v1.10.18: nur EINE Lesung gleichzeitig — der Server ist multithreaded, und parallele
    Automations-Läufe im selben Tab bremsten sich gegenseitig aus (« timed out »)."""
    global LATEST_DATA, LATEST_AT
    if not READ_BROWSER_LOCK.acquire(blocking=False):
        raise RuntimeError("Lecture déjà en cours – merci de patienter.")
    try:
        payload = read_from_firefox(selected_group_id) if ACTIVE_BROWSER_MODE == "firefox-current" else read_from_chrome(selected_group_id)
    finally:
        READ_BROWSER_LOCK.release()
    with LOCK:
        LATEST_DATA = payload
        LATEST_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    return payload


def get_current() -> tuple:
    with LOCK:
        return LATEST_DATA, LATEST_AT


def clear_current() -> None:
    global LATEST_DATA, LATEST_AT
    with LOCK:
        LATEST_DATA = None
        LATEST_AT = None

# ===================================================================
# v305 – ein kontrolliertes Firefox-Fenster mit WebDriver BiDi
# App und e-Bichelchen laufen als zwei Tabs derselben Firefox-Instanz.
# Die sichtbare e-Bichelchen-Seite wird nicht umgeschaltet; die Klasse
# wird ausschließlich über die von EntretienConnect gewählte groupId gelesen.
# ===================================================================

# Referenzen auf v304-Fallbacks, bevor die v305-Funktionen sie überschreiben.
_launch_browser_legacy_v304 = launch_browser
_check_login_ready_legacy_v304 = check_login_ready
_focus_app_legacy_v304 = focus_app_tab
_close_eb_legacy_v304 = close_ebichelchen_target
_force_close_legacy_v304 = force_close_launched_browser
_soft_reset_legacy_v304 = soft_reset_login
_reset_login_legacy_v304 = reset_login_session

BIDI_PORT = 9224
FIREFOX_BIDI = None
FIREFOX_BIDI_START_ERROR = ""


def _is_local_app_url(url: str) -> bool:
    u = str(url or "").lower()
    return (
        u.startswith("http://127.0.0.1:")
        or u.startswith("http://localhost:")
        or u.startswith("http://[::1]:")
    )


def find_firefox_executable() -> dict | None:
    system = platform.system().lower()
    candidates: list[str] = []
    if system == "darwin":
        candidates = [
            "/Applications/Firefox.app/Contents/MacOS/firefox",
            os.path.expanduser("~/Applications/Firefox.app/Contents/MacOS/firefox"),
            "/Applications/Firefox Developer Edition.app/Contents/MacOS/firefox",
        ]
    elif system == "windows":
        pf = os.environ.get("PROGRAMFILES", "")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", "")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(pf, "Mozilla Firefox", "firefox.exe"),
            os.path.join(pfx86, "Mozilla Firefox", "firefox.exe"),
            os.path.join(local, "Mozilla Firefox", "firefox.exe"),
            shutil.which("firefox.exe") or "",
        ]
    else:
        candidates = [shutil.which("firefox") or "", shutil.which("firefox-esr") or ""]
    for path in candidates:
        if path and pathlib.Path(path).exists():
            return {"path": path, "id": "firefox", "name": "Mozilla Firefox"}
    return None


def _free_loopback_port(preferred: int = BIDI_PORT) -> int:
    for port in list(range(preferred, preferred + 20)) + [0]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return int(sock.getsockname()[1])
        except OSError:
            pass
        finally:
            sock.close()
    raise RuntimeError("Kein freier lokaler Port für Firefox WebDriver BiDi gefunden.")


def _write_firefox_profile_prefs(profile_dir: pathlib.Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    prefs = (
        '// EntretienConnect v305 – ruhiger, dauerhafter Firefox-Hilfsprofilstart\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.aboutwelcome.enabled", false);\n'
        'user_pref("browser.startup.page", 0);\n'
        'user_pref("browser.tabs.warnOnClose", false);\n'
        'user_pref("browser.tabs.warnOnCloseOtherTabs", false);\n'
        'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);\n'
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n'
        'user_pref("browser.newtabpage.activity-stream.showSponsored", false);\n'
        'user_pref("browser.newtabpage.activity-stream.showSponsoredTopSites", false);\n'
    )
    try:
        (profile_dir / "user.js").write_text(prefs, encoding="utf-8")
    except Exception:
        pass


def _bidi_remote_value(remote):
    if not isinstance(remote, dict):
        return remote
    typ = remote.get("type")
    if typ in ("string", "number", "boolean", "bigint"):
        return remote.get("value")
    if typ in ("null", "undefined"):
        return None
    if typ in ("array", "set"):
        return [_bidi_remote_value(v) for v in (remote.get("value") or [])]
    if typ in ("object", "map"):
        out = {}
        for pair in remote.get("value") or []:
            if isinstance(pair, list) and len(pair) == 2:
                key = _bidi_remote_value(pair[0]) if isinstance(pair[0], dict) else pair[0]
                out[str(key)] = _bidi_remote_value(pair[1])
        return out
    return remote.get("value")


def _bidi_bytes_value(value) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value or "")
    typ = value.get("type")
    raw = value.get("value") or ""
    if typ == "base64":
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return ""
    return str(raw)


class FirefoxBiDiController:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.ws: SimpleWebSocket | None = None
        self.lock = threading.RLock()
        self.next_id = 1
        self.session_id = None
        self.capabilities: dict = {}
        self.port: int | None = None
        self.profile_dir: pathlib.Path | None = None
        self.firefox_path: str | None = None
        self.app_context: str | None = None
        self.eb_context: str | None = None

    def alive(self) -> bool:
        return bool(self.ws and self.process and self.process.poll() is None)

    def _command_locked(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if not self.ws:
            raise RuntimeError("Firefox-BiDi ist nicht verbunden.")
        msg_id = self.next_id
        self.next_id += 1
        self.ws.sock.settimeout(max(1.0, float(timeout)))
        self.ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}, ensure_ascii=False))
        deadline = time.time() + max(1.0, float(timeout))
        while time.time() < deadline:
            raw = self.ws.recv_text()
            msg = json.loads(raw)
            if msg.get("id") != msg_id:
                # Events gehören nicht zu dieser Anfrage und werden hier bewusst ignoriert.
                continue
            if msg.get("type") == "error" or msg.get("error"):
                err = msg.get("error") or "error"
                detail = msg.get("message") or ""
                raise RuntimeError(f"Firefox BiDi {method}: {err} – {detail}".strip())
            return msg.get("result") or {}
        raise RuntimeError(f"Firefox BiDi {method}: keine Antwort erhalten.")

    def command(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        with self.lock:
            return self._command_locked(method, params, timeout)

    def start(self, app_url: str, profile: str = "default", timeout_s: float = 30.0) -> dict:
        global ACTIVE_BROWSER_MODE, ACTIVE_BROWSER_USER_AGENT
        with self.lock:
            if self.alive():
                self._refresh_contexts()
                if self.app_context:
                    self._command_locked("browsingContext.activate", {"context": self.app_context}, 5)
                    ACTIVE_BROWSER_MODE = "firefox-bidi"
                    return {
                        "opened": True,
                        "alreadyRunning": True,
                        "browser": "Mozilla Firefox",
                        "sameBrowser": True,
                        "appContext": self.app_context,
                    }
                self.shutdown(close_browser=True)

            browser = find_firefox_executable()
            if not browser:
                raise RuntimeError("Mozilla Firefox wurde nicht gefunden. Bitte Firefox installieren und EntretienConnect erneut starten.")
            self.firefox_path = browser["path"]
            self.port = _free_loopback_port(BIDI_PORT)
            self.profile_dir = PROFILE_ROOT / "firefox-bidi" / sanitize_profile_name(profile)
            _write_firefox_profile_prefs(self.profile_dir)

            args = [
                self.firefox_path,
                "--no-remote",
                "--profile", str(self.profile_dir),
                "--remote-debugging-port", str(self.port),
                "--new-window", app_url,
            ]
            env = os.environ.copy()
            env.setdefault("MOZ_CRASHREPORTER_DISABLE", "1")
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            BROWSER_PROCESSES["firefox-bidi"] = self.process

            deadline = time.time() + max(10.0, float(timeout_s))
            last_error = ""
            while time.time() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Firefox wurde beendet, bevor EntretienConnect geöffnet werden konnte.")
                try:
                    self.ws = SimpleWebSocket(f"ws://127.0.0.1:{self.port}/session", timeout=3.0)
                    result = self._command_locked(
                        "session.new",
                        {
                            "capabilities": {
                                "alwaysMatch": {"browserName": "firefox"}
                            }
                        },
                        12,
                    )
                    self.session_id = result.get("sessionId")
                    self.capabilities = result.get("capabilities") or {}
                    ACTIVE_BROWSER_USER_AGENT = str(self.capabilities.get("userAgent") or "Mozilla/5.0 Firefox")
                    break
                except Exception as exc:
                    last_error = str(exc)
                    if self.ws:
                        try:
                            self.ws.close()
                        except Exception:
                            pass
                    self.ws = None
                    time.sleep(0.22)
            if not self.ws:
                raise RuntimeError("Firefox WebDriver BiDi wurde nicht rechtzeitig verfügbar. " + last_error)

            while time.time() < deadline:
                self._refresh_contexts()
                if self.app_context:
                    try:
                        self._command_locked("browsingContext.activate", {"context": self.app_context}, 5)
                    except Exception:
                        pass
                    ACTIVE_BROWSER_MODE = "firefox-bidi"
                    return {
                        "opened": True,
                        "alreadyRunning": False,
                        "browser": "Mozilla Firefox",
                        "sameBrowser": True,
                        "profileDir": str(self.profile_dir),
                        "port": self.port,
                        "appContext": self.app_context,
                    }
                time.sleep(0.16)
            raise RuntimeError("Firefox läuft, aber der EntretienConnect-Tab wurde nicht gefunden.")

    def _refresh_contexts(self) -> list[dict]:
        tree = self._command_locked("browsingContext.getTree", {"maxDepth": 0}, 6)
        contexts = tree.get("contexts") or []
        ids = {str(c.get("context")) for c in contexts}
        app = next((c for c in contexts if _is_local_app_url(str(c.get("url") or ""))), None)
        if app:
            self.app_context = str(app.get("context"))
        elif self.app_context not in ids:
            self.app_context = None
        if self.eb_context not in ids:
            self.eb_context = None
        if not self.eb_context:
            eb = next(
                (
                    c
                    for c in contexts
                    if "education.lu" in str(c.get("url") or "").lower()
                    and not _is_local_app_url(str(c.get("url") or ""))
                ),
                None,
            )
            if not eb and self.app_context:
                eb = next(
                    (
                        c
                        for c in contexts
                        if str(c.get("originalOpener") or "") == self.app_context
                        and not _is_local_app_url(str(c.get("url") or ""))
                    ),
                    None,
                )
            if eb:
                self.eb_context = str(eb.get("context"))
        return contexts

    def context_info(self, context_id: str | None) -> dict | None:
        if not context_id:
            return None
        with self.lock:
            for context in self._refresh_contexts():
                if str(context.get("context")) == str(context_id):
                    return context
        return None

    def open_ebichelchen(self) -> dict:
        with self.lock:
            contexts = self._refresh_contexts()
            if self.eb_context and any(str(c.get("context")) == self.eb_context for c in contexts):
                self._command_locked("browsingContext.activate", {"context": self.eb_context}, 5)
                current_url = next((c.get("url") for c in contexts if str(c.get("context")) == self.eb_context), "")
                return {
                    "alreadyRunning": True,
                    "reusedTab": True,
                    "sameBrowser": True,
                    "active": True,
                    "browser": "Mozilla Firefox",
                    "url": current_url,
                }
            params: dict = {"type": "tab", "background": False}
            if self.app_context:
                params["referenceContext"] = self.app_context
            created = self._command_locked("browsingContext.create", params, 8)
            self.eb_context = str(created.get("context") or "")
            if not self.eb_context:
                raise RuntimeError("Firefox konnte keinen zweiten Tab anlegen.")
            self._command_locked(
                "browsingContext.navigate",
                {"context": self.eb_context, "url": EB_URL, "wait": "none"},
                8,
            )
            self._command_locked("browsingContext.activate", {"context": self.eb_context}, 5)
            return {
                "alreadyRunning": False,
                "openedTab": True,
                "active": True,
                "sameBrowser": True,
                "browser": "Mozilla Firefox",
                "url": EB_URL,
                "context": self.eb_context,
            }

    def evaluate_json(self, context_id: str, expression: str, timeout: float = 20.0):
        result = self.command(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": context_id},
                "awaitPromise": True,
                "resultOwnership": "none",
                "serializationOptions": {"maxObjectDepth": 2, "maxDomDepth": 0},
            },
            timeout,
        )
        if result.get("type") == "exception":
            details = result.get("exceptionDetails") or {}
            raise RuntimeError(details.get("text") or "JavaScript-Auswertung in Firefox fehlgeschlagen.")
        remote = result.get("result") or {}
        value = _bidi_remote_value(remote)
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value

    def capture_session(self, context_id: str) -> dict:
        result = self.command(
            "storage.getCookies",
            {"partition": {"type": "context", "context": context_id}},
            10,
        )
        merged: dict[str, str] = {}
        for cookie in result.get("cookies") or []:
            domain = str(cookie.get("domain") or "").lower()
            name = str(cookie.get("name") or "")
            if not name or "education.lu" not in domain:
                continue
            merged[name] = _bidi_bytes_value(cookie.get("value"))
        if not merged:
            raise RuntimeError("Keine e-Bichelchen-Sitzungscookies in Firefox gefunden.")
        return {
            "cookieHeader": "; ".join(f"{key}={value}" for key, value in merged.items()),
            "cookieNames": sorted(merged),
            "userAgent": str(self.capabilities.get("userAgent") or ACTIVE_BROWSER_USER_AGENT or "Mozilla/5.0 Firefox"),
            "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "targetUrl": (self.context_info(context_id) or {}).get("url") or EB_URL,
            "browser": "firefox-bidi",
            "profileDir": str(self.profile_dir or ""),
        }

    def close_eb(self) -> dict:
        with self.lock:
            self._refresh_contexts()
            context = self.eb_context
            if not context:
                return {"closed": True, "alreadyClosed": True, "method": "firefox-bidi"}
            try:
                self._command_locked("browsingContext.close", {"context": context, "promptUnload": False}, 8)
            finally:
                self.eb_context = None
            return {"closed": True, "method": "firefox-bidi", "context": context}

    def focus_app(self) -> dict:
        with self.lock:
            self._refresh_contexts()
            if not self.app_context:
                raise RuntimeError("EntretienConnect-Tab wurde in Firefox nicht gefunden.")
            self._command_locked("browsingContext.activate", {"context": self.app_context}, 6)
            return {
                "method": "firefox-bidi",
                "foundExistingTab": True,
                "context": self.app_context,
                "openedNewTab": False,
            }

    def delete_education_cookies(self) -> int:
        deleted = 0
        for domain in ("ssl.education.lu", ".education.lu", "education.lu"):
            try:
                self.command("storage.deleteCookies", {"filter": {"domain": domain}}, 7)
                deleted += 1
            except Exception:
                pass
        return deleted

    def shutdown(self, close_browser: bool = True) -> dict:
        result = {"closed": False, "method": "firefox-bidi"}
        with self.lock:
            if self.ws and close_browser:
                try:
                    self._command_locked("browser.close", {}, 6)
                    result["closed"] = True
                except Exception:
                    pass
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
            self.ws = None
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                    result["closed"] = True
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
            self.process = None
            self.session_id = None
            self.app_context = None
            self.eb_context = None
            BROWSER_PROCESSES.pop("firefox-bidi", None)
        return result


def launch_firefox_app(app_url: str, profile: str = "default", timeout_s: float = 30.0) -> dict:
    global FIREFOX_BIDI, FIREFOX_BIDI_START_ERROR
    if FIREFOX_BIDI is None:
        FIREFOX_BIDI = FirefoxBiDiController()
    try:
        info = FIREFOX_BIDI.start(app_url, profile=profile, timeout_s=timeout_s)
        FIREFOX_BIDI_START_ERROR = ""
        return info
    except Exception as exc:
        FIREFOX_BIDI_START_ERROR = str(exc)
        raise


def supports_firefox_bidi() -> bool:
    # Feature-capability, nicht nur momentaner Verbindungsstatus. So versucht die
    # Oberfläche nie, als Fallback noch Chrome parallel zu öffnen.
    return True


def debug_browser_running() -> bool:
    if ACTIVE_BROWSER_MODE == "firefox-bidi" or (FIREFOX_BIDI and FIREFOX_BIDI.alive()):
        return bool(FIREFOX_BIDI and FIREFOX_BIDI.alive())
    if ACTIVE_BROWSER_MODE == "firefox-current":
        return True
    try:
        data = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1.0)
        return isinstance(data, dict)
    except Exception:
        return False


def launch_browser(profile: str, preferred_browser: str = "auto", user_agent: str = "") -> dict:
    global ACTIVE_BROWSER_MODE, ACTIVE_BROWSER_USER_AGENT
    pref = str(preferred_browser or "").lower()
    if pref == "firefox-bidi" or (FIREFOX_BIDI and FIREFOX_BIDI.alive()):
        if not FIREFOX_BIDI or not FIREFOX_BIDI.alive():
            raise RuntimeError(
                FIREFOX_BIDI_START_ERROR
                or "Der kontrollierte Firefox ist nicht verfügbar. EntretienConnect bitte vollständig beenden und neu starten."
            )
        ACTIVE_BROWSER_MODE = "firefox-bidi"
        ACTIVE_BROWSER_USER_AGENT = str(
            FIREFOX_BIDI.capabilities.get("userAgent") or user_agent or "Mozilla/5.0 Firefox"
        )
        return FIREFOX_BIDI.open_ebichelchen()
    return _launch_browser_legacy_v304(profile, preferred_browser, user_agent)


def _firefox_bidi_probe_expression() -> str:
    return r'''(async () => {
      const out={ready:false,pageUrl:String(location.href||""),groupCount:0,via:"firefox-bidi"};
      if(!out.pageUrl.includes('/ebichelchen/app/')) return JSON.stringify(out);
      const controller=new AbortController();
      const timer=setTimeout(()=>controller.abort(),1800);
      try{
        const res=await fetch('/ebichelchen/app/api/group/get-groups-from-teacher',{
          method:'GET',credentials:'include',signal:controller.signal,
          headers:{'accept':'application/json, text/plain, */*','mobileappversion':'web'}
        });
        out.status=res.status;
        if(!res.ok) return JSON.stringify(out);
        const json=await res.json();
        const lists=[json,json&&json.objects,json&&json.groups,json&&json.data,
          json&&json.data&&json.data.objects,json&&json.result,
          json&&json.result&&json.result.objects];
        const arr=lists.find(Array.isArray)||[];
        out.groupCount=arr.length;
        out.ready=arr.length>0;
        return JSON.stringify(out);
      }catch(e){
        out.error=String(e&&e.message||e);
        return JSON.stringify(out);
      }finally{ clearTimeout(timer); }
    })()'''


def check_login_ready() -> dict:
    if ACTIVE_BROWSER_MODE != "firefox-bidi":
        return _check_login_ready_legacy_v304()
    if not FIREFOX_BIDI or not FIREFOX_BIDI.alive():
        return {"ok": True, "ready": False, "browserClosed": True, "stage": "closed", "lightweight": True}
    try:
        info = FIREFOX_BIDI.context_info(FIREFOX_BIDI.eb_context)
        if not info:
            return {"ok": True, "ready": False, "browserClosed": True, "stage": "tab-closed", "lightweight": True}
        url = str(info.get("url") or "")
        if "/ebichelchen/app/" not in url:
            return {
                "ok": True,
                "ready": False,
                "browserClosed": False,
                "stage": "login",
                "pageUrl": url,
                "lightweight": True,
            }
        probe = FIREFOX_BIDI.evaluate_json(
            FIREFOX_BIDI.eb_context,
            _firefox_bidi_probe_expression(),
            timeout=8,
        )
        if not isinstance(probe, dict):
            probe = {}
        return {
            "ok": True,
            "ready": bool(probe.get("ready")),
            "browserClosed": False,
            "stage": "ready" if probe.get("ready") else "loading",
            "groupCount": int(probe.get("groupCount") or 0),
            "status": probe.get("status"),
            "pageUrl": probe.get("pageUrl") or url,
            "via": "firefox-bidi",
            "lightweight": True,
            "detail": probe.get("error") or "",
        }
    except Exception as exc:
        # Ein interner Route-Wechsel von e-Bichelchen zerstört kurz den Realm. Das ist
        # während des Logins normal und wird beim nächsten Poll erneut geprüft.
        return {
            "ok": True,
            "ready": False,
            "browserClosed": False,
            "stage": "loading",
            "detail": str(exc),
            "lightweight": True,
        }


def _read_direct_with_session(session: dict, selected_group_id: int) -> dict:
    t0 = time.perf_counter()
    groups_t0 = time.perf_counter()
    groups, group_result = _firefox_groups(session)
    groups_ms = round((time.perf_counter() - groups_t0) * 1000)
    if not groups:
        raise RuntimeError(
            "Die Klassen konnten aus der gespeicherten e-Bichelchen-Sitzung nicht gelesen werden. Bitte neu verbinden."
        )
    group = next((g for g in groups if int(g.get("id")) == int(selected_group_id)), None)
    if group is None:
        raise RuntimeError(
            f"Die in EntretienConnect gewählte Klasse wurde nicht gefunden (groupId {selected_group_id})."
        )
    subjects_t0 = time.perf_counter()
    subjects, subjects_source, subject_attempts = _read_subjects_direct(session, group)
    message_subject = _detect_message_subject_py(subjects)
    subjects_ms = round((time.perf_counter() - subjects_t0) * 1000)
    if not message_subject:
        raise RuntimeError("Die Kategorie « Nachricht / Message » konnte für die gewählte Klasse nicht gelesen werden.")
    return {
        "version": "1.10.30",
        "importedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pageUrl": EB_URL,
        "groups": groups,
        "needsGroupSelection": False,
        "group": {key: group.get(key) for key in ("id", "classAlias", "name", "classGrade", "entityCode")},
        "loggedInUser": None,
        "students": group.get("students", []),
        "teachers": group.get("teachers", []),
        "tutors": group.get("tutors", []),
        "excluded": group.get("excluded", []),
        "subjects": subjects,
        "messageSubject": {
            "id": message_subject.get("id"),
            "labelDeu": message_subject.get("labelDeu", ""),
            "labelFra": message_subject.get("labelFra", ""),
            "label": message_subject.get("label", ""),
            "source": message_subject.get("source", subjects_source or ""),
        },
        "endpoints": {
            "groupsUrl": group_result.get("url"),
            "subjectsSource": subjects_source,
            "scannedStorageKeys": [],
            "knownSubjectUrls": [],
            "subjectAttempts": subject_attempts,
        },
        "timing": {
            "groupsMs": groups_ms,
            "subjectsMs": subjects_ms,
            "totalMs": round((time.perf_counter() - t0) * 1000),
        },
        "summary": {
            "groups": len(groups),
            "students": len(group.get("students", [])),
            "teachers": len(group.get("teachers", [])),
            "tutors": len(group.get("tutors", [])),
            "excluded": len(group.get("excluded", [])),
            "subjects": len(subjects),
            "messageSubjectId": message_subject.get("id"),
        },
        "source": {
            "browser": "firefox-bidi-direct",
            "sessionCaptured": True,
            "sessionCookieNames": session.get("cookieNames", []),
            "selectionAuthority": "EntretienConnect",
            "groupChosenAutomatically": False,
        },
    }


def read_from_firefox_bidi(selected_group_id: int | None = None) -> dict:
    global LATEST_SESSION, LATEST_SESSION_AT
    if not FIREFOX_BIDI or not FIREFOX_BIDI.alive():
        raise RuntimeError("Der kontrollierte Firefox ist nicht mehr geöffnet.")
    info = FIREFOX_BIDI.context_info(FIREFOX_BIDI.eb_context)
    if not info:
        if selected_group_id is not None:
            return _read_direct_with_session(get_saved_session(), int(selected_group_id))
        raise RuntimeError("Der e-Bichelchen-Tab wurde geschlossen. Bitte erneut verbinden.")
    url = str(info.get("url") or "")
    if "/ebichelchen/app/" not in url:
        raise RuntimeError("e-Bichelchen ist noch nicht vollständig angemeldet.")
    payload = FIREFOX_BIDI.evaluate_json(
        FIREFOX_BIDI.eb_context,
        build_read_expression(selected_group_id),
        timeout=36,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Firefox hat keine gültigen e-Bichelchen-Daten zurückgegeben.")
    session = FIREFOX_BIDI.capture_session(FIREFOX_BIDI.eb_context)
    with LOCK:
        LATEST_SESSION = session
        LATEST_SESSION_AT = session.get("capturedAt")
    payload.setdefault("source", {})
    payload["source"].update(
        {
            "browser": "firefox-bidi",
            "sessionCaptured": True,
            "sessionCookieNames": session.get("cookieNames", []),
            "selectionAuthority": (
                "EntretienConnect" if selected_group_id is not None else "automatic-only-for-single-group"
            ),
        }
    )
    payload["version"] = "1.10.30"
    payload["pageUrl"] = url
    return payload


def read_browser_and_store(selected_group_id=None) -> dict:
    global LATEST_DATA, LATEST_AT
    if not READ_BROWSER_LOCK.acquire(blocking=False):
        raise RuntimeError("Lecture déjà en cours – merci de patienter.")
    try:
        if ACTIVE_BROWSER_MODE == "firefox-bidi":
            payload = read_from_firefox_bidi(selected_group_id)
        elif ACTIVE_BROWSER_MODE == "firefox-current":
            payload = read_from_firefox(selected_group_id)
        else:
            payload = read_from_chrome(selected_group_id)
    finally:
        READ_BROWSER_LOCK.release()
    with LOCK:
        LATEST_DATA = payload
        LATEST_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    return payload


def focus_app_tab() -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-bidi":
        if not FIREFOX_BIDI:
            raise RuntimeError("Firefox-BiDi ist nicht verfügbar.")
        return FIREFOX_BIDI.focus_app()
    return _focus_app_legacy_v304()


def close_ebichelchen_target() -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-bidi":
        if not FIREFOX_BIDI:
            return {"closed": True, "alreadyClosed": True, "method": "firefox-bidi"}
        return FIREFOX_BIDI.close_eb()
    return _close_eb_legacy_v304()


def force_close_launched_browser(force: bool = False) -> dict:
    global FIREFOX_BIDI
    if ACTIVE_BROWSER_MODE == "firefox-bidi" or (FIREFOX_BIDI and FIREFOX_BIDI.alive()):
        return FIREFOX_BIDI.shutdown(close_browser=True) if FIREFOX_BIDI else {
            "closed": False,
            "method": "firefox-bidi",
        }
    return _force_close_legacy_v304(force=force)


def soft_reset_login() -> dict:
    global LATEST_SESSION, LATEST_SESSION_AT
    if ACTIVE_BROWSER_MODE == "firefox-bidi":
        closed = None
        cleared = 0
        if FIREFOX_BIDI:
            try:
                closed = FIREFOX_BIDI.close_eb()
            except Exception:
                pass
            try:
                cleared = FIREFOX_BIDI.delete_education_cookies()
            except Exception:
                pass
        clear_current()
        with LOCK:
            LATEST_SESSION = None
            LATEST_SESSION_AT = None
        return {
            "softReset": True,
            "browserRunning": debug_browser_running(),
            "cookiesCleared": bool(cleared),
            "closedEbichelchen": closed,
            "profilePreserved": True,
        }
    return _soft_reset_legacy_v304()


def reset_login_session(profile: str = "default", preserve_profile: bool = False) -> dict:
    if ACTIVE_BROWSER_MODE == "firefox-bidi":
        info = soft_reset_login()
        return {
            "closed": False,
            "profilesRemoved": [],
            "sessionDataRemoved": [],
            "cookiesCleared": info.get("cookiesCleared", False),
            "profilePreserved": True,
            "browserRunning": info.get("browserRunning", False),
        }
    return _reset_login_legacy_v304(profile, preserve_profile)

