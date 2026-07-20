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
$EbUrl = "https://ssl.education.lu/ebichelchen/app/login"
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

function Get-EbTargets {
    try {
        $targets = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json" "GET" 3
        return @($targets)
    } catch { return @() }
}

function Close-EbTargetById($targetId) {
    if (-not $targetId) { return $false }
    try {
        $encoded = [Uri]::EscapeDataString([string]$targetId)
        $null = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/close/$encoded" "GET" 3
        return $true
    } catch { return $false }
}

function Normalize-EbTabs {
    # v299: Im isolierten Hilfsbrowser höchstens einen e-Bichelchen-Tab behalten.
    # Leere Start-Tabs werden entfernt, andere Seiten des Nutzers aber nie angefasst.
    $targets = @(Get-EbTargets)
    $hits = @($targets | Where-Object { $_.type -eq "page" -and ([string]$_.url) -match '/ebichelchen/app/' })
    $keep = $null
    if ($hits.Count -gt 0) {
        $keep = $hits | Select-Object -First 1
        foreach ($dup in ($hits | Select-Object -Skip 1)) {
            Close-EbTargetById $dup.id | Out-Null
        }
    }
    foreach ($t in $targets) {
        if ($keep -and ([string]$t.id -eq [string]$keep.id)) { continue }
        $u = [string]$t.url
        if ($t.type -eq "page" -and ($u -eq "about:blank" -or $u -match '^(chrome|edge)://newtab/?')) {
            Close-EbTargetById $t.id | Out-Null
        }
    }
    return $keep
}

function Get-EbOwnerProcessId {
    try {
        $c = Get-NetTCPConnection -LocalPort $EbCdpPort -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($c -and $c.OwningProcess) { return [int]$c.OwningProcess }
    } catch {}
    try {
        $line = (& netstat -ano -p tcp 2>$null | Select-String -Pattern (":$EbCdpPort\s+.*LISTENING\s+(\d+)\s*$") | Select-Object -First 1)
        if ($line -and $line.Matches.Count -gt 0) { return [int]$line.Matches[0].Groups[1].Value }
    } catch {}
    return 0
}

function Activate-EbBrowserWindow($processId = 0) {
    # Page.bringToFront aktiviert nur den Tab innerhalb des Fensters. v299 bringt
    # zusätzlich das isolierte Chrome-/Edge-Fenster auf Windows sichtbar nach vorne.
    try {
        if (-not ("EntretienConnectWin32" -as [type])) {
            Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EntretienConnectWin32 {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
"@
        }
    } catch {}

    $candidateIds = @()
    if ($processId) { $candidateIds += [int]$processId }
    $owner = Get-EbOwnerProcessId
    if ($owner -and ($candidateIds -notcontains $owner)) { $candidateIds += $owner }

    # Der Prozess, der Port 9223 hält, besitzt bei Chromium nicht auf jedem Rechner
    # selbst das sichtbare Fenster. Deshalb zusätzlich sichtbare e-Bichelchen-/IAM-
    # Browserfenster berücksichtigen.
    try {
        $visible = @(Get-Process chrome,msedge -ErrorAction SilentlyContinue | Where-Object {
            $_.MainWindowHandle -ne 0 -and (
                ([string]$_.MainWindowTitle) -match 'e-Bichelchen|EduKey|education\.lu|IAM|Connexion'
            )
        })
        foreach ($v in $visible) {
            if ($candidateIds -notcontains ([int]$v.Id)) { $candidateIds += [int]$v.Id }
        }
    } catch {}

    foreach ($id in $candidateIds) {
        try {
            $proc = Get-Process -Id $id -ErrorAction Stop
            $h = $proc.MainWindowHandle
            if ($h -and $h -ne 0) {
                try { [EntretienConnectWin32]::ShowWindowAsync($h, 9) | Out-Null } catch {}
                try { [EntretienConnectWin32]::BringWindowToTop($h) | Out-Null } catch {}
                try {
                    $shell = New-Object -ComObject WScript.Shell
                    $null = $shell.AppActivate($id)
                } catch {}
                try { return [bool][EntretienConnectWin32]::SetForegroundWindow($h) } catch { return $true }
            }
        } catch {}
    }
    return $false
}

function Close-EbHelperBrowser {
    # v301: Nur den e-Bichelchen-Tab schließen. Der kontrollierte Browserprozess
    # bleibt ohne sichtbares Fenster vorgewärmt, damit der nächste Connect keinen
    # langsamen Kaltstart mehr benötigt. Beim echten Beenden der App wird er über
    # Stop-EbControlledBrowser vollständig beendet.
    $targets = @(Get-EbTargets)
    $ebTargets = @($targets | Where-Object { $_.type -eq "page" -and ([string]$_.url) -match '/ebichelchen/app/' })
    if ($ebTargets.Count -eq 0) {
        return @{ closed=$false; method="none"; reason="Kein e-Bichelchen-Tab gefunden."; browserKeptWarm=$true }
    }
    $closedTabs = 0
    foreach ($t in $ebTargets) {
        if (Close-EbTargetById $t.id) { $closedTabs++ }
    }
    Start-Sleep -Milliseconds 220
    $rewarm = $null
    try { $rewarm = Start-EbPrewarm "default" "auto" } catch {}
    return @{ closed=($closedTabs -gt 0); method="tabs-only"; wholeHelperBrowser=$false; closedTabs=$closedTabs; browserKeptWarm=$true; rewarm=$rewarm }
}

function Open-EbRemoteTab($url) {
    try {
        $encoded = [Uri]::EscapeDataString($url)
        Invoke-WebRequest -Uri ("http://127.0.0.1:$EbCdpPort/json/new?" + $encoded) -Method Put -TimeoutSec 3 | Out-Null
        return $true
    } catch { return $false }
}

function Set-EbWindowState($target, $state) {
    # v291: Fenster eines CDP-Targets minimieren/normalisieren – rein über DevTools
    # (Browser.setWindowBounds), ohne Windows-Fenster-API. state: "minimized" | "normal".
    try {
        if (-not $target -or -not $target.webSocketDebuggerUrl -or -not $target.id) { return $false }
        $win = Invoke-CdpCall $target.webSocketDebuggerUrl "Browser.getWindowForTarget" @{ targetId = $target.id } 915 6
        $windowId = $win.result.windowId
        if ($null -eq $windowId) { return $false }
        $null = Invoke-CdpCall $target.webSocketDebuggerUrl "Browser.setWindowBounds" @{ windowId = $windowId; bounds = @{ windowState = $state } } 916 6
        return $true
    } catch { return $false }
}

function Start-EbPrewarm($profile = "default", $preferredBrowser = "auto") {
    try {
        $version = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
        return @{ prewarmed=$true; alreadyRunning=$true; ready=$true; port=$EbCdpPort; devtoolsBrowser=$version.Browser }
    } catch {}

    $browser = Find-EbBrowserExecutable $preferredBrowser
    $safeProfile = ([regex]::Replace(([string]$profile), '[^A-Za-z0-9_.-]', '_')).Trim('._-')
    if (-not $safeProfile) { $safeProfile = "default" }
    $baseProfileRoot = $env:LOCALAPPDATA
    if (-not $baseProfileRoot) { $baseProfileRoot = $ScriptDir }
    $profileDir = Join-Path $baseProfileRoot ("EntretienConnect\profiles\" + $browser.id + "\" + $safeProfile)
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    $pidFile = Join-Path $profileDir "entretienconnect-browser.pid"
    $argLine = "--remote-debugging-port=$EbCdpPort --user-data-dir=`"$profileDir`" --no-first-run --no-default-browser-check --no-startup-window"
    $proc = Start-Process -FilePath $browser.path -ArgumentList $argLine -PassThru -WindowStyle Hidden
    try { Set-Content -LiteralPath $pidFile -Value ([string]$proc.Id) -Encoding ASCII } catch {}
    for ($i=0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 100
        try {
            $version = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
            return @{ prewarmed=$true; alreadyRunning=$false; ready=$true; profile=$safeProfile; profileDir=$profileDir; processId=$proc.Id; browser=$browser.name; port=$EbCdpPort; devtoolsBrowser=$version.Browser }
        } catch {}
        try { if ($proc.HasExited) { break } } catch {}
    }
    return @{ prewarmed=$true; alreadyRunning=$false; ready=$false; profile=$safeProfile; profileDir=$profileDir; processId=$proc.Id; browser=$browser.name; port=$EbCdpPort }
}

function Stop-EbControlledBrowser {
    $closed = $false
    try {
        $targets = @(Get-EbTargets)
        $ws = [string](($targets | Where-Object { $_.webSocketDebuggerUrl } | Select-Object -First 1).webSocketDebuggerUrl)
        if (-not $ws) {
            $version = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
            $ws = [string]$version.webSocketDebuggerUrl
        }
        if ($ws) {
            try { $null = Invoke-CdpCall $ws "Browser.close" @{} 931 3; $closed = $true } catch {}
        }
    } catch {}
    try {
        $owner = Get-EbOwnerProcessId
        if ($owner) {
            Start-Sleep -Milliseconds 250
            try { Stop-Process -Id $owner -Force -ErrorAction Stop; $closed = $true } catch {}
        }
    } catch {}
    return @{ closed=$closed; method=($(if ($closed) { "Browser.close/Process" } else { "none" })) }
}

function Start-EbBrowser($profile = "default", $preferredBrowser = "auto") {
    $browser = Find-EbBrowserExecutable $preferredBrowser
    $safeProfile = ([regex]::Replace(([string]$profile), '[^A-Za-z0-9_.-]', '_')).Trim('._-')
    if (-not $safeProfile) { $safeProfile = "default" }
    $baseProfileRoot = $env:LOCALAPPDATA
    if (-not $baseProfileRoot) { $baseProfileRoot = $ScriptDir }
    $profileDir = Join-Path $baseProfileRoot ("EntretienConnect\profiles\" + $browser.id + "\" + $safeProfile)
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    $pidFile = Join-Path $profileDir "entretienconnect-browser.pid"

    try {
        $version = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
        $existing = Normalize-EbTabs
        if ($existing) {
            Set-EbWindowState $existing "normal" | Out-Null
            try { $null = Invoke-CdpCall $existing.webSocketDebuggerUrl "Page.bringToFront" @{} 912 6 } catch {}
            $savedPid = 0
            try { $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw -ErrorAction Stop) } catch {}
            $activated = Activate-EbBrowserWindow $savedPid
            return @{ alreadyRunning=$true; reusedTab=$true; openedTab=$false; active=$activated; profile=$safeProfile; profileDir=$profileDir; url=$EbUrl; port=$EbCdpPort; browser=$browser.name; browserId=$browser.id; browserPath=$browser.path; devtoolsBrowser=$version.Browser }
        }

        # v299: Läuft am Hilfsport nur noch eine alte IAM-/Zwischenseite, die
        # verwaiste Instanz vollständig beenden und danach genau ein neues Fenster
        # starten. So sammeln sich nach abgebrochenen Logins keine Chrome-Fenster an.
        $stalePages = @(Get-EbTargets | Where-Object { $_.type -eq "page" })
        if ($stalePages.Count -gt 0) {
            $ws = [string]$stalePages[0].webSocketDebuggerUrl
            if (-not $ws) { $ws = [string]$version.webSocketDebuggerUrl }
            if ($ws) { try { $null = Invoke-CdpCall $ws "Browser.close" @{} 929 2 } catch {} }
            for ($w=0; $w -lt 12; $w++) {
                Start-Sleep -Milliseconds 100
                try { $null = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1 }
                catch { break }
            }
            throw "Verwaister e-Bichelchen-Hilfsbrowser wurde neu gestartet."
        }

        Open-EbRemoteTab $EbUrl | Out-Null
        Start-Sleep -Milliseconds 180
        $target = $null
        try { $target = Get-EbTarget } catch {}
        if ($target) {
            try { $null = Invoke-CdpCall $target.webSocketDebuggerUrl "Page.bringToFront" @{} 913 6 } catch {}
        }
        $savedPid = 0
        try { $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw -ErrorAction Stop) } catch {}
        $activated = Activate-EbBrowserWindow $savedPid
        return @{ alreadyRunning=$true; openedTab=$true; active=$activated; profile=$safeProfile; profileDir=$profileDir; url=$EbUrl; port=$EbCdpPort; browser=$browser.name; browserId=$browser.id; browserPath=$browser.path; devtoolsBrowser=$version.Browser }
    } catch {}

    # v299: separates, aktives Hilfsfenster. Es wird nur die e-Bichelchen-App geöffnet;
    # deren eigene Startseite (z. B. Pinnwand) darf unverändert bleiben.
    $argLine = "--remote-debugging-port=$EbCdpPort --user-data-dir=`"$profileDir`" --no-first-run --no-default-browser-check --new-window `"$EbUrl`""
    $proc = Start-Process -FilePath $browser.path -ArgumentList $argLine -PassThru
    try { Set-Content -LiteralPath $pidFile -Value ([string]$proc.Id) -Encoding ASCII } catch {}

    for ($i=0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 250
        try {
            $version = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
            $target = $null
            try { $target = Get-EbTarget } catch {}
            if ($target) {
                try { $null = Invoke-CdpCall $target.webSocketDebuggerUrl "Page.bringToFront" @{} 914 6 } catch {}
            }
            $activated = Activate-EbBrowserWindow $proc.Id
            return @{ alreadyRunning=$false; active=$activated; profile=$safeProfile; profileDir=$profileDir; url=$EbUrl; port=$EbCdpPort; browser=$browser.name; browserId=$browser.id; browserPath=$browser.path; processId=$proc.Id; devtoolsBrowser=$version.Browser }
        } catch {}
    }
    return @{ alreadyRunning=$false; active=$false; profile=$safeProfile; profileDir=$profileDir; url=$EbUrl; port=$EbCdpPort; browser=$browser.name; browserId=$browser.id; browserPath=$browser.path; processId=$proc.Id; warning="Browser wurde gestartet, DevTools war aber noch nicht erreichbar." }
}

function Get-EbTarget {
    $targets = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json" "GET" 4
    $hits = @($targets | Where-Object { ([string]$_.url) -match '/ebichelchen/app/' })
    if ($hits.Count -eq 0) {
        $hint = @($targets | Select-Object -First 6 | ForEach-Object { (([string]$_.title) + " -> " + ([string]$_.url)).Trim() }) -join " | "
        throw "Warte auf e-Bichelchen. Bitte Login abschließen. Aktuelle Tabs: $hint"
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

    // 2) v299: keinerlei Reiter oder Seiten in e-Bichelchen mehr anklicken.
    // Ein sichtbarer Route-Wechsel zerstört sonst den laufenden CDP-Kontext.
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

  const groupsResult = await getGroupsFromTeacher();
  let groupObjects = extractGroupObjects(groupsResult.json);
  const groups = groupObjects.map(mapGroup).filter(g => Number.isFinite(g.id)).sort((a,b) => String(a.classAlias || a.name).localeCompare(String(b.classAlias || b.name)));
  if (!groups.length) throw new Error("Keine Klassen aus get-groups-from-teacher erhalten. Automatik-Versuche: " + JSON.stringify(groupsResult.attempts || []).slice(0, 900));

  const groupStore = parseStore("groupStore");
  const userStore = parseStore("userStore");
  const selectedFromStore = Number(groupStore?.selectedGroup?.id);

  // v299: Bei mehreren Klassen entscheidet ausschließlich EntretienConnect.
  // Die zuletzt in e-Bichelchen aktive Klasse wird ignoriert. Ein Klick in der App
  // arbeitet direkt mit der groupId und löst keinerlei sichtbare Navigation aus.
  let group = null;
  let groupChosenAutomatically = false;
  if (requestedGroupId !== null) {
    group = groups.find(g => Number(g.id) === Number(requestedGroupId)) || null;
    if (!group) throw new Error("Die in EntretienConnect gewählte Klasse wurde in e-Bichelchen nicht gefunden (groupId " + requestedGroupId + ").");
  } else if (groups.length === 1) {
    group = groups[0];
    groupChosenAutomatically = true;
  }

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

  const loggedInUser = userStore?.loggedInUser ? {
    firstName: userStore.loggedInUser.firstName || "",
    lastName: userStore.loggedInUser.lastName || "",
    fullName: userStore.loggedInUser.fullName || [userStore.loggedInUser.firstName, userStore.loggedInUser.lastName].filter(Boolean).join(" "),
    id: userStore.loggedInUser.id || null
  } : null;

  const payload = {
    version: "1.10.25",
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

function New-EbReadyExpression {
    return @'
(async () => {
  const out = { ready:false, pageUrl:String(location.href || ""), groupCount:0 };
  if (!out.pageUrl.includes('/ebichelchen/app/')) return JSON.stringify(out);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1800);
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
    return JSON.stringify(out);
  } catch (e) {
    out.error = String(e && (e.message || e) || '');
    return JSON.stringify(out);
  } finally { clearTimeout(timer); }
})()
'@
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
    if ($Action -eq "prewarm") {
        $info = Start-EbPrewarm "default" $Browser
        @{ ok=$true; info=$info } | ConvertTo-Json -Depth 20 -Compress
        exit 0
    }

    if ($Action -eq "shutdown") {
        $info = Stop-EbControlledBrowser
        @{ ok=$true; info=$info } | ConvertTo-Json -Depth 20 -Compress
        exit 0
    }

    if ($Action -eq "open") {
        $info = Start-EbBrowser "default" $Browser
        @{ ok=$true; info=$info } | ConvertTo-Json -Depth 20 -Compress
        exit 0
    }

    if ($Action -eq "ready") {
        $expr = New-EbReadyExpression
        $data = Invoke-CdpEval $expr 3000 609
        @{ ok=$true; ready=[bool]$data.ready; groupCount=[int]($data.groupCount); status=$data.status; browserClosed=$false; lightweight=$true } | ConvertTo-Json -Depth 10 -Compress
        exit 0
    }

    if ($Action -eq "read") {
        $expr = New-EbReadExpression $GroupId
        $data = $null; $lastError = $null
        for ($attempt = 0; $attempt -lt 3; $attempt++) {
            try {
                $data = Invoke-CdpEval $expr 35000 (610 + $attempt)
                break
            } catch {
                $lastError = $_.Exception
                $msg = [string]$_.Exception.Message
                $transient = ($msg -match 'Execution context was destroyed|Cannot find context with specified id|Inspected target navigated or closed|Target closed|WebSocket wurde geschlossen|No frame with given id')
                if (-not $transient -or $attempt -ge 2) { throw }
                Start-Sleep -Milliseconds (350 + 250 * $attempt)
            }
        }
        if ($null -eq $data) { throw $lastError }
        try { $data.source.contextRetries = $attempt } catch {}
        @{ ok=$true; data=$data; receivedAt=(Get-Date).ToString("yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json -Depth 30 -Compress
        exit 0
    }

    if ($Action -eq "cleanup") {
        $info = Close-EbHelperBrowser
        @{ ok=$true; info=$info } | ConvertTo-Json -Depth 10 -Compress
        exit 0
    }

    if ($Action -eq "park") {
        # Kompatibilitätsaktion für alte Oberflächen: niemals minimieren.
        # Nur den e-Bichelchen-Tab schließen; das vollständige v299-Aufräumen
        # verwendet die eigene Aktion "cleanup".
        $closed = $false; $detail = ""
        try {
            $target = Get-EbTarget
            $closed = Close-EbTargetById $target.id
        } catch { $detail = [string]$_.Exception.Message }
        @{ ok=$true; info=@{ parked=$false; minimized=$false; closedInstead=$closed; detail=$detail } } | ConvertTo-Json -Depth 10 -Compress
        exit 0
    }

    if ($Action -eq "soft-reset") {
        # v292: halbfertige IAM-Sitzung verwerfen, Browser aber "warm" lassen (nicht schließen).
        # Cookies per DevTools löschen und e-Bichelchen-Tab frisch laden. Nächster Connect
        # verwendet den Browser weiter, statt einen neuen zu starten.
        $running = $false; $cleared = $false; $navigated = $false
        try { $null = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1; $running = $true } catch {}
        if ($running) {
            try {
                $target = Get-EbTarget
                try { $null = Invoke-CdpCall $target.webSocketDebuggerUrl "Network.clearBrowserCookies" @{} 720 6; $cleared = $true } catch {}
                try { $null = Invoke-CdpCall $target.webSocketDebuggerUrl "Page.navigate" @{ url = $EbUrl } 721 6; $navigated = $true } catch {}
            } catch {}
        }
        @{ ok=$true; info=@{ softReset=$true; browserRunning=$running; cookiesCleared=$cleared; navigated=$navigated } } | ConvertTo-Json -Depth 10 -Compress
        exit 0
    }

    @{ ok=$false; error=("Unbekannte Aktion: " + $Action) } | ConvertTo-Json -Depth 10 -Compress
    exit 2
} catch {
    $browserClosed = $false
    if ($Action -in @("read","ready")) {
        try {
            $null = Invoke-JsonUrl "http://127.0.0.1:$EbCdpPort/json/version" "GET" 1
        } catch {
            $browserClosed = $true
        }
    }
    @{ ok=$false; action=$Action; error=$_.Exception.Message; browserClosed=$browserClosed } | ConvertTo-Json -Depth 10 -Compress
    exit 1
}
