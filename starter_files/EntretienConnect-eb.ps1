# =====================================================================
# EntretienConnect-eb.ps1
# Isolierter e-Bichelchen-Helfer. Wird nur bei e-Bichelchen-Aktionen
# gestartet, damit die Haupt-App auch bei e-Bichelchen-Fehlern startet.
# =====================================================================
param(
    [string]$Action = "open",
    [string]$GroupId = "",
    [string]$PayloadFile = "",
    [string]$Browser = "auto"
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ----------------------------------------------------------- e-Bichelchen (PowerShell, ohne Python)
$EbCdpPort = 9223
$EbUrl = "https://ssl.education.lu/ebichelchen/app/tabs/calendar"
$script:EbData = $null
$script:EbReceivedAt = $null
$script:EbCreatedEntries = @()

function Get-QueryParam($requestPath, $name, $default = "") {
    try {
        $q = ([string]$requestPath -split '\?',2)
        if ($q.Count -lt 2) { return $default }
        foreach ($pair in ($q[1] -split '&')) {
            if (-not $pair) { continue }
            $kv = $pair -split '=',2
            $k = [Uri]::UnescapeDataString($kv[0])
            if ($k -eq $name) {
                if ($kv.Count -gt 1) { return [Uri]::UnescapeDataString(($kv[1] -replace '\+',' ')) }
                return ""
            }
        }
    } catch {}
    return $default
}

function Find-EbBrowserExecutable($preferred = "auto") {
    $local = $env:LOCALAPPDATA
    $prog = $env:PROGRAMFILES
    $prog86 = ${env:PROGRAMFILES(X86)}
    $candidates = @()
    $chrome = @(
        (Join-Path $local "Google\Chrome\Application\chrome.exe"),
        (Join-Path $prog "Google\Chrome\Application\chrome.exe"),
        (Join-Path $prog86 "Google\Chrome\Application\chrome.exe")
    )
    $edge = @(
        (Join-Path $local "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $prog "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $prog86 "Microsoft\Edge\Application\msedge.exe")
    )
    if ($preferred -eq "edge") {
        $candidates += @(@{ id="edge"; name="Microsoft Edge"; paths=$edge }, @{ id="chrome"; name="Google Chrome"; paths=$chrome })
    } elseif ($preferred -eq "chrome") {
        $candidates += @(@{ id="chrome"; name="Google Chrome"; paths=$chrome }, @{ id="edge"; name="Microsoft Edge"; paths=$edge })
    } else {
        $candidates += @(@{ id="chrome"; name="Google Chrome"; paths=$chrome }, @{ id="edge"; name="Microsoft Edge"; paths=$edge })
    }
    foreach ($b in $candidates) {
        foreach ($p in $b.paths) {
            if ($p -and (Test-Path $p -PathType Leaf)) { return @{ id=$b.id; name=$b.name; path=$p } }
        }
    }
    throw "Kein unterstützter Browser gefunden. Installiert sein muss Google Chrome oder Microsoft Edge."
}

function Invoke-JsonUrl($url, $method = "GET", $timeoutSec = 4) {
    return Invoke-RestMethod -Uri $url -Method $method -TimeoutSec $timeoutSec -Headers @{ Accept="application/json" }
}

function Open-EbRemoteTab($url) {
    try {
        $encoded = [Uri]::EscapeDataString($url)
        Invoke-WebRequest -Uri ("http://127.0.0.1:$EbCdpPort/json/new?" + $encoded) -Method Put -TimeoutSec 3 | Out-Null
        return $true
    } catch { return $false }
}

function Start-EbBrowser($profile = "default", $preferredBrowser = "auto") {
    $browser = Find-EbBrowserExecutable $preferredBrowser
    $safeProfile = ([regex]::Replace(([string]$profile), '[^A-Za-z0-9_.-]', '_')).Trim('._-')
    if (-not $safeProfile) { $safeProfile = "default" }
    # Profil bewusst NICHT im App-Ordner/OneDrive speichern.
    # Alte Versionen haben den Pfad mit Leerzeichen (z.B. "OneDrive - 365education")
    # an Edge/Chrome weitergegeben; Chromium interpretierte einzelne Pfadteile dann
    # als URLs und öffnete mehrere fehlerhafte Fenster.
    $baseProfileRoot = $env:LOCALAPPDATA
    if (-not $baseProfileRoot) { $baseProfileRoot = $ScriptDir }
    $profileDir = Join-Path $baseProfileRoot ("EntretienConnect\profiles\" + $browser.id + "\" + $safeProfile)
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

    try {
        $version = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
        Open-EbRemoteTab $EbUrl | Out-Null
        return @{ alreadyRunning=$true; profile=$safeProfile; profileDir=$profileDir; url=$EbUrl; port=$EbCdpPort; browser=$browser.name; browserId=$browser.id; browserPath=$browser.path; devtoolsBrowser=$version.Browser }
    } catch {}

    # Start-Process in Windows PowerShell quotet Array-Argumente nicht zuverlässig.
    # Deshalb bauen wir die Commandline selbst und quoten den Profilpfad explizit.
    $argLine = "--remote-debugging-port=$EbCdpPort --user-data-dir=`"$profileDir`" --no-first-run --no-default-browser-check `"$EbUrl`""
    Start-Process -FilePath $browser.path -ArgumentList $argLine | Out-Null
    for ($i=0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 250
        try {
            $version = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
            return @{ alreadyRunning=$false; profile=$safeProfile; profileDir=$profileDir; url=$EbUrl; port=$EbCdpPort; browser=$browser.name; browserId=$browser.id; browserPath=$browser.path; devtoolsBrowser=$version.Browser }
        } catch {}
    }
    return @{ alreadyRunning=$false; profile=$safeProfile; profileDir=$profileDir; url=$EbUrl; port=$EbCdpPort; browser=$browser.name; browserId=$browser.id; browserPath=$browser.path; warning="Browser wurde gestartet, DevTools war aber noch nicht erreichbar." }
}

function Get-EbTarget {
    $targets = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json" "GET" 4
    $hits = @($targets | Where-Object { ([string]$_.url) -match '/ebichelchen/app/' })
    if ($hits.Count -eq 0) {
        $hint = @($targets | Select-Object -First 6 | ForEach-Object { (([string]$_.title) + " -> " + ([string]$_.url)).Trim() }) -join " | "
        throw "Warte auf e-Bichelchen-Kalender. Bitte Login abschließen. Aktuelle Tabs: $hint"
    }
    $target = $hits | Select-Object -First 1
    if (-not $target.webSocketDebuggerUrl) { throw "Der e-Bichelchen-Tab hat keine DevTools-WebSocket-URL geliefert." }
    return $target
}

function Invoke-CdpCall($wsUrl, $method, $params, $msgId = 1, $timeoutSec = 18) {
    $client = [Net.WebSockets.ClientWebSocket]::new()
    $cts = [Threading.CancellationTokenSource]::new()
    $cts.CancelAfter([TimeSpan]::FromSeconds($timeoutSec))
    try {
        $client.ConnectAsync([Uri]$wsUrl, $cts.Token).Wait()
        if ($null -eq $params) { $params = @{} }
        $payload = (@{ id=$msgId; method=$method; params=$params } | ConvertTo-Json -Depth 30 -Compress)
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
        $client.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).Wait()
        $buffer = New-Object byte[] 262144
        $ms = [IO.MemoryStream]::new()
        while (-not $cts.IsCancellationRequested) {
            $result = $client.ReceiveAsync([ArraySegment[byte]]::new($buffer), $cts.Token).Result
            if ($result.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) { throw "CDP-WebSocket wurde geschlossen." }
            $ms.Write($buffer, 0, $result.Count)
            if ($result.EndOfMessage) {
                $text = [Text.Encoding]::UTF8.GetString($ms.ToArray())
                $ms.SetLength(0)
                try { $msg = $text | ConvertFrom-Json } catch { continue }
                if ($msg.id -eq $msgId) { return $msg }
            }
        }
        throw "Keine CDP-Antwort erhalten."
    } finally {
        try { $client.Dispose() } catch {}
        try { $cts.Dispose() } catch {}
    }
}

function Invoke-CdpEval($expression, $timeoutMs = 22000, $msgId = 501) {
    $target = Get-EbTarget
    $params = @{ expression=$expression; awaitPromise=$true; returnByValue=$true; timeout=$timeoutMs }
    $msg = Invoke-CdpCall $target.webSocketDebuggerUrl "Runtime.evaluate" $params $msgId ([Math]::Max(18, [int]($timeoutMs/1000) + 5))
    if ($msg.error) { throw ("CDP-Fehler: " + ($msg.error | ConvertTo-Json -Compress)) }
    if ($msg.result.exceptionDetails) {
        $details = $msg.result.exceptionDetails
        $text = [string]$details.text
        $exc = ""
        try { $exc = [string]$details.exception.description } catch {}
        if (-not $exc) { try { $exc = [string]$details.exception.value } catch {} }
        throw ("Fehler im e-Bichelchen-Tab: " + $text + " " + $exc)
    }
    $value = $msg.result.result.value
    if (-not $value) { throw "Keine Antwort vom e-Bichelchen-Tab erhalten." }
    return ($value | ConvertFrom-Json)
}

function New-EbReadExpression($selectedGroupId = $null) {
    $lit = "null"
    if ($null -ne $selectedGroupId -and ([string]$selectedGroupId).Trim() -ne "") { $lit = [string][int]$selectedGroupId }
    $js = @'

(async () => {
  const requestedGroupId = __GROUP_LITERAL__;

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

'@
    return $js.Replace("__GROUP_LITERAL__", $lit)
}

function Set-EbData($data) {
    $script:EbData = $data
    $script:EbReceivedAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
}

function Get-PropValue($obj, $name) {
    if ($null -eq $obj) { return $null }
    if ($obj -is [System.Collections.IDictionary]) { return $obj[$name] }
    $p = $obj.PSObject.Properties[$name]
    if ($p) { return $p.Value }
    return $null
}

function As-IntOrNull($v) {
    try { if ($null -eq $v -or [string]$v -eq "") { return $null }; return [int]$v } catch { return $null }
}

function Normalize-Scope($v) {
    $out = @()
    if ($null -eq $v -or [string]$v -eq "") { return $out }
    if ($v -is [System.Array]) { foreach ($x in $v) { $i=As-IntOrNull $x; if ($null -ne $i) { $out += $i } }; return $out }
    $i = As-IntOrNull $v
    if ($null -ne $i) { return @($i) }
    return $out
}

function Find-EntryObject($value, $depth = 0) {
    if ($null -eq $value -or $depth -gt 8) { return $null }
    if ($value -is [System.Array]) {
        foreach ($item in $value) { $f = Find-EntryObject $item ($depth + 1); if ($f) { return $f } }
        return $null
    }
    if ($value -isnot [psobject]) { return $null }
    foreach ($key in @("objects","object","entry","savedEntry")) {
        $child = Get-PropValue $value $key
        if ($null -ne $child) { $f = Find-EntryObject $child ($depth + 1); if ($f) { return $f } }
    }
    $id = As-IntOrNull (Get-PropValue $value "id")
    if ($null -ne $id) {
        if ($null -ne (Get-PropValue $value "isIndividual") -or $null -ne (Get-PropValue $value "scope") -or $null -ne (Get-PropValue $value "groupId") -or $null -ne (Get-PropValue $value "subjectId")) { return $value }
    }
    foreach ($p in $value.PSObject.Properties) {
        if ($p.Value -is [psobject] -or $p.Value -is [System.Array]) { $f = Find-EntryObject $p.Value ($depth + 1); if ($f) { return $f } }
    }
    return $null
}

function Validate-EbPayload($payload) {
    if ($null -eq $script:EbData) { throw "Es wurden noch keine e-Bichelchen-Daten eingelesen. Erst verbinden und Daten lesen." }
    $group = Get-PropValue $script:EbData "group"
    $messageSubject = Get-PropValue $script:EbData "messageSubject"
    $students = @(Get-PropValue $script:EbData "students")
    $groupId = [int](Get-PropValue $payload "groupId")
    $subjectId = [int](Get-PropValue $payload "subjectId")
    $scope = [int](Get-PropValue $payload "scope")
    $expectedGroupId = [int](Get-PropValue $group "id")
    $expectedSubjectId = [int](Get-PropValue $messageSubject "id")
    if ($groupId -ne $expectedGroupId) { throw "Sicherheitsstopp: groupId $groupId passt nicht zur eingelesenen Klasse $expectedGroupId." }
    if ($subjectId -ne $expectedSubjectId) { throw "Sicherheitsstopp: subjectId $subjectId ist nicht die erkannte Nachricht/Message-ID $expectedSubjectId." }
    $allowed = @($students | ForEach-Object { As-IntOrNull (Get-PropValue $_ "id") } | Where-Object { $null -ne $_ })
    if ($allowed -notcontains $scope) { throw "Sicherheitsstopp: scope $scope gehört nicht zu den eingelesenen Schülern." }
    $title = ([string](Get-PropValue $payload "title")).Trim()
    $description = ([string](Get-PropValue $payload "description")).Trim()
    $startDate = ([string](Get-PropValue $payload "startDate")).Trim()
    if (-not $title) { throw "Titel fehlt." }
    if (-not $description) { throw "Beschreibung/Nachricht fehlt." }
    if (-not $startDate -or $startDate.Substring(0, [Math]::Min(10,$startDate.Length)) -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "startDate fehlt oder hat ein unerwartetes Format." }
    $recEnd = [string](Get-PropValue $payload "recurrenceEndDate")
    if (-not $recEnd) { $recEnd = $startDate }
    return @{
        groupId=$groupId; startDate=$startDate; title=$title.Substring(0,[Math]::Min(500,$title.Length)); subjectId=$subjectId; type=0;
        description=$description.Substring(0,[Math]::Min(5000,$description.Length)); scope=$scope;
        recurrenceEndDate=$recEnd; selectedRecurrenceDays=@(); skipHolidays=$true; weekInterval=1
    }
}

function New-EbSaveExpression($payload) {
    $raw = ($payload | ConvertTo-Json -Depth 20 -Compress)
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($raw))
    return @"
(async () => {
  const payload = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob("$b64"), c => c.charCodeAt(0))));
  if (!location.href.includes('/ebichelchen/app/')) throw new Error('Nicht im e-Bichelchen-Kontext. Bitte e-Bichelchen erneut verbinden und das Fenster offen lassen.');
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
  const res = await fetch('/ebichelchen/app/api/v6/save-entry', { method: 'POST', credentials: 'include', headers: { 'accept': 'application/json, text/plain, */*', 'mobileappversion': 'web' }, body: fd });
  const text = await res.text();
  let body = null; try { body = JSON.parse(text); } catch (_) { body = text.slice(0, 1000); }
  return JSON.stringify({ ok: res.ok, status: res.status, statusText: res.statusText, body });
})()
"@
}

function Verify-EbSaveResponse($resp, $payload) {
    if (-not $resp.ok) { throw ("e-Bichelchen hat nicht gespeichert: " + ($resp | ConvertTo-Json -Depth 10 -Compress)) }
    $obj = Find-EntryObject $resp.body
    if (-not $obj) { throw "Serverantwort enthält keine prüfbaren Eintragsdetails. Aus Sicherheitsgründen wird nicht weiter erstellt." }
    $entryId = As-IntOrNull (Get-PropValue $obj "id")
    if ($null -eq $entryId) { throw "Serverantwort enthält keine prüfbare entryId." }
    $isIndividual = Get-PropValue $obj "isIndividual"
    $scope = Normalize-Scope (Get-PropValue $obj "scope")
    $expectedScope = [int]$payload.scope
    $problems = @()
    if ($isIndividual -ne $true) { $problems += "isIndividual ist nicht true" }
    if ($scope.Count -ne 1 -or $scope[0] -ne $expectedScope) { $problems += ("scope ist [" + ($scope -join ',') + "], erwartet [$expectedScope]") }
    $rg = As-IntOrNull (Get-PropValue $obj "groupId")
    $rs = As-IntOrNull (Get-PropValue $obj "subjectId")
    if ($null -ne $rg -and $rg -ne [int]$payload.groupId) { $problems += "groupId passt nicht" }
    if ($null -ne $rs -and $rs -ne [int]$payload.subjectId) { $problems += "subjectId passt nicht" }
    if ($problems.Count) { throw ("Eintrag $entryId wurde vom Server nicht als individueller Eintrag bestätigt: " + ($problems -join '; ')) }
    return @{ entryId=$entryId; isIndividual=$true; scope=$scope; groupId=$rg; subjectId=$rs; serverEntry=$obj }
}



try {
    if ($Action -eq "open") {
        $info = Start-EbBrowser "default" $Browser
        @{ ok=$true; info=$info } | ConvertTo-Json -Depth 20 -Compress
        exit 0
    }

    if ($Action -eq "read") {
        $expr = New-EbReadExpression $GroupId
        $data = Invoke-CdpEval $expr 35000 610
        @{ ok=$true; data=$data; receivedAt=(Get-Date).ToString("yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Depth 30 -Compress
        exit 0
    }

    @{ ok=$false; error=("Unbekannte Aktion: " + $Action) } | ConvertTo-Json -Depth 10 -Compress
    exit 2
} catch {
    @{ ok=$false; action=$Action; error=$_.Exception.Message } | ConvertTo-Json -Depth 10 -Compress
    exit 1
}
