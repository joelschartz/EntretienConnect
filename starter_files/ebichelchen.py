#!/usr/bin/env python3
# eBichelchenHelper v1.10.16 - lokaler Helfer für individuelle e-Bichelchen-Nachrichten.
# Keine e-Bichelchen-Zugangsdaten. v1.10.16 kann nach Vorschau mehrere individuelle Message-Einträge erstellen und wieder löschen.

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


def open_remote_tab(url: str) -> bool:
    # Browser DevTools: /json/new?{url} requires PUT in current Chrome versions.
    try:
        target = f"http://127.0.0.1:{CDP_PORT}/json/new?" + urllib.parse.quote(url, safe="")
        req = urllib.request.Request(target, method="PUT")
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
        return True
    except Exception:
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

    # Falls schon ein Browser mit CDP-Port läuft, nur neuen e-Bichelchen-Tab öffnen.
    try:
        version = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
        open_remote_tab(EB_URL)
        return {"alreadyRunning": True, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
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

    for _ in range(30):
        try:
            version = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=0.5)
            return {"alreadyRunning": False, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "devtoolsBrowser": version.get("Browser") if isinstance(version, dict) else None}
        except Exception:
            time.sleep(0.2)

    return {"alreadyRunning": False, "profile": profile, "profileDir": str(profile_dir), "url": EB_URL, "port": CDP_PORT, "browser": browser_name, "browserId": browser_id, "browserPath": browser_path, "warning": "Browser wurde gestartet, DevTools war aber noch nicht erreichbar."}


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
      const key = label + " " + url + " " + (options?.method || "GET") + " " + (options?.body || "");
      if (triedKeys.has(key)) return [];
      triedKeys.add(key);
      try {
        const opts = Object.assign({
          method: "GET",
          credentials: "include",
          headers: { "accept": "application/json, text/plain, */*", "mobileappversion": "web" }
        }, options || {});
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
        attempts.push({ label, url, method: options?.method || "GET", ok:false, error:String(e.message || e).slice(0,180) });
        return [];
      }
    }

    // 1) URLs verwenden, die e-Bichelchen selbst schon geladen hat.
    for (const url of knownUrls) {
      const subjects = await tryFetch("known-resource", url);
      if (subjects.length) return { subjects, source:"known-resource", attempts, knownSubjectUrls: knownUrls };
    }

    // 2) Direkt den bekannten e-Bichelchen-Endpunkt aufrufen.
    // Das ist weiterhin read-only: Es wird nur dieselbe Kategorienliste abgefragt, die e-Bichelchen beim Kalender lädt.
    const baseUrls = [
      "/ebichelchen/app/api/group/get-subjects-for-groups",
      "/ebichelchen/app/api/get-subjects-for-groups",
      "/ebichelchen/app/api/v6/get-subjects-for-groups"
    ];
    const queryParts = [""];
    if (Number.isFinite(gid)) {
      queryParts.push("?groupId=" + encodeURIComponent(gid));
      queryParts.push("?groupIds=" + encodeURIComponent(gid));
      queryParts.push("?ids=" + encodeURIComponent(gid));
      if (group.classGrade) queryParts.push("?groupId=" + encodeURIComponent(gid) + "&classGrade=" + encodeURIComponent(group.classGrade));
    }
    for (const base of baseUrls) {
      for (const q of queryParts) {
        const subjects = await tryFetch("direct-get", base + q);
        if (subjects.length) return { subjects, source:"direct-get " + base + q, attempts, knownSubjectUrls: knownUrls };
      }
    }

    // 3) Falls der echte Frontend-Call POST verwendet, probieren wir harmlose JSON-Körper.
    if (Number.isFinite(gid)) {
      const bodies = [
        { groupId: gid },
        { groupIds: [gid] },
        { ids: [gid] },
        [gid],
        [{ id: gid }],
        { groups: [{ id: gid }] }
      ];
      for (const base of baseUrls) {
        for (const body of bodies) {
          const subjects = await tryFetch("direct-post", base, { method:"POST", body: JSON.stringify(body), headers: { "accept":"application/json, text/plain, */*", "mobileappversion":"web", "content-type":"application/json" } });
          if (subjects.length) return { subjects, source:"direct-post " + base, attempts, knownSubjectUrls: knownUrls };
        }
      }
    }

    return { subjects: [], source:null, attempts, knownSubjectUrls: knownUrls };
  }

  if (!location.href.includes("/ebichelchen/app/")) throw new Error("Bitte im echten e-Bichelchen-Tab bleiben.");

  const groupsResult = await getGroupsFromTeacher();
  let groupObjects = extractGroupObjects(groupsResult.json);
  const groups = groupObjects.map(mapGroup).filter(g => Number.isFinite(g.id)).sort((a,b) => String(a.classAlias || a.name).localeCompare(String(b.classAlias || b.name)));
  if (!groups.length) throw new Error("Keine Klassen aus get-groups-from-teacher erhalten. Automatik-Versuche: " + JSON.stringify(groupsResult.attempts || []).slice(0, 900));

  const groupStore = parseStore("groupStore");
  const userStore = parseStore("userStore");
  const selectedFromStore = Number(groupStore?.selectedGroup?.id);

  let group = null;
  if (requestedGroupId !== null) group = groups.find(g => Number(g.id) === Number(requestedGroupId)) || null;
  if (!group && Number.isFinite(selectedFromStore)) group = groups.find(g => Number(g.id) === selectedFromStore) || null;
  if (!group && groups.length === 1) group = groups[0];

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

  const loggedInUser = userStore?.loggedInUser ? {
    firstName: userStore.loggedInUser.firstName || "",
    lastName: userStore.loggedInUser.lastName || "",
    fullName: userStore.loggedInUser.fullName || [userStore.loggedInUser.firstName, userStore.loggedInUser.lastName].filter(Boolean).join(" "),
    id: userStore.loggedInUser.id || null
  } : null;

  const payload = {
    version: "1.10.16",
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

    macOS: sucht den vorhandenen Chrome-Tab per AppleScript.
    Windows: sucht ein sichtbares Fenster mit "eBichelchen Helper" im Titel und bringt es nach vorne.
    Wichtig: Diese Funktion öffnet absichtlich KEINE neue App-URL, um doppelte App-Tabs zu vermeiden.
    """
    app_base = f"http://127.0.0.1:{PORT}"
    system = platform.system().lower()

    if system == "darwin":
        script = f"""
set targetBase to "{app_base}"
set didFind to false

tell application "Google Chrome"
    repeat with w in windows
        set tabIndex to 0
        repeat with t in tabs of w
            set tabIndex to tabIndex + 1
            try
                set u to URL of t
                if (u is targetBase) or (u starts with (targetBase & "/")) then
                    set active tab index of w to tabIndex
                    set index of w to 1
                    set didFind to true
                    exit repeat
                end if
            end try
        end repeat
        if didFind then exit repeat
    end repeat
    if didFind then activate
end tell

return didFind
"""
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "AppleScript konnte Chrome nicht fokussieren.")
        found = "true" in proc.stdout.lower()
        return {"method": "osascript", "foundExistingTab": found, "message": proc.stdout.strip(), "openedNewTab": False}

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

def close_ebichelchen_target() -> dict:
    """Schließt das von der App gestartete e-Bichelchen-Chrome vollständig.
    In v1.7.3 wurde nur ein einzelner Tab geschlossen und danach versehentlich die App-URL
    im e-Bichelchen-Fenster geöffnet. v1.10.16 schließt deshalb bevorzugt den ganzen
    DevTools-Browser über Browser.close. Das betrifft nur den Chrome mit unserem lokalen Testprofil.
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
    try:
        targets = read_url_json(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2)
        if isinstance(targets, list):
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


def reset_login_session(profile: str = "default") -> dict:
    """v155: Kompletter Neustart der e-Bichelchen-Anmeldung. Schließt den App-Browser
    und löscht die App-Browserprofile (Cookies / halbfertige IAM-Sitzung). Ohne diesen
    Reset blockiert eine abgebrochene Anmeldung (falsches Passwort, Fenster zu früh
    geschlossen) die nächste EduKey-Sicherheitsanfrage."""
    closed = force_close_launched_browser()
    removed = []
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
    return {"closed": closed, "profilesRemoved": removed}


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

def read_browser_and_store(selected_group_id=None) -> dict:
    """Liest Klassen/Schüler/Message-subjectId aus dem e-Bichelchen-Tab und
    merkt sie als LATEST_DATA (Grundlage der serverseitigen Sicherheitsprüfung)."""
    global LATEST_DATA, LATEST_AT
    payload = read_from_chrome(selected_group_id)
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
