#!/usr/bin/env python3
# eBichelchenHelper v1.17.0 - lokaler Helfer für individuelle e-Bichelchen-Nachrichten.
# Keine e-Bichelchen-Zugangsdaten. v1.10.16 kann nach Vorschau mehrere individuelle Message-Einträge erstellen und wieder löschen.
# v1.10.17: Browser.close/Profil-Löschung nur noch, wenn KEIN App-Tab (127.0.0.1/localhost) im
# Debug-Browser läuft — sonst verschwand die App mitsamt Fenster beim Verbinden/Aufräumen.

from __future__ import annotations

import base64
import hashlib
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
import stat
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
    # v311: Im App-Speicher liegen kurzlebige e-Bichelchen-Sitzungsdaten. Der Ordner
    # gehört ausschließlich dem angemeldeten Benutzer.
    try:
        path.chmod(0o700)
    except Exception:
        pass
    return path


def _write_private_text(path: pathlib.Path, text: str) -> None:
    """Schreibt atomar und ausschließlich für den eigenen Benutzer lesbar (0600).

    v311: Wird für alles verwendet, was eine gültige e-Bichelchen-Sitzung enthalten
    kann. Ohne das entstünde die Datei mit der Standardmaske (0644).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise
    os.replace(str(tmp), str(path))
    try:
        path.chmod(0o600)
    except Exception:
        pass


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

HELPER_VERSION = "1.17.0"

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
# v311: Nur eine Mehrfach-Veröffentlichung gleichzeitig. Zwei offene App-Tabs oder ein
# Doppelklick haben vorher zwei parallele Läufe gestartet – und damit doppelte Einträge.
PUBLISH_LOCK = threading.Lock()
# v311: Der zuletzt erfolgreiche Fächer-Endpunkt. Ohne diesen Hinweis probiert
# _read_subjects_direct bei jedem Lesen bis zu neun Varianten nacheinander durch.
SUBJECTS_ENDPOINT_HINT_FILE = DATA_ROOT / "eb-endpoints.json"


class EbSessionExpired(RuntimeError):
    """Die übernommene e-Bichelchen-Sitzung ist nicht mehr gültig.

    v311: Vorher lief das stumm ins Leere – ssl.education.lu antwortet auf eine
    abgelaufene Sitzung mit einer Weiterleitung auf die IAM-Anmeldeseite, also mit
    Status 200 und HTML. Das sah wie eine leere Antwort aus und erzeugte
    Folgefehler wie « Kategorie Message nicht gefunden » oder einen Abbruch
    mitten in der Veröffentlichung.
    """

    def __init__(self, message: str = ""):
        super().__init__(message or "La session e-Bichelchen a expiré. Reconnectez e-Bichelchen, puis réessayez.")


def _response_is_login_redirect(final_url: str, text: str, content_type: str) -> bool:
    """Erkennt eine Weiterleitung auf die Anmeldung – und NUR die.

    v332: Vorher galt JEDE HTML-Antwort als « Sitzung abgelaufen ». Das war falsch
    und richtete echten Schaden an: `_read_subjects_direct` probiert mehrere
    Fächer-Endpunkte durch, und der erste Fehlversuch liefert von e-Bichelchen ein
    HTTP 404 mit HTML-Fehlerseite. Das wurde als abgelaufene Sitzung gewertet und
    brach das Lesen sofort ab – nachgemessen an einer gültigen Sitzung.
    Dieselbe Falle hat sehr wahrscheinlich schon den v315-Versuch zu Fall gebracht,
    den ich damals fälschlich einem Serverausfall zugeschrieben habe.

    Verlässlich ist allein die Ziel-URL: Ist die Sitzung wirklich abgelaufen,
    landet die Anfrage auf der IAM-Anmeldung. Statuscodes 401/403 prüft der
    Aufrufer davon getrennt.
    """
    url = str(final_url or "").lower()
    if not url:
        return False
    if "/iam/" in url or "iam2fa" in url or "auth.education.lu" in url:
        return True
    if "/ebichelchen/app/login" in url:
        return True
    last = url.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    return last in ("login", "signin", "sso")


def _forget_session(reason: str = "") -> None:
    """Verwirft die gespeicherte Sitzung, damit die Oberfläche neu verbinden lässt."""
    global LATEST_SESSION, LATEST_SESSION_AT
    with LOCK:
        LATEST_SESSION = None
        LATEST_SESSION_AT = None


def _load_subjects_endpoint_hint() -> str:
    try:
        data = json.loads(SUBJECTS_ENDPOINT_HINT_FILE.read_text(encoding="utf-8"))
        value = data.get("subjectsPath") if isinstance(data, dict) else None
        return str(value or "")
    except Exception:
        return ""


def _save_subjects_endpoint_hint(method: str, base_path: str) -> None:
    try:
        _write_private_text(
            SUBJECTS_ENDPOINT_HINT_FILE,
            json.dumps({"subjectsMethod": method, "subjectsPath": base_path}, ensure_ascii=False),
        )
    except Exception:
        pass


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

    # v306: Es darf nur EIN kontrolliertes Loginfenster existieren. Wenn bereits
    # genau ein e-Bichelchen-Target läuft, wird es wieder nach vorne geholt. Ein
    # verwaister/leer gebliebener CDP-Browser wird vollständig beendet und danach
    # sauber im App-Modus neu gestartet; dadurch entstehen keine normalen Tabs.
    try:
        version = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
        try:
            existing = find_ebichelchen_target()
        except Exception:
            existing = None
        if existing:
            focus_info = _bring_ebichelchen_target_forward(browser_name, wait_s=1.5)
            return {"alreadyRunning": True, "reusedWindow": True, "active": bool(focus_info.get("focused")), "focus": focus_info, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "appWindow": True, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}

        # Kein verwendbares Loginfenster: den isolierten Helferbrowser schließen.
        ws = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if ws:
            try:
                cdp_call(ws, "Browser.close", {}, msg_id=930, timeout=2)
            except Exception:
                pass
        for proc_key, old_proc in list(BROWSER_PROCESSES.items()):
            try:
                if old_proc and old_proc.poll() is None:
                    old_proc.terminate()
                    try:
                        old_proc.wait(timeout=3)
                    except Exception:
                        old_proc.kill()
            except Exception:
                pass
            BROWSER_PROCESSES.pop(proc_key, None)
        for _ in range(20):
            if not debug_browser_running():
                break
            time.sleep(0.10)
    except Exception:
        pass

    args = [
        browser_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--window-size=1120,820",
        "--window-position=120,80",
        f"--app={EB_URL}",
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
            return {"alreadyRunning": False, "active": bool(focus_info.get("focused")), "focus": focus_info, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "appWindow": True, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(f"{browser_name} s’est fermé avant l’ouverture de la fenêtre e-Bichelchen.")
            time.sleep(0.25)

    return {"alreadyRunning": False, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "appWindow": True, "warning": "La fenêtre de connexion a été lancée, mais sa préparation prend plus de temps que prévu."}


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


def build_login_probe_expression() -> str:
    """Anmelde-Sonde – v315 eingeführt, in v316 WIEDER STILLGELEGT.

    ACHTUNG, bevor das jemand erneut aktiviert: In v315 schrieb
    `_mac_wk_launch` nur noch diese Sonde ins Loginfenster und las Klassen und
    Fächer anschließend in Python über HTTP mit den übernommenen Cookies. Real
    scheiterte das sofort – die Anfrage landete auf der IAM-Anmeldeseite, die
    App meldete « session expirée » direkt nach dem erfolgreichen Login. Und
    zwar, obwohl DIESE Sonde Sekunden vorher genau dieselbe Klassenabfrage im
    Fenster erfolgreich ausgeführt hatte.

    Der Unterschied: Im Fenster schickt der Browser bei credentials:"include"
    die vollständige Sitzung mit, einschliesslich HttpOnly-Cookies.
    `WKHTTPCookieStore.getAllCookies` liefert diese offenbar nicht vollständig,
    sodass die exportierten Cookies für einen eigenständigen HTTP-Aufruf nicht
    ausreichen.

    Wer die Leselogik erneut vereinheitlichen will, muss ZUERST dieses
    Cookie-Problem lösen (und daran hängt vermutlich auch das Schreiben von
    Einträgen über direct_ebichelchen_request). Die Sonde bleibt als
    dokumentierter Ausgangspunkt erhalten.

    Wirft absichtlich, solange die Sitzung noch nicht nutzbar ist: Der Aufrufer
    im nativen Fenster wertet das als « noch nicht fertig » und fragt erneut.
    """
    return r"""
(async () => {
  const out = { probe: "login", url: String(location.href || ""), groupCount: 0 };
  if (out.url.indexOf('/ebichelchen/app/') < 0) throw new Error("e-Bichelchen noch nicht geöffnet");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    const res = await fetch('/ebichelchen/app/api/group/get-groups-from-teacher', {
      method: "GET",
      credentials: "include",
      signal: controller.signal,
      headers: { "accept": "application/json, text/plain, */*", "mobileappversion": "web" }
    });
    if (!res.ok) throw new Error("Klassenabfrage: HTTP " + res.status);
    const json = await res.json();
    const lists = [json, json && json.objects, json && json.groups, json && json.data,
                   json && json.data && json.data.objects, json && json.result,
                   json && json.result && json.result.objects];
    const arr = lists.find(Array.isArray) || [];
    if (!arr.length) throw new Error("Noch keine Klassen in der Sitzung");
    out.probe = "ready";
    out.groupCount = arr.length;
    return JSON.stringify(out);
  } finally {
    clearTimeout(timer);
  }
})()
"""


def build_read_expression(selected_group_id: int | None = None) -> str:
    # Gibt JSON-String zurück, damit Browser DevTools ihn sauber by value liefert.
    selected_literal = "null" if selected_group_id is None else str(int(selected_group_id))
    js = r"""
(async () => {
  const requestedGroupId = __SELECTED_GROUP_ID__;
  const perfStart = performance.now();
  const timing = {};

  // v317: Auf der Anmeldeseite KEINE API-Aufrufe.
  //
  // Das native Loginfenster startet auf .../ebichelchen/app/login, und diese URL
  // erfüllt die Bedingung « enthält /ebichelchen/app/ » bereits. Dadurch lief dieser
  // Leser ab der ersten Sekunde los und feuerte – alle rund zwei Sekunden erneut –
  // unauthentifizierte Abfragen gegen e-Bichelchen, die ganze Anmeldung hindurch.
  // e-Bichelchen meldete daraufhin selbst « Es konnte keine Verbindung zum Server
  // erstellt werden ».
  //
  // Nach 90 Sekunden wird es trotzdem versucht: Sollte e-Bichelchen wider Erwarten
  // auch nach der Anmeldung auf einer /login-URL bleiben, verhält sich die App dann
  // wieder wie vorher, statt endlos zu warten.
  const onLoginRoute = /\/ebichelchen\/app\/login(\/|\?|#|$)/i.test(String(location.href || ""));
  if (onLoginRoute) {
    if (!window.__ecLoginRouteSince) window.__ecLoginRouteSince = Date.now();
    if (Date.now() - window.__ecLoginRouteSince < 90000) {
      throw new Error("Anmeldung noch nicht abgeschlossen");
    }
  } else {
    window.__ecLoginRouteSince = 0;
  }

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
    version: "__HELPER_VERSION__",
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
    return js.replace("__SELECTED_GROUP_ID__", selected_literal).replace("__HELPER_VERSION__", HELPER_VERSION)

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

def _cookie_header_for_host(session: dict, host: str) -> str:
    """v332: Cookie-Kopfzeile NUR aus den Cookies, die für diesen Host gelten.

    Die erfasste Sitzung umfasst mehrere Hosts (ssl.education.lu, auth.education.lu,
    iam2fa.auth.education.lu). Der früher benutzte `cookieHeader` warf alle in einen
    Topf – dabei überschreiben sich sogar gleichnamige Cookies verschiedener Hosts
    (SAML, SAMLAuthToken, TS0104efb4 kommen doppelt vor, mit anderen Werten).
    Fällt auf den alten Sammel-Header zurück, wenn keine Einzel-Cookies vorliegen.
    """
    cookies = session.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return str(session.get("cookieHeader") or "")
    host = str(host or "").lower()
    parts = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        dom = str(c.get("domain") or "").lower().lstrip(".")
        if not dom or host == dom or host.endswith("." + dom):
            parts.append(f'{c["name"]}={c.get("value", "")}')
    if not parts:
        return str(session.get("cookieHeader") or "")
    return "; ".join(parts)


def _session_request(session: dict, method: str, path: str, json_body=None, timeout: float = 5.0) -> dict:
    url = path if str(path).startswith("http") else "https://ssl.education.lu" + str(path)
    try:
        host = urllib.parse.urlparse(url).hostname or "ssl.education.lu"
    except Exception:
        host = "ssl.education.lu"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://ssl.education.lu/ebichelchen/app/",
        "Cookie": _cookie_header_for_host(session, host),
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
    final_url = url
    content_type = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            status = int(resp.getcode() or 0)
            final_url = str(resp.geturl() or url)
            content_type = str(resp.headers.get("Content-Type") or "")
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        status = int(getattr(exc, "code", 0) or 0)
        try:
            final_url = str(exc.geturl() or url)
        except Exception:
            final_url = url
        try:
            content_type = str(exc.headers.get("Content-Type") or "")
        except Exception:
            content_type = ""
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = str(exc)
    # v311: Abgelaufene Sitzung sichtbar machen, statt eine leere Antwort weiterzureichen.
    if status in (401, 403) or _response_is_login_redirect(final_url, text, content_type):
        _forget_session("session-expired")
        raise EbSessionExpired()
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
    # v311: Der zuletzt erfolgreiche Pfad kommt zuerst dran. Bei unveränderter
    # e-Bichelchen-Version braucht das Lesen damit eine Anfrage statt bis zu neun.
    hint = _load_subjects_endpoint_hint()
    if hint:
        hinted = hint.replace("__GID__", gid_q)
        if hinted in get_paths:
            get_paths = [hinted] + [p for p in get_paths if p != hinted]
    for path in get_paths:
        res = _session_request(session, "GET", path, timeout=3.0)
        subjects = _normalize_subjects_py(res.get("body"), "direct-get " + path) if res.get("ok") else []
        attempts.append({"url": path, "method": "GET", "status": res.get("status"), "subjects": len(subjects), "messageSubjectId": (_detect_message_subject_py(subjects) or {}).get("id")})
        if _detect_message_subject_py(subjects):
            _save_subjects_endpoint_hint("GET", path.replace(gid_q, "__GID__"))
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
        return {"softReset": True, "browserRunning": True, "cookiesCleared": False, "normalBrowserProtected": True}
    # v306: Ein abgebrochener Login darf kein unsichtbares oder verwaistes
    # Loginfenster zurücklassen. Cookies werden nach Möglichkeit gelöscht und die
    # komplette isolierte App-Instanz anschließend beendet; das Profil selbst bleibt.
    cleared = False
    if debug_browser_running():
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
    try:
        closed = force_close_launched_browser(force=True)
    except Exception as exc:
        closed = {"closed": False, "error": str(exc)}
    try:
        clear_current()
    except Exception:
        pass
    return {
        "softReset": True,
        "browserRunning": debug_browser_running(),
        "cookiesCleared": cleared,
        "closedLoginWindow": closed,
        "profilePreserved": True,
    }


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
    # v306: Das Login läuft in einer eigenen app-artigen Chromium/Edge-Instanz,
    # während EntretienConnect im normalen Standardbrowser bleibt. Nach dem sicheren
    # Erfassen der Sitzung wird deshalb das komplette Loginfenster beendet.
    result = {"closedEbichelchen": None, "closedLoginWindow": None, "focusedApp": None, "prewarmScheduled": False, "sharedBrowser": False}
    if close_eb:
        try:
            result["closedEbichelchen"] = close_ebichelchen_target()
        except Exception as exc:
            result["closedEbichelchen"] = {"closed": False, "error": str(exc)}
        try:
            result["closedLoginWindow"] = force_close_launched_browser(force=True)
        except Exception as exc:
            result["closedLoginWindow"] = {"closed": False, "error": str(exc)}
    if focus_app:
        try:
            result["focusedApp"] = focus_app_tab()
        except Exception as exc:
            result["focusedApp"] = {"ok": False, "error": str(exc)}
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
        raise RuntimeError("Aucune session e-Bichelchen enregistrée. Reconnectez e-Bichelchen une fois.")
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
    final_url = url
    content_type = ""
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as resp:
            status = resp.getcode()
            status_text = getattr(resp, "reason", "") or ""
            final_url = str(resp.geturl() or url)
            content_type = str(resp.headers.get("Content-Type") or "")
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        status_text = getattr(exc, "reason", "") or ""
        try:
            final_url = str(exc.geturl() or url)
        except Exception:
            final_url = url
        try:
            content_type = str(exc.headers.get("Content-Type") or "")
        except Exception:
            content_type = ""
        text = exc.read().decode("utf-8", "replace")
    # v311: Eine abgelaufene Sitzung darf nicht als « e-Bichelchen hat nicht gespeichert »
    # erscheinen – sonst sucht der Benutzer den Fehler beim Eintrag statt bei der Anmeldung.
    if int(status or 0) in (401, 403) or _response_is_login_redirect(final_url, text, content_type):
        _forget_session("session-expired")
        raise EbSessionExpired()
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


# ---------------------------------------------------------------------------
# v314: Die folgenden fünf Funktionen (build_save_entry_expression bis
# delete_created_test_entry) schreiben Einträge über JavaScript IM Browser-Tab,
# statt über die direkte HTTP-Route mit den übernommenen Cookies. Der reguläre
# Ablauf nutzt sie nicht mehr. Sie bleiben bewusst erhalten: Sie dokumentieren
# den alternativen Schreibweg und dienen als Einzeleintrags-Testwerkzeug, falls
# der direkte Weg einmal ausfällt. Nicht löschen, ohne dafür Ersatz zu haben.
# ---------------------------------------------------------------------------


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


def check_session_alive() -> dict:
    """Prüft die gespeicherte Sitzung mit einer einzigen billigen Anfrage.

    v311: Wird vor einer Mehrfach-Veröffentlichung aufgerufen. Vorher konnte eine
    zwischenzeitlich abgelaufene Sitzung erst beim zwölften von fünfundzwanzig
    Schülern auffallen – mit elf bereits erstellten Einträgen.
    """
    session = get_saved_session()
    groups, _ = _firefox_groups(session)
    if not groups:
        raise EbSessionExpired()
    return {"alive": True, "groups": len(groups)}


def create_bulk_entries(body: dict) -> dict:
    """Erstellt mehrere individuell adressierte Nachricht/Message-Einträge.

    Sicherheitsprinzip: Jeder Payload wird mit denselben harten Checks validiert wie der einzelne
    Test-Eintrag. Wenn unterwegs ein Fehler auftritt, werden bereits erstellte Einträge trotzdem
    in der Antwort zurückgegeben, damit sie sofort wieder gelöscht werden können.
    """
    # v311: Zwei App-Tabs oder ein Doppelklick dürfen nicht zwei parallele Läufe starten.
    if not PUBLISH_LOCK.acquire(blocking=False):
        raise RuntimeError("Une publication est déjà en cours. Merci d'attendre qu'elle se termine.")
    try:
        return _create_bulk_entries_locked(body)
    finally:
        PUBLISH_LOCK.release()


def _create_bulk_entries_locked(body: dict) -> dict:
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

    # v311: Sitzung EINMAL vorab prüfen, statt mitten in der Liste zu scheitern.
    check_session_alive()

    created: list[dict] = []
    error = None
    session_expired = False
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
            session_expired = isinstance(exc, EbSessionExpired)
            break

    print("\n🟠 Mehrfach-Erstellung:")
    print(f"   erstellt: {len(created)} von {len(items)}")
    if error:
        print("   Fehler: " + error)
        return {
            "ok": False,
            "created": created,
            "error": error,
            "requested": len(items),
            "sessionExpired": bool(session_expired),
        }
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


def supports_firefox_bidi() -> bool:
    # v314: Der ferngesteuerte Firefox (WebDriver BiDi) ist vollständig entfernt.
    # Diese Funktion bleibt nur, weil server.py sie für den capabilities-Endpunkt
    # abfragt und ältere, noch gecachte Oberflächen sie erwarten können.
    return False


def _read_direct_with_session(session: dict, selected_group_id: int | None = None,
                              known_subjects: dict | None = None) -> dict:
    """Liest Klassen, Schüler und die Kategorie « Nachricht » direkt über HTTP.

    v315: `selected_group_id=None` ist neu und wird für die ERSTE Lesung nach dem
    Login verwendet. Bei genau einer Klasse wird sie automatisch übernommen, bei
    mehreren meldet das Payload `needsGroupSelection` und EntretienConnect fragt
    nach. Vorher kam dieses erste Payload aus dem JavaScript im Loginfenster,
    die zweite Lesung dagegen von hier – zwei Wege für dasselbe Ergebnis.

    v332: `known_subjects` sind die vom Loginfenster gemerkten Fächer je Klasse.
    Ohne sie muss der Fächer-Endpunkt geraten werden, was real fehlschlägt.
    """
    t0 = time.perf_counter()
    groups_t0 = time.perf_counter()
    groups, group_result = _firefox_groups(session)
    groups_ms = round((time.perf_counter() - groups_t0) * 1000)
    if not groups:
        raise RuntimeError(
            "Die Klassen konnten aus der gespeicherten e-Bichelchen-Sitzung nicht gelesen werden. Bitte neu verbinden."
        )
    group = None
    chosen_automatically = False
    if selected_group_id is not None:
        group = next((g for g in groups if int(g.get("id")) == int(selected_group_id)), None)
        if group is None:
            raise RuntimeError(
                f"Die in EntretienConnect gewählte Klasse wurde nicht gefunden (groupId {selected_group_id})."
            )
    elif len(groups) == 1:
        group = groups[0]
        chosen_automatically = True

    subjects: list = []
    message_subject = None
    subjects_source = None
    subject_attempts: list = []
    subjects_t0 = time.perf_counter()
    if group is not None:
        # v332: Zuerst die vom Loginfenster gemerkte Kategorie verwenden. Python kann
        # den Fächer-Endpunkt nicht erraten – alle bekannten Varianten antworten mit
        # HTTP 404; das Fenster ermittelt ihn zur Laufzeit in der Seite selbst.
        known = (known_subjects or {}).get(str(group.get("id"))) if isinstance(known_subjects, dict) else None
        if isinstance(known, dict) and (known.get("messageSubject") or {}).get("id"):
            subjects = known.get("subjects") or []
            message_subject = known.get("messageSubject")
            subjects_source = "gemerkt"
        else:
            subjects, subjects_source, subject_attempts = _read_subjects_direct(session, group)
            message_subject = _detect_message_subject_py(subjects)
        if not message_subject:
            raise RuntimeError("Die Kategorie « Nachricht / Message » konnte für die gewählte Klasse nicht gelesen werden.")
    subjects_ms = round((time.perf_counter() - subjects_t0) * 1000)
    return {
        "version": HELPER_VERSION,
        "importedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pageUrl": EB_URL,
        "groups": groups,
        "needsGroupSelection": group is None and len(groups) > 1,
        "group": ({key: group.get(key) for key in ("id", "classAlias", "name", "classGrade", "entityCode")} if group else None),
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
            "browser": "session-direct",
            "sessionCaptured": True,
            "sessionCookieNames": session.get("cookieNames", []),
            "selectionAuthority": "EntretienConnect",
            "groupChosenAutomatically": chosen_automatically,
        },
    }


# ===================================================================
# v332 – native macOS login window (WKWebView / Safari WebKit)
# -------------------------------------------------------------------
# The main EntretienConnect UI remains in the user's default browser.
# On macOS, e-Bichelchen is opened in a small native WKWebView window,
# so Google Chrome, Microsoft Edge and remotely controlled Firefox are
# no longer required. The native window exports only education.lu
# cookies and the already parsed class payload to this local helper.
# Windows keeps the proven Chromium/CDP path for now.
# ===================================================================

_launch_browser_cdp = launch_browser
_check_login_ready_cdp = check_login_ready
_read_browser_and_store_cdp = read_browser_and_store
_debug_browser_running_cdp = debug_browser_running
_focus_app_tab_cdp = focus_app_tab
_close_ebichelchen_target_cdp = close_ebichelchen_target
_force_close_launched_browser_cdp = force_close_launched_browser
_soft_reset_login_cdp = soft_reset_login
_reset_login_session_cdp = reset_login_session

MAC_WK_STATE_FILE = DATA_ROOT / "native-wkwebview-state.json"
MAC_WK_EXPRESSION_FILE = DATA_ROOT / "native-wkwebview-read.js"
MAC_WK_SCRIPT_NAME = "EntretienConnect-WKWebView.js"
# v325: Gespeicherte e-Bichelchen-Sitzung, damit nicht bei jedem App-Start neu
# angemeldet werden muss. Enthält die education.lu-Cookies aus dem letzten Login
# (nur für den eigenen Benutzer lesbar, 0600). Läuft nach EB_SESSION_MAX_AGE ab.
MAC_WK_SAVED_SESSION_FILE = DATA_ROOT / "eb-session.json"
MAC_WK_RESTORE_COOKIES_FILE = DATA_ROOT / "native-wkwebview-cookies.json"
EB_SESSION_MAX_AGE = 12 * 3600  # 12 Stunden
# v308: The module remains a normal packaged file, but is also embedded here.
# This makes the native login independent of App Translocation, updater caches,
# and the directory from which server.py was started.
MAC_WK_SCRIPT_B64 = "T2JqQy5pbXBvcnQoJ0NvY29hJyk7Ck9iakMuaW1wb3J0KCdXZWJLaXQnKTsKT2JqQy5pbXBvcnQoJ0ZvdW5kYXRpb24nKTsKCi8qCiAqIEVudHJldGllbkNvbm5lY3QgdjMzMiDigJMgbmF0aXZlIG1hY09TIGUtQmljaGVsY2hlbiBsb2dpbiB3aW5kb3cuCiAqIFJ1bnMgdGhyb3VnaCAvdXNyL2Jpbi9vc2FzY3JpcHQgLWwgSmF2YVNjcmlwdCBhbmQgdXNlcyBXS1dlYlZpZXcgKFNhZmFyaS9XZWJLaXQpLAogKiBzbyBubyBDaHJvbWUsIEVkZ2Ugb3IgcmVtb3RlbHkgY29udHJvbGxlZCBGaXJlZm94IGlzIHJlcXVpcmVkLgogKi8KCmxldCBFQ19BUFAgPSBudWxsOwpsZXQgRUNfV0lORE9XID0gbnVsbDsKbGV0IEVDX1dFQlZJRVcgPSBudWxsOwpsZXQgRUNfVElNRVIgPSBudWxsOwpsZXQgRUNfQlVTWSA9IGZhbHNlOwpsZXQgRUNfRklOSVNIRUQgPSBmYWxzZTsKbGV0IEVDX1NUQVRFX1BBVEggPSAnJzsKbGV0IEVDX1JFQURfRVhQUkVTU0lPTiA9ICcnOwpsZXQgRUNfU1RBUlRfVVJMID0gJyc7CmxldCBFQ19SRVNUT1JFX0NPT0tJRVNfUEFUSCA9ICcnOwpsZXQgRUNfU1RBUlRFRF9BVCA9IERhdGUubm93KCk7Ci8vIHYzMjA6IFNhZmFyaS1LZW5udW5nIGbDvHIgZGVuIFVzZXItQWdlbnQgZGVzIExvZ2luZmVuc3RlcnMuCmNvbnN0IEVDX0FQUF9OQU1FX0ZPUl9VQSA9ICdWZXJzaW9uLzE3LjQgU2FmYXJpLzYwNS4xLjE1JzsKCi8vIHYzMjU6IEVpZ2VuZSBNZW7DvGxlaXN0ZS4gT2huZSBzaWUgZ2lidCBlcyBrZWluZSBUYXN0ZW5rw7xyemVsIOKAkyB1bmQg4oyYViBicmF1Y2h0Ci8vIG1hbiBpbSBMb2dpbmZlbnN0ZXIsIHdlbm4gZGFzIFBhc3N3b3J0IGF1cyBlaW5lbSBQYXNzd29ydG1hbmFnZXIga29tbXQuIERhcwovLyBXS1dlYlZpZXcgaXN0IGVyc3RlciBSZXNwb25kZXIgdW5kIGJlaGFuZGVsdCBwYXN0ZTovY29weTovY3V0OiBzZWxic3Q7IGRpZQovLyBNZW7DvHB1bmt0ZSByZWljaGVuIGRpZSBLw7xyemVsIG51ciBhbiBkaWUgUmVzcG9uZGVyLUtldHRlIHdlaXRlci4KZnVuY3Rpb24gaW5zdGFsbEVkaXRNZW51KCkgewogIHRyeSB7CiAgICBjb25zdCBtYWluID0gJC5OU01lbnUuYWxsb2MuaW5pdDsKICAgIGNvbnN0IGFwcEl0ZW0gPSAkLk5TTWVudUl0ZW0uYWxsb2MuaW5pdDsgbWFpbi5hZGRJdGVtKGFwcEl0ZW0pOwogICAgY29uc3QgYXBwTWVudSA9ICQuTlNNZW51LmFsbG9jLmluaXRXaXRoVGl0bGUoJCgnRW50cmV0aWVuQ29ubmVjdCcpKTsKICAgIGFwcE1lbnUuYWRkSXRlbVdpdGhUaXRsZUFjdGlvbktleUVxdWl2YWxlbnQoJCgnRmVuc3RlciBzY2hsaWXDn2VuJyksICdwZXJmb3JtQ2xvc2U6JywgJCgndycpKTsKICAgIGFwcE1lbnUuYWRkSXRlbVdpdGhUaXRsZUFjdGlvbktleUVxdWl2YWxlbnQoJCgnQmVlbmRlbicpLCAndGVybWluYXRlOicsICQoJ3EnKSk7CiAgICBhcHBJdGVtLnNldFN1Ym1lbnUoYXBwTWVudSk7CiAgICBjb25zdCBlZGl0SXRlbSA9ICQuTlNNZW51SXRlbS5hbGxvYy5pbml0OyBtYWluLmFkZEl0ZW0oZWRpdEl0ZW0pOwogICAgY29uc3QgZWRpdCA9ICQuTlNNZW51LmFsbG9jLmluaXRXaXRoVGl0bGUoJCgnQmVhcmJlaXRlbicpKTsKICAgIGVkaXQuYWRkSXRlbVdpdGhUaXRsZUFjdGlvbktleUVxdWl2YWxlbnQoJCgnQW5udWxlcicpLCAndW5kbzonLCAkKCd6JykpOwogICAgZWRpdC5hZGRJdGVtV2l0aFRpdGxlQWN0aW9uS2V5RXF1aXZhbGVudCgkKCdBdXNzY2huZWlkZW4nKSwgJ2N1dDonLCAkKCd4JykpOwogICAgZWRpdC5hZGRJdGVtV2l0aFRpdGxlQWN0aW9uS2V5RXF1aXZhbGVudCgkKCdLb3BpZXJlbicpLCAnY29weTonLCAkKCdjJykpOwogICAgZWRpdC5hZGRJdGVtV2l0aFRpdGxlQWN0aW9uS2V5RXF1aXZhbGVudCgkKCdFaW5mw7xnZW4nKSwgJ3Bhc3RlOicsICQoJ3YnKSk7CiAgICBlZGl0LmFkZEl0ZW1XaXRoVGl0bGVBY3Rpb25LZXlFcXVpdmFsZW50KCQoJ0FsbGVzIGF1c3fDpGhsZW4nKSwgJ3NlbGVjdEFsbDonLCAkKCdhJykpOwogICAgZWRpdEl0ZW0uc2V0U3VibWVudShlZGl0KTsKICAgICQuTlNBcHBsaWNhdGlvbi5zaGFyZWRBcHBsaWNhdGlvbi5zZXRNYWluTWVudShtYWluKTsKICB9IGNhdGNoIChfKSB7fQp9CgovLyB2MzI1OiBFaW5lIG5vY2ggZ8O8bHRpZ2UgZS1CaWNoZWxjaGVuLVNpdHp1bmcgYXVzIGVpbmVtIGZyw7xoZXJlbiBBcHAtU3RhcnQgd2lyZAovLyB6dXLDvGNrZ2VzY2hyaWViZW4sIEJFVk9SIGRpZSBTZWl0ZSBnZWxhZGVuIHdpcmQuIFdhciBkaWUgSUFNLVNpdHp1bmcgbmljaHQKLy8gYWJnZWxhdWZlbiwgbGFuZGV0IGRlciBCZW51dHplciBkaXJla3QgYW5nZW1lbGRldC4gRGFzIFNpdHp1bmdzLUNvb2tpZSBsZWJ0Ci8vIHNvbnN0IG51ciBpbSBTcGVpY2hlciBkaWVzZXMgUHJvemVzc2VzIHVuZCBpc3QgYmVpIGplZGVtIE5ldXN0YXJ0IHdlZyDigJMgZGFzIHdhcgovLyBkZXIgR3J1bmQsIHdhcnVtIGJpc2hlciBqZWRlcyBNYWwgZWluZSBuZXVlIEFubWVsZHVuZyBuw7Z0aWcgd2FyLgpmdW5jdGlvbiBpbmplY3RDb29raWVzVGhlbkxvYWQod2VidmlldywgdXJsLCBjb29raWVzUGF0aCkgewogIGxldCBhcnIgPSBbXTsKICBpZiAoY29va2llc1BhdGgpIHsKICAgIHRyeSB7CiAgICAgIGNvbnN0IHJhdyA9ICQuTlNTdHJpbmcuc3RyaW5nV2l0aENvbnRlbnRzT2ZGaWxlRW5jb2RpbmdFcnJvcigkKGNvb2tpZXNQYXRoKSwgJC5OU1VURjhTdHJpbmdFbmNvZGluZywgUmVmKCkpOwogICAgICBjb25zdCB0eHQgPSBTdHJpbmcoanNWYWx1ZShyYXcpIHx8ICcnKTsKICAgICAgaWYgKHR4dCkgYXJyID0gSlNPTi5wYXJzZSh0eHQpOwogICAgfSBjYXRjaCAoXykgeyBhcnIgPSBbXTsgfQogIH0KICBjb25zdCByZXF1ZXN0ID0gJC5OU1VSTFJlcXVlc3QucmVxdWVzdFdpdGhVUkwodXJsKTsKICBpZiAoIUFycmF5LmlzQXJyYXkoYXJyKSB8fCAhYXJyLmxlbmd0aCkgeyB3ZWJ2aWV3LmxvYWRSZXF1ZXN0KHJlcXVlc3QpOyByZXR1cm47IH0KICBsZXQgc3RvcmUgPSBudWxsOwogIHRyeSB7IHN0b3JlID0gd2Vidmlldy5jb25maWd1cmF0aW9uLndlYnNpdGVEYXRhU3RvcmUuaHR0cENvb2tpZVN0b3JlOyB9IGNhdGNoIChfKSB7IHN0b3JlID0gbnVsbDsgfQogIGlmICghc3RvcmUgfHwgdHlwZW9mIHN0b3JlLnNldENvb2tpZUNvbXBsZXRpb25IYW5kbGVyICE9PSAnZnVuY3Rpb24nKSB7IHdlYnZpZXcubG9hZFJlcXVlc3QocmVxdWVzdCk7IHJldHVybjsgfQogIGxldCByZW1haW5pbmcgPSBhcnIubGVuZ3RoOwogIGxldCBsb2FkZWQgPSBmYWxzZTsKICBjb25zdCBsb2FkT25jZSA9ICgpID0+IHsgaWYgKCFsb2FkZWQpIHsgbG9hZGVkID0gdHJ1ZTsgdHJ5IHsgd2Vidmlldy5sb2FkUmVxdWVzdChyZXF1ZXN0KTsgfSBjYXRjaCAoXykge30gfSB9OwogIGNvbnN0IGRvbmUgPSAoKSA9PiB7IHJlbWFpbmluZyAtPSAxOyBpZiAocmVtYWluaW5nIDw9IDApIGxvYWRPbmNlKCk7IH07CiAgLy8gU2ljaGVyaGVpdHNuZXR6OiBmYWxscyBlaW4gQ29tcGxldGlvbi1IYW5kbGVyIGF1c2JsZWlidCwgd2lyZCB0cm90emRlbSBnZWxhZGVuLgogICQuTlNUaW1lci5zY2hlZHVsZWRUaW1lcldpdGhUaW1lSW50ZXJ2YWxSZXBlYXRzQmxvY2soMi4wLCBmYWxzZSwgZnVuY3Rpb24oXykgeyBsb2FkT25jZSgpOyB9KTsKICBhcnIuZm9yRWFjaChmdW5jdGlvbihjKSB7CiAgICB0cnkgewogICAgICBjb25zdCBwcm9wcyA9ICQuTlNNdXRhYmxlRGljdGlvbmFyeS5hbGxvYy5pbml0OwogICAgICBwcm9wcy5zZXRPYmplY3RGb3JLZXkoJChTdHJpbmcoYy5uYW1lIHx8ICcnKSksICQuTlNIVFRQQ29va2llTmFtZSk7CiAgICAgIHByb3BzLnNldE9iamVjdEZvcktleSgkKFN0cmluZyhjLnZhbHVlIHx8ICcnKSksICQuTlNIVFRQQ29va2llVmFsdWUpOwogICAgICBwcm9wcy5zZXRPYmplY3RGb3JLZXkoJChTdHJpbmcoYy5kb21haW4gfHwgJ3NzbC5lZHVjYXRpb24ubHUnKSksICQuTlNIVFRQQ29va2llRG9tYWluKTsKICAgICAgcHJvcHMuc2V0T2JqZWN0Rm9yS2V5KCQoU3RyaW5nKGMucGF0aCB8fCAnLycpKSwgJC5OU0hUVFBDb29raWVQYXRoKTsKICAgICAgaWYgKGMuc2VjdXJlKSBwcm9wcy5zZXRPYmplY3RGb3JLZXkoJCgnVFJVRScpLCAkLk5TSFRUUENvb2tpZVNlY3VyZSk7CiAgICAgIGNvbnN0IGNvb2tpZSA9ICQuTlNIVFRQQ29va2llLmNvb2tpZVdpdGhQcm9wZXJ0aWVzKHByb3BzKTsKICAgICAgaWYgKGNvb2tpZSAmJiAhY29va2llLmlzTmlsKCkpIHsgc3RvcmUuc2V0Q29va2llQ29tcGxldGlvbkhhbmRsZXIoY29va2llLCBmdW5jdGlvbigpIHsgZG9uZSgpOyB9KTsgfQogICAgICBlbHNlIHsgZG9uZSgpOyB9CiAgICB9IGNhdGNoIChfKSB7IGRvbmUoKTsgfQogIH0pOwp9CgpmdW5jdGlvbiBqc1ZhbHVlKHZhbHVlKSB7CiAgdHJ5IHsKICAgIGlmICh2YWx1ZSA9PT0gdW5kZWZpbmVkIHx8IHZhbHVlID09PSBudWxsKSByZXR1cm4gbnVsbDsKICAgIGlmICh2YWx1ZS5qcyAhPT0gdW5kZWZpbmVkKSByZXR1cm4gdmFsdWUuanM7CiAgfSBjYXRjaCAoXykge30KICB0cnkgeyByZXR1cm4gT2JqQy5kZWVwVW53cmFwKHZhbHVlKTsgfSBjYXRjaCAoXykge30KICB0cnkgeyByZXR1cm4gT2JqQy51bndyYXAodmFsdWUpOyB9IGNhdGNoIChfKSB7fQogIHJldHVybiBTdHJpbmcodmFsdWUpOwp9CgovLyB2MzEzOiBSZXR1cm5zIGEgdXNhYmxlIG1lc3NhZ2UgZm9yIGEgUkVBTCBlcnJvciwgYW5kICcnIGZvciBhbiBPYmpDIG5pbCB0aGF0Ci8vIEpYQSBoYW5kcyBvdmVyIGFzIGEgdHJ1dGh5IHdyYXBwZXIgKGl0cyBTdHJpbmcoKSBmb3JtIGlzICJbaWQgbmlsXSIpLgpmdW5jdGlvbiBlcnJvclRleHQoZXJyKSB7CiAgaWYgKCFlcnIpIHJldHVybiAnJzsKICBsZXQgZGVzYyA9IG51bGw7CiAgdHJ5IHsgZGVzYyA9IGpzVmFsdWUoZXJyLmxvY2FsaXplZERlc2NyaXB0aW9uKTsgfSBjYXRjaCAoXykgeyBkZXNjID0gbnVsbDsgfQogIGlmIChkZXNjICE9PSBudWxsICYmIGRlc2MgIT09IHVuZGVmaW5lZCkgewogICAgY29uc3QgdGV4dCA9IFN0cmluZyhkZXNjKS50cmltKCk7CiAgICByZXR1cm4gKCF0ZXh0IHx8IHRleHQgPT09ICdudWxsJyB8fCB0ZXh0ID09PSAndW5kZWZpbmVkJykgPyAnJyA6IHRleHQ7CiAgfQogIGxldCByYXcgPSAnJzsKICB0cnkgeyByYXcgPSBTdHJpbmcoZXJyKS50cmltKCk7IH0gY2F0Y2ggKF8pIHsgcmF3ID0gJyc7IH0KICBpZiAoIXJhdyB8fCByYXcgPT09ICdbaWQgbmlsXScgfHwgcmF3ID09PSAnbnVsbCcgfHwgcmF3ID09PSAndW5kZWZpbmVkJykgcmV0dXJuICcnOwogIHJldHVybiByYXc7Cn0KCmZ1bmN0aW9uIHdyaXRlU3RhdGUob2JqKSB7CiAgdHJ5IHsKICAgIG9iaiA9IG9iaiB8fCB7fTsKICAgIGlmICghb2JqLnVwZGF0ZWRBdCkgb2JqLnVwZGF0ZWRBdCA9IG5ldyBEYXRlKCkudG9JU09TdHJpbmcoKTsKICAgIGNvbnN0IHRleHQgPSAkKEpTT04uc3RyaW5naWZ5KG9iaikpOwogICAgY29uc3QgZXJyID0gUmVmKCk7CiAgICBjb25zdCBvayA9IHRleHQud3JpdGVUb0ZpbGVBdG9taWNhbGx5RW5jb2RpbmdFcnJvcigkKEVDX1NUQVRFX1BBVEgpLCB0cnVlLCAkLk5TVVRGOFN0cmluZ0VuY29kaW5nLCBlcnIpOwogICAgcmV0dXJuIEJvb2xlYW4ob2spOwogIH0gY2F0Y2ggKF8pIHsKICAgIHJldHVybiBmYWxzZTsKICB9Cn0KCmZ1bmN0aW9uIGZpbmlzaFdpdGhFcnJvcihtZXNzYWdlLCBkZXRhaWwpIHsKICBpZiAoRUNfRklOSVNIRUQpIHJldHVybjsKICBFQ19GSU5JU0hFRCA9IHRydWU7CiAgd3JpdGVTdGF0ZSh7CiAgICBzdGF0dXM6ICdlcnJvcicsCiAgICBlcnJvcjogU3RyaW5nKG1lc3NhZ2UgfHwgJ1dLV2ViVmlldyBlcnJvcicpLAogICAgZGV0YWlsOiBTdHJpbmcoZGV0YWlsIHx8ICcnKSwKICAgIHN0YXJ0ZWRBdDogbmV3IERhdGUoRUNfU1RBUlRFRF9BVCkudG9JU09TdHJpbmcoKQogIH0pOwogIHRyeSB7IGlmIChFQ19USU1FUikgRUNfVElNRVIuaW52YWxpZGF0ZTsgfSBjYXRjaCAoXykge30KICB0cnkgeyBpZiAoRUNfV0lORE9XKSBFQ19XSU5ET1cub3JkZXJPdXQobnVsbCk7IH0gY2F0Y2ggKF8pIHt9CiAgJC5OU1RocmVhZC5zbGVlcEZvclRpbWVJbnRlcnZhbCgwLjEyKTsKICB0cnkgeyBFQ19BUFAudGVybWluYXRlKG51bGwpOyB9IGNhdGNoIChfKSB7fQp9CgpmdW5jdGlvbiBlZHVjYXRpb25Db29raWVzKGNvb2tpZXMpIHsKICBjb25zdCBvdXQgPSBbXTsKICB0cnkgewogICAgY29uc3QgY291bnQgPSBOdW1iZXIoY29va2llcy5jb3VudCB8fCAwKTsKICAgIGZvciAobGV0IGkgPSAwOyBpIDwgY291bnQ7IGkrKykgewogICAgICBjb25zdCBjID0gY29va2llcy5vYmplY3RBdEluZGV4KGkpOwogICAgICBjb25zdCBkb21haW4gPSBTdHJpbmcoanNWYWx1ZShjLmRvbWFpbikgfHwgJycpOwogICAgICBjb25zdCBuYW1lID0gU3RyaW5nKGpzVmFsdWUoYy5uYW1lKSB8fCAnJyk7CiAgICAgIGlmICghbmFtZSB8fCBkb21haW4udG9Mb3dlckNhc2UoKS5pbmRleE9mKCdlZHVjYXRpb24ubHUnKSA8IDApIGNvbnRpbnVlOwogICAgICBvdXQucHVzaCh7CiAgICAgICAgbmFtZTogbmFtZSwKICAgICAgICB2YWx1ZTogU3RyaW5nKGpzVmFsdWUoYy52YWx1ZSkgfHwgJycpLAogICAgICAgIGRvbWFpbjogZG9tYWluLAogICAgICAgIHBhdGg6IFN0cmluZyhqc1ZhbHVlKGMucGF0aCkgfHwgJy8nKSwKICAgICAgICBzZWN1cmU6IEJvb2xlYW4oanNWYWx1ZShjLnNlY3VyZSkpLAogICAgICAgIGh0dHBPbmx5OiBCb29sZWFuKGpzVmFsdWUoYy5IVFRQT25seSkpCiAgICAgIH0pOwogICAgfQogIH0gY2F0Y2ggKF8pIHt9CiAgcmV0dXJuIG91dDsKfQoKZnVuY3Rpb24gZmluYWxpemVQYXlsb2FkKHBheWxvYWQsIHBhZ2VVcmwpIHsKICBpZiAoRUNfRklOSVNIRUQpIHJldHVybjsKICBFQ19GSU5JU0hFRCA9IHRydWU7CgogIC8vIHYzMTM6IFRoZSBXS0hUVFBDb29raWVTdG9yZSBzZWxlY3RvciBpcyBgZ2V0QWxsQ29va2llczpgLCBzbyB0aGUgSlhBIG5hbWUgaXMKICAvLyBgZ2V0QWxsQ29va2llc2AuIFRoZSBwcmV2aW91cyBgZ2V0QWxsQ29va2llc1dpdGhDb21wbGV0aW9uSGFuZGxlcmAgZGlkIG5vdAogIC8vIGV4aXN0IGFuZCByYWlzZWQgYW4gdW5jYXVnaHQgTlNFeGNlcHRpb24gdGhhdCBraWxsZWQgdGhlIHdob2xlIGxvZ2luIHdpbmRvdy4KICAvLyBJdCBuZXZlciBzdXJmYWNlZCBiZWNhdXNlIHRoZSBuaWwtZXJyb3IgYnVnIGFib3ZlIG1lYW50IHRoaXMgbGluZSB3YXMgbmV2ZXIKICAvLyByZWFjaGVkLiBLZXB0IGJlaGluZCBhIHRyeS9jYXRjaCBzbyBhIGZ1dHVyZSBBUEkgY2hhbmdlIGRlZ3JhZGVzIGludG8gYQogIC8vIHJlYWRhYmxlIGVycm9yIHN0YXRlIGluc3RlYWQgb2YgYSBzaWxlbnQgY3Jhc2guCiAgbGV0IHN0b3JlID0gbnVsbDsKICB0cnkgewogICAgc3RvcmUgPSBFQ19XRUJWSUVXLmNvbmZpZ3VyYXRpb24ud2Vic2l0ZURhdGFTdG9yZS5odHRwQ29va2llU3RvcmU7CiAgfSBjYXRjaCAoZSkgewogICAgZmluaXNoV2l0aEVycm9yKCdUaGUgZS1CaWNoZWxjaGVuIHNlc3Npb24gY291bGQgbm90IGJlIHJlYWQuJywgU3RyaW5nKGUpKTsKICAgIHJldHVybjsKICB9CiAgaWYgKCFzdG9yZSB8fCB0eXBlb2Ygc3RvcmUuZ2V0QWxsQ29va2llcyAhPT0gJ2Z1bmN0aW9uJykgewogICAgZmluaXNoV2l0aEVycm9yKCdUaGUgZS1CaWNoZWxjaGVuIHNlc3Npb24gY291bGQgbm90IGJlIHJlYWQuJywgJ1dLSFRUUENvb2tpZVN0b3JlLmdldEFsbENvb2tpZXMgdW5hdmFpbGFibGUnKTsKICAgIHJldHVybjsKICB9CiAgc3RvcmUuZ2V0QWxsQ29va2llcyhmdW5jdGlvbihjb29raWVzKSB7CiAgICBsZXQgY29va2llUm93cyA9IGVkdWNhdGlvbkNvb2tpZXMoY29va2llcyk7CiAgICBFQ19XRUJWSUVXLmV2YWx1YXRlSmF2YVNjcmlwdENvbXBsZXRpb25IYW5kbGVyKCQoJ25hdmlnYXRvci51c2VyQWdlbnQgfHwgIk1vemlsbGEvNS4wIicpLCBmdW5jdGlvbih1YVZhbHVlLCB1YUVycm9yKSB7CiAgICAgIGNvbnN0IHVhID0gU3RyaW5nKGpzVmFsdWUodWFWYWx1ZSkgfHwgJ01vemlsbGEvNS4wIChNYWNpbnRvc2gpIEFwcGxlV2ViS2l0Jyk7CiAgICAgIGNvbnN0IG1lcmdlZCA9IHt9OwogICAgICBjb29raWVSb3dzLmZvckVhY2goZnVuY3Rpb24oYykgeyBtZXJnZWRbYy5uYW1lXSA9IGMudmFsdWU7IH0pOwogICAgICBjb25zdCBjb29raWVIZWFkZXIgPSBPYmplY3Qua2V5cyhtZXJnZWQpLm1hcChmdW5jdGlvbihrKSB7IHJldHVybiBrICsgJz0nICsgbWVyZ2VkW2tdOyB9KS5qb2luKCc7ICcpOwogICAgICBpZiAoIWNvb2tpZUhlYWRlcikgewogICAgICAgIEVDX0ZJTklTSEVEID0gZmFsc2U7CiAgICAgICAgd3JpdGVTdGF0ZSh7CiAgICAgICAgICBzdGF0dXM6ICd3YWl0aW5nJywKICAgICAgICAgIHN0YWdlOiAnc2Vzc2lvbicsCiAgICAgICAgICBtZXNzYWdlOiAnZS1CaWNoZWxjaGVuIGlzIG9wZW4sIGJ1dCB0aGUgYXV0aGVudGljYXRlZCBzZXNzaW9uIGlzIG5vdCBhdmFpbGFibGUgeWV0LicsCiAgICAgICAgICBwYWdlVXJsOiBTdHJpbmcocGFnZVVybCB8fCAnJykKICAgICAgICB9KTsKICAgICAgICByZXR1cm47CiAgICAgIH0KICAgICAgdHJ5IHsgaWYgKEVDX1RJTUVSKSBFQ19USU1FUi5pbnZhbGlkYXRlOyB9IGNhdGNoIChfKSB7fQogICAgICB3cml0ZVN0YXRlKHsKICAgICAgICBzdGF0dXM6ICdyZWFkeScsCiAgICAgICAgcGFnZVVybDogU3RyaW5nKHBhZ2VVcmwgfHwgJycpLAogICAgICAgIGRhdGE6IHBheWxvYWQsCiAgICAgICAgc2Vzc2lvbjogewogICAgICAgICAgY29va2llSGVhZGVyOiBjb29raWVIZWFkZXIsCiAgICAgICAgICBjb29raWVOYW1lczogT2JqZWN0LmtleXMobWVyZ2VkKS5zb3J0KCksCiAgICAgICAgICBjb29raWVzOiBjb29raWVSb3dzLAogICAgICAgICAgdXNlckFnZW50OiB1YSwKICAgICAgICAgIGNhcHR1cmVkQXQ6IG5ldyBEYXRlKCkudG9JU09TdHJpbmcoKSwKICAgICAgICAgIHRhcmdldFVybDogU3RyaW5nKHBhZ2VVcmwgfHwgJycpLAogICAgICAgICAgYnJvd3NlcjogJ21hY09TIFdLV2ViVmlldyB2MzMyJwogICAgICAgIH0sCiAgICAgICAgZW5naW5lOiAnV0tXZWJWaWV3LXYzMzInLAogICAgICAgIHN0YXJ0ZWRBdDogbmV3IERhdGUoRUNfU1RBUlRFRF9BVCkudG9JU09TdHJpbmcoKQogICAgICB9KTsKICAgICAgdHJ5IHsgRUNfV0lORE9XLm9yZGVyT3V0KG51bGwpOyB9IGNhdGNoIChfKSB7fQogICAgICAkLk5TVGhyZWFkLnNsZWVwRm9yVGltZUludGVydmFsKDAuMTgpOwogICAgICB0cnkgeyBFQ19BUFAudGVybWluYXRlKG51bGwpOyB9IGNhdGNoIChfKSB7fQogICAgfSk7CiAgfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkQ29udHJvbGxlclNjcmlwdCgpIHsKICAvLyBUaGUgcmVhZCBleHByZXNzaW9uIGlzIGFuIGFzeW5jIElJRkUuIFdlIGxhdW5jaCBpdCBvbmNlIGFuZCBzdG9yZSBpdHMgcmVzdWx0CiAgLy8gaW4gYSBwYWdlLWdsb2JhbCBvYmplY3QuIGV2YWx1YXRlSmF2YVNjcmlwdCBpdHNlbGYgb25seSByZXR1cm5zIGEgc3luY2hyb25vdXMKICAvLyBzdGF0dXMgc25hcHNob3QsIHdoaWNoIHdvcmtzIG9uIG9sZGVyIFdLV2ViVmlldyB2ZXJzaW9ucyBhcyB3ZWxsLgogIHJldHVybiBgKCgpID0+IHsKICAgIGNvbnN0IGhyZWYgPSBTdHJpbmcobG9jYXRpb24uaHJlZiB8fCAnJyk7CiAgICBjb25zdCBvbkViID0gaHJlZi5pbmRleE9mKCcvZWJpY2hlbGNoZW4vYXBwLycpID49IDA7CiAgICBpZiAoIW9uRWIpIHJldHVybiBKU09OLnN0cmluZ2lmeSh7cGhhc2U6J2xvZ2luJyx1cmw6aHJlZn0pOwogICAgaWYgKCF3aW5kb3cuX19lbnRyZXRpZW5Db25uZWN0TmF0aXZlMzMyKSB7CiAgICAgIHdpbmRvdy5fX2VudHJldGllbkNvbm5lY3ROYXRpdmUzMzIgPSB7cGhhc2U6J3N0YXJ0aW5nJyx1cmw6aHJlZixlcnJvcjonJyxkYXRhOm51bGwsc3RhcnRlZEF0OkRhdGUubm93KCl9OwogICAgICBjb25zdCBzID0gd2luZG93Ll9fZW50cmV0aWVuQ29ubmVjdE5hdGl2ZTMzMjsKICAgICAgcy5waGFzZSA9ICdyZWFkaW5nJzsKICAgICAgUHJvbWlzZS5yZXNvbHZlKCR7RUNfUkVBRF9FWFBSRVNTSU9OfSkKICAgICAgICAudGhlbih2ID0+IHsgcy5kYXRhID0gdjsgcy5waGFzZSA9ICdyZWFkeSc7IHMudXJsID0gU3RyaW5nKGxvY2F0aW9uLmhyZWYgfHwgaHJlZik7IH0pCiAgICAgICAgLmNhdGNoKGUgPT4gewogICAgICAgICAgcy5lcnJvciA9IFN0cmluZyhlICYmIChlLm1lc3NhZ2UgfHwgZSkgfHwgJ3Vua25vd24gZXJyb3InKTsKICAgICAgICAgIHMucGhhc2UgPSAnd2FpdGluZyc7CiAgICAgICAgICBzLnVybCA9IFN0cmluZyhsb2NhdGlvbi5ocmVmIHx8IGhyZWYpOwogICAgICAgICAgc2V0VGltZW91dCgoKSA9PiB7IHRyeSB7IGRlbGV0ZSB3aW5kb3cuX19lbnRyZXRpZW5Db25uZWN0TmF0aXZlMzMyOyB9IGNhdGNoIChfKSB7fSB9LCAxMjAwKTsKICAgICAgICB9KTsKICAgIH0KICAgIGNvbnN0IHMgPSB3aW5kb3cuX19lbnRyZXRpZW5Db25uZWN0TmF0aXZlMzMyOwogICAgcmV0dXJuIEpTT04uc3RyaW5naWZ5KHtwaGFzZTpzLnBoYXNlLHVybDpTdHJpbmcocy51cmx8fGhyZWYpLGVycm9yOlN0cmluZyhzLmVycm9yfHwnJyksZGF0YTpzLmRhdGF8fG51bGwsYWdlOkRhdGUubm93KCktTnVtYmVyKHMuc3RhcnRlZEF0fHxEYXRlLm5vdygpKX0pOwogIH0pKClgOwp9CgpmdW5jdGlvbiBwb2xsV2ViVmlldygpIHsKICBpZiAoRUNfRklOSVNIRUQgfHwgRUNfQlVTWSB8fCAhRUNfV0VCVklFVykgcmV0dXJuOwogIHRyeSB7CiAgICBpZiAoRUNfV0lORE9XICYmICFCb29sZWFuKEVDX1dJTkRPVy5pc1Zpc2libGUpKSB7CiAgICAgIEVDX0ZJTklTSEVEID0gdHJ1ZTsKICAgICAgd3JpdGVTdGF0ZSh7c3RhdHVzOidjbG9zZWQnLCBtZXNzYWdlOidMb2dpbiB3aW5kb3cgY2xvc2VkIGJ5IHVzZXIuJ30pOwogICAgICB0cnkgeyBFQ19BUFAudGVybWluYXRlKG51bGwpOyB9IGNhdGNoIChfKSB7fQogICAgICByZXR1cm47CiAgICB9CiAgfSBjYXRjaCAoXykge30KCiAgRUNfQlVTWSA9IHRydWU7CiAgY29uc3Qgc2NyaXB0ID0gYnVpbGRDb250cm9sbGVyU2NyaXB0KCk7CiAgRUNfV0VCVklFVy5ldmFsdWF0ZUphdmFTY3JpcHRDb21wbGV0aW9uSGFuZGxlcigkKHNjcmlwdCksIGZ1bmN0aW9uKHJlc3VsdCwgZXJyb3IpIHsKICAgIEVDX0JVU1kgPSBmYWxzZTsKICAgIGlmIChFQ19GSU5JU0hFRCkgcmV0dXJuOwogICAgLy8gdjMxMzogQSBuaWwgTlNFcnJvciBhcnJpdmVzIGluIEpYQSBhcyBhIFRSVVRIWSB3cmFwcGVyIG9iamVjdCwgc28gdGhlIG9sZAogICAgLy8gYGlmIChlcnJvcilgIHRvb2sgdGhlIGZhaWx1cmUgYnJhbmNoIG9uIGV2ZXJ5IHNpbmdsZSBzdWNjZXNzZnVsIGNhbGwgYW5kCiAgICAvLyB0aGUgcmVzdWx0IHdhcyBuZXZlciBwYXJzZWQg4oCUIHRoZSBsb2dpbiB3aW5kb3cgc3RheWVkIG9wZW4gZm9yZXZlciBhbmQgdGhlCiAgICAvLyBzdGF0ZSBmaWxlIHdhcyBzdHVjayBvbiBzdGFnZSAibmF2aWdhdGlvbiIgLyBkZXRhaWwgIltpZCBuaWxdIi4KICAgIC8vIFRoZSByZXN1bHQgbm93IGRlY2lkZXM7IHRoZSBlcnJvciBvYmplY3QgaXMgb25seSB1c2VkIGZvciBpdHMgbWVzc2FnZS4KICAgIGxldCBvdXRlciA9IG51bGw7CiAgICB0cnkgeyBvdXRlciA9IEpTT04ucGFyc2UoU3RyaW5nKGpzVmFsdWUocmVzdWx0KSB8fCAne30nKSk7IH0gY2F0Y2ggKF8pIHsgb3V0ZXIgPSBudWxsOyB9CiAgICBpZiAoIW91dGVyIHx8ICFvdXRlci5waGFzZSkgewogICAgICBjb25zdCBkZXRhaWwgPSBlcnJvclRleHQoZXJyb3IpOwogICAgICBpZiAoZGV0YWlsKSB3cml0ZVN0YXRlKHtzdGF0dXM6J3dhaXRpbmcnLCBzdGFnZTonbmF2aWdhdGlvbicsIGRldGFpbDpkZXRhaWwsIHBhZ2VVcmw6Jyd9KTsKICAgICAgcmV0dXJuOwogICAgfQogICAgY29uc3QgcGhhc2UgPSBTdHJpbmcob3V0ZXIucGhhc2UgfHwgJ3dhaXRpbmcnKTsKICAgIGNvbnN0IHBhZ2VVcmwgPSBTdHJpbmcob3V0ZXIudXJsIHx8ICcnKTsKICAgIGlmIChwaGFzZSA9PT0gJ2xvZ2luJykgewogICAgICB3cml0ZVN0YXRlKHtzdGF0dXM6J29wZW4nLCBzdGFnZTonbG9naW4nLCBwYWdlVXJsOnBhZ2VVcmwsIGVuZ2luZTonV0tXZWJWaWV3J30pOwogICAgICByZXR1cm47CiAgICB9CiAgICBpZiAocGhhc2UgPT09ICdyZWFkaW5nJyB8fCBwaGFzZSA9PT0gJ3N0YXJ0aW5nJykgewogICAgICB3cml0ZVN0YXRlKHtzdGF0dXM6J29wZW4nLCBzdGFnZToncmVhZGluZycsIHBhZ2VVcmw6cGFnZVVybCwgZW5naW5lOidXS1dlYlZpZXcnfSk7CiAgICAgIHJldHVybjsKICAgIH0KICAgIGlmIChwaGFzZSA9PT0gJ3dhaXRpbmcnKSB7CiAgICAgIHdyaXRlU3RhdGUoe3N0YXR1czonb3BlbicsIHN0YWdlOidsb2FkaW5nJywgcGFnZVVybDpwYWdlVXJsLCBkZXRhaWw6U3RyaW5nKG91dGVyLmVycm9yIHx8ICcnKSwgZW5naW5lOidXS1dlYlZpZXcnfSk7CiAgICAgIHJldHVybjsKICAgIH0KICAgIGlmIChwaGFzZSA9PT0gJ3JlYWR5JyAmJiBvdXRlci5kYXRhKSB7CiAgICAgIGxldCBwYXlsb2FkID0gbnVsbDsKICAgICAgdHJ5IHsKICAgICAgICBwYXlsb2FkID0gdHlwZW9mIG91dGVyLmRhdGEgPT09ICdzdHJpbmcnID8gSlNPTi5wYXJzZShvdXRlci5kYXRhKSA6IG91dGVyLmRhdGE7CiAgICAgIH0gY2F0Y2ggKGUpIHsKICAgICAgICBmaW5pc2hXaXRoRXJyb3IoJ1RoZSBlLUJpY2hlbGNoZW4gZGF0YSBjb3VsZCBub3QgYmUgZGVjb2RlZC4nLCBTdHJpbmcoZSkpOwogICAgICAgIHJldHVybjsKICAgICAgfQogICAgICBmaW5hbGl6ZVBheWxvYWQocGF5bG9hZCwgcGFnZVVybCk7CiAgICB9CiAgfSk7Cn0KCmZ1bmN0aW9uIHJ1bihhcmd2KSB7CiAgdHJ5IHsKICAgIGlmICghYXJndiB8fCBhcmd2Lmxlbmd0aCA8IDMpIHRocm93IG5ldyBFcnJvcignTWlzc2luZyBuYXRpdmUgbG9naW4gYXJndW1lbnRzLicpOwogICAgRUNfU1RBVEVfUEFUSCA9IFN0cmluZyhhcmd2WzBdKTsKICAgIGNvbnN0IGV4cHJlc3Npb25QYXRoID0gU3RyaW5nKGFyZ3ZbMV0pOwogICAgRUNfU1RBUlRfVVJMID0gU3RyaW5nKGFyZ3ZbMl0pOwogICAgLy8gdjMyNTogT3B0aW9uYWxlciA0LiBQYXJhbWV0ZXIg4oCTIFBmYWQgenUgZGVuIHp1csO8Y2t6dXNjaHJlaWJlbmRlbiBDb29raWVzLgogICAgRUNfUkVTVE9SRV9DT09LSUVTX1BBVEggPSAoYXJndi5sZW5ndGggPiAzKSA/IFN0cmluZyhhcmd2WzNdKSA6ICcnOwogICAgY29uc3QgcmVhZEVyciA9IFJlZigpOwogICAgY29uc3QgcmVhZE9iaiA9ICQuTlNTdHJpbmcuc3RyaW5nV2l0aENvbnRlbnRzT2ZGaWxlRW5jb2RpbmdFcnJvcigkKGV4cHJlc3Npb25QYXRoKSwgJC5OU1VURjhTdHJpbmdFbmNvZGluZywgcmVhZEVycik7CiAgICBFQ19SRUFEX0VYUFJFU1NJT04gPSBTdHJpbmcoanNWYWx1ZShyZWFkT2JqKSB8fCAnJyk7CiAgICBpZiAoIUVDX1JFQURfRVhQUkVTU0lPTikgdGhyb3cgbmV3IEVycm9yKCdUaGUgZS1CaWNoZWxjaGVuIHJlYWQgc2NyaXB0IGlzIGVtcHR5LicpOwoKICAgIHdyaXRlU3RhdGUoe3N0YXR1czonc3RhcnRpbmcnLCBlbmdpbmU6J1dLV2ViVmlldycsIHN0YXJ0ZWRBdDpuZXcgRGF0ZShFQ19TVEFSVEVEX0FUKS50b0lTT1N0cmluZygpfSk7CgogICAgRUNfQVBQID0gJC5OU0FwcGxpY2F0aW9uLnNoYXJlZEFwcGxpY2F0aW9uOwogICAgLy8gdjMyNTogQWxzIFp1YmVow7ZyIHN0YXR0IGFscyB2b2xsd2VydGlnZXMgUHJvZ3JhbW0gYW5tZWxkZW4uIERhbWl0CiAgICAvLyB2ZXJzY2h3aW5kZXQgZGFzIHp1c8OkdHpsaWNoZSBEb2NrLVN5bWJvbDsgZGFzIEZlbnN0ZXIgYmxlaWJ0IHZvbGwgYmVkaWVuYmFyCiAgICAvLyAoZ2VtZXNzZW46IEtleS1XaW5kb3csIEVpbmdhYmVmb2t1cywg4oyYViBmdW5rdGlvbmllcmVuKS4gRGVyIFByb3plc3NuYW1lIHdpcmQKICAgIC8vIGFscyBCZXN0ZS1CZW3DvGh1bmcgZ2VzZXR6dCDigJMgZGVuIGZldHRnZWRydWNrdGVuIE1lbsO8bGVpc3Rlbm5hbWVuIGJlc3RpbW10CiAgICAvLyBhYmVyIGRhcyBsYXVmZW5kZSBQcm9ncmFtbSAob3Nhc2NyaXB0KSwgZGFzIGzDpHNzdCBzaWNoIGhpZXIgbmljaHQgdW1iZW5lbm5lbi4KICAgIHRyeSB7ICQuTlNQcm9jZXNzSW5mby5wcm9jZXNzSW5mby5zZXRQcm9jZXNzTmFtZSgkKCdFbnRyZXRpZW5Db25uZWN0JykpOyB9IGNhdGNoIChfKSB7fQogICAgRUNfQVBQLnNldEFjdGl2YXRpb25Qb2xpY3koJC5OU0FwcGxpY2F0aW9uQWN0aXZhdGlvblBvbGljeUFjY2Vzc29yeSk7CiAgICBpbnN0YWxsRWRpdE1lbnUoKTsKCiAgICBjb25zdCByZWN0ID0gJC5OU01ha2VSZWN0KDAsIDAsIDEwODAsIDc2MCk7CiAgICBjb25zdCBzdHlsZSA9ICQuTlNXaW5kb3dTdHlsZU1hc2tUaXRsZWQgfCAkLk5TV2luZG93U3R5bGVNYXNrQ2xvc2FibGUgfCAkLk5TV2luZG93U3R5bGVNYXNrTWluaWF0dXJpemFibGUgfCAkLk5TV2luZG93U3R5bGVNYXNrUmVzaXphYmxlOwogICAgRUNfV0lORE9XID0gJC5OU1dpbmRvdy5hbGxvYy5pbml0V2l0aENvbnRlbnRSZWN0U3R5bGVNYXNrQmFja2luZ0RlZmVyKHJlY3QsIHN0eWxlLCAkLk5TQmFja2luZ1N0b3JlQnVmZmVyZWQsIGZhbHNlKTsKICAgIEVDX1dJTkRPVy5zZXRUaXRsZSgkKCdlLUJpY2hlbGNoZW4g4oCTIEVudHJldGllbkNvbm5lY3QnKSk7CiAgICBFQ19XSU5ET1cuc2V0UmVsZWFzZWRXaGVuQ2xvc2VkKGZhbHNlKTsKICAgIEVDX1dJTkRPVy5jZW50ZXI7CgogICAgY29uc3QgY29uZmlnID0gJC5XS1dlYlZpZXdDb25maWd1cmF0aW9uLmFsbG9jLmluaXQ7CiAgICBjb25maWcud2Vic2l0ZURhdGFTdG9yZSA9ICQuV0tXZWJzaXRlRGF0YVN0b3JlLmRlZmF1bHREYXRhU3RvcmU7CiAgICAvLyB2MzIwOiBPaG5lIGRpZXNlIFplaWxlIG1lbGRldCBzaWNoIGRhcyBGZW5zdGVyIGFscwogICAgLy8gICAiTW96aWxsYS81LjAgKE1hY2ludG9zaDsg4oCmKSBBcHBsZVdlYktpdC82MDUuMS4xNSAoS0hUTUwsIGxpa2UgR2Vja28pIgogICAgLy8g4oCTIG9obmUgIlZlcnNpb24v4oCmIFNhZmFyaS/igKYiLiBGw7xyIGUtQmljaGVsY2hlbiAoYnp3LiBlaW5lIHZvcmdlbGFnZXJ0ZQogICAgLy8gU2NodXR6c2NoaWNodCkgaXN0IGRhcyBrZWluIGVya2VubmJhcmVyIEJyb3dzZXI7IGRpZSBTZWl0ZSBtZWxkZXRlCiAgICAvLyBkYXJhdWZoaW4gIkVzIGtvbm50ZSBrZWluZSBWZXJiaW5kdW5nIHp1bSBTZXJ2ZXIgZXJzdGVsbHQgd2VyZGVuIi4KICAgIC8vIGFwcGxpY2F0aW9uTmFtZUZvclVzZXJBZ2VudCB3aXJkIHZvbiBXZWJLaXQgYW4gZGllIGVjaHRlIEVuZ2luZS1LZW5udW5nCiAgICAvLyBhbmdlaMOkbmd0LCBkYXMgRXJnZWJuaXMgaXN0IGVpbiB2b2xsd2VydGlnZXIgU2FmYXJpLVVzZXItQWdlbnQuCiAgICB0cnkgewogICAgICBjb25maWcuYXBwbGljYXRpb25OYW1lRm9yVXNlckFnZW50ID0gJChFQ19BUFBfTkFNRV9GT1JfVUEpOwogICAgfSBjYXRjaCAoXykge30KICAgIEVDX1dFQlZJRVcgPSAkLldLV2ViVmlldy5hbGxvYy5pbml0V2l0aEZyYW1lQ29uZmlndXJhdGlvbihyZWN0LCBjb25maWcpOwogICAgLy8gWndlaXRlciBXZWcsIGZhbGxzIGFwcGxpY2F0aW9uTmFtZUZvclVzZXJBZ2VudCBuaWNodCBncmVpZnQuCiAgICB0cnkgewogICAgICBjb25zdCB1YSA9IFN0cmluZyhqc1ZhbHVlKEVDX1dFQlZJRVcudmFsdWVGb3JLZXkoJCgndXNlckFnZW50JykpKSB8fCAnJyk7CiAgICAgIGlmICh1YSAmJiB1YS5pbmRleE9mKCdTYWZhcmkvJykgPCAwKSB7CiAgICAgICAgRUNfV0VCVklFVy5zZXRDdXN0b21Vc2VyQWdlbnQoJCh1YS50cmltKCkgKyAnICcgKyBFQ19BUFBfTkFNRV9GT1JfVUEpKTsKICAgICAgfQogICAgfSBjYXRjaCAoXykge30KICAgIEVDX1dFQlZJRVcuc2V0QWxsb3dzQmFja0ZvcndhcmROYXZpZ2F0aW9uR2VzdHVyZXModHJ1ZSk7CiAgICBFQ19XSU5ET1cuc2V0Q29udGVudFZpZXcoRUNfV0VCVklFVyk7CiAgICBFQ19XSU5ET1cubWFrZUtleUFuZE9yZGVyRnJvbnQobnVsbCk7CiAgICBFQ19BUFAuYWN0aXZhdGVJZ25vcmluZ090aGVyQXBwcyh0cnVlKTsKCiAgICBjb25zdCB1cmwgPSAkLk5TVVJMLlVSTFdpdGhTdHJpbmcoJChFQ19TVEFSVF9VUkwpKTsKICAgIGlmICghdXJsKSB0aHJvdyBuZXcgRXJyb3IoJ0ludmFsaWQgZS1CaWNoZWxjaGVuIFVSTC4nKTsKICAgIGluamVjdENvb2tpZXNUaGVuTG9hZChFQ19XRUJWSUVXLCB1cmwsIEVDX1JFU1RPUkVfQ09PS0lFU19QQVRIKTsKCiAgICBFQ19USU1FUiA9ICQuTlNUaW1lci5zY2hlZHVsZWRUaW1lcldpdGhUaW1lSW50ZXJ2YWxSZXBlYXRzQmxvY2soMC44LCB0cnVlLCBmdW5jdGlvbihfKSB7IHBvbGxXZWJWaWV3KCk7IH0pOwogICAgd3JpdGVTdGF0ZSh7c3RhdHVzOidvcGVuJywgc3RhZ2U6J2xvZ2luJywgZW5naW5lOidXS1dlYlZpZXcnLCBwYWdlVXJsOkVDX1NUQVJUX1VSTH0pOwogICAgRUNfQVBQLnJ1bjsKICB9IGNhdGNoIChlKSB7CiAgICBmaW5pc2hXaXRoRXJyb3IoJ1RoZSBtYWNPUyBsb2dpbiB3aW5kb3cgY291bGQgbm90IGJlIHN0YXJ0ZWQuJywgU3RyaW5nKGUgJiYgKGUubWVzc2FnZSB8fCBlKSB8fCBlKSk7CiAgfQp9Cg=="
MAC_WK_PROCESS: subprocess.Popen | None = None
MAC_WK_LOCK = threading.RLock()


def _mac_wk_resolve_script() -> pathlib.Path:
    """Create the WKWebView module from the embedded v332 source every time.

    This deliberately does not depend on the bundle path, App Translocation, a
    previous updater cache, or an external helper file.  Atomic replacement also
    prevents an old v307 file from being reused after a partial update.
    """
    target_dir = DATA_ROOT / "native-runtime" / "v332"
    target = target_dir / MAC_WK_SCRIPT_NAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(MAC_WK_SCRIPT_B64.encode("ascii"), validate=True)
        if len(data) < 1000 or b"EntretienConnect v332" not in data:
            raise RuntimeError("embedded v332 module is incomplete")
        expected = hashlib.sha256(data).hexdigest()
        current_ok = False
        try:
            current_ok = target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected
        except Exception:
            current_ok = False
        if not current_ok:
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(str(tmp), str(target))
        try:
            target.chmod(0o700)
        except Exception:
            pass
        if not target.is_file():
            raise RuntimeError("runtime module was not created")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("runtime module checksum mismatch")
        return target
    except Exception as exc:
        raise RuntimeError(
            "WKWebView v332 konnte nicht vorbereitet werden (" + str(target) + "): " + str(exc)
        ) from exc

def _mac_wk_read_state() -> dict:
    try:
        if not MAC_WK_STATE_FILE.exists():
            return {}
        # v312: Die Rechte werden hier von Python nachgezogen, NICHT mehr im nativen
        # Loginfenster. In v311 stand dafür ein NSFileManager-Aufruf im JXA-writeState –
        # und der sitzt im selben Pfad, der nach dem Login das Fenster schließt. Schlug er
        # fehl, blieb das Fenster offen und der Status nie « ready ». Python pollt diese
        # Datei ohnehin im Sekundentakt, die Absicherung greift also genauso.
        try:
            if stat.S_IMODE(MAC_WK_STATE_FILE.stat().st_mode) != 0o600:
                MAC_WK_STATE_FILE.chmod(0o600)
        except Exception:
            pass
        raw = MAC_WK_STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _mac_wk_write_state(data: dict) -> None:
    try:
        # v311: 0600 statt Standardmaske – die Datei kann eine gültige Sitzung enthalten.
        _write_private_text(MAC_WK_STATE_FILE, json.dumps(data or {}, ensure_ascii=False))
    except Exception:
        pass


def _mac_wk_purge_stale_state() -> None:
    """Entfernt eine Übergabedatei aus einem früheren, abgebrochenen Lauf.

    v311: Bisher wurde sie nur nach einem erfolgreichen Lesen gelöscht. Nach einem
    Absturz oder « Sofort beenden » blieb ein gültiger Sitzungs-Cookie unbegrenzt
    im App-Speicher liegen – und damit auch in jedem Time-Machine-Backup.
    Beim Start ist ohnehin keine Sitzung im Speicher, die Datei ist also wertlos.
    """
    try:
        MAC_WK_STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _mac_wk_save_session(session: dict, payload: dict | None = None) -> None:
    """Sichert die e-Bichelchen-Cookies für den nächsten App-Start (v325).

    Nur die education.lu-Cookies werden gespeichert, mit Zeitstempel. Datei 0600.
    Der cookieHeader wird bewusst NICHT abgelegt – für das Zurückschreiben ins
    Fenster genügen die Einzel-Cookies.
    """
    try:
        cookies = session.get("cookies") if isinstance(session, dict) else None
        if not isinstance(cookies, list) or not cookies:
            return
        slim = []
        for c in cookies:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            slim.append({
                "name": c.get("name"),
                "value": c.get("value", ""),
                "domain": c.get("domain", "ssl.education.lu"),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure")),
            })
        if not slim:
            return
        # v330: Auch merken, WO die Sitzung zuletzt gültig war. Beim nächsten Start
        # wird das Fenster dorthin geschickt statt auf die Anmeldeseite – sonst
        # startet e-Bichelchen die IAM-Anmeldung neu, obwohl die Cookies noch gelten.
        page_url = _mac_wk_valid_app_url(session.get("targetUrl"))
        entry = {
            "savedAt": time.time(),
            "cookies": slim,
            "pageUrl": page_url,
            # v332: für das Weiterlesen ohne Fenster wird derselbe User-Agent gebraucht.
            "userAgent": str(session.get("userAgent") or ""),
        }
        # v332: Die Kategorie « Nachricht » findet nur das Fenster – es entdeckt den
        # richtigen Endpunkt zur Laufzeit in der Seite. Python kann ihn nicht erraten
        # (alle bekannten Varianten antworten mit 404). Also merken wir uns das
        # Ergebnis pro Klasse und benutzen es beim stillen Weitermachen wieder.
        if isinstance(payload, dict):
            group = payload.get("group") or {}
            subject = payload.get("messageSubject") or {}
            if group.get("id") and subject.get("id"):
                entry["knownSubjects"] = {
                    str(group["id"]): {
                        "messageSubject": subject,
                        "subjects": payload.get("subjects") or [],
                    }
                }
        _write_private_text(MAC_WK_SAVED_SESSION_FILE, json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass


def _mac_wk_valid_app_url(value) -> str:
    """Akzeptiert nur echte e-Bichelchen-App-Adressen – und nie die Anmeldeseite."""
    url = str(value or "").strip()
    if not url.startswith("https://ssl.education.lu/ebichelchen/app/"):
        return ""
    rest = url[len("https://ssl.education.lu/ebichelchen/app/"):].lower()
    if rest == "login" or rest.startswith(("login/", "login?", "login#")):
        return ""
    return url


def has_saved_session() -> bool:
    """v331: Liegt eine noch gültige, gemerkte e-Bichelchen-Sitzung vor?

    Die Oberfläche fragt das beim Start ab und verbindet dann von selbst, statt
    auf einen Klick zu warten – so ist e-Bichelchen wie Microsoft gleich grün.
    Geprüft wird nur, ob Cookies gespeichert und nicht zu alt sind; ob
    education.lu sie noch akzeptiert, zeigt erst das Loginfenster.
    """
    if platform.system().lower() != "darwin":
        return False
    try:
        if not MAC_WK_SAVED_SESSION_FILE.exists():
            return False
        data = json.loads(MAC_WK_SAVED_SESSION_FILE.read_text(encoding="utf-8"))
        cookies = data.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            return False
        return (time.time() - float(data.get("savedAt") or 0)) <= EB_SESSION_MAX_AGE
    except Exception:
        return False


def resume_saved_session(selected_group_id: int | None = None) -> dict:
    """v332: Klassen ohne Loginfenster lesen, allein aus der gemerkten Sitzung.

    Damit ist e-Bichelchen beim Start still verbunden – wie Microsoft, dessen Token
    ebenfalls einfach von der Platte gelesen wird. Erst wenn das hier scheitert,
    braucht es wieder das Fenster.

    Nachgemessen an einer echten Sitzung: Es genügen die Cookies für
    ssl.education.lu (3 von 15), der Endpunkt antwortet mit HTTP 200 und JSON.
    Der frühere Fehlschlag dieses Weges (v315) ging sehr wahrscheinlich auf einen
    Ausfall von education.lu zurück, dessen HTML-Fehlerseite als « Sitzung
    abgelaufen » gewertet wurde – nicht auf fehlende Cookies.
    """
    global LATEST_SESSION, LATEST_SESSION_AT, LATEST_DATA, LATEST_AT
    if not MAC_WK_SAVED_SESSION_FILE.exists():
        raise RuntimeError("Keine gemerkte e-Bichelchen-Sitzung vorhanden.")
    data = json.loads(MAC_WK_SAVED_SESSION_FILE.read_text(encoding="utf-8"))
    cookies = data.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise RuntimeError("Die gemerkte Sitzung ist unvollständig.")
    if time.time() - float(data.get("savedAt") or 0) > EB_SESSION_MAX_AGE:
        _mac_wk_clear_saved_session()
        raise EbSessionExpired()
    session = {
        "cookies": cookies,
        "cookieHeader": "; ".join(f'{c.get("name")}={c.get("value", "")}' for c in cookies if c.get("name")),
        "cookieNames": sorted(str(c.get("name")) for c in cookies if c.get("name")),
        "userAgent": data.get("userAgent") or ACTIVE_BROWSER_USER_AGENT or "Mozilla/5.0",
    }
    payload = _read_direct_with_session(session, selected_group_id, data.get("knownSubjects"))
    payload.setdefault("source", {})
    payload["source"].update({
        "browser": "saved-session-direct",
        "engine": "WKWebView",
        "sessionCaptured": True,
        "resumed": True,
        "requiresInstalledBrowser": False,
    })
    payload["version"] = HELPER_VERSION
    with LOCK:
        LATEST_SESSION = session
        LATEST_SESSION_AT = time.strftime("%Y-%m-%d %H:%M:%S")
        LATEST_DATA = payload
        LATEST_AT = LATEST_SESSION_AT
    return payload


def _mac_wk_clear_saved_session() -> None:
    for path in (MAC_WK_SAVED_SESSION_FILE, MAC_WK_RESTORE_COOKIES_FILE):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def _mac_wk_prepare_restore_cookies() -> tuple:
    """Stellt die gespeicherte Sitzung für das Loginfenster bereit (v325/v330).

    Liefert (Cookie-Dateipfad, Start-URL). Die Start-URL ist die Seite, auf der die
    Sitzung zuletzt gültig war – dorthin zu starten ist entscheidend, weil die
    Anmeldeseite die IAM-Anmeldung neu beginnt, selbst wenn die Cookies noch gelten.
    Ohne gültige gespeicherte Sitzung kommt ("", "") zurück.
    """
    try:
        if not MAC_WK_SAVED_SESSION_FILE.exists():
            return "", ""
        data = json.loads(MAC_WK_SAVED_SESSION_FILE.read_text(encoding="utf-8"))
        saved_at = float(data.get("savedAt") or 0)
        cookies = data.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            return "", ""
        if time.time() - saved_at > EB_SESSION_MAX_AGE:
            _mac_wk_clear_saved_session()
            return "", ""
        _write_private_text(MAC_WK_RESTORE_COOKIES_FILE, json.dumps(cookies, ensure_ascii=False))
        return str(MAC_WK_RESTORE_COOKIES_FILE), _mac_wk_valid_app_url(data.get("pageUrl"))
    except Exception:
        return "", ""


def _mac_wk_process_alive() -> bool:
    with MAC_WK_LOCK:
        return bool(MAC_WK_PROCESS and MAC_WK_PROCESS.poll() is None)


def _mac_wk_terminate(mark_closed: bool = False) -> dict:
    global MAC_WK_PROCESS
    result = {"closed": False, "method": "macos-wkwebview", "alreadyClosed": False}
    with MAC_WK_LOCK:
        proc = MAC_WK_PROCESS
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
                result["closed"] = True
            except Exception:
                try:
                    proc.kill()
                    result["closed"] = True
                except Exception as exc:
                    result["error"] = str(exc)
        else:
            result["alreadyClosed"] = True
        MAC_WK_PROCESS = None
    if mark_closed:
        state = _mac_wk_read_state()
        if str(state.get("status") or "") != "ready":
            _mac_wk_write_state({"status": "closed", "engine": "WKWebView"})
    return result


def _mac_wk_launch(profile: str = "default", user_agent: str = "") -> dict:
    global MAC_WK_PROCESS, ACTIVE_BROWSER_MODE, ACTIVE_BROWSER_USER_AGENT
    if platform.system().lower() != "darwin":
        raise RuntimeError("WKWebView ist nur auf macOS verfügbar.")
    script_file = _mac_wk_resolve_script()
    if not pathlib.Path("/usr/bin/osascript").exists():
        raise RuntimeError("Die macOS-Systemkomponente osascript wurde nicht gefunden.")

    _mac_wk_terminate(mark_closed=False)
    try:
        MAC_WK_STATE_FILE.unlink(missing_ok=True)
    except TypeError:  # Python < 3.8 compatibility
        try:
            if MAC_WK_STATE_FILE.exists():
                MAC_WK_STATE_FILE.unlink()
        except Exception:
            pass
    MAC_WK_EXPRESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    # v316: ZURÜCK auf die vollständige Leselogik im Fenster.
    #
    # v315 hatte hier nur noch eine Anmelde-Sonde geschrieben und die Klassen
    # anschließend in Python über HTTP gelesen. Das schlug real fehl: Die Anfrage
    # landete auf der IAM-Anmeldeseite (« session expirée » direkt nach dem Login),
    # obwohl dieselbe Abfrage Sekunden vorher IM Fenster funktioniert hatte. Grund:
    # Im Fenster schickt der Browser bei credentials:"include" alle Cookies mit,
    # auch HttpOnly. Über WKHTTPCookieStore.getAllCookies kommt offenbar nicht die
    # vollständige Sitzung heraus, sodass die übernommenen Cookies für einen
    # eigenständigen HTTP-Aufruf nicht ausreichen.
    MAC_WK_EXPRESSION_FILE.write_text(build_read_expression(None), encoding="utf-8")
    _mac_wk_write_state({
        "status": "starting",
        "engine": "WKWebView",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })

    # v325: Falls eine noch gültige Sitzung gespeichert ist, wird sie dem Fenster als
    # 4. Parameter mitgegeben und dort vor dem Laden zurückgeschrieben.
    restore_path, restore_url = _mac_wk_prepare_restore_cookies()
    # v330: Mit wiederhergestellter Sitzung NICHT auf die Anmeldeseite starten.
    # e-Bichelchen beginnt dort die IAM-Anmeldung neu, auch wenn die Cookies noch
    # gültig sind – genau deshalb musste man sich trotz gemerkter Sitzung erneut
    # anmelden. Ist die Sitzung doch abgelaufen, leitet der Server von der App-Seite
    # ohnehin auf die Anmeldung um; schlimmstenfalls ist es also wie vorher.
    start_url = restore_url or EB_URL
    args = [
        "/usr/bin/osascript",
        "-l", "JavaScript",
        str(script_file),
        str(MAC_WK_STATE_FILE),
        str(MAC_WK_EXPRESSION_FILE),
        start_url,
    ]
    if restore_path:
        args.append(restore_path)
    env = os.environ.copy()
    env.setdefault("LANG", "en_US.UTF-8")
    with MAC_WK_LOCK:
        MAC_WK_PROCESS = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    ACTIVE_BROWSER_MODE = "mac-wkwebview"
    ACTIVE_BROWSER_USER_AGENT = user_agent or "Mozilla/5.0 (Macintosh) AppleWebKit WKWebView"
    return {
        "alreadyRunning": False,
        "openedWindow": True,
        "active": True,
        "sameBrowser": False,
        "browser": "macOS WKWebView",
        "browserId": "mac-wkwebview",
        "systemEngine": True,
        "requiresInstalledBrowser": False,
        "url": EB_URL,
        "profile": sanitize_profile_name(profile),
    }


def _mac_wk_state_session(state: dict) -> dict:
    session = state.get("session") if isinstance(state, dict) else None
    if not isinstance(session, dict) or not session.get("cookieHeader"):
        raise RuntimeError(
            "Die WKWebView-Sitzung ist noch nicht verfügbar. Bitte die Anmeldung vollständig abschließen."
        )
    return dict(session)


def _mac_wk_read_payload(selected_group_id: int | None = None) -> dict:
    global LATEST_SESSION, LATEST_SESSION_AT
    state = _mac_wk_read_state()

    # After the first successful transfer the disk state is deleted for security.
    # A later class choice therefore uses the in-memory session captured during
    # that first transfer instead of reopening the native login window.
    session = None
    if selected_group_id is not None:
        with LOCK:
            if isinstance(LATEST_SESSION, dict) and LATEST_SESSION.get("cookieHeader"):
                session = dict(LATEST_SESSION)
    if session is None:
        if str(state.get("status") or "") != "ready":
            detail = state.get("error") or state.get("detail") or "Anmeldung noch nicht abgeschlossen."
            raise RuntimeError(str(detail))
        session = _mac_wk_state_session(state)
        with LOCK:
            LATEST_SESSION = session
            LATEST_SESSION_AT = session.get("capturedAt") or time.strftime("%Y-%m-%d %H:%M:%S")
        # v325/v332: Sitzung für den nächsten App-Start merken (Cookies, 0600, 12 h)
        # – zusammen mit der vom Fenster ermittelten Kategorie « Nachricht ».
        payload_from_disk = state.get("data")
        _mac_wk_save_session(session, payload_from_disk if isinstance(payload_from_disk, dict) else None)
        # v311: Sobald die Sitzung im Arbeitsspeicher liegt, wird die Übergabedatei
        # sofort gelöscht – nicht erst beim späteren Schließen des Loginfensters.
        _mac_wk_purge_stale_state()
        if isinstance(payload_from_disk, dict):
            state = {"status": "ready", "data": payload_from_disk}

    if selected_group_id is not None:
        # Klassenwechsel ohne erneutes Loginfenster: Das geht nur über die
        # übernommenen Cookies. Sollte das an derselben Ursache scheitern wie der
        # v315-Versuch, meldet die Oberfläche « Sitzung abgelaufen » und der
        # Benutzer verbindet neu – die Klassenliste bleibt dabei erhalten.
        payload = _read_direct_with_session(session, int(selected_group_id))
        payload.setdefault("source", {})
        payload["source"].update({
            "browser": "macos-wkwebview-direct",
            "engine": "WKWebView",
            "selectionAuthority": "EntretienConnect",
            "requiresInstalledBrowser": False,
        })
        payload["version"] = HELPER_VERSION
        return payload

    # v316: Die erste Lesung kommt wieder aus dem Loginfenster – das ist der Weg,
    # der nachweislich funktioniert (siehe Begründung in _mac_wk_launch).
    payload = state.get("data")
    if not isinstance(payload, dict):
        raise RuntimeError("WKWebView hat keine gültigen e-Bichelchen-Daten zurückgegeben.")
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    payload.setdefault("source", {})
    payload["source"].update({
        "browser": "macos-wkwebview",
        "engine": "WKWebView",
        "sessionCaptured": True,
        "sessionCookieNames": session.get("cookieNames", []),
        "selectionAuthority": "EntretienConnect",
        "requiresInstalledBrowser": False,
    })
    payload["version"] = HELPER_VERSION
    return payload


def launch_browser(profile: str, preferred_browser: str = "auto", user_agent: str = "") -> dict:
    if platform.system().lower() == "darwin":
        # v320: Ausweichweg auf dem Mac. Standard bleibt das native Fenster
        # ("auto"). Wählt der Benutzer in der Oberfläche ausdrücklich Chrome oder
        # Edge, wird wieder der bewährte Chromium-Weg benutzt – nützlich, falls
        # e-Bichelchen im nativen Fenster nicht arbeiten will.
        pref = str(preferred_browser or "").strip().lower()
        if pref in ("chrome", "edge", "firefox-current"):
            return _launch_browser_cdp(profile, pref, user_agent)
        return _mac_wk_launch(profile, user_agent)
    return _launch_browser_cdp(profile, preferred_browser, user_agent)


def debug_browser_running() -> bool:
    if ACTIVE_BROWSER_MODE == "mac-wkwebview":
        state = _mac_wk_read_state()
        # A completed native login is still readable even though its window has
        # already closed. This prevents /read-browser?quiet=1 from reporting a
        # false "browserClosed" between login and data transfer.
        return _mac_wk_process_alive() or str(state.get("status") or "") == "ready"
    return _debug_browser_running_cdp()


def check_login_ready() -> dict:
    if ACTIVE_BROWSER_MODE != "mac-wkwebview":
        return _check_login_ready_cdp()
    state = _mac_wk_read_state()
    status = str(state.get("status") or "starting")
    if status == "ready":
        # v316: `data` ist wieder das vollständige Klassen-Payload aus dem Fenster.
        data = state.get("data") if isinstance(state.get("data"), dict) else {}
        groups = data.get("groups") if isinstance(data, dict) else []
        return {
            "ok": True,
            "ready": True,
            "browserClosed": False,
            "stage": "ready",
            "groupCount": len(groups or []),
            "pageUrl": state.get("pageUrl") or EB_URL,
            "via": "macos-wkwebview",
            "lightweight": True,
        }
    if status == "error":
        return {
            "ok": True,
            "ready": False,
            "browserClosed": True,
            "stage": "error",
            "detail": state.get("error") or state.get("detail") or "WKWebView error",
            "lightweight": True,
        }
    if status == "closed" or not _mac_wk_process_alive():
        return {
            "ok": True,
            "ready": False,
            "browserClosed": True,
            "stage": "closed",
            "detail": state.get("detail") or state.get("message") or "Das native Loginfenster wurde beendet.",
            "lightweight": True,
        }
    return {
        "ok": True,
        "ready": False,
        "browserClosed": False,
        "stage": state.get("stage") or "login",
        "pageUrl": state.get("pageUrl") or EB_URL,
        "detail": state.get("detail") or state.get("message") or "",
        "via": "macos-wkwebview",
        "lightweight": True,
    }


def read_browser_and_store(selected_group_id=None) -> dict:
    global LATEST_DATA, LATEST_AT
    if ACTIVE_BROWSER_MODE != "mac-wkwebview":
        return _read_browser_and_store_cdp(selected_group_id)
    if not READ_BROWSER_LOCK.acquire(blocking=False):
        raise RuntimeError("Lecture déjà en cours – merci de patienter.")
    try:
        payload = _mac_wk_read_payload(selected_group_id)
    finally:
        READ_BROWSER_LOCK.release()
    with LOCK:
        LATEST_DATA = payload
        LATEST_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    return payload


def focus_app_tab() -> dict:
    if ACTIVE_BROWSER_MODE == "mac-wkwebview":
        # The native window is placed in front of the standard browser. Once it
        # closes, the user's original EntretienConnect window is revealed again.
        # Avoid AppleScript browser automation permission prompts.
        return {"method": "native-window-closed", "foundExistingTab": True, "openedNewTab": False}
    return _focus_app_tab_cdp()


def close_ebichelchen_target() -> dict:
    if ACTIVE_BROWSER_MODE == "mac-wkwebview":
        result = _mac_wk_terminate(mark_closed=False)
        # The authenticated cookie header is now held only in process memory.
        # Remove the short-lived transfer file as soon as the UI has read it.
        with LOCK:
            have_session = bool(isinstance(LATEST_SESSION, dict) and LATEST_SESSION.get("cookieHeader"))
        if have_session:
            try:
                MAC_WK_STATE_FILE.unlink(missing_ok=True)
                result["transferFileRemoved"] = True
            except Exception:
                result["transferFileRemoved"] = False
        return result
    return _close_ebichelchen_target_cdp()


def force_close_launched_browser(force: bool = False) -> dict:
    if ACTIVE_BROWSER_MODE == "mac-wkwebview" or _mac_wk_process_alive():
        return _mac_wk_terminate(mark_closed=False)
    return _force_close_launched_browser_cdp(force=force)


def soft_reset_login() -> dict:
    global LATEST_SESSION, LATEST_SESSION_AT
    if ACTIVE_BROWSER_MODE == "mac-wkwebview" or platform.system().lower() == "darwin":
        closed = _mac_wk_terminate(mark_closed=True)
        clear_current()
        with LOCK:
            LATEST_SESSION = None
            LATEST_SESSION_AT = None
        try:
            MAC_WK_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        # v325: Beim bewussten Neuanmelden auch die gemerkte Sitzung verwerfen –
        # sonst würde das Fenster gleich wieder mit der alten Sitzung starten.
        _mac_wk_clear_saved_session()
        return {
            "softReset": True,
            "browserRunning": False,
            "cookiesCleared": True,
            "closedEbichelchen": closed,
            "profilePreserved": True,
            "engine": "WKWebView",
        }
    return _soft_reset_login_cdp()


def reset_login_session(profile: str = "default", preserve_profile: bool = False) -> dict:
    if ACTIVE_BROWSER_MODE == "mac-wkwebview" or platform.system().lower() == "darwin":
        info = soft_reset_login()
        return {
            "closed": bool((info.get("closedEbichelchen") or {}).get("closed")),
            "profilesRemoved": [],
            "sessionDataRemoved": [],
            "cookiesCleared": False,
            "profilePreserved": True,
            "browserRunning": False,
            "engine": "WKWebView",
        }
    return _reset_login_session_cdp(profile, preserve_profile)


# v311: Beim Laden des Moduls liegt garantiert keine Sitzung im Speicher. Eine noch
# vorhandene Übergabedatei stammt also aus einem abgebrochenen Lauf und wird verworfen.
if platform.system().lower() == "darwin":
    _mac_wk_purge_stale_state()
