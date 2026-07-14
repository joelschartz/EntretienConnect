#!/usr/bin/env python3
# eBichelchenHelper v1.10.21 - lokaler Helfer für individuelle e-Bichelchen-Nachrichten.
# Keine e-Bichelchen-Zugangsdaten. v1.10.16 kann nach Vorschau mehrere individuelle Message-Einträge erstellen und wieder löschen.
# v1.10.17: Browser.close/Profil-Löschung nur noch, wenn KEIN App-Tab (127.0.0.1/localhost) im
# Debug-Browser läuft — sonst verschwand die App mitsamt Fenster beim Verbinden/Aufräumen.

from __future__ import annotations

import base64
import json
import mimetypes
import os
import pathlib
import platform
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
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
EB_URL = "https://ssl.education.lu/ebichelchen/app/tabs/calendar"

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
CREATED_TEST_ENTRIES: list[dict] = []
LATEST_SESSION: dict | None = None
LATEST_SESSION_AT: str | None = None


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
    """Öffnet zuverlässig einen neuen CDP-Tab.

    Aktuelle Chrome-/Edge-Versionen erwarten PUT; einige ältere Builds akzeptieren
    nur GET. Beide Varianten werden unterstützt. Ein fehlgeschlagener Aufruf darf
    nicht mehr fälschlich als erfolgreicher Browserstart gemeldet werden.
    """
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


def launch_browser(profile: str, preferred_browser: str = "auto") -> dict:
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
            _cdp_set_window_state(existing, "normal")
            try:
                cdp_call(existing.get("webSocketDebuggerUrl"), "Page.bringToFront", {}, msg_id=912)
            except Exception:
                pass
            return {"alreadyRunning": True, "reusedTab": True, "openedTab": False, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
        opened = False
        for _ in range(8):
            if open_remote_tab(EB_URL):
                opened = True
                break
            time.sleep(0.25)
        if opened:
            return {"alreadyRunning": True, "openedTab": True, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
        # Fallback: URL über den Browser-Prozess an die bereits laufende Instanz geben.
        # Chrome/Edge leitet dies normalerweise an dieselbe Profilinstanz weiter.
        try:
            subprocess.Popen([browser_path, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile_dir}", EB_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        for _ in range(16):
            if open_remote_tab(EB_URL):
                return {"alreadyRunning": True, "openedTab": True, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
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
            return {"alreadyRunning": False, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
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
            b1, b2 = self.sock.recv(2)
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
            if opcode == 0x9:  # ping -> ignore
                continue
            if opcode in (0x1, 0x0):
                chunks.append(payload)
                if b1 & 0x80:
                    return b"".join(chunks).decode("utf-8")

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

  async function clickClassesTabIfVisible() {
    const log = [];
    const rxText = /(klassen|classes|classe|classes|groupes|gruppen)/i;
    const rxRoute = /(class|classe|group|groups|klasse)/i;
    const nodes = [...document.querySelectorAll('a,button,ion-tab-button,[role="tab"],[routerlink],[href],.tab-button,.nav-link')];
    const scored = nodes.map(el => {
      const txt = String((el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '')).trim();
      const route = String(el.getAttribute('href') || el.getAttribute('routerlink') || el.getAttribute('ng-reflect-router-link') || '');
      const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      let score = 0;
      if (visible) score += 1;
      if (rxText.test(txt)) score += 10;
      if (rxRoute.test(route)) score += 5;
      return { el, txt, route, visible, score };
    }).filter(x => x.score >= 6).sort((a,b) => b.score - a.score);
    log.push('visible candidates: ' + scored.slice(0,4).map(x => (x.txt || x.route || '?')).join(' | '));
    const hit = scored[0];
    if (!hit) return { clicked:false, log };
    try {
      hit.el.scrollIntoView({block:'center', inline:'center'});
      hit.el.click();
      await waitMs(1800);
      return { clicked:true, text:hit.txt, route:hit.route, log };
    } catch (e) {
      return { clicked:false, error:String(e.message || e), text:hit.txt, route:hit.route, log };
    }
  }



  function normalizeGroupLabel(value) {
    return String(value || "")
      .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
      .toLowerCase().replace(/\s+/g, " ").trim();
  }

  async function ensureGroupSelectedInPage(group, rawGroup) {
    if (!group || !Number.isFinite(Number(group.id))) return { ok:false, reason:"no-group" };
    const gid = String(group.id);
    const labels = [group.classAlias, group.name].map(normalizeGroupLabel).filter(Boolean);

    function selectedIdFromStore() {
      const store = parseStore("groupStore");
      return String(store?.selectedGroup?.id ?? store?.selectedGroupId ?? "");
    }
    if (selectedIdFromStore() === gid) return { ok:true, via:"store", changed:false };

    function isVisible(el) {
      return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    }
    function textOf(el) {
      return normalizeGroupLabel(el?.innerText || el?.textContent || el?.getAttribute?.("aria-label") || el?.getAttribute?.("title") || "");
    }

    // 1) Echte Auswahlfelder bevorzugen: das löst die Frontend-Logik am saubersten aus.
    for (const select of [...document.querySelectorAll("select")]) {
      if (!isVisible(select)) continue;
      const options = [...select.options].map(opt => {
        const value = String(opt.value || opt.getAttribute("data-id") || opt.getAttribute("data-group-id") || "");
        const txt = textOf(opt);
        let score = value === gid ? 100 : 0;
        for (const label of labels) {
          if (txt === label) score = Math.max(score, 90);
          else if (label.length >= 3 && txt.includes(label)) score = Math.max(score, 55);
        }
        return { opt, score };
      }).sort((a,b) => b.score - a.score);
      if (options[0] && options[0].score >= 55) {
        select.value = options[0].opt.value;
        options[0].opt.selected = true;
        select.dispatchEvent(new Event("input", { bubbles:true }));
        select.dispatchEvent(new Event("change", { bubbles:true }));
        await waitMs(900);
        if (selectedIdFromStore() === gid) return { ok:true, via:"select", changed:true };
      }
    }

    // 2) Klassenkarte/-zeile anklicken, falls e-Bichelchen die Auswahl nicht als <select> rendert.
    const selector = 'button,a,ion-item,[role="button"],[role="option"],[data-id],[data-group-id],[data-groupid],.list-group-item,.mat-list-item';
    const candidates = [...document.querySelectorAll(selector)].filter(isVisible).map(el => {
      const txt = textOf(el);
      const attrs = ["data-id","data-group-id","data-groupid","value","href","routerlink","ng-reflect-router-link"]
        .map(k => String(el.getAttribute?.(k) || ""));
      let score = attrs.some(v => v === gid) ? 120 : (attrs.some(v => v.includes(gid)) ? 70 : 0);
      for (const label of labels) {
        if (txt === label) score = Math.max(score, 100);
        else if (label.length >= 3 && txt.includes(label)) score = Math.max(score, 60);
      }
      if (/connect|connexion|logout|deconnexion|abmelden/i.test(txt)) score = 0;
      return { el, txt, score };
    }).filter(x => x.score >= 60).sort((a,b) => b.score - a.score);
    if (candidates[0]) {
      try {
        candidates[0].el.scrollIntoView({ block:"center", inline:"center" });
        candidates[0].el.click();
        await waitMs(1400);
        if (selectedIdFromStore() === gid) return { ok:true, via:"click", changed:true };
      } catch (_) {}
    }

    // 3) Robuster Fallback: denselben groupStore setzen, den e-Bichelchen selbst nutzt,
    // und anschließend den Kalender neu laden. Der nächste automatische Leseversuch
    // läuft nach der Navigation weiter; der Nutzer muss die Klasse nicht mehr im
    // e-Bichelchen-Fenster anklicken.
    try {
      const current = parseStore("groupStore");
      const store = current && typeof current === "object" ? current : {};
      store.selectedGroup = rawGroup || group;
      store.selectedGroupId = Number(group.id);
      sessionStorage.setItem("groupStore", JSON.stringify(store));
      setTimeout(() => location.replace("/ebichelchen/app/tabs/calendar"), 30);
      return { ok:true, via:"store-navigation", changed:true, navigating:true };
    } catch (e) {
      return { ok:false, reason:String(e.message || e) };
    }
  }

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

    // 2) Bei Lehrpersonen mit mehreren Klassen lädt e-Bichelchen die Klassenliste manchmal erst,
    // wenn man links/unten den Reiter „Klassen“ öffnet. Das macht die App jetzt selbst im Tab.
    const nudge1 = await clickClassesTabIfVisible();
    attempts.push({label:'auto-click-classes-tab', ok:!!nudge1.clicked, detail:nudge1});
    result = await tryAll('after-class-tab-click');
    if (extractGroupObjects(result.json).length) return result;

    // 3) Noch einmal kurz warten: IAM/e-Bichelchen füllt Stores manchmal verzögert.
    await waitMs(1800);
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

  async function tryReadSubjects(group) {
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

    // 1) Den echten Frontend-Aufruf bevorzugen, falls e-Bichelchen ihn bereits geladen hat.
    // Das ist normalerweise der schnellste und genaueste Weg.
    for (const url of knownUrls) {
      const subjects = await tryFetch("known-resource", url, { timeoutMs:1800 });
      if (subjects.length) return { subjects, source:"known-resource", attempts, knownSubjectUrls: knownUrls };
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
      { url:v6 },
      { url:groupApi + "?groupId=" + gidQ }
    ] : [{ url:v6 }, { url:groupApi }, { url:legacy }];
    let hit = await tryBatch("fast-get", fastGet);
    if (hit) return { subjects:hit.subjects, source:"fast-get " + hit.spec.url, attempts, knownSubjectUrls: knownUrls };

    // 3) Seltenere GET-Varianten ebenfalls parallel und mit kurzem Timeout prüfen.
    const baseUrls = [v6, groupApi, legacy];
    const queryParts = [""];
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

  let group = null;
  let groupChosenAutomatically = false;
  if (requestedGroupId !== null) group = groups.find(g => Number(g.id) === Number(requestedGroupId)) || null;
  if (!group && Number.isFinite(selectedFromStore)) group = groups.find(g => Number(g.id) === selectedFromStore) || null;
  if (!group && groups.length === 1) { group = groups[0]; groupChosenAutomatically = true; }
  if (!group) {
    const activeGroups = groups.filter(g => g.isActivatedByTeacher && !g.isInactive && !g.isTestClass);
    if (activeGroups.length === 1) { group = activeGroups[0]; groupChosenAutomatically = true; }
  }

  // v286: e-Bichelchen landet nach IAM teilweise auf « Neuigkeiten / Affiches ».
  // Bei einer eindeutig bestimmbaren Klasse (oder nach expliziter Wahl in der App)
  // wird sie im e-Bichelchen-Tab automatisch aktiviert und der Kalender geöffnet.
  // v296: Eine eindeutig automatisch erkannte Klasse muss nicht erst im sichtbaren
  // Frontend angeklickt und der Kalender neu geladen werden. Gruppen-/Schülerdaten sind
  // bereits vorhanden; die Kategorie wird direkt per API gelesen. Nur eine ausdrückliche
  // Klassenwahl des Nutzers synchronisiert weiterhin den e-Bichelchen-Tab.
  const shouldPrepareGroup = !!(group && Number(group.id) !== Number(selectedFromStore) && requestedGroupId !== null);
  if (shouldPrepareGroup) {
    const rawGroup = groupObjects.find(g => Number(g?.id ?? g?.groupId) === Number(group.id)) || group;
    const prepared = await ensureGroupSelectedInPage(group, rawGroup);
    if (prepared && prepared.navigating) {
      await waitMs(90);
      throw new Error("Classe e-Bichelchen sélectionnée automatiquement. Relecture en cours.");
    }
  }

  const subjectsT0 = performance.now();
  const storageSubjects = findSubjectsFromStorage();
  let subjects = storageSubjects.subjects.map(s => ({ id:s.id, labelDeu:s.labelDeu||"", labelFra:s.labelFra||"", label:s.label||"", icon:s.icon||"", defaultColor:s.defaultColor ?? null, source:s.source||"storage" }));
  let subjectsSource = subjects.length ? "storage" : null;
  let subjectAttempts = [];
  let messageSubject = detectMessageSubject(subjects);
  if (!messageSubject) {
    const apiSubj = await tryReadSubjects(group);
    if (apiSubj.subjects.length) {
      subjects = apiSubj.subjects;
      subjectsSource = apiSubj.source;
      messageSubject = detectMessageSubject(subjects);
    }
    subjectAttempts = apiSubj.attempts || [];
    storageSubjects.knownSubjectUrls = apiSubj.knownSubjectUrls || [];
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
    version: "1.10.23",
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
        raise RuntimeError("En attente du calendrier e-Bichelchen. Terminez la connexion dans la fenêtre ouverte ; aucun nouvel onglet ne sera ouvert automatiquement. Onglets actuels : " + hint)

    # bevorzugt Kalender-Tab; Login-/Zwischenseiten bleiben nur Fallback
    candidates.sort(key=lambda t: ("/tabs/calendar" not in (t.get("url") or ""), t.get("url") or ""))
    target = candidates[0]
    if not target.get("webSocketDebuggerUrl"):
        raise RuntimeError("Der e-Bichelchen-Tab hat keine DevTools-WebSocket-URL geliefert.")
    return target




def check_login_ready() -> dict:
    """Sehr leichte Bereitschaftsprüfung für den Login-Polling-Loop.

    v297: weiterhin nur ein DevTools-Listenaufruf pro Poll. Die Freigabe erfolgt aber
    nicht mehr allein anhand alter sessionStorage-Werte: e-Bichelchen darf seine Route
    erst stabilisieren und der echte Klassen-Endpunkt muss antworten. Das verhindert
    das sichtbare Kalender → Pinnwand → Kalender-Flackern durch eine zu frühe Lesung.
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

    candidates.sort(key=lambda t: ("/tabs/calendar" not in str(t.get("url") or ""), str(t.get("url") or "")))
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


def read_from_chrome(selected_group_id: int | None = None) -> dict:
    target = find_ebichelchen_target()

    # v1.7: Hauptquelle ist get-groups-from-teacher. Dadurch kann die App auch auf Pinwand/Klassenauswahl
    # lesen und muss nicht zuerst blind zum Kalender springen.
    expr = build_read_expression(selected_group_id)
    msg = cdp_eval(target["webSocketDebuggerUrl"], expr, await_promise=True, timeout_ms=18000)
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
        raise RuntimeError("Keine Daten aus dem e-Bichelchen-Tab erhalten. Ist e-Bichelchen nach dem Login sichtbar?")
    try:
        payload = json.loads(value)
    except Exception as exc:
        raise RuntimeError(f"Antwort konnte nicht gelesen werden: {exc}; Wert: {str(value)[:300]}")
    payload.setdefault("source", {})
    payload["source"].update({"targetTitle": target.get("title"), "targetUrl": target.get("url")})
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
                if "eBichelchen Helper" in title and "e-Bichelchen" not in title:
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
            return {"method": "windows-user32", "foundExistingTab": False, "message": "Kein vorhandenes App-Fenster mit aktivem eBichelchen-Helper-Tab gefunden.", "sampleTitles": titles[:8], "openedNewTab": False}
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
    """Schließt das von der App gestartete e-Bichelchen-Chrome vollständig.
    In v1.7.3 wurde nur ein einzelner Tab geschlossen und danach versehentlich die App-URL
    im e-Bichelchen-Fenster geöffnet. v1.10.16 schließt deshalb bevorzugt den ganzen
    DevTools-Browser über Browser.close. Das betrifft nur den Chrome mit unserem lokalen Testprofil.
    v1.10.17: läuft im selben Browser auch der App-Tab, wird nur der e-Bichelchen-Tab geschlossen.
    """
    target = find_ebichelchen_target()
    target_id = target.get("id")
    url = target.get("url") or ""
    title = target.get("title") or ""
    ws_url = target.get("webSocketDebuggerUrl")
    if not target_id or not ws_url:
        raise RuntimeError("Kein e-Bichelchen-Target zum Schließen gefunden.")

    if "/ebichelchen/app/" not in url:
        raise RuntimeError("Sicherheitsstopp: Target ist kein e-Bichelchen-Tab: " + url)

    if any(_is_app_target(t) for t in _list_cdp_targets()):
        if _cdp_close_tab(target_id):
            return {"closed": True, "method": "json-close", "targetId": target_id, "url": url, "title": title, "appTabProtected": True}
        cdp_call(ws_url, "Page.close", {}, msg_id=700)
        return {"closed": True, "method": "Page.close", "targetId": target_id, "url": url, "title": title, "appTabProtected": True}

    # Bevorzugt: kompletten von der App gestarteten Chrome schließen.
    try:
        cdp_call(ws_url, "Browser.close", {}, msg_id=701)
        return {"closed": True, "method": "Browser.close", "targetId": target_id, "url": url, "title": title}
    except Exception as first_exc:
        # Fallback: nur den e-Bichelchen-Tab schließen.
        close_url = f"http://127.0.0.1:{CDP_PORT}/json/close/{urllib.parse.quote(target_id, safe='')}"
        try:
            req = urllib.request.Request(close_url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                text = resp.read().decode("utf-8", "replace")
            return {"closed": True, "method": "json-close", "targetId": target_id, "url": url, "title": title, "warning": str(first_exc), "response": text[:200]}
        except Exception as second_exc:
            try:
                cdp_call(ws_url, "Page.close", {}, msg_id=700)
                return {"closed": True, "method": "Page.close", "targetId": target_id, "url": url, "title": title, "warning": f"{first_exc}; {second_exc}"}
            except Exception as third_exc:
                raise RuntimeError(f"e-Bichelchen konnte nicht geschlossen werden: Browser.close={first_exc}; json-close={second_exc}; Page.close={third_exc}")

def force_close_launched_browser() -> dict:
    """v155: Schließt den vom Helfer gestarteten Browser komplett – egal welche Seite
    gerade offen ist (auch IAM-/EduKey-Login). Betrifft ausschließlich den Browser am
    lokalen CDP-Port mit App-Profil; der normale Browser des Benutzers bleibt unberührt."""
    result = {"closed": False, "method": None}
    targets = _list_cdp_targets()
    if any(_is_app_target(t) for t in targets):
        # v1.10.17: der App-Tab läuft in diesem Browser — Browser niemals komplett beenden,
        # sonst verschwindet die App mitsamt Fenster. Nur die übrigen Tabs schließen.
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
    result = {"closedEbichelchen": None, "focusedApp": None}
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
        "Referer": "https://ssl.education.lu/ebichelchen/app/tabs/calendar",
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
        payload = read_from_chrome(selected_group_id)
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
