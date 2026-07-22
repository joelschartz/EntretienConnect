#!/usr/bin/env python3
"""
Lokaler Helfer fuer die EntretienConnect-Browser-App.

- Liefert die index.html aus
- Nimmt unter POST /api/send die fertigen E-Mails entgegen und
  verschickt sie ueber Gmail (SMTP, SSL) als BCC.

Keine Zusatz-Pakete noetig (nur Python-Standardbibliothek).
Start:  python3 server.py
"""

import http.server
import socketserver
import json
import hashlib
import smtplib
import ssl
import sys
sys.dont_write_bytecode = True
import webbrowser
import threading
import os
import re
import time
import subprocess
import shutil
import urllib.request
import urllib.parse
import urllib.error
import errno
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta

# e-Bichelchen-Kanal (optional, zusammengeführt aus dem eBichelchenHelper).
# Schlägt der Import fehl, bleibt die App voll als reine E-Mail-App nutzbar.
try:
    import ebichelchen as eb
    EB_AVAILABLE = True
except Exception as _eb_exc:  # pragma: no cover
    eb = None
    EB_AVAILABLE = False
    _EB_IMPORT_ERROR = str(_eb_exc)

# Standardport 8765; optional per Umgebungsvariable oder erstem Argument überschreibbar.
def _initial_port():
    env = os.environ.get("ENTRETIENCONNECT_PORT") or os.environ.get("ELTERN_PORT")
    if env and env.isdigit():
        return int(env)
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        return int(sys.argv[1])
    return 8765

PORT = _initial_port()
last_heartbeat_time = None
server_started_time = time.time()
HEARTBEAT_TIMEOUT_SECONDS = 180  # v204: Browser-Heartbeat; Helper beendet sich ca. 3 Minuten nach geschlossenem Tab
STARTUP_NO_HEARTBEAT_TIMEOUT_SECONDS = 300  # wenn der Browser gar nicht startet: nach ca. 5 Minuten beenden


def _helper_version():
    # v185: version du helper lue depuis VERSION.txt (une seule source de vérité,
    # plus de numéros codés en dur qui dérivent de la version réelle).
    try:
        with open(os.path.join(DATA_DIR, "VERSION.txt"), encoding="utf-8") as f:
            return int(f.read().strip().lstrip("vV") or 0)
    except Exception:
        return 0


def _port_is_in_use_error(exc):
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in (errno.EADDRINUSE, 48, 98, 10048)


def _make_server_with_fallback(host, preferred_port, handler_cls):
    """Startet auf preferred_port; falls belegt, nimmt automatisch einen freien Port."""
    try:
        return Server((host, preferred_port), handler_cls), preferred_port, False
    except OSError as exc:
        if not _port_is_in_use_error(exc):
            raise
        # Port 0 laesst macOS/Windows/Linux automatisch einen freien Port waehlen.
        httpd = Server((host, 0), handler_cls)
        return httpd, httpd.server_address[1], True

# Als .exe (PyInstaller) gebuendelt liegen die HTML-Dateien im temporaeren
# Entpackordner (_MEIPASS); der Login-Schluessel muss aber an einen dauerhaften,
# beschreibbaren Ort (neben die .exe). Als normales Skript: alles im Skriptordner.
if getattr(sys, "frozen", False):
    RES_DIR = sys._MEIPASS                          # ausgelieferte Dateien (read-only)
    DATA_DIR = os.path.dirname(sys.executable)      # beschreibbar, neben der .exe
else:
    RES_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = RES_DIR
LOCAL_CSV_NAME = "eleves_contacts.csv"

# Die CSV wird bewusst nicht in Downloads/Desktop/Dokumente gesucht.
# Erwartet wird nur: eleves_contacts.csv liegt neben dem Starter / der App
# oder in einem explizit vom Starter gesetzten Ordner.
def _local_csv_candidate_paths():
    # Le fichier est prévu à côté du lanceur (EntretienConnect_MAC.app ou EntretienConnect_WINDOWS.vbs).
    # v204: le chemin peut aussi être fourni par le lanceur Mac .app via ENTRETIENCONNECT_CSV_DIR.
    paths = []
    try:
        csv_dir = os.environ.get("ENTRETIENCONNECT_CSV_DIR")
        if csv_dir:
            paths.append(os.path.join(csv_dir, LOCAL_CSV_NAME))
    except Exception:
        pass
    try:
        paths.append(os.path.join(os.path.dirname(DATA_DIR), LOCAL_CSV_NAME))
    except Exception:
        pass
    try:
        paths.append(os.path.join(DATA_DIR, LOCAL_CSV_NAME))
    except Exception:
        pass
    try:
        paths.append(os.path.join(PERSIST_DIR, LOCAL_CSV_NAME))
    except Exception:
        pass
    seen = set(); out = []
    for x in paths:
        try:
            ax = os.path.abspath(x)
            if ax not in seen:
                seen.add(ax); out.append(ax)
        except Exception:
            pass
    return out


def _user_app_data_dir():
    """Dauerhafter, benutzerspezifischer Speicher ohne Admin-Rechte."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "EntretienConnect")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = DATA_DIR
    return path


PERSIST_DIR = _user_app_data_dir()
STATE_FILE = os.path.join(PERSIST_DIR, "state.json")
STATE_BACKUP_DIR = os.path.join(PERSIST_DIR, "backups")
try:
    os.makedirs(STATE_BACKUP_DIR, exist_ok=True)
except Exception:
    STATE_BACKUP_DIR = PERSIST_DIR

PID_FILE = os.path.join(PERSIST_DIR, "helper.pid")


# v202: Web-/UI-Updates werden nicht mehr in den Starter-/App-Ordner geschrieben,
# sondern in den benutzerspezifischen App-Speicher. Das ist wichtig für spätere
# signierte Mac-Apps und funktioniert ohne Admin-Rechte.
WEB_MANIFEST_NAME = "web-manifest.json"
WEB_FILES = ("index.html", "graph.html", "schullogo.png")
WEB_CACHE_DIR = os.path.join(PERSIST_DIR, "web")
WEB_CURRENT_DIR = os.path.join(WEB_CACHE_DIR, "current")
WEB_PREVIOUS_DIR = os.path.join(WEB_CACHE_DIR, "previous")
WEB_STAGING_DIR = os.path.join(WEB_CACHE_DIR, "staging")


def _safe_makedirs(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception:
        return False


def _copy_file_atomic(src, dst):
    _safe_makedirs(os.path.dirname(dst))
    tmp = dst + ".tmp"
    with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
        fdst.write(fsrc.read())
    os.replace(tmp, dst)


def _web_dir_version(path):
    try:
        gp = os.path.join(path, "graph.html")
        if os.path.exists(gp):
            with open(gp, "rb") as f:
                return _html_app_version(f.read())
    except Exception:
        pass
    return 0


def _valid_web_dir(path):
    try:
        gp = os.path.join(path, "graph.html")
        ip = os.path.join(path, "index.html")
        return os.path.isfile(gp) and os.path.getsize(gp) > 1000 and os.path.isfile(ip) and os.path.getsize(ip) > 100
    except Exception:
        return False


def _seed_runtime_web_from_bundle(force=False):
    """Copie les fichiers web livrés avec le starter vers le cache utilisateur.
    N'écrase pas une version GitHub plus récente déjà installée.
    """
    try:
        _safe_makedirs(WEB_CURRENT_DIR)
        bundled_v = _web_dir_version(RES_DIR)
        current_v = _web_dir_version(WEB_CURRENT_DIR)
        if (not force) and _valid_web_dir(WEB_CURRENT_DIR) and (not bundled_v or current_v >= bundled_v):
            return WEB_CURRENT_DIR
        for name in WEB_FILES:
            src = os.path.join(RES_DIR, name)
            if os.path.exists(src):
                _copy_file_atomic(src, os.path.join(WEB_CURRENT_DIR, name))
        if _valid_web_dir(WEB_CURRENT_DIR):
            return WEB_CURRENT_DIR
    except Exception as exc:
        print("Initialisation du cache web ignorée:", exc)
    return RES_DIR


def _restore_previous_web_if_needed():
    global DIRECTORY
    if _valid_web_dir(WEB_CURRENT_DIR):
        DIRECTORY = WEB_CURRENT_DIR
        return DIRECTORY
    if _valid_web_dir(WEB_PREVIOUS_DIR):
        try:
            if os.path.exists(WEB_CURRENT_DIR):
                shutil.rmtree(WEB_CURRENT_DIR, ignore_errors=True)
            shutil.copytree(WEB_PREVIOUS_DIR, WEB_CURRENT_DIR)
            DIRECTORY = WEB_CURRENT_DIR
            print("Interface restaurée depuis la dernière version fonctionnelle.")
            return DIRECTORY
        except Exception:
            pass
    DIRECTORY = _seed_runtime_web_from_bundle(force=True)
    return DIRECTORY

# Répertoire effectivement servi par le mini-serveur local.
DIRECTORY = _seed_runtime_web_from_bundle()

def _write_pid_file(port):
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()) + "\n" + str(port) + "\n")
    except Exception:
        pass

def _remove_pid_file():
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, encoding="utf-8") as f:
                first = (f.readline() or "").strip()
            if first == str(os.getpid()):
                os.remove(PID_FILE)
    except Exception:
        pass


def _as_line(s):
    """Einzeiliger String (z. B. Betreff) fuer ein AppleScript-Literal."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _as_text(s):
    """Mehrzeiliger Text fuer ein AppleScript-Literal; Zeilenumbrueche bleiben erhalten."""
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return s


def build_outlook_script(subject, body, to_list, bcc_list, action):
    """AppleScript-Zeilen, die in Microsoft Outlook (Mac) eine Mail erstellen
    und je nach action senden ('send') oder zur Kontrolle oeffnen ('open').
    Inhalt wird als REINER TEXT gesetzt (Outlook fuer Mac rendert hier kein HTML)."""
    lines = ['tell application "Microsoft Outlook"']
    lines.append(
        'set newMsg to make new outgoing message with properties '
        '{{subject:"{s}", content:"{c}"}}'.format(s=_as_line(subject), c=_as_text(body))
    )
    for a in (to_list or []):
        lines.append(
            'make new to recipient at newMsg with properties '
            '{{email address:{{address:"{a}"}}}}'.format(a=_as_line(a))
        )
    for b in (bcc_list or []):
        lines.append(
            'make new bcc recipient at newMsg with properties '
            '{{email address:{{address:"{a}"}}}}'.format(a=_as_line(b))
        )
    lines.append("send newMsg" if action == "send" else "open newMsg")
    lines.append("end tell")
    return lines


def html_to_text(s):
    """Word/Outlook-HTML-Signatur in sauberen Klartext umwandeln."""
    import re
    import html as _html
    s = re.sub(r"(?is)<!--.*?-->", "", s)
    s = re.sub(r"(?is)<head.*?</head>", "", s)
    s = re.sub(r"(?is)<style.*?</style>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6])\s*>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = _html.unescape(s).replace("\xa0", " ")
    out = []
    for ln in s.split("\n"):
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out)


def _signatures_mac():
    names_script = (
        'set out to ""\n'
        'tell application "Microsoft Outlook"\n'
        '  repeat with s in signatures\n'
        '    set out to out & (name of s) & linefeed\n'
        '  end repeat\n'
        'end tell\n'
        'return out'
    )
    proc = subprocess.run(["osascript", "-e", names_script],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Signatures illisibles")
    names = [n.strip() for n in proc.stdout.splitlines() if n.strip()]
    sigs = []
    for name in names:
        cp = subprocess.run(
            ["osascript", "-e",
             'tell application "Microsoft Outlook" to get content of signature "{n}"'.format(
                 n=name.replace("\\", "\\\\").replace('"', '\\"'))],
            capture_output=True, text=True, timeout=30)
        text = html_to_text(cp.stdout) if cp.returncode == 0 else ""
        sigs.append({"name": name, "text": text})
    return sigs


def _signatures_windows():
    """Klassisches Outlook fuer Windows: Signatur-.htm-Dateien lesen."""
    base = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Signatures")
    if not os.path.isdir(base):
        return []
    sigs = []
    for fn in sorted(os.listdir(base)):
        if not fn.lower().endswith(".htm") and not fn.lower().endswith(".html"):
            continue
        path = os.path.join(base, fn)
        raw = b""
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        html = None
        for enc in ("utf-8", "cp1252", "utf-16", "latin-1"):
            try:
                html = raw.decode(enc)
                break
            except Exception:
                continue
        if html is None:
            html = raw.decode("utf-8", "replace")
        sigs.append({"name": os.path.splitext(fn)[0], "text": html_to_text(html)})
    return sigs


def get_outlook_signatures():
    """Liste der Outlook-Signaturen als [{name, text}] (Inhalt als Klartext).
    Funktioniert auf macOS (AppleScript) und Windows (Signatur-Dateien)."""
    if os.name == "nt":
        return _signatures_windows()
    return _signatures_mac()


def find_signature_logo():
    """Versucht, das Logo-Bild der Signatur automatisch zu finden.
    Windows: groesstes Bild im Outlook-Signaturordner.
    macOS:  haeufigstes kleines Bild in den Apple-Mail-Anhaengen (Signatur-Logo
            wiederholt sich in vielen Mails)."""
    import base64
    import glob
    import hashlib
    import collections

    def as_logo(path, data, count=None):
        ext = path.lower().rsplit(".", 1)[-1] if "." in path else "png"
        mime = "image/" + ("jpeg" if ext in ("jpg", "jpeg") else ext)
        return {"name": "logo." + ext, "mime": mime,
                "dataUrl": "data:%s;base64,%s" % (mime, base64.b64encode(data).decode()),
                "count": count}

    if os.name == "nt":
        base = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Signatures")
        best, best_sz = None, 0
        for root, _dirs, files in os.walk(base):
            for fn in files:
                ext = fn.lower().rsplit(".", 1)[-1] if "." in fn else ""
                if ext in ("png", "jpg", "jpeg", "gif"):
                    p = os.path.join(root, fn)
                    try:
                        sz = os.path.getsize(p)
                    except Exception:
                        continue
                    if 1500 < sz and sz > best_sz:
                        best, best_sz = p, sz
        if best:
            with open(best, "rb") as f:
                return as_logo(best, f.read())
        return None

    # macOS: haeufigstes kleines Bild in Apple-Mail
    files = []
    for ext in ("png", "jpg", "jpeg"):
        files += glob.glob(os.path.expanduser("~/Library/Mail/**/image00*." + ext), recursive=True)
    counter = collections.Counter()
    info = {}
    for f in files[:4000]:
        try:
            sz = os.path.getsize(f)
            if sz < 3000 or sz > 300000:
                continue
            data = open(f, "rb").read()
            hh = hashlib.md5(data).hexdigest()
            counter[hh] += 1
            info[hh] = (f, data)
        except Exception:
            continue
    if not counter:
        return None
    hh, n = counter.most_common(1)[0]
    if n < 2:
        return None
    f, data = info[hh]
    return as_logo(f, data, n)


## ===================== Microsoft Graph (Geraetecode-Login) =====================
# Oeffentlicher Microsoft-Client "Microsoft Graph Command Line Tools" -> keine
# eigene App-Registrierung / keine IT noetig. Jeder meldet sich einmal an.
GRAPH_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH_TENANT = "organizations"
GRAPH_SCOPE = ("https://graph.microsoft.com/Mail.Send "
               "https://graph.microsoft.com/User.Read offline_access")
OAUTH_BASE = "https://login.microsoftonline.com/" + GRAPH_TENANT + "/oauth2/v2.0"
TOKEN_FILE = os.path.join(PERSIST_DIR, "graph_token.json")

_pending_device = {}   # zwischen Login-Start und Polling
_pending_web = {}      # v176: state -> {verifier, redirect_uri} für den Login ohne Code (PKCE)
_last_login_result = {"ok": None, "message": "", "at": 0}


def _gctx():
    return make_ssl_context()


def _form_post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, context=_gctx(), timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"error": "http_" + str(e.code)}


def _graph_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, context=_gctx(), timeout=30) as r:
        return json.load(r)


def _graph_post(url, token, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, body, {
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_gctx(), timeout=30) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _save_tokens(tok, account=None):
    data = {
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
    }
    if account:
        data["account"] = account
    else:
        old = _load_tokens()
        if old and old.get("account"):
            data["account"] = old["account"]
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _access_token():
    """Gibt ein gueltiges Access-Token zurueck (erneuert bei Bedarf) oder None."""
    t = _load_tokens()
    if not t:
        return None
    if t.get("access_token") and time.time() < t.get("expires_at", 0):
        return t["access_token"]
    rt = t.get("refresh_token")
    if not rt:
        return None
    st, tok = _form_post(OAUTH_BASE + "/token", {
        "grant_type": "refresh_token", "client_id": GRAPH_CLIENT_ID,
        "refresh_token": rt, "scope": GRAPH_SCOPE})
    if "access_token" in tok:
        _save_tokens(tok)
        return tok["access_token"]
    return None


def make_ssl_context():
    """SSL-Kontext mit zuverlaessigem Zertifikatsspeicher.

    Auf manchen Macs hat Python keinen funktionierenden System-CA-Store.
    Darum versuchen wir der Reihe nach:
    1) certifi, falls installiert,
    2) das mitgelieferte cacert.pem neben der App,
    3) den Python-Standard.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        bundled = os.path.join(RES_DIR, "cacert.pem")
        if os.path.exists(bundled):
            return ssl.create_default_context(cafile=bundled)
    except Exception:
        pass
    return ssl.create_default_context()


def make_github_ssl_context():
    """SSL-Kontext nur fuer den Download der statischen GitHub-Oberflaeche.

    Wenn auch das mitgelieferte CA-Buendel scheitert, wird nur fuer diese
    statischen HTML/PNG-Dateien einmal ohne Zertifikatspruefung versucht.
    Microsoft/e-Bichelchen-Aufrufe verwenden weiterhin make_ssl_context().
    """
    try:
        return make_ssl_context()
    except Exception:
        return ssl._create_unverified_context()


def normalize_defer_until(value):
    """UTC-Zeitstempel fuer Graph/PidTagDeferredSendTime oder Exception.
    Sicherheitsregel: Ist der Zeitpunkt ungueltig oder bereits vorbei, wird NICHT sofort gesendet.
    """
    raw = (value or "").strip() if isinstance(value, str) else ""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except Exception:
        raise ValueError("Moment de rappel invalide: " + raw)
    if dt <= datetime.now(timezone.utc) + timedelta(seconds=30):
        raise ValueError("Le moment du rappel est déjà passé.")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_load_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _backup_state_if_needed():
    """Legt höchstens etwa alle 6 Stunden ein Backup an und behält die letzten 12."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        os.makedirs(STATE_BACKUP_DIR, exist_ok=True)
        last = 0
        for name in os.listdir(STATE_BACKUP_DIR):
            if name.startswith("state-") and name.endswith(".json"):
                try:
                    last = max(last, os.path.getmtime(os.path.join(STATE_BACKUP_DIR, name)))
                except Exception:
                    pass
        if time.time() - last < 6 * 3600:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(STATE_BACKUP_DIR, "state-" + stamp + ".json")
        try:
            import shutil
            shutil.copy2(STATE_FILE, dst)
        except Exception:
            return
        items = []
        for name in os.listdir(STATE_BACKUP_DIR):
            if name.startswith("state-") and name.endswith(".json"):
                p = os.path.join(STATE_BACKUP_DIR, name)
                try:
                    items.append((os.path.getmtime(p), p))
                except Exception:
                    pass
        for _, old in sorted(items, reverse=True)[12:]:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # v310: only the harmless capabilities endpoint is cross-origin readable,
        # so a stale local tab can discover a newer helper on another loopback port.
        if self.path.split("?", 1)[0] == "/api/graph/capabilities":
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Etwas ruhiger im Terminal
        pass

    def _host_header_is_local(self) -> bool:
        """v311: Schutz vor DNS-Rebinding.

        Der Helfer lauscht nur auf 127.0.0.1, aber eine fremde Webseite kann einen
        eigenen Namen auf 127.0.0.1 auflösen lassen und wäre dann für den Browser
        gleichursprünglich – und käme an Microsoft-Konto und Schülerdaten. Ein
        echter lokaler Aufruf trägt immer eine Loopback-Adresse im Host-Header.
        """
        host = str(self.headers.get("Host") or "")
        if not host:
            return True  # HTTP/1.0-Aufrufe ohne Host-Header, z. B. curl
        name = host.rsplit(":", 1)[0].strip().strip("[]").lower()
        return name in ("127.0.0.1", "localhost", "::1", "")

    def _reject_foreign_host(self) -> bool:
        if self._host_header_is_local():
            return False
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        try:
            self.wfile.write("EntretienConnect: seuls les accès locaux sont autorisés.\n".encode("utf-8"))
        except Exception:
            pass
        return True

    def do_GET(self):
        global last_heartbeat_time
        if self._reject_foreign_host():
            return
        if self.path.split("?",1)[0] == "/api/app/heartbeat":
            last_heartbeat_time = time.time()
            return self._json(200, {"ok": True, "serverTime": datetime.now(timezone.utc).isoformat()})
        if self.path.split("?",1)[0] == "/api/app/shutdown":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json(200, {"ok": True, "shuttingDown": True})
        if self.path.split("?",1)[0] == "/api/app/storage":
            return self.handle_app_storage_get()
        if self.path.split("?",1)[0] == "/api/app/local-csv":
            return self.handle_app_local_csv()
        if self.path.split("?",1)[0] == "/api/app/session-status":
            return self.handle_app_session_status()
        parsed0 = urllib.parse.urlparse(self.path)
        path0 = parsed0.path
        qs0 = urllib.parse.parse_qs(parsed0.query)
        # v176: OAuth-Code-Login kehrt auf die Wurzel zurueck:
        # http://localhost:<port>/?code=...&state=...
        # Der Microsoft-Client "Graph Command Line Tools" ist nur fuer
        # http://localhost ohne Pfad registriert. Azure ignoriert beim
        # Loopback-Redirect den Port, aber NICHT den Pfad. Deshalb darf hier
        # nicht /oauth/redirect verwendet werden.
        if path0 == "/" and (qs0.get("code") or qs0.get("state") or qs0.get("error")):
            return self.handle_oauth_redirect()
        if self.path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/graph.html")
            self.end_headers()
            return
        if self.path == "/api/outlook-signatures":
            return self.handle_signatures()
        if self.path.split("?", 1)[0] == "/api/graph/capabilities":
            return self._json(200, {"ok": True, "deferredSend": True, "platform": "python", "appVersion": _helper_version(), "backendGeneration": 332, "nativeLoginEngine": ("WKWebView-v332" if sys.platform == "darwin" else "chromium-helper"), "port": getattr(self.server, "server_address", (None, PORT))[1], "ebichelchen": EB_AVAILABLE, "firefoxBidi": bool(EB_AVAILABLE and getattr(eb, "supports_firefox_bidi", lambda: False)()), "webDir": DIRECTORY, "persistDir": PERSIST_DIR})
        if self.path == "/api/graph/account":
            return self.handle_graph_account()
        if self.path.split("?", 1)[0] == "/oauth/redirect":
            return self.handle_oauth_redirect()
        if self.path == "/api/find-logo":
            return self.handle_find_logo()
        if self.path.startswith("/api/eb/"):
            return self.handle_eb_get()
        return super().do_GET()

    # ---------------------------------------------------------- App-Speicher / Status
    def handle_app_storage_get(self):
        st = _json_load_file(STATE_FILE)
        try:
            mtime = os.path.getmtime(STATE_FILE) if os.path.exists(STATE_FILE) else 0
        except Exception:
            mtime = 0
        return self._json(200, {"ok": True, "state": st, "updatedAt": mtime, "path": STATE_FILE})

    def handle_app_storage_post(self):
        data = self._read_json()
        st = data.get("state") if isinstance(data, dict) else None
        if not isinstance(st, dict):
            return self._json(400, {"ok": False, "error": "Aucun état à enregistrer."})
        try:
            _backup_state_if_needed()
            st["_savedAt"] = int(time.time() * 1000)
            _atomic_write_json(STATE_FILE, st)
            return self._json(200, {"ok": True, "path": STATE_FILE})
        except Exception as exc:
            return self._json(500, {"ok": False, "error": str(exc)})


    def handle_app_local_csv(self):
        """Liefert die optionale lokale CSV aus dem Starter-Ordner.

        Erwarteter Ort:
        - eleves_contacts.csv liegt direkt neben EntretienConnect_MAC.app / EntretienConnect_WINDOWS.vbs
        - kein Raten in Downloads/Desktop/Dokumente
        - technischer Fallback: Helper-Ordner oder dauerhafter App-Speicher
        """
        candidates = _local_csv_candidate_paths()
        found_path = None
        for p in candidates:
            try:
                if p and os.path.isfile(p):
                    found_path = p
                    break
            except Exception:
                pass
        if not found_path:
            return self._json(200, {"ok": True, "found": False, "filename": LOCAL_CSV_NAME, "checked": candidates})
        try:
            with open(found_path, "rb") as f:
                raw = f.read()
            text = None
            encoding = ""
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    encoding = enc
                    break
                except Exception:
                    continue
            if text is None:
                text = raw.decode("utf-8", "replace")
                encoding = "utf-8-replace"
            st = os.stat(found_path)
            return self._json(200, {
                "ok": True,
                "found": True,
                "filename": LOCAL_CSV_NAME,
                "path": found_path,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "encoding": encoding,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": text,
            })
        except Exception as exc:
            return self._json(500, {"ok": False, "found": False, "error": str(exc), "filename": LOCAL_CSV_NAME, "checked": candidates})

    def handle_app_session_status(self):
        acct = None
        signed = False
        t = _load_tokens()
        if t and t.get("account") and _access_token():
            signed = True
            acct = t.get("account")
        eb_status = {"available": EB_AVAILABLE, "hasData": False}
        if EB_AVAILABLE:
            try:
                data, at = eb.get_current()
                eb_status.update({"hasData": bool(data), "receivedAt": at})
            except Exception as exc:
                eb_status.update({"error": str(exc)})
        return self._json(200, {"ok": True, "microsoft": {"signedIn": signed, "account": acct}, "ebichelchen": eb_status})

    # ---------------------------------------------------------- e-Bichelchen
    def handle_eb_get(self):
        if not EB_AVAILABLE:
            return self._json(200, {"ok": False, "error": "Module e-Bichelchen indisponible: " + globals().get("_EB_IMPORT_ERROR", "?")})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        def q(name, default=""):
            return qs.get(name, [default])[0]

        try:
            if path == "/api/eb/status":
                data, at = eb.get_current()
                # v331: savedSession sagt der Oberfläche, dass eine gemerkte Sitzung
                # vorliegt – sie verbindet dann beim Start von selbst.
                saved = False
                try:
                    saved = bool(getattr(eb, "has_saved_session", lambda: False)())
                except Exception:
                    saved = False
                return self._json(200, {"ok": True, "hasData": bool(data), "receivedAt": at, "data": data, "savedSession": saved})
            if path == "/api/eb/resume":
                # v332: Klassen aus der gemerkten Sitzung lesen – ohne Loginfenster.
                # Scheitert das, meldet die Oberfläche einfach « nicht verbunden »
                # und der Benutzer klickt wie bisher auf « Verbinden ».
                try:
                    payload = eb.resume_saved_session()
                    _, at = eb.get_current()
                    return self._json(200, {"ok": True, "data": payload, "receivedAt": at, "resumed": True})
                except Exception as exc:
                    expired = isinstance(exc, getattr(eb, "EbSessionExpired", ()))
                    return self._json(200, {"ok": False, "resumed": False, "sessionExpired": bool(expired), "error": str(exc)})
            if path == "/api/eb/open-browser":
                info = eb.launch_browser(q("profile", "default"), preferred_browser=q("browser", "auto"), user_agent=q("ua", ""))
                return self._json(200, {"ok": True, "info": info})
            if path == "/api/eb/login-ready":
                return self._json(200, eb.check_login_ready())
            if path == "/api/eb/read-browser":
                quiet = q("quiet", "0") in ("1", "true", "True", "yes")
                gid_raw = q("groupId", "")
                gid = None
                if gid_raw not in ("", "null", "undefined"):
                    try:
                        gid = int(gid_raw)
                    except Exception:
                        gid = None
                # v287: a manually closed helper browser is not a login state. Report it
                # explicitly so the UI can return to « Non connecté / Connecter ».
                if quiet and not eb.debug_browser_running():
                    return self._json(200, {"ok": False, "waiting": False, "browserClosed": True, "error": "Fenêtre e-Bichelchen fermée."})
                try:
                    payload = eb.read_browser_and_store(gid)
                    _, at = eb.get_current()
                    return self._json(200, {"ok": True, "data": payload, "receivedAt": at})
                except Exception as exc:
                    browser_closed = not eb.debug_browser_running()
                    error_text = str(exc)
                    # v311: Eine abgelaufene Sitzung ist kein Wartezustand – die Oberfläche
                    # soll sofort wieder « Connecter » anbieten statt weiterzupollen.
                    session_expired = isinstance(exc, getattr(eb, "EbSessionExpired", ()))
                    if session_expired:
                        return self._json(200, {"ok": False, "waiting": False, "browserClosed": True, "sessionExpired": True, "error": error_text})
                    retry_soon = ("Relecture en cours" in error_text or "sélectionnée automatiquement" in error_text)
                    return self._json(200 if quiet else 500, {"ok": False, "waiting": bool(quiet and not browser_closed), "browserClosed": browser_closed, "retrySoon": retry_soon, "error": error_text})
            if path == "/api/eb/focus-app":
                return self._json(200, {"ok": True, "info": eb.focus_app_tab()})
            if path == "/api/eb/close":
                return self._json(200, {"ok": True, "info": eb.close_ebichelchen_target()})
            if path == "/api/eb/cleanup":
                close_eb = q("closeEb", "1") not in ("0", "false", "False", "no")
                focus_app = q("focusApp", "1") not in ("0", "false", "False", "no")
                return self._json(200, {"ok": True, "info": eb.cleanup_after_read(close_eb=close_eb, focus_app=focus_app)})
            if path == "/api/eb/park":
                # v297: Kompatibilitätsroute für gecachte v296-Seiten; minimiert nie mehr, sondern schließt nur den e-Bichelchen-Tab.
                focus_app = q("focusApp", "1") not in ("0", "false", "False", "no")
                return self._json(200, {"ok": True, "info": eb.park_after_read(focus_app=focus_app)})
            if path == "/api/eb/clear":
                eb.clear_current()
                return self._json(200, {"ok": True})
            if path == "/api/eb/reset-session":
                # v292: soft=1 verwirft die halbfertige IAM-Sitzung, hält den Browser aber
                # "warm" (Cookies per DevTools löschen, kein Schließen/Kaltstart).
                soft = q("soft", "0") in ("1", "true", "True", "yes")
                if soft:
                    return self._json(200, {"ok": True, "info": eb.soft_reset_login()})
                # v285: Im normalen Verbindungsfluss bleibt das App-Browserprofil erhalten.
                # Das spart den langsamen Kaltstart; Cookies/Sitzungsdaten werden trotzdem
                # entfernt. Mit light=0 bleibt der frühere vollständige Reset verfügbar.
                light = q("light", "0") in ("1", "true", "True", "yes")
                return self._json(200, {"ok": True, "info": eb.reset_login_session(q("profile", "default"), preserve_profile=light)})
            return self._json(404, {"ok": False, "error": "Route e-Bichelchen inconnue."})
        except Exception as exc:
            return self._json(500, {"ok": False, "error": str(exc)})

    def handle_eb_post(self):
        if not EB_AVAILABLE:
            return self._json(200, {"ok": False, "error": "Module e-Bichelchen indisponible."})
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/eb/create-bulk-entries":
                result = eb.create_bulk_entries(body)
                return self._json(200 if result.get("ok") else 500, result)
            if path == "/api/eb/delete-created-entries":
                result = eb.delete_created_entries(body)
                return self._json(200 if result.get("ok") else 500, result)
            return self._json(404, {"ok": False, "error": "Route e-Bichelchen inconnue."})
        except Exception as exc:
            # v311: Abgelaufene Sitzung getrennt melden, damit die Oberfläche zum
            # Neuverbinden auffordert statt einen Speicherfehler anzuzeigen.
            expired = isinstance(exc, getattr(eb, "EbSessionExpired", ()))
            return self._json(500, {"ok": False, "error": str(exc), "sessionExpired": bool(expired), "created": [], "deleted": [], "errors": []})

    def handle_find_logo(self):
        try:
            logo = find_signature_logo()
            if logo:
                return self._json(200, {"ok": True, "logo": logo})
            return self._json(200, {"ok": False, "error": "Aucun logo trouvé."})
        except Exception as e:
            return self._json(200, {"ok": False, "error": str(e)})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---- Graph: Konto-Status ----
    def handle_graph_account(self):
        t = _load_tokens()
        extra = {}
        try:
            if _last_login_result.get("ok") is False and time.time() - float(_last_login_result.get("at") or 0) < 600:
                extra["lastLoginError"] = _last_login_result.get("message") or "Connexion impossible."
        except Exception:
            pass
        if t and t.get("account") and _access_token():
            extra["lastLoginError"] = ""
            return self._json(200, dict({"ok": True, "signedIn": True, "account": t["account"]}, **extra))
        return self._json(200, dict({"ok": True, "signedIn": False}, **extra))

    # ---- Graph: Login starten (page web, sans code à saisir) — v176 ----
    # Auth-code + PKCE avec redirection loopback vers ce helper. L'utilisateur ne fait
    # que la connexion IAM ; aucun code d'appareil à copier.
    # IMPORTANT v176 : redirect_uri = http://localhost:<port>/ (racine).
    # Avec ce client public, Azure accepte le port loopback mais le chemin doit
    # rester la racine enregistrée ; /oauth/redirect provoque AADSTS50011.
    def handle_graph_login_start_web(self):
        import base64, secrets
        global _last_login_result
        _last_login_result = {"ok": None, "message": "", "at": time.time()}
        _pending_device.clear()
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(24)
        host = self.headers.get("Host", "") or "127.0.0.1"
        port = host.split(":")[1] if ":" in host else "80"
        redirect_uri = "http://localhost:" + port + "/"
        _pending_web.clear()
        _pending_web[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "created": time.time()}
        auth_url = OAUTH_BASE + "/authorize?" + urllib.parse.urlencode({
            "client_id": GRAPH_CLIENT_ID, "response_type": "code",
            "redirect_uri": redirect_uri, "scope": GRAPH_SCOPE,
            "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account",
        })
        return self._json(200, {"ok": True, "authUrl": auth_url})

    def handle_oauth_redirect(self):
        import html as _html
        global _last_login_result
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        err = (qs.get("error_description") or qs.get("error") or [""])[0]
        pend = _pending_web.pop(state, None) if state else None
        ok = False
        msg = ""
        if not pend:
            msg = "Session de connexion inconnue ou expirée. Réessayez depuis EntretienConnect."
        elif not code:
            msg = err or "Connexion annulée."
        else:
            st, tok = _form_post(OAUTH_BASE + "/token", {
                "grant_type": "authorization_code", "client_id": GRAPH_CLIENT_ID,
                "code": code, "redirect_uri": pend["redirect_uri"],
                "code_verifier": pend["verifier"], "scope": GRAPH_SCOPE})
            if "access_token" in tok:
                account = {"name": "", "email": ""}
                try:
                    me = _graph_get("https://graph.microsoft.com/v1.0/me?$select=displayName,mail,userPrincipalName",
                                    tok["access_token"])
                    account = {"name": me.get("displayName", ""),
                               "email": me.get("mail") or me.get("userPrincipalName", "")}
                except Exception:
                    pass
                _save_tokens(tok, account)
                ok = True
            else:
                msg = tok.get("error_description") or tok.get("error") or "Échec de l'échange du code."
        _last_login_result = {"ok": bool(ok), "message": ("" if ok else msg), "at": time.time()}
        if ok:
            # v324: Der Tab schliesst sich schneller. Vorher wartete er 1,5 Sekunden,
            # und die App fragte anschliessend nur alle 3 Sekunden nach – zusammen bis
            # zu viereinhalb Sekunden, in denen nach der Anmeldung scheinbar nichts
            # geschah. Das war der ganze Unterschied zum e-Bichelchen-Login.
            inner = ("<div style='font-size:42px'>✅</div><h2>Connexion réussie</h2>"
                     "<p>Cet onglet se ferme tout seul.</p>"
                     "<script>setTimeout(function(){ try{ window.close(); }catch(e){} }, 400);</script>")
        else:
            inner = ("<div style='font-size:42px'>⚠️</div><h2>Connexion impossible</h2>"
                     "<p>" + _html.escape(msg) + "</p><p>Fermez cet onglet et réessayez depuis EntretienConnect.</p>")
        page = ("<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>EntretienConnect</title></head>"
                "<body style=\"font-family:-apple-system,'Segoe UI',Arial,sans-serif;display:flex;align-items:center;"
                "justify-content:center;height:100vh;margin:0;background:#f6f7fb;color:#1c2333\">"
                "<div style='text-align:center;max-width:480px;padding:20px'>" + inner + "</div></body></html>")
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- Graph: Login starten (Geraetecode) ----
    def handle_graph_login_start(self):
        global _last_login_result
        _last_login_result = {"ok": None, "message": "", "at": time.time()}
        _pending_web.clear()
        st, dc = _form_post(OAUTH_BASE + "/devicecode",
                            {"client_id": GRAPH_CLIENT_ID, "scope": GRAPH_SCOPE})
        if "device_code" not in dc:
            return self._json(200, {"ok": False, "error": dc.get("error_description") or dc.get("error") or "Erreur"})
        _pending_device["device_code"] = dc["device_code"]
        _pending_device["interval"] = int(dc.get("interval", 5))
        return self._json(200, {"ok": True,
                                "user_code": dc.get("user_code"),
                                "verification_uri": dc.get("verification_uri"),
                                "message": dc.get("message")})

    # ---- Graph: Login abfragen (Polling) ----
    def handle_graph_login_poll(self):
        dcode = _pending_device.get("device_code")
        if not dcode:
            return self._json(200, {"ok": False, "error": "Aucune connexion démarrée."})
        st, tok = _form_post(OAUTH_BASE + "/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": GRAPH_CLIENT_ID, "device_code": dcode})
        if "access_token" in tok:
            _save_tokens(tok)
            account = {"name": "", "email": ""}
            try:
                me = _graph_get("https://graph.microsoft.com/v1.0/me?$select=displayName,mail,userPrincipalName",
                                tok["access_token"])
                account = {"name": me.get("displayName", ""),
                           "email": me.get("mail") or me.get("userPrincipalName", "")}
            except Exception:
                pass
            _save_tokens(tok, account)
            _pending_device.clear()
            return self._json(200, {"ok": True, "status": "ok", "account": account})
        err = tok.get("error")
        if err in ("authorization_pending", "slow_down"):
            return self._json(200, {"ok": True, "status": "pending"})
        _pending_device.clear()
        return self._json(200, {"ok": True, "status": "error",
                                "error": tok.get("error_description") or err or "Erreur"})

    # ---- Graph: hängenden Loginversuch zurücksetzen ----
    def handle_graph_login_reset(self):
        global _last_login_result
        _pending_device.clear()
        _pending_web.clear()
        _last_login_result = {"ok": None, "message": "", "at": time.time()}
        return self._json(200, {"ok": True})

    # ---- Graph: Abmelden ----
    def handle_graph_logout(self):
        try:
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
        except Exception:
            pass
        _pending_device.clear()
        _pending_web.clear()
        try:
            global _last_login_result
            _last_login_result = {"ok": None, "message": "", "at": time.time()}
        except Exception:
            pass
        return self._json(200, {"ok": True})

    # ---- Graph: Senden ----
    def handle_graph_send(self):
        data = self._read_json()
        messages = data.get("messages", []) or []
        logo = data.get("logo")  # {name, mime, contentBytes} oder None
        # PDF global pour les rappels (compat < v163) ; depuis v163 chaque message porte son propre "attachment".
        reminder_attachment = data.get("reminderAttachment") or None
        if not messages:
            return self._json(200, {"ok": False, "error": "Aucun e-mail."})
        token = _access_token()
        if not token:
            return self._json(200, {"ok": False, "error": "Non connecté. Veuillez vous reconnecter avec Microsoft."})
        results = []
        for m in messages:
            payload = {
                "message": {
                    "subject": m.get("subject", ""),
                    "body": {"contentType": "HTML", "content": m.get("html", "")},
                    "toRecipients": [{"emailAddress": {"address": a}} for a in (m.get("to") or []) if a],
                },
                "saveToSentItems": True,
            }
            try:
                defer_until = normalize_defer_until(m.get("deferUntil"))
            except Exception as e:
                results.append({"id": m.get("id"), "ok": False, "error": str(e)})
                continue
            if defer_until:
                payload["message"]["singleValueExtendedProperties"] = [{
                    "id": "SystemTime 0x3FEF",
                    "value": defer_until,
                }]
            attachments = []
            if logo and logo.get("contentBytes") and "cid:siglogo" in m.get("html", ""):
                attachments.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": logo.get("name", "logo.png"),
                    "contentType": logo.get("mime", "image/png"),
                    "contentBytes": logo["contentBytes"],
                    "isInline": True, "contentId": "siglogo",
                })
            att = m.get("attachment") or None
            if (not att and m.get("kind") == "reminder" and reminder_attachment
                    and reminder_attachment.get("contentBytes")):
                att = reminder_attachment
            if att and att.get("contentBytes"):
                attachments.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": att.get("name", "rappel.pdf"),
                    "contentType": att.get("contentType") or att.get("mime") or "application/pdf",
                    "contentBytes": att["contentBytes"],
                })
            if attachments:
                payload["message"]["attachments"] = attachments
            # v322: Jede Nachricht einzeln absichern. Vorher riss ein Netzwerkaussetzer
            # bei Nachricht 3 die gesamte Antwort ab (die Verbindung brach ohne Antwort
            # zusammen). Die bereits verschickten Mails waren dann für die App verloren –
            # sie galten als nicht gesendet, und ein zweiter Versuch schickte sie den
            # Eltern ein zweites Mal.
            try:
                code, body = _graph_post("https://graph.microsoft.com/v1.0/me/sendMail", token, payload)
                # 429 = Microsoft drosselt. Dabei wurde NICHTS verschickt, ein zweiter
                # Versuch ist also gefahrlos.
                if code == 429:
                    wait = 0.0
                    match = re.search(r'"Retry-After"\s*:\s*"?(\d+)', body or "")
                    if match:
                        wait = float(match.group(1))
                    time.sleep(max(1.0, min(wait or 5.0, 15.0)))
                    code, body = _graph_post("https://graph.microsoft.com/v1.0/me/sendMail", token, payload)
                if code == 202:
                    results.append({"id": m.get("id"), "ok": True})
                else:
                    results.append({"id": m.get("id"), "ok": False, "error": "HTTP " + str(code) + " " + body[:200]})
            except Exception as exc:
                # Hier ist UNBEKANNT, ob Microsoft die Mail noch angenommen hat. Deshalb
                # nicht als schlichter Fehlschlag melden: Ein blindes Wiederholen könnte
                # dieselbe Mail ein zweites Mal zustellen.
                results.append({
                    "id": m.get("id"),
                    "ok": False,
                    "uncertain": True,
                    "error": "Verbindung unterbrochen: " + str(exc)[:160],
                })
        return self._json(200, {"ok": True, "results": results})

    def handle_signatures(self):
        try:
            sigs = get_outlook_signatures()
            return self._json(200, {"ok": True, "signatures": sigs})
        except Exception as e:
            return self._json(200, {"ok": False, "error": str(e)})

    def do_POST(self):
        global last_heartbeat_time
        if self._reject_foreign_host():
            return
        if self.path.split("?",1)[0] == "/api/app/heartbeat":
            last_heartbeat_time = time.time()
            return self._json(200, {"ok": True, "serverTime": datetime.now(timezone.utc).isoformat()})
        if self.path.split("?",1)[0] == "/api/app/shutdown":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json(200, {"ok": True, "shuttingDown": True})
        if self.path.split("?",1)[0] == "/api/app/storage":
            return self.handle_app_storage_post()
        if self.path.split("?",1)[0] == "/api/app/session-status":
            return self.handle_app_session_status()
        if self.path.split("?",1)[0] == "/api/graph/login-reset":
            return self.handle_graph_login_reset()
        if self.path == "/api/send":
            self.handle_send()
        elif self.path == "/api/outlook-send":
            self.handle_outlook_send()
        elif self.path == "/api/graph/login-start":
            self.handle_graph_login_start()
        elif self.path == "/api/graph/login-start-web":
            self.handle_graph_login_start_web()
        elif self.path == "/api/graph/login-poll":
            self.handle_graph_login_poll()
        elif self.path == "/api/graph/logout":
            self.handle_graph_logout()
        elif self.path == "/api/graph/send":
            self.handle_graph_send()
        elif self.path.startswith("/api/eb/"):
            self.handle_eb_post()
        else:
            self.send_error(404, "Not found")

    # ---------------------------------------------------------- Outlook (Mac)
    def handle_outlook_send(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"ok": False, "error": "Requête non valable."})

        from_addr = (data.get("from") or "").strip()
        action = data.get("action", "send")  # "send" oder "open"
        messages = data.get("messages", []) or []
        if not messages:
            return self._json(200, {"ok": False, "error": "Aucun e-mail."})

        results = []
        for m in messages:
            # Empfaenger direkt ins An-Feld (Einzelversand, kein BCC noetig)
            recipients = [r for r in (m.get("to") or m.get("bcc") or []) if r]
            script_lines = build_outlook_script(
                subject=m.get("subject", ""),
                body=m.get("body", ""),
                to_list=recipients,
                bcc_list=[],
                action=action,
            )
            args = ["osascript"]
            for line in script_lines:
                args += ["-e", line]
            try:
                proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
                if proc.returncode == 0:
                    results.append({"id": m.get("id"), "ok": True})
                else:
                    err = (proc.stderr or "").strip() or "Erreur AppleScript"
                    results.append({"id": m.get("id"), "ok": False, "error": err})
            except Exception as e:
                results.append({"id": m.get("id"), "ok": False, "error": str(e)})
        return self._json(200, {"ok": True, "results": results})

    # ------------------------------------------------------------------ send
    def handle_send(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json(400, {"ok": False, "error": "Requête non valable."})

        sender = data.get("sender", {}) or {}
        host = sender.get("host", "smtp.gmail.com")
        port = int(sender.get("port", 465))
        user = (sender.get("user") or sender.get("email") or "").strip()
        password = sender.get("password", "")
        from_name = sender.get("name", "")
        from_addr = (sender.get("email") or user).strip()
        messages = data.get("messages", []) or []

        if not user or not password:
            return self._json(200, {"ok": False,
                                    "error": "Adresse e-mail ou mot de passe d’application manquant."})
        if not messages:
            return self._json(200, {"ok": False, "error": "Aucun e-mail à envoyer."})

        results = []
        try:
            context = make_ssl_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                for m in messages:
                    try:
                        msg = EmailMessage()
                        msg["Subject"] = m.get("subject", "")
                        msg["From"] = (f"{from_name} <{from_addr}>"
                                       if from_name else from_addr)
                        # Empfaenger direkt ins An-Feld (Einzelversand)
                        recipients = [r for r in (m.get("to") or m.get("bcc") or []) if r]
                        if recipients:
                            msg["To"] = ", ".join(recipients)
                        msg.set_content(m.get("body", ""))
                        html = m.get("html")
                        if html:
                            msg.add_alternative(html, subtype="html")
                        server.send_message(msg)
                        results.append({"id": m.get("id"), "ok": True})
                    except Exception as e:
                        results.append({"id": m.get("id"), "ok": False,
                                        "error": str(e)})
            return self._json(200, {"ok": True, "results": results})
        except smtplib.SMTPAuthenticationError:
            return self._json(200, {"ok": False,
                                    "error": ("Échec de la connexion à Gmail. "
                                              "Vérifiez l’adresse de l’expéditeur et utilisez "
                                              "un mot de passe d’application (pas votre mot de passe "
                                              "habituel).")})
        except Exception as e:
            return self._json(200, {"ok": False, "error": str(e)})

    # ------------------------------------------------------------------ util
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # Browser/alter Helper wurde gerade geschlossen; Antwort kann dann ins Leere laufen.
            # Das ist harmlos und soll nicht als dramatischer Traceback im Log erscheinen.
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)) or getattr(exc, "errno", None) in (errno.EPIPE, errno.ECONNRESET, 54, 10053, 10054):
                return
            raise


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


GITHUB_UI_URL = "https://joelschartz.github.io/EntretienConnect/"
HELPER_MANIFEST_NAME = "starter-manifest.json"
HELPER_ALLOWED_TARGETS = {
    "server.py",
    "ebichelchen.py",
    "EntretienConnect.ps1",
    "EntretienConnect-eb.ps1",
    "EntretienConnect-Start.bat",
    "EntretienConnect-Start-Hidden.bat",
    "EntretienConnect-WKWebView.js",
    "cacert.pem",
    "VERSION.txt",
}


def _download_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent":"EntretienConnect"})
    try:
        ctx = make_ssl_context()
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return r.read()
    except ssl.SSLError:
        # Nur fuer GitHub Pages: statische App-/Helper-Dateien.
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return r.read()


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _version_number(value):
    import re
    m = re.search(r"(\d+)", str(value or ""))
    return int(m.group(1)) if m else 0


def _local_version_number():
    try:
        with open(os.path.join(DATA_DIR, "VERSION.txt"), "r", encoding="utf-8") as f:
            return _version_number(f.read())
    except Exception:
        return 0


def _html_app_version(data):
    try:
        txt = data.decode("utf-8", "ignore") if isinstance(data, (bytes, bytearray)) else str(data or "")
        import re
        m = re.search(r"const\s+APP_VERSION\s*=\s*(\d+)", txt)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def update_helper_from_github():
    """Télécharge le helper local depuis GitHub Pages, si un manifeste est présent.

    But: les collègues gardent le même Starter. Les fichiers Python/PowerShell
    du helper sont remplacés au démarrage; une mise à jour de server.py ou
    ebichelchen.py est donc active au prochain démarrage.
    """
    if getattr(sys, "frozen", False) or os.environ.get("ENTRETIENCONNECT_HELPER_READONLY") == "1":
        print("Mise à jour du helper ignorée: le Starter signé reste stable; l’interface se met à jour dans le dossier utilisateur.")
        return []
    base = GITHUB_UI_URL.rstrip("/") + "/"
    manifest_url = base + HELPER_MANIFEST_NAME + "?t=" + str(int(time.time()))
    try:
        raw = _download_bytes(manifest_url, timeout=12)
        if not raw or len(raw) > 2 * 1024 * 1024:
            return
        manifest = json.loads(raw.decode("utf-8"))
        remote_version = _version_number(manifest.get("version"))
        local_version = _local_version_number()
        if remote_version and local_version and remote_version < local_version:
            print(f"Mise à jour du helper ignorée: GitHub v{remote_version} est plus ancien que local v{local_version}.")
            return
        files = manifest.get("files", []) or []
    except Exception as e:
        print("Mise à jour du helper ignorée:", e)
        return

    updated = []
    for item in files:
        try:
            target = str(item.get("target", "")).replace("\\", "/").strip("/")
            rel_url = str(item.get("url", "")).strip()
            expected = str(item.get("sha256", "")).lower().strip()
            if target not in HELPER_ALLOWED_TARGETS or not rel_url:
                continue
            dst = os.path.join(DATA_DIR, target)
            if os.path.exists(dst) and expected and _sha256_file(dst).lower() == expected:
                continue
            src_url = rel_url if rel_url.startswith(("http://", "https://")) else base + rel_url.lstrip("/")
            data = _download_bytes(src_url + ("&" if "?" in src_url else "?") + "t=" + str(int(time.time())), timeout=20)
            if not data:
                continue
            if expected and _sha256_bytes(data).lower() != expected:
                print("Mise à jour du helper ignorée pour", target + ": checksum différent")
                continue
            tmp = dst + ".download"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            updated.append(target)
        except Exception as e:
            try:
                tmp = os.path.join(DATA_DIR, target) + ".download"
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            print("Mise à jour du helper ignorée pour", item.get("target", "?") + ":", e)
    if updated:
        print("Helper local actualisé depuis GitHub :", ", ".join(updated))
    return updated


def restart_after_helper_update_if_needed(updated):
    critical = {"server.py", "ebichelchen.py", "cacert.pem"}
    if not updated or not (set(updated) & critical):
        return
    if os.environ.get("ENTRETIENCONNECT_RESTARTED_AFTER_HELPER_UPDATE") == "1":
        return
    try:
        os.environ["ENTRETIENCONNECT_RESTARTED_AFTER_HELPER_UPDATE"] = "1"
        print("Redémarrage du helper local pour appliquer la mise à jour…")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        print("Redémarrage automatique impossible. La mise à jour sera active au prochain démarrage:", e)


def _promote_web_staging(remote_version=0):
    global DIRECTORY
    if not _valid_web_dir(WEB_STAGING_DIR):
        return False
    try:
        if os.path.exists(WEB_PREVIOUS_DIR):
            shutil.rmtree(WEB_PREVIOUS_DIR, ignore_errors=True)
        if _valid_web_dir(WEB_CURRENT_DIR):
            shutil.copytree(WEB_CURRENT_DIR, WEB_PREVIOUS_DIR)
        if os.path.exists(WEB_CURRENT_DIR):
            shutil.rmtree(WEB_CURRENT_DIR, ignore_errors=True)
        os.replace(WEB_STAGING_DIR, WEB_CURRENT_DIR)
        if not _valid_web_dir(WEB_CURRENT_DIR):
            raise RuntimeError("cache web incomplet après mise à jour")
        DIRECTORY = WEB_CURRENT_DIR
        return True
    except Exception as exc:
        print("Activation de la mise à jour GitHub impossible:", exc)
        try:
            if os.path.exists(WEB_STAGING_DIR):
                shutil.rmtree(WEB_STAGING_DIR, ignore_errors=True)
        except Exception:
            pass
        _restore_previous_web_if_needed()
        return False


def _update_ui_from_web_manifest(base):
    """Mise à jour robuste via web-manifest.json: téléchargement en staging,
    vérification SHA-256, puis activation atomique avec sauvegarde de secours.
    """
    manifest_url = base + WEB_MANIFEST_NAME + "?t=" + str(int(time.time()))
    try:
        raw = _download_bytes(manifest_url, timeout=12)
        if not raw or len(raw) > 512 * 1024:
            return False
        manifest = json.loads(raw.decode("utf-8"))
    except Exception:
        return False

    remote_version = _version_number(manifest.get("version"))
    local_version = max(_web_dir_version(WEB_CURRENT_DIR), _web_dir_version(RES_DIR))
    if remote_version and local_version and remote_version < local_version:
        print(f"Mise à jour GitHub ignorée: GitHub v{remote_version} est plus ancien que local v{local_version}.")
        _restore_previous_web_if_needed()
        return True

    files = manifest.get("files", []) or []
    wanted = {name: None for name in WEB_FILES}
    for item in files:
        target = str(item.get("target", "")).replace("\\", "/").strip("/")
        if target in wanted:
            wanted[target] = item
    if not all(wanted.values()):
        return False

    try:
        if os.path.exists(WEB_STAGING_DIR):
            shutil.rmtree(WEB_STAGING_DIR, ignore_errors=True)
        os.makedirs(WEB_STAGING_DIR, exist_ok=True)
        updated_names = []
        for name, item in wanted.items():
            rel_url = str(item.get("url", name)).strip()
            expected = str(item.get("sha256", "")).lower().strip()
            src_url = rel_url if rel_url.startswith(("http://", "https://")) else base + rel_url.lstrip("/")
            data = _download_bytes(src_url + ("&" if "?" in src_url else "?") + "t=" + str(int(time.time())), timeout=20)
            if not data or len(data) < 50:
                raise RuntimeError(name + ": fichier vide")
            if expected and _sha256_bytes(data).lower() != expected:
                raise RuntimeError(name + ": checksum différent")
            with open(os.path.join(WEB_STAGING_DIR, name), "wb") as f:
                f.write(data)
            updated_names.append(name)
        if _promote_web_staging(remote_version):
            print("Interface actualisée depuis GitHub :", ", ".join(updated_names))
            return True
    except Exception as exc:
        print("Mise à jour GitHub ignorée:", exc)
        try:
            if os.path.exists(WEB_STAGING_DIR):
                shutil.rmtree(WEB_STAGING_DIR, ignore_errors=True)
        except Exception:
            pass
        _restore_previous_web_if_needed()
        return True
    return False


def update_ui_from_github():
    """Télécharge au démarrage la dernière interface depuis GitHub Pages.
    v202: les fichiers web sont installés dans le dossier utilisateur
    (Application Support/AppData), avec checksum et retour à la dernière version
    fonctionnelle si un téléchargement est incomplet.
    """
    global DIRECTORY
    base = GITHUB_UI_URL.rstrip("/") + "/"
    _seed_runtime_web_from_bundle()
    if _update_ui_from_web_manifest(base):
        _restore_previous_web_if_needed()
        return

    # Fallback ancien, si web-manifest.json n'est pas encore publié.
    _safe_makedirs(WEB_CURRENT_DIR)
    for name in WEB_FILES:
        url = base + name + "?t=" + str(int(time.time()))
        tmp = os.path.join(WEB_CURRENT_DIR, "." + name + ".download")
        dst = os.path.join(WEB_CURRENT_DIR, name)
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"EntretienConnect"})
            try:
                ctx = make_ssl_context()
                with urllib.request.urlopen(req, context=ctx, timeout=20) as r, open(tmp, "wb") as f:
                    f.write(r.read())
            except ssl.SSLError:
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, context=ctx, timeout=20) as r, open(tmp, "wb") as f:
                    f.write(r.read())
            if os.path.exists(tmp) and os.path.getsize(tmp) > 100:
                if name == "graph.html":
                    try:
                        local_v = _html_app_version(open(dst, "rb").read()) if os.path.exists(dst) else _web_dir_version(RES_DIR)
                        remote_v = _html_app_version(open(tmp, "rb").read())
                        if remote_v and local_v and remote_v < local_v:
                            os.remove(tmp)
                            print(f"Mise à jour GitHub ignorée pour graph.html: GitHub v{remote_v} est plus ancien que local v{local_v}.")
                            continue
                    except Exception:
                        pass
                os.replace(tmp, dst)
                print("Interface actualisée depuis GitHub :", name)
            elif os.path.exists(tmp):
                os.remove(tmp)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            print("Mise à jour GitHub ignorée pour", name + ":", e)
    DIRECTORY = _restore_previous_web_if_needed()

def _open_in_browser(url):
    """Ouvre l'app via le navigateur par défaut, sans automation macOS.

    v211: ne plus lancer directement le binaire Chrome. Quand Chrome est déjà ouvert,
    cet appel pouvait parfois ne pas ouvrir le nouvel onglet. LaunchServices (`open URL`)
    est plus robuste et ne demande pas le contrôle de Google Chrome comme AppleScript.
    """
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
            return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    os.chdir(DIRECTORY)
    updated_helper = update_helper_from_github()
    restart_after_helper_update_if_needed(updated_helper)
    update_ui_from_github()
    host = "127.0.0.1"
    with _make_server_with_fallback(host, PORT, Handler)[0] as httpd:
        actual_port = httpd.server_address[1]
        used_fallback = actual_port != PORT
        url = f"http://{host}:{actual_port}/graph.html"
        print("=" * 56)
        print("  EntretienConnect est lancé.")
        if used_fallback:
            print(f"  Remarque : le port {PORT} était déjà utilisé.")
            print(f"  Le port {actual_port} a donc été utilisé automatiquement.")
        print(f"  Ouvrir dans le navigateur :  {url}")
        print("  Pour quitter : fermez le navigateur. Le helper local s’arrête ensuite automatiquement.")
        print("=" * 56)
        _write_pid_file(actual_port)
        # Ein eventuell von v302 übrig gebliebenes kontrolliertes Chrome-Fenster
        # gehört nur zum EntretienConnect-Hilfsprofil und wird einmalig entfernt.
        if EB_AVAILABLE:
            try:
                # Nur eine von v302 kontrollierte Chromium-Instanz schließen. Ein
                # fremder Browser, der zufällig einen DevTools-Port verwendet, bleibt
                # unangetastet.
                stale_targets = eb._list_cdp_targets()
                if any(eb._is_app_target(t) for t in stale_targets):
                    eb.force_close_launched_browser(force=True)
            except Exception:
                pass
        # v310: Die Haupt-App öffnet wieder ganz normal im Standardbrowser des
        # Benutzers. e-Bichelchen wird erst beim Klick auf « Connecter » in einem
        # separaten, app-artigen Loginfenster geöffnet. Dadurch ist EntretienConnect
        # weder an Firefox noch an Chrome/Safari als Standardbrowser gebunden.
        threading.Timer(0.45, lambda: _open_in_browser(url)).start()
        def _watchdog():
            global last_heartbeat_time
            last_tick = time.time()
            while True:
                time.sleep(5)
                now = time.time()
                # v167: un grand saut d'horloge = le Mac sortait de veille. Le navigateur se
                # réveille en même temps ; on lui redonne une fenêtre complète pour renvoyer
                # un battement au lieu d'arrêter le helper immédiatement.
                if now - last_tick > 60:
                    if last_heartbeat_time is not None:
                        last_heartbeat_time = now
                    last_tick = now
                    continue
                last_tick = now
                if HEARTBEAT_TIMEOUT_SECONDS and last_heartbeat_time is not None and now - last_heartbeat_time > HEARTBEAT_TIMEOUT_SECONDS:
                    print("Aucun onglet EntretienConnect actif. Arrêt automatique du helper local.")
                    try:
                        httpd.shutdown()
                    except Exception:
                        pass
                    return
                if STARTUP_NO_HEARTBEAT_TIMEOUT_SECONDS and last_heartbeat_time is None and now - server_started_time > STARTUP_NO_HEARTBEAT_TIMEOUT_SECONDS:
                    print("Aucun onglet EntretienConnect démarré. Arrêt automatique du helper local.")
                    try:
                        httpd.shutdown()
                    except Exception:
                        pass
                    return
        threading.Thread(target=_watchdog, daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nTerminé.")
        finally:
            # v297: Beim echten Beenden von EntretienConnect wird ein eventuell noch
            # offener e-Bichelchen-Hilfstab zuverlässig geschlossen.
            try:
                if EB_AVAILABLE:
                    eb.force_close_launched_browser(force=True)
            except Exception:
                pass
            _remove_pid_file()


if __name__ == "__main__":
    main()
