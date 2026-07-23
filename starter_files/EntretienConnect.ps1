# =====================================================================
#  EntretienConnect - lokaler Helfer fuer Windows (PowerShell)
#  Ersetzt server.py: liefert graph.html aus und erledigt Login (Geraetecode)
#  + Versand ueber Microsoft Graph. Kein Python, keine Installation, kein Admin.
#  Start ueber: EntretienConnect.vbs oder EntretienConnect-Start.bat
# =====================================================================

param([switch]$NoAutoOpen, [switch]$UpdateUiOnly)

$script:SelfPath = $MyInvocation.MyCommand.Path
try { $script:ProcessStartedAt = (Get-Process -Id $PID -ErrorAction Stop).StartTime } catch { $script:ProcessStartedAt = Get-Date }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ClientId = "14d82eec-204b-4c2f-b7e8-296a70dab67e"   # Microsoft Graph Command Line Tools (oeffentlich)
$Tenant = "organizations"
$Scope  = "https://graph.microsoft.com/Mail.Send offline_access"
$Base   = "https://login.microsoftonline.com/$Tenant/oauth2/v2.0"
$PreferredPort = 8765
$Port   = $PreferredPort
$UiBase = "https://joelschartz.github.io/EntretienConnect/"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = Split-Path -Parent $ScriptDir
$LocalCsvName = "eleves_contacts.csv"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "EntretienConnect"
try { if (-not (Test-Path $RuntimeDir -PathType Container)) { New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null } } catch { $RuntimeDir = $ScriptDir }
$TokenFile = Join-Path $RuntimeDir "graph_token.json"
$StateFile = Join-Path $RuntimeDir "state.json"
$BackupDir = Join-Path $RuntimeDir "backups"
try { if (-not (Test-Path $BackupDir -PathType Container)) { New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null } } catch {}
$LogFile   = Join-Path $RuntimeDir "EntretienConnect-log.txt"
if (-not $UpdateUiOnly) {
    try { [System.IO.File]::WriteAllText($LogFile, ("=== Start " + $script:ProcessStartedAt + " ===" + [Environment]::NewLine), [Text.Encoding]::UTF8) } catch {}
}
$PidFile   = Join-Path $RuntimeDir "helper.pid"
$EbCacheFile = Join-Path $RuntimeDir "ebichelchen_cache.json"
# v339: Vom e-Bichelchen-Helfer nach jedem Lesen geschrieben. Liegt hier eine noch
# gueltige Sitzung, verbindet sich die Oberflaeche beim Start von selbst.
$EbSessionFile = Join-Path $RuntimeDir "eb-session.json"
$EbSessionMaxAgeSeconds = 12 * 3600
$script:Pending = $null
$script:PendingWeb = $null   # v176: état PKCE du login sans code (state/verifier/redirect)
$script:LastLoginError = $null
$script:LastHeartbeatUtc = $null
$script:ServerStartedUtc = [DateTime]::UtcNow
$script:ShutdownRequested = $false
# Si l’onglet App est fermé, aucun heartbeat n’arrive plus.
# Le helper s’arrête alors seul pour libérer le dossier/OneDrive.
# v167: 25 s était trop court — Chrome ne laisse battre les onglets en arrière-plan
# qu'une fois par minute (après 5 min), et une mise en veille coupait le helper.
$script:HeartbeatTimeoutSeconds = 0   # v177: kein automatischer Timeout
$script:LastWatchdogTickUtc = $null
$script:StartupNoHeartbeatTimeoutSeconds = 0
# v185: version du helper lue depuis VERSION.txt — une seule source de vérité,
# plus de numéros codés en dur qui dérivent de la version réelle.
$script:HelperVersion = 0
try { $script:HelperVersion = [int](((Get-Content (Join-Path $ScriptDir "VERSION.txt") -ErrorAction Stop | Select-Object -First 1)).Trim().TrimStart('v','V')) } catch {}

function Log($msg) {
    $line = ((Get-Date -Format "HH:mm:ss") + "  " + $msg)
    Write-Host $line
    try { [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, [Text.Encoding]::UTF8) } catch {}
}

if (-not $UpdateUiOnly) {
    $initMs = [int](((Get-Date) - $script:ProcessStartedAt).TotalMilliseconds)
    Log ("PowerShell-Helfer initialisiert nach " + $initMs + " ms.")
}


function Test-EntretienConnectPortOpen {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect("127.0.0.1", $Port, $null, $null)
        # v346: Ein lokaler Listener antwortet praktisch sofort. Eine lange
        # Wartezeit bremst nur den Normalfall, in dem noch kein Helfer läuft.
        $ok = $iar.AsyncWaitHandle.WaitOne(80, $false)
        if ($ok) { $tcp.EndConnect($iar); $tcp.Close(); return $true }
        try { $tcp.Close() } catch {}
    } catch {}
    return $false
}
function Wait-EntretienConnectPortClosed($msTotal) {
    $deadline = (Get-Date).AddMilliseconds($msTotal)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-EntretienConnectPortOpen)) { return $true }
        Start-Sleep -Milliseconds 150
    }
    return (-not (Test-EntretienConnectPortOpen))
}
function Get-ReusableEntretienConnectHelper {
    # v346: Nur aufrufen, wenn der Port bereits als offen bestätigt wurde. So
    # wartet ein normaler Kaltstart nicht eine Sekunde auf eine HTTP-Zeitüberschreitung.
    # Ein bereits laufender Helfer derselben Version und desselben Starter-
    # Ordners kann sofort weiterverwendet werden. Das spart PowerShell-Neustart,
    # Prozesssuche und erneutes Binden des Servers bei jedem App-Oeffnen.
    try {
        $cap = Invoke-WebRequest -Uri ("http://127.0.0.1:$PreferredPort/api/graph/capabilities") -UseBasicParsing -TimeoutSec 1
        if (-not $cap -or $cap.StatusCode -ne 200) { return $null }
        $data = $cap.Content | ConvertFrom-Json
        $theirVersion = [int]$data.appVersion
        $theirPath = [string]$data.instancePath
        $ourPath = [System.IO.Path]::GetFullPath($ScriptDir).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
        if ($theirVersion -eq $script:HelperVersion -and $theirPath) {
            $theirFull = [System.IO.Path]::GetFullPath($theirPath).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
            if ($theirFull -ieq $ourPath) {
                return [pscustomobject]@{ Port = [int]$data.port; Version = $theirVersion; Path = $theirFull }
            }
        }
    } catch {}
    return $null
}
function Stop-OldEntretienConnectHelpers {
    # v195: robuste Startbereinigung. Nutzer müssen nicht manuell beenden.
    try {
        $cap = $null
        try { $cap = Invoke-WebRequest -Uri ("http://127.0.0.1:$Port/api/graph/capabilities") -UseBasicParsing -TimeoutSec 1 } catch {}
        if ($cap -and $cap.StatusCode -eq 200 -and (('' + $cap.Content) -match 'deferredSend|appVersion|EntretienConnect')) {
            Log "Alter EntretienConnect-Helper wird sauber beendet."
            try { Invoke-WebRequest -Uri ("http://127.0.0.1:$Port/api/app/shutdown") -UseBasicParsing -TimeoutSec 1 | Out-Null } catch {}
            [void](Wait-EntretienConnectPortClosed 3500)
        }
    } catch {}
    try {
        if (Test-Path $PidFile -PathType Leaf) {
            $oldPid = 0
            try { $oldPid = [int]((Get-Content -LiteralPath $PidFile -ErrorAction Stop | Select-Object -First 1).Trim()) } catch {}
            if ($oldPid -and $oldPid -ne $PID) {
                try {
                    $p = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
                    if ($p) {
                        Log ("Alter EntretienConnect-Prozess wird beendet: PID " + $oldPid)
                        Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
                        Start-Sleep -Milliseconds 500
                    }
                } catch {}
            }
            try { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue } catch {}
        }
    } catch {}
    try {
        $me = $PID
        $parentPid = 0
        try { $parentPid = [int]((Get-CimInstance Win32_Process -Filter ("ProcessId=" + $me) -ErrorAction SilentlyContinue).ParentProcessId) } catch {}
        $known = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ProcessId -ne $me -and $_.ProcessId -ne $parentPid -and $_.CommandLine -and (
                $_.CommandLine -like '*EntretienConnect.ps1*' -or
                $_.CommandLine -like '*EntretienConnect-Start-Hidden.bat*' -or
                $_.CommandLine -like '*_EntretienConnect*' -or
                $_.CommandLine -like '*.EntretienConnect*' -or
                $_.CommandLine -like '*server.py*'
            )
        }
        foreach ($proc in @($known)) {
            try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
        }
        if (@($known).Count -gt 0) { Start-Sleep -Milliseconds 700 }
    } catch {}
    try {
        if (Test-EntretienConnectPortOpen) {
            $owners = @()
            try { $owners = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue } catch {}
            foreach ($c in @($owners)) {
                $opid = [int]$c.OwningProcess
                if ($opid -and $opid -ne $PID) {
                    try {
                        $wp = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $opid) -ErrorAction SilentlyContinue
                        $cmd = "" + $wp.CommandLine
                        if ($cmd -like '*EntretienConnect*' -or $cmd -like '*server.py*') {
                            Log ("Port $Port war noch belegt. Prozess wird beendet: PID " + $opid)
                            Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue
                        }
                    } catch {}
                }
            }
            [void](Wait-EntretienConnectPortClosed 2000)
        }
    } catch {}
}

function New-EntretienConnectListener {
    param([int]$Preferred)
    # v195: preferred port first, then safe fallback ports.
    # A foreign process on 8765 must not be killed; in that case we switch ports.
    $ports = @($Preferred)
    try { $ports += @(8766..8785) } catch {}
    $ports += @(0)  # ask Windows for any free local port as final fallback
    $lastError = $null
    foreach ($p in $ports) {
        try {
            $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, [int]$p)
            $l.Start()
            $actual = ([System.Net.IPEndPoint]$l.LocalEndpoint).Port
            return [pscustomobject]@{ Listener = $l; Port = [int]$actual; Fallback = ([int]$actual -ne [int]$Preferred) }
        } catch {
            $lastError = $_.Exception.Message
        }
    }
    throw ("Aucun port local libre pour EntretienConnect. Dernière erreur: " + $lastError)
}

function Write-EntretienConnectPidFile {
    try { [System.IO.File]::WriteAllText($PidFile, (("" + $PID) + [Environment]::NewLine + ("" + $Port) + [Environment]::NewLine), [Text.Encoding]::UTF8) } catch {}
}
function Remove-EntretienConnectPidFile {
    try {
        if (Test-Path $PidFile -PathType Leaf) {
            $first = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if (("" + $first).Trim() -eq ("" + $PID)) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue }
        }
    } catch {}
}

function Get-VersionNumber($value) {
    try {
        if (("" + $value) -match '(\d+)') { return [int]$matches[1] }
    } catch {}
    return 0
}
function Get-LocalVersionNumber {
    try { return Get-VersionNumber (Get-Content -LiteralPath (Join-Path $ScriptDir "VERSION.txt") -Raw -ErrorAction Stop) } catch { return 0 }
}
function Get-HtmlAppVersion($path) {
    try {
        if ((Test-Path $path -PathType Leaf)) {
            $txt = Get-Content -LiteralPath $path -Raw -ErrorAction Stop
            if ($txt -match 'const\s+APP_VERSION\s*=\s*(\d+)') { return [int]$matches[1] }
        }
    } catch {}
    return 0
}

# Laedt nach dem sichtbaren Start die neuesten Webdateien aus dem GitHub-Repo und
# ersetzt die lokalen Kopien fuer den naechsten Start. Offline bleibt alles erhalten.
function Update-UiFromGitHub {
    # Télécharge au démarrage la dernière interface depuis GitHub Pages.
    # La page est ensuite servie localement (127.0.0.1) afin que Microsoft/e-Bichelchen
    # puissent utiliser les API locales sans problème de sécurité du navigateur.
    $files = @("index.html", "graph.html", "schullogo.png")
    foreach ($name in $files) {
        try {
            $uri = $UiBase.TrimEnd("/") + "/" + $name + "?t=" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            $tmp = Join-Path $ScriptDir ("." + $name + ".download")
            Invoke-WebRequest -Uri $uri -OutFile $tmp -UseBasicParsing -TimeoutSec 20
            if ((Test-Path $tmp -PathType Leaf) -and ((Get-Item $tmp).Length -gt 100)) {
                if ($name -eq "graph.html") {
                    $localV = Get-HtmlAppVersion (Join-Path $ScriptDir $name)
                    if (-not $localV) { $localV = Get-LocalVersionNumber }
                    $remoteV = Get-HtmlAppVersion $tmp
                    if ($remoteV -and $localV -and ($remoteV -lt $localV)) {
                        try { Remove-Item $tmp -Force } catch {}
                        Log ("Mise à jour GitHub ignorée pour graph.html : GitHub v" + $remoteV + " est plus ancien que local v" + $localV)
                        continue
                    }
                }
                Move-Item -Force $tmp (Join-Path $ScriptDir $name)
                Log ("Interface actualisée depuis GitHub : " + $name)
            }
        } catch {
            Log ("Mise à jour GitHub ignorée pour " + $name + " : " + $_.Exception.Message)
        }
    }
}

function Start-UiUpdateInBackground {
    # v345: Die drei GitHub-Downloads blockierten bisher jeden App-Start. Jetzt
    # laufen sie erst nach dem sichtbaren Start in einem separaten Prozess und
    # gelten damit spaetestens beim naechsten Oeffnen.
    try {
        $hostExe = (Get-Process -Id $PID -ErrorAction Stop).Path
        $quotedScript = '"' + $script:SelfPath.Replace('"','""') + '"'
        Start-Process -FilePath $hostExe -WindowStyle Hidden -ArgumentList @("-NoLogo","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",$quotedScript,"-UpdateUiOnly") | Out-Null
        Log "GitHub-Aktualisierung im Hintergrund gestartet."
    } catch {
        Log ("GitHub-Aktualisierung konnte nicht im Hintergrund starten: " + $_.Exception.Message)
    }
}

# ----------------------------------------------------------- HTTP-Helfer
function PostForm($url, $form) {
    # Genau wie der funktionierende Test: Hashtable-Body, KEIN expliziter ContentType.
    Log ("PostForm -> " + $url)
    Log ("  client_id = " + $form["client_id"] + " | scope = " + $form["scope"])
    try {
        $r = Invoke-RestMethod -Method Post -Uri $url -Body $form -TimeoutSec 30
        if ($null -eq $r) { Log "  PostForm: Antwort war LEER (null)" }
        else { Log "  PostForm: Antwort erhalten" }
        return $r
    } catch {
        Log ("  PostForm EXCEPTION: " + $_.Exception.Message)
        # In PowerShell 5.1 steht der echte Fehler-Body in ErrorDetails.Message
        $bodyTxt = $null
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $bodyTxt = $_.ErrorDetails.Message }
        elseif ($_.Exception.Response) {
            try { $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream()); $bodyTxt = $sr.ReadToEnd() } catch {}
        }
        Log ("  PostForm Antwort-Body: " + $bodyTxt)
        if ($bodyTxt) {
            try { return ($bodyTxt | ConvertFrom-Json) }
            catch { return [pscustomobject]@{ error = "http_error"; error_description = $bodyTxt } }
        }
        return [pscustomobject]@{ error = "net_error"; error_description = $_.Exception.Message }
    }
}

function GraphGet($url, $token) {
    return Invoke-RestMethod -Uri $url -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 30
}

function Decode-Jwt($token) {
    # Liest Name/E-Mail direkt aus dem Zugangs-Token (keine extra Berechtigung noetig).
    try {
        $payload = $token.Split('.')[1].Replace('-', '+').Replace('_', '/')
        switch ($payload.Length % 4) { 2 { $payload += '==' } 3 { $payload += '=' } }
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload))
        return ($json | ConvertFrom-Json)
    } catch { return $null }
}

function GraphSendMail($token, $payload) {
    try {
        $json = ($payload | ConvertTo-Json -Depth 20)
        Invoke-RestMethod -Method Post -Uri "https://graph.microsoft.com/v1.0/me/sendMail" `
            -Headers @{ Authorization = "Bearer $token" } `
            -ContentType "application/json; charset=utf-8" `
            -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 60 | Out-Null
        return @{ ok = $true }
    } catch {
        $msg = $_.Exception.Message
        try {
            $resp = $_.Exception.Response
            if ($resp) { $sr = New-Object IO.StreamReader($resp.GetResponseStream()); $msg = $sr.ReadToEnd() }
        } catch {}
        return @{ ok = $false; error = $msg }
    }
}

function Normalize-DeferUntil($value) {
    # Gibt einen UTC-Zeitstempel im Graph-SystemTime-Format zurück oder $null.
    # Wichtig: Bei ungültigem/vergangenem Zeitpunkt wird NICHT sofort gesendet.
    if ($null -eq $value) { return $null }
    $raw = ([string]$value).Trim()
    if (-not $raw) { return $null }
    try {
        $dto = [DateTimeOffset]::Parse($raw, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
        if ($dto.ToUniversalTime() -le [DateTimeOffset]::UtcNow.AddSeconds(30)) {
            throw "Le moment du rappel est déjà passé."
        }
        return $dto.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
    } catch {
        throw ("Moment de rappel invalide: " + $raw)
    }
}

# ----------------------------------------------------------- Token
function Load-Tokens {
    if (Test-Path $TokenFile) {
        try { return (Get-Content -Raw -Path $TokenFile | ConvertFrom-Json) } catch { return $null }
    }
    return $null
}
function Save-Tokens($tok, $account) {
    $data = @{
        access_token  = $tok.access_token
        refresh_token = $tok.refresh_token
        expires_at    = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + [int]$tok.expires_in - 60)
    }
    if ($account) { $data.account = $account }
    else { $old = Load-Tokens; if ($old -and $old.account) { $data.account = $old.account } }
    ($data | ConvertTo-Json -Depth 6) | Set-Content -Path $TokenFile -Encoding UTF8
}
function Get-AccessToken {
    $t = Load-Tokens
    if (-not $t) { return $null }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ($t.access_token -and $now -lt [int64]$t.expires_at) { return $t.access_token }
    if (-not $t.refresh_token) { return $null }
    $tok = PostForm "$Base/token" @{ grant_type = "refresh_token"; client_id = $ClientId; refresh_token = $t.refresh_token; scope = $Scope }
    if ($tok.access_token) { Save-Tokens $tok $null; return $tok.access_token }
    return $null
}

# ----------------------------------------------------------- Signatur (Windows)
function HtmlToText($html) {
    $s = $html
    $s = [regex]::Replace($s, '(?is)<!--.*?-->', '')
    $s = [regex]::Replace($s, '(?is)<head.*?</head>', '')
    $s = [regex]::Replace($s, '(?is)<style.*?</style>', '')
    $s = [regex]::Replace($s, '(?i)<br\s*/?>', "`n")
    $s = [regex]::Replace($s, '(?i)</(p|div|tr|li|h[1-6])\s*>', "`n")
    $s = [regex]::Replace($s, '(?s)<[^>]+>', '')
    $s = [System.Net.WebUtility]::HtmlDecode($s)
    $lines = @()
    foreach ($ln in ($s -split "`n")) {
        $t = ($ln -replace '[ \t\xA0]+', ' ').Trim()
        if ($t -eq '' -and ($lines.Count -eq 0 -or $lines[-1] -eq '')) { continue }
        $lines += $t
    }
    return (($lines -join "`n").Trim())
}
function Get-Signatures {
    $dir = Join-Path $env:APPDATA "Microsoft\Signatures"
    $out = @()
    if (Test-Path $dir) {
        Get-ChildItem -Path $dir -Filter *.htm -File | ForEach-Object {
            try {
                $html = [System.IO.File]::ReadAllText($_.FullName)
                $out += @{ name = $_.BaseName; text = (HtmlToText $html) }
            } catch {}
        }
    }
    return $out
}
function Find-Logo {
    $dir = Join-Path $env:APPDATA "Microsoft\Signatures"
    Log ("Find-Logo: Ordner = " + $dir + " | existiert: " + (Test-Path $dir))
    if (-not (Test-Path $dir)) { return $null }
    # Robust: alle Dateien rekursiv, dann nach Bild-Endung filtern (kein -Include-Quirk)
    $imgs = Get-ChildItem -Path $dir -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -match '(?i)\.(png|jpe?g|gif|bmp)$' }
    Log ("Find-Logo: " + @($imgs).Count + " Bilddatei(en) gefunden")
    $best = $null; $bestSize = 0
    foreach ($f in @($imgs)) {
        Log ("   - " + $f.FullName + "  (" + $f.Length + " Bytes)")
        if ($f.Length -gt 1000 -and $f.Length -gt $bestSize) { $best = $f; $bestSize = $f.Length }
    }
    if (-not $best) { Log "Find-Logo: kein geeignetes Bild (>1000 Bytes)"; return $null }
    Log ("Find-Logo: gewaehlt -> " + $best.Name + " (" + $best.Length + " Bytes)")
    $bytes = [IO.File]::ReadAllBytes($best.FullName)
    $ext = $best.Extension.TrimStart('.').ToLower()
    $mime = if ($ext -eq 'jpg' -or $ext -eq 'jpeg') { 'image/jpeg' } else { "image/$ext" }
    $b64 = [Convert]::ToBase64String($bytes)
    return @{ name = $best.Name; mime = $mime; dataUrl = "data:$mime;base64,$b64" }
}

# ----------------------------------------------------------- Signatur aus gesendeter Mail (Graph)
function Graph-LogoFromMail($token) {
    try {
        $dollar = [char]36
        $u = "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages?" + $dollar + "top=30&" + $dollar + "select=id&" + $dollar + "filter=hasAttachments%20eq%20true"
        $msgs = (GraphGet $u $token).value
        Log ("Graph-Logo: " + @($msgs).Count + " gesendete Mails mit Anhang")
        $countByKey = @{}; $store = @{}
        foreach ($m in $msgs) {
            $au = "https://graph.microsoft.com/v1.0/me/messages/" + $m.id + "/attachments?" + $dollar + "select=id,name,contentType,isInline,size"
            $atts = (GraphGet $au $token).value
            foreach ($a in $atts) {
                if (($a.contentType -like "image/*") -and ($a.size -gt 1500)) {
                    $key = [string]$a.size
                    if (-not $countByKey.ContainsKey($key)) { $countByKey[$key] = 0; $store[$key] = @{ mid = $m.id; aid = $a.id; name = $a.name; mime = $a.contentType } }
                    $countByKey[$key] = $countByKey[$key] + 1
                }
            }
        }
        $bestKey = $null; $bestCount = 0
        foreach ($k in $countByKey.Keys) { if ($countByKey[$k] -gt $bestCount) { $bestCount = $countByKey[$k]; $bestKey = $k } }
        if (-not $bestKey) { Log "Graph-Logo: kein Inline-Bild gefunden"; return $null }
        $sel = $store[$bestKey]
        $full = GraphGet ("https://graph.microsoft.com/v1.0/me/messages/" + $sel.mid + "/attachments/" + $sel.aid) $token
        if (-not $full.contentBytes) { return $null }
        Log ("Graph-Logo: gewaehlt " + $sel.name + " (" + $bestKey + " Bytes, " + $bestCount + "x)")
        return @{ name = $sel.name; mime = $sel.mime; dataUrl = ("data:" + $sel.mime + ";base64," + $full.contentBytes) }
    } catch { Log ("Graph-Logo FEHLER: " + $_.Exception.Message); return $null }
}

function Graph-SignatureFromMail($token) {
    try {
        $dollar = [char]36
        $u = "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages?" + $dollar + "top=15&" + $dollar + "select=id,body"
        $msgs = (GraphGet $u $token).value
        $keywords = @("Bien cordialement", "Cordialement", "Mit freundlichen", "Freundliche", "Beste Gruesse", "Liebe Gruesse", "Bien a vous", "Sportlechen")
        foreach ($m in $msgs) {
            $html = $m.body.content
            if (-not $html) { continue }
            $txt = HtmlToText $html
            $idx = -1
            foreach ($kw in $keywords) { $p = $txt.LastIndexOf($kw); if ($p -gt $idx) { $idx = $p } }
            if ($idx -ge 0) {
                $sig = $txt.Substring($idx)
                $lines = ($sig -split "`n") | Select-Object -First 12
                $res = (($lines -join "`n").Trim())
                if ($res.Length -gt 3) { Log "Graph-Signatur: Text aus gesendeter Mail extrahiert"; return $res }
            }
        }
        Log "Graph-Signatur: kein Signaturblock gefunden"
        return $null
    } catch { Log ("Graph-Signatur FEHLER: " + $_.Exception.Message); return $null }
}


# ----------------------------------------------------------- e-Bichelchen: isolierter Helfer
# Wichtig: Die komplizierte Browser-Automation liegt in EntretienConnect-eb.ps1.
# Dadurch kann die App selbst weiter starten, auch wenn der e-Bichelchen-Teil auf
# einem bestimmten Windows-Rechner noch einen Fehler ausgibt.

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

function Start-EbPlainBrowser {
    $urlEb = "https://ssl.education.lu/ebichelchen/app/login"
    try {
        Start-Process $urlEb
        return @{ ok=$true; info=@{ opened=$true; mode="plain"; url=$urlEb } }
    } catch {
        return @{ ok=$false; error=$_.Exception.Message }
    }
}

function Invoke-EbHelper($action, $groupId = "", $payloadFile = "", $browser = "auto", $appUrl = "") {
    $helper = Join-Path $ScriptDir "EntretienConnect-eb.ps1"
    if (-not (Test-Path $helper -PathType Leaf)) {
        return @{ ok=$false; error="EntretienConnect-eb.ps1 fehlt im App-Ordner." }
    }
    $ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path $ps -PathType Leaf)) { $ps = "powershell.exe" }

    $args = @("-NoLogo","-NoProfile","-ExecutionPolicy","Bypass","-File",$helper,"-Action",$action)
    if ($groupId) { $args += @("-GroupId",$groupId) }
    if ($payloadFile) { $args += @("-PayloadFile",$payloadFile) }
    if ($browser) { $args += @("-Browser",$browser) }
    if ($appUrl) { $args += @("-AppUrl",$appUrl) }

    try {
        $out = & $ps @args 2>&1
        $txt = (($out | Out-String).Trim())
        if (-not $txt) { return @{ ok=$false; error="Der e-Bichelchen-Helfer hat keine Antwort geliefert." } }
        try { return ($txt | ConvertFrom-Json) }
        catch { return @{ ok=$false; error=$txt } }
    } catch {
        return @{ ok=$false; error=$_.Exception.Message }
    }
}


function Start-EbPrewarmAsync {
    $helper = Join-Path $ScriptDir "EntretienConnect-eb.ps1"
    if (-not (Test-Path $helper -PathType Leaf)) { return }
    $ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path $ps -PathType Leaf)) { $ps = "powershell.exe" }
    try {
        $args = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$helper`" -Action prewarm -Browser auto"
        Start-Process -FilePath $ps -ArgumentList $args -WindowStyle Hidden | Out-Null
    } catch {}
}


function Focus-EntretienConnectWindow {
    # v302: App und e-Bichelchen sind Tabs desselben kontrollierten Browsers.
    # Zuerst den konkreten App-Tab per DevTools aktivieren; Fenster-API nur als Fallback.
    try {
        $cdp = Invoke-EbHelper "focus-app"
        if ($cdp.ok -and $cdp.info.focused) { return $cdp.info }
    } catch {}
    return (Raise-AppWindowOnly)
}

function Get-DefaultBrowserExe {
    # v337: Die im System hinterlegte Standard-App für http ermitteln.
    try {
        $uc = "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        $progId = $null
        try { $progId = (Get-ItemProperty -Path $uc -ErrorAction Stop).ProgId } catch {}
        if (-not $progId) { return $null }
        $cmdKey = "Registry::HKEY_CLASSES_ROOT\" + $progId + "\shell\open\command"
        $cmd = $null
        try { $cmd = (Get-ItemProperty -Path $cmdKey -ErrorAction Stop).'(default)' } catch {}
        if (-not $cmd) { return $null }
        $exe = $null
        if ($cmd -match '^\s*"([^"]+\.exe)"') { $exe = $matches[1] }
        elseif ($cmd -match '^\s*([^\s"]+\.exe)') { $exe = $matches[1] }
        if ($exe -and (Test-Path -LiteralPath $exe -PathType Leaf)) { return $exe }
    } catch {}
    return $null
}

function Get-AppWindowPlacement {
    # v347: Windows Forms liefert bei 125/150 % Skalierung logische Pixel,
    # Chromium erwartet für --window-size/--window-position auf diesem System
    # jedoch physische Pixel. AppliedDPI gleicht beide Koordinatensysteme ab.
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $screen = [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position)
        $area = $screen.WorkingArea
        $dpi = 96
        try {
            $appliedDpi = (Get-ItemProperty -Path "HKCU:\Control Panel\Desktop\WindowMetrics" -Name AppliedDPI -ErrorAction Stop).AppliedDPI
            if ([int]$appliedDpi -ge 96 -and [int]$appliedDpi -le 480) {
                $dpi = [int]$appliedDpi
            }
        } catch {}
        $scale = [double]$dpi / 96.0
        $physicalX = [int][Math]::Round($area.X * $scale)
        $physicalY = [int][Math]::Round($area.Y * $scale)
        $physicalWidth = [int][Math]::Round($area.Width * $scale)
        $physicalHeight = [int][Math]::Round($area.Height * $scale)
        $width = [int][Math]::Min($physicalWidth, [Math]::Max(1100, [Math]::Round($physicalWidth * 0.67)))
        $height = [int][Math]::Min($physicalHeight, [Math]::Max(720, [Math]::Round($physicalHeight * 0.67)))
        $left = [int]($physicalX + [Math]::Floor(($physicalWidth - $width) / 2))
        $top = [int]($physicalY + [Math]::Floor(($physicalHeight - $height) / 2))
        return [pscustomobject]@{
            Width = $width
            Height = $height
            Left = $left
            Top = $top
            Dpi = $dpi
            ScalePercent = [int][Math]::Round($scale * 100)
        }
    } catch {
        return [pscustomobject]@{
            Width = 1600
            Height = 960
            Left = 80
            Top = 60
            Dpi = 96
            ScalePercent = 100
        }
    }
}

function Initialize-EntretienConnectAppWin32 {
    if ("EntretienConnectAppWin32" -as [type]) { return }
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EntretienConnectAppWin32 {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern void SwitchToThisWindow(IntPtr hWnd, bool altTab);
  [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr dpiContext);

  public static bool IsForeground(IntPtr hWnd) { return GetForegroundWindow() == hWnd; }

  public static bool SetBoundsPhysical(IntPtr hWnd, int x, int y, int width, int height) {
    IntPtr previous = IntPtr.Zero;
    try { previous = SetThreadDpiAwarenessContext(new IntPtr(-4)); } catch {}
    try { return SetWindowPos(hWnd, IntPtr.Zero, x, y, width, height, 0x0014); }
    finally {
      if (previous != IntPtr.Zero) {
        try { SetThreadDpiAwarenessContext(previous); } catch {}
      }
    }
  }

  public static bool ForceForeground(IntPtr hWnd) {
    if (GetForegroundWindow() == hWnd) return true;
    IntPtr fg = GetForegroundWindow();
    uint fgThread = (fg == IntPtr.Zero) ? 0 : GetWindowThreadProcessId(fg, IntPtr.Zero);
    uint me = GetCurrentThreadId();
    bool attached = false;
    if (fgThread != 0 && fgThread != me) { attached = AttachThreadInput(fgThread, me, true); }
    SetWindowPos(hWnd, new IntPtr(-1), 0, 0, 0, 0, 0x0043);
    BringWindowToTop(hWnd);
    bool ok = SetForegroundWindow(hWnd);
    SetWindowPos(hWnd, new IntPtr(-2), 0, 0, 0, 0, 0x0043);
    if (attached) { AttachThreadInput(fgThread, me, false); }
    if (!ok) { ok = (GetForegroundWindow() == hWnd); }
    if (!ok) {
      SwitchToThisWindow(hWnd, true);
      ok = (GetForegroundWindow() == hWnd);
    }
    return ok;
  }
}
"@
}

function Open-AppInBrowser($u) {
    # v347: EntretienConnect startet in Chromium immer als eigenes, sofort
    # passend dimensioniertes und zentriertes
    # Browser-App-Fenster - unabhaengig davon, ob der Browser schon laeuft. Dadurch
    # sind Darstellung und Bedienung immer gleich und es entsteht nie ein Leertab.
    # Firefox besitzt keinen entsprechenden App-Modus und bekommt stets ein eigenes
    # normales Fenster.
    # Registry nicht lesbar oder unbekannter Browser: Windows-Verknuepfung nutzen.
    $exe = Get-DefaultBrowserExe
    $leaf = ""
    if ($exe) { $leaf = (Split-Path $exe -Leaf).ToLower() }
    if ($exe -and ($leaf -match '^(msedge|chrome|brave|vivaldi|opera)\.exe$')) {
        try {
            $window = Get-AppWindowPlacement
            # v348: Edge stellt bei --app gelegentlich seine alte gespeicherte
            # Fenstergeometrie wieder her. Die native Größenkorrektur ist bereits
            # kompiliert, bevor das Fenster entsteht, und kann es beim ersten
            # erkannten Handle ohne nachträgliche Wartephase korrigieren.
            try { Initialize-EntretienConnectAppWin32 }
            catch { Log ("Native Fenstervorbereitung fehlgeschlagen: " + $_.Exception.Message) }
            $script:AppWindowPlacement = $window
            $sizeArg = "--window-size=" + $window.Width + "," + $window.Height
            $positionArg = "--window-position=" + $window.Left + "," + $window.Top
            # v349: Zunächst minimiert erzeugen. Sobald das Fensterhandle existiert,
            # setzt der schnelle native Suchlauf die endgültige Geometrie und zeigt
            # das Fenster erst danach an. So ist die alte Edge-Größe nie sichtbar.
            Start-Process -FilePath $exe -ArgumentList @("--no-first-run","--no-default-browser-check","--start-minimized",$sizeArg,$positionArg,("--app=" + $u))
            Log ("Browser direkt gestartet: App-Modus " + $window.Width + "x" + $window.Height + " bei " + $window.Left + "," + $window.Top + ", Windows-Skalierung " + $window.ScalePercent + "% ohne Leertab (" + $leaf + ")")
            return
        } catch {
            Log ("Direktstart fehlgeschlagen (" + $_.Exception.Message + ") - zurück zur Windows-Verknüpfung.")
        }
    }
    if ($exe -and $leaf -eq 'firefox.exe') {
        try {
            Start-Process -FilePath $exe -ArgumentList @("-new-window",$u)
            Log "Firefox direkt gestartet: eigenes Fenster (kein Browser-App-Modus verfügbar)."
            return
        } catch {
            Log ("Firefox-Direktstart fehlgeschlagen (" + $_.Exception.Message + ") - zurück zur Windows-Verknüpfung.")
        }
    }
    Log ("Browser über die Windows-Verknüpfung geöffnet (Standard-Browser: " + $(if ($exe) { $leaf } else { "unbekannt" }) + ").")
    try { Start-Process $u } catch {}
}

function Raise-AppWindowOnly {
    # v336: Nur der Fenster-Teil, ohne den Umweg über den e-Bichelchen-Helfer.
    # Beim Programmstart wird genau das gebraucht: der Explorer-Ordner, aus dem
    # gestartet wurde, bleibt sonst vor dem frisch geöffneten Browserfenster.
    # Keine neue URL / kein zusätzlicher Tab wird geöffnet.
    $candidates = @()
    try {
        $candidates = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.MainWindowHandle -ne 0 -and
            ([string]$_.ProcessName) -match '^(msedge|chrome|brave|vivaldi|opera|firefox)$' -and
            ([string]$_.MainWindowTitle) -like "*EntretienConnect*"
        } | Sort-Object StartTime -Descending)
    } catch {}

    foreach ($proc in $candidates) {
        try {
            Initialize-EntretienConnectAppWin32
            $h = $proc.MainWindowHandle
            $resized = $false
            if ($script:AppWindowPlacement) {
                $p = $script:AppWindowPlacement
                # SWP_NOZORDER | SWP_NOACTIVATE: nur Größe und Position setzen.
                $resized = [bool][EntretienConnectAppWin32]::SetBoundsPhysical(
                    $h, [int]($p.Left), [int]($p.Top), [int]($p.Width), [int]($p.Height)
                )
            }
            # v335: SW_RESTORE (9) NUR bei einem wirklich minimierten Fenster.
            # Auf ein MAXIMIERTES Fenster angewandt stellt SW_RESTORE die vorherige,
            # kleinere Größe wieder her. Das App-Fenster schrumpfte deshalb jedes Mal,
            # wenn nach dem Login der Fokus zurückgeholt wurde.
            if ([EntretienConnectAppWin32]::IsIconic($h)) {
                [EntretienConnectAppWin32]::ShowWindowAsync($h, 9) | Out-Null
            }
            if ([EntretienConnectAppWin32]::IsForeground($h)) {
                return @{ focused=$true; resized=$resized; method="already-front"; processId=$proc.Id; title=$proc.MainWindowTitle }
            }
            try {
                $shell = New-Object -ComObject WScript.Shell
                $null = $shell.AppActivate($proc.Id)
            } catch {}
            $ok = [bool][EntretienConnectAppWin32]::ForceForeground($h)
            return @{ focused=$ok; resized=$resized; method="windows-user32"; processId=$proc.Id; title=$proc.MainWindowTitle }
        } catch {}
    }

    # Kein AppActivate per Teilstring: Ein geöffneter Ordner
    # "EntretienConnect_Starter..." oder die Logdatei würde sonst als App gelten,
    # den Suchlauf vorzeitig beenden und die echte Edge-Größenkorrektur verhindern.
    return @{ focused=$false; method="no-browser-window-yet" }
}


function Read-EbCache {
    try {
        if (Test-Path $EbCacheFile -PathType Leaf) {
            $raw = Get-Content -Path $EbCacheFile -Raw -Encoding UTF8
            if ($raw -and $raw.Trim()) { return ($raw | ConvertFrom-Json) }
        }
    } catch {}
    return $null
}

function Write-EbCache($data, $receivedAt) {
    try {
        if ($null -eq $data) { return }
        $obj = [pscustomobject]@{ data=$data; receivedAt=$receivedAt; cachedAt=(Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }
        ($obj | ConvertTo-Json -Depth 50) | Set-Content -Path $EbCacheFile -Encoding UTF8
    } catch { Log ("e-Bichelchen cache konnte nicht geschrieben werden: " + $_.Exception.Message) }
}

function Clear-EbCache {
    try { if (Test-Path $EbCacheFile -PathType Leaf) { Remove-Item -Force $EbCacheFile } } catch {}
}

function Test-EbSavedSession {
    # v339: Nur nachsehen, ob gemerkte Cookies da und nicht zu alt sind. Ob
    # education.lu sie noch annimmt, zeigt erst /api/eb/resume. Bewusst ohne
    # Helferaufruf - diese Route wird bei jedem Start abgefragt.
    try {
        if (-not (Test-Path $EbSessionFile -PathType Leaf)) { return $false }
        $data = (Get-Content -LiteralPath $EbSessionFile -Raw -Encoding UTF8) | ConvertFrom-Json
        if (@($data.cookies).Count -eq 0) { return $false }
        $now = [int64](([DateTime]::UtcNow) - (New-Object DateTime(1970,1,1,0,0,0,[DateTimeKind]::Utc))).TotalSeconds
        return (($now - [int64]$data.savedAt) -le $EbSessionMaxAgeSeconds)
    } catch { return $false }
}

function Test-EbVisibleLoginPage {
    # v349: Extrem schnelle Direktprüfung im bereits laufenden Haupthelfer.
    # Der vollständige e-Bichelchen-Helfer muss dafür nicht eigens starten.
    try {
        $res = Invoke-WebRequest -Uri "http://127.0.0.1:9223/json" -UseBasicParsing -TimeoutSec 1
        if (-not $res -or $res.StatusCode -ne 200) { return $false }
        $targets = @($res.Content | ConvertFrom-Json)
        $pages = @($targets | Where-Object {
            $_.type -eq "page" -and
            ([string]$_.url) -notmatch '^(about:blank|(chrome|edge)://newtab/?)$'
        })
        return ($pages.Count -gt 0)
    } catch {
        return $false
    }
}

function Handle-EbRequest($stream, $req) {
    $path = ($req.Path -split '\?')[0]
    try {
        if ($req.Method -eq "GET" -and $path -eq "/api/eb/status") {
            # v339: savedSession sagt der Oberfläche, dass eine gemerkte Sitzung
            # vorliegt – sie verbindet dann beim Start von selbst (wie auf dem Mac).
            $saved = Test-EbSavedSession
            $cache = Read-EbCache
            if ($null -ne $cache -and $null -ne $cache.data) {
                Send-Json $stream @{ ok=$true; hasData=$true; data=$cache.data; receivedAt=$cache.receivedAt; cachedAt=$cache.cachedAt; savedSession=$saved; note="Données e-Bichelchen récupérées depuis le cache local." }
            } else {
                Send-Json $stream @{ ok=$true; hasData=$false; data=$null; savedSession=$saved; note="Aucune donnée e-Bichelchen lue pour cette session." }
            }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/resume") {
            # v339: Klassen aus der gemerkten Sitzung lesen – ohne Loginfenster.
            # Scheitert das, meldet die Oberfläche einfach « nicht verbunden » und
            # der Benutzer klickt wie bisher auf « Connecter ».
            if (-not (Test-EbSavedSession)) {
                Send-Json $stream @{ ok=$false; resumed=$false; error="Keine gemerkte e-Bichelchen-Sitzung vorhanden." }
                return
            }
            $groupId = Get-QueryParam $req.Path "groupId" ""
            $r = Invoke-EbHelper "resume" $groupId
            if ($r.ok) {
                Write-EbCache $r.data $r.receivedAt
                Send-Json $stream @{ ok=$true; data=$r.data; receivedAt=$r.receivedAt; resumed=$true }
            } else {
                Send-Json $stream @{ ok=$false; resumed=$false; sessionExpired=(-not (Test-EbSavedSession)); error=[string]$r.error }
            }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/open-browser") {
            Clear-EbCache
            $browser = Get-QueryParam $req.Path "browser" "auto"
            $r = Invoke-EbHelper "open" "" "" $browser
            if ($r.ok) {
                Send-Json $stream $r
            } else {
                # v138: Un navigateur ouvert sans DevTools ne peut pas être lu ensuite.
                # On ne fait donc plus semblant que la connexion a démarré.
                $fallback = Start-EbPlainBrowser
                if ($fallback.ok) {
                    Send-Json $stream @{ ok=$false; plainBrowserOpened=$true; error=("La fenêtre e-Bichelchen a été ouverte, mais l’automatisation locale n’a pas démarré. Fermez cette fenêtre puis réessayez avec Chrome ou Edge. Détail: " + [string]$r.error) }
                } else {
                    Send-Json $stream @{ ok=$false; error=("e-Bichelchen konnte nicht geöffnet werden. Helper: " + [string]$r.error + " | Browser: " + [string]$fallback.error) }
                }
            }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/login-ready") {
            # Zweimal kurz bestätigen, damit ein flüchtiger Target-Wechsel während
            # einer IAM-Weiterleitung nicht als manuelles Schließen gilt.
            if (-not (Test-EbVisibleLoginPage)) {
                Start-Sleep -Milliseconds 120
                if (-not (Test-EbVisibleLoginPage)) {
                    Send-Json $stream @{ ok=$true; ready=$false; browserClosed=$true; lightweight=$true; stage="closed" }
                    return
                }
            }
            $r = Invoke-EbHelper "ready"
            if ($r.ok) {
                Send-Json $stream @{ ok=$true; ready=[bool]$r.ready; groupCount=$r.groupCount; status=$r.status; browserClosed=$false; lightweight=$true }
            } else {
                Send-Json $stream @{ ok=$true; ready=$false; browserClosed=[bool]$r.browserClosed; lightweight=$true; stage="waiting" }
            }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/read-browser") {
            $groupId = Get-QueryParam $req.Path "groupId" ""
            $quiet = (Get-QueryParam $req.Path "quiet" "0") -in @("1","true","True","yes")
            $r = Invoke-EbHelper "read" $groupId ""
            if ($r.ok) {
                Write-EbCache $r.data $r.receivedAt
                Send-Json $stream @{ ok=$true; data=$r.data; receivedAt=$r.receivedAt }
            }
            else {
                $browserClosed = [bool]$r.browserClosed
                $errorText = [string]$r.error
                $retrySoon = ($errorText -match 'Relecture en cours|sélectionnée automatiquement')
                Send-Json $stream @{ ok=$false; waiting=($quiet -and -not $browserClosed); browserClosed=$browserClosed; retrySoon=$retrySoon; error=$errorText }
            }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/park") {
            # v297: Kompatibilitätsroute für eventuell gecachte v296-Seiten.
            # Die Helper-Aktion minimiert nie mehr, sondern schließt nur den e-Bichelchen-Tab.
            $r = Invoke-EbHelper "park"
            if ($r.ok) { Send-Json $stream @{ ok=$true; info=$r.info } }
            else { Send-Json $stream @{ ok=$true; info=@{ parked=$false; minimized=$false; keptOpenForPublishing=$true } } }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/reset-session") {
            # v292: soft=1 → session IAM à moitié faite effacée (cookies via DevTools),
            # navigateur gardé au chaud. Windows ne ferme de toute façon jamais la fenêtre.
            $r = Invoke-EbHelper "soft-reset"
            if ($r.ok) { Send-Json $stream @{ ok=$true; info=$r.info } }
            else { Send-Json $stream @{ ok=$true; info=@{ softReset=$true; browserRunning=$false } } }
            return
        }

        if ($req.Method -eq "GET" -and ($path -eq "/api/eb/cleanup" -or $path -eq "/api/eb/close")) {
            # v299: Nach der vollständigen Lesung den isolierten Hilfsbrowser schließen.
            # Falls Windows Browser.close nicht zulässt, wird zumindest der e-Bichelchen-
            # Tab geschlossen. Danach wird das bestehende App-Fenster fokussiert.
            $r = Invoke-EbHelper "cleanup"
            $focus = Focus-EntretienConnectWindow
            if ($r.ok) { Send-Json $stream @{ ok=$true; info=$r.info; focusedApp=$focus } }
            else { Send-Json $stream @{ ok=$true; info=@{ closed=$false; error=$r.error }; focusedApp=$focus } }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/focus-app") {
            Send-Json $stream @{ ok=$true; info=(Focus-EntretienConnectWindow) }
            return
        }

        if ($req.Method -eq "GET" -and $path -eq "/api/eb/clear") {
            Clear-EbCache
            Send-Json $stream @{ ok=$true }
            return
        }

        if ($req.Method -eq "POST" -and $path -eq "/api/eb/create-bulk-entries") {
            Send-Json $stream @{ ok=$false; error="Das Veröffentlichen nach e-Bichelchen ist in dieser Rettungsversion deaktiviert. Erst wird der Start stabilisiert; danach bauen wir diesen Schritt wieder sauber ein." }
            return
        }

        if ($req.Method -eq "POST" -and $path -eq "/api/eb/delete-created-entries") {
            Send-Json $stream @{ ok=$false; deleted=@(); errors=@(@{ error="Löschen ist in dieser Rettungsversion nicht aktiv." }) }
            return
        }

        Send-Json $stream @{ ok=$false; error="Route e-Bichelchen inconnue." }
    } catch {
        Send-Json $stream @{ ok=$false; error=$_.Exception.Message }
    }
}



# ----------------------------------------------------------- HTTP-Server
function Find-HeaderEnd($arr, $len) {
    for ($i = 0; $i -lt $len - 3; $i++) {
        if ($arr[$i] -eq 13 -and $arr[$i+1] -eq 10 -and $arr[$i+2] -eq 13 -and $arr[$i+3] -eq 10) { return $i }
    }
    return -1
}
function Read-Request($stream) {
    try { $stream.ReadTimeout = 15000 } catch {}
    $buf = New-Object byte[] 16384
    $ms = New-Object System.IO.MemoryStream
    $headerEnd = -1
    while ($true) {
        $arr = $ms.ToArray()
        $headerEnd = Find-HeaderEnd $arr $arr.Length
        if ($headerEnd -ge 0) { break }
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        $ms.Write($buf, 0, $n)
        if ($ms.Length -gt 5000000) { break }
    }
    $arr = $ms.ToArray()
    if ($headerEnd -lt 0) { return $null }
    $headerText = [Text.Encoding]::ASCII.GetString($arr, 0, $headerEnd)
    $contentLength = 0
    foreach ($line in ($headerText -split "`r`n")) {
        if ($line -match '^(?i)Content-Length:\s*(\d+)') { $contentLength = [int]$matches[1] }
    }
    $bodyStart = $headerEnd + 4
    while (($arr.Length - $bodyStart) -lt $contentLength) {
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        $ms.Write($buf, 0, $n)
        $arr = $ms.ToArray()
        if ($ms.Length -gt 50000000) { break }
    }
    $arr = $ms.ToArray()
    $body = ""
    if ($contentLength -gt 0 -and $arr.Length -ge $bodyStart) {
        $blen = [Math]::Min($contentLength, $arr.Length - $bodyStart)
        $body = [Text.Encoding]::UTF8.GetString($arr, $bodyStart, $blen)
    }
    $firstLine = ($headerText -split "`r`n")[0]
    $parts = $firstLine -split ' '
    return [pscustomobject]@{ Method = $parts[0]; Path = $parts[1]; Body = $body }
}
function Send-Bytes($stream, $status, $contentType, [byte[]]$body) {
    $head = "HTTP/1.1 $status`r`nContent-Type: $contentType`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
    $hb = [Text.Encoding]::ASCII.GetBytes($head)
    $stream.Write($hb, 0, $hb.Length)
    if ($body.Length -gt 0) { $stream.Write($body, 0, $body.Length) }
    $stream.Flush()
}
function Send-Json($stream, $obj) {
    $json = ($obj | ConvertTo-Json -Depth 12 -Compress)
    Send-Bytes $stream "200 OK" "application/json; charset=utf-8" ([Text.Encoding]::UTF8.GetBytes($json))
}
function Get-LocalCsvCandidates {
    $paths = @()
    try { if ($AppRoot) { $paths += (Join-Path $AppRoot $LocalCsvName) } } catch {}
    try { if ($ScriptDir) { $paths += (Join-Path $ScriptDir $LocalCsvName) } } catch {}
    try { if ($RuntimeDir) { $paths += (Join-Path $RuntimeDir $LocalCsvName) } } catch {}
    $seen = @{}
    $out = @()
    foreach ($p in $paths) {
        try {
            $full = [System.IO.Path]::GetFullPath($p)
            if (-not $seen.ContainsKey($full)) { $seen[$full] = $true; $out += $full }
        } catch {}
    }
    return $out
}

function Handle-Request($stream, $req) {
    $path = ($req.Path -split '\?')[0]
    $method = $req.Method
    Write-Host ("[" + $method + "] " + $path)
    try {

    # ---- API ----
    if ($path -eq "/api/app/heartbeat") {
        $script:LastHeartbeatUtc = [DateTime]::UtcNow
        Send-Json $stream @{ ok = $true; serverTime = $script:LastHeartbeatUtc.ToString("o") }
        return
    }
    if ($path -eq "/api/app/shutdown") {
        $script:ShutdownRequested = $true
        Send-Json $stream @{ ok = $true; shuttingDown = $true }
        return
    }
    if ($path -eq "/api/app/storage") {
        if ($method -eq "GET") {
            if (Test-Path $StateFile -PathType Leaf) {
                try {
                    $raw = Get-Content -LiteralPath $StateFile -Raw -ErrorAction Stop
                    $st = if ($raw) { $raw | ConvertFrom-Json } else { $null }
                    Send-Json $stream @{ ok = $true; state = $st; path = $StateFile }
                } catch { Send-Json $stream @{ ok = $false; error = $_.Exception.Message } }
            } else { Send-Json $stream @{ ok = $true; state = $null; path = $StateFile } }
        } else {
            try {
                $data = if ($req.Body) { $req.Body | ConvertFrom-Json } else { $null }
                if (-not $data -or -not $data.state) { Send-Json $stream @{ ok = $false; error = "Aucun état à enregistrer." }; return }
                try {
                    if ((Test-Path $StateFile -PathType Leaf) -and (Test-Path $BackupDir -PathType Container)) {
                        $recent = Get-ChildItem -LiteralPath $BackupDir -Filter "state-*.json" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
                        if ((-not $recent) -or (((Get-Date) - $recent.LastWriteTime).TotalHours -ge 6)) { Copy-Item -LiteralPath $StateFile -Destination (Join-Path $BackupDir ("state-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json")) -Force }
                        Get-ChildItem -LiteralPath $BackupDir -Filter "state-*.json" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -Skip 12 | Remove-Item -Force -ErrorAction SilentlyContinue
                    }
                } catch {}
                try { $data.state | Add-Member -NotePropertyName "_savedAt" -NotePropertyValue ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) -Force } catch {}
                ($data.state | ConvertTo-Json -Depth 100 -Compress) | Set-Content -LiteralPath $StateFile -Encoding UTF8
                Send-Json $stream @{ ok = $true; path = $StateFile }
            } catch { Send-Json $stream @{ ok = $false; error = $_.Exception.Message } }
        }
        return
    }
    if ($path -eq "/api/app/local-csv") {
        try {
            foreach ($csvPath in (Get-LocalCsvCandidates)) {
                if (Test-Path -LiteralPath $csvPath -PathType Leaf) {
                    $item = Get-Item -LiteralPath $csvPath -ErrorAction Stop
                    if ($item.Length -gt (2 * 1024 * 1024)) { Send-Json $stream @{ ok = $false; found = $true; filename = $LocalCsvName; path = $csvPath; error = "Le fichier CSV est trop volumineux." }; return }
                    try { $content = [System.IO.File]::ReadAllText($csvPath, [Text.Encoding]::UTF8) } catch { $content = Get-Content -LiteralPath $csvPath -Raw -ErrorAction Stop }
                    Send-Json $stream @{ ok = $true; found = $true; filename = $LocalCsvName; path = $csvPath; content = $content }
                    return
                }
            }
            Send-Json $stream @{ ok = $true; found = $false; filename = $LocalCsvName; paths = (Get-LocalCsvCandidates) }
        } catch { Send-Json $stream @{ ok = $false; found = $false; filename = $LocalCsvName; error = $_.Exception.Message } }
        return
    }
    if ($path -eq "/api/graph/capabilities") {
        Send-Json $stream @{ ok = $true; deferredSend = $true; platform = "windows-powershell"; appVersion = $script:HelperVersion; backendGeneration = $script:HelperVersion; port = $Port; instancePath = $ScriptDir }
        return
    }
    if ($path -eq "/api/graph/account") {
        $t = Load-Tokens
        if ($t -and $t.account -and (Get-AccessToken)) { Send-Json $stream @{ ok = $true; signedIn = $true; account = $t.account; lastLoginError = "" } }
        else { Send-Json $stream @{ ok = $true; signedIn = $false; lastLoginError = $script:LastLoginError } }
        return
    }
    if ($path -eq "/api/graph/login-reset") {
        $script:Pending = $null; $script:PendingWeb = $null; $script:LastLoginError = $null
        Send-Json $stream @{ ok = $true }
        return
    }
    # ---- v176: Login sans code (auth-code + PKCE, redirection loopback) ----
    if ($path -eq "/api/graph/login-start-web") {
        $script:Pending = $null; $script:LastLoginError = $null
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        $vb = New-Object byte[] 64
        $rng.GetBytes($vb)
        $verifier = [Convert]::ToBase64String($vb).TrimEnd('=').Replace('+','-').Replace('/','_')
        $sha = [Security.Cryptography.SHA256]::Create()
        $challenge = [Convert]::ToBase64String($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($verifier))).TrimEnd('=').Replace('+','-').Replace('/','_')
        $sb = New-Object byte[] 24
        $rng.GetBytes($sb)
        $oauthState = [Convert]::ToBase64String($sb).TrimEnd('=').Replace('+','-').Replace('/','_')
        # v176: Racine seulement. Azure accepte le port loopback, mais le chemin
        # doit correspondre à http://localhost ; /oauth/redirect donnait AADSTS50011.
        $redirect = "http://localhost:$Port/"
        $script:PendingWeb = @{ state = $oauthState; verifier = $verifier; redirect = $redirect }
        $q = "client_id=" + [Uri]::EscapeDataString($ClientId) +
             "&response_type=code" +
             "&redirect_uri=" + [Uri]::EscapeDataString($redirect) +
             "&scope=" + [Uri]::EscapeDataString($Scope) +
             "&state=" + $oauthState +
             "&code_challenge=" + $challenge +
             "&code_challenge_method=S256&prompt=select_account"
        Send-Json $stream @{ ok = $true; authUrl = ($Base + "/authorize?" + $q) }
        return
    }
    # v185: aligné sur le helper Python — il faut state ET code/error, sinon une simple
    # URL « /?state=x » afficherait la page d'erreur OAuth au lieu de l'app.
    $isOAuthRoot = (($path -eq "/" -or $path -eq "") -and ($req.Path -match '[?&]state=') -and ($req.Path -match '[?&](code|error)='))
    if ($path -eq "/oauth/redirect" -or $isOAuthRoot) {
        $qs = @{}
        if ($req.Path -match '\?') {
            $qraw = ($req.Path -split '\?', 2)[1]
            foreach ($pair in ($qraw -split '&')) {
                if ($pair -match '=') {
                    $kv = $pair -split '=', 2
                    $qs[[Uri]::UnescapeDataString($kv[0])] = [Uri]::UnescapeDataString(($kv[1] -replace '\+', ' '))
                }
            }
        }
        $ok = $false
        $msg = ""
        $pend = $script:PendingWeb
        if ((-not $pend) -or (-not $qs["state"]) -or ($qs["state"] -ne $pend.state)) {
            $msg = "Session de connexion inconnue ou expirée. Réessayez depuis EntretienConnect."
        } elseif (-not $qs["code"]) {
            if ($qs["error_description"]) { $msg = $qs["error_description"] }
            elseif ($qs["error"]) { $msg = $qs["error"] }
            else { $msg = "Connexion annulée." }
        } else {
            $script:PendingWeb = $null
            $tok = PostForm "$Base/token" @{ grant_type = "authorization_code"; client_id = $ClientId; code = $qs["code"]; redirect_uri = $pend.redirect; code_verifier = $pend.verifier; scope = $Scope }
            if ($tok -and $tok.access_token) {
                $acct = @{ name = ""; email = "" }
                try {
                    $me = GraphGet 'https://graph.microsoft.com/v1.0/me?$select=displayName,mail,userPrincipalName' $tok.access_token
                    $mail = if ($me.mail) { $me.mail } else { $me.userPrincipalName }
                    $acct = @{ name = ("" + $me.displayName); email = ("" + $mail) }
                } catch {}
                Save-Tokens $tok $acct
                $ok = $true
                Log ("login OK (web): " + $acct.email)
            } else {
                if ($tok -and $tok.error_description) { $msg = "" + $tok.error_description }
                else { $msg = "Échec de l'échange du code." }
            }
        }
        if ($ok) {
            $script:LastLoginError = $null
            $inner = "<div style='font-size:42px'>&#9989;</div><h2>Connexion r&#233;ussie</h2><p>Vous pouvez fermer cet onglet et revenir &#224; EntretienConnect.</p><script>setTimeout(function(){ try{ window.close(); }catch(e){} }, 1500);</script>"
        } else {
            $script:LastLoginError = $msg
            $safeMsg = ("" + $msg).Replace("&","&amp;").Replace("<","&lt;").Replace(">","&gt;")
            $inner = "<div style='font-size:42px'>&#9888;&#65039;</div><h2>Connexion impossible</h2><p>" + $safeMsg + "</p><p>Fermez cet onglet et r&#233;essayez depuis EntretienConnect.</p>"
        }
        $page = "<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>EntretienConnect</title></head><body style=""font-family:-apple-system,'Segoe UI',Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f6f7fb;color:#1c2333""><div style='text-align:center;max-width:480px;padding:20px'>" + $inner + "</div></body></html>"
        Send-Bytes $stream "200 OK" "text/html; charset=utf-8" ([Text.Encoding]::UTF8.GetBytes($page))
        return
    }
    if ($path -eq "/api/graph/login-start") {
        $script:PendingWeb = $null; $script:LastLoginError = $null
        $dc = PostForm "$Base/devicecode" @{ client_id = $ClientId; scope = $Scope }
        if ($dc.device_code) {
            Write-Host ("  login-start: Code erhalten (" + $dc.user_code + ")")
            $script:Pending = $dc
            Send-Json $stream @{ ok = $true; user_code = $dc.user_code; verification_uri = $dc.verification_uri; message = $dc.message }
        } else {
            $emsg = (("" + $dc.error_description + " " + $dc.error)).Trim()
            if (-not $emsg) { $emsg = "Unerwartete Antwort: " + ($dc | ConvertTo-Json -Compress) }
            Write-Host ("  login-start FEHLER: " + $emsg)
            Send-Json $stream @{ ok = $false; error = $emsg }
        }
        return
    }
    if ($path -eq "/api/graph/login-poll") {
        if (-not $script:Pending) { Send-Json $stream @{ ok = $false; error = "Kein Login gestartet." }; return }
        $tok = PostForm "$Base/token" @{ grant_type = "urn:ietf:params:oauth:grant-type:device_code"; client_id = $ClientId; device_code = $script:Pending.device_code }
        if ($tok.access_token) {
            $acct = @{ name = ""; email = "" }
            try {
                $c = Decode-Jwt $tok.access_token
                if ($c) {
                    $acct.name = $c.name
                    $acct.email = if ($c.upn) { $c.upn } elseif ($c.preferred_username) { $c.preferred_username } else { $c.unique_name }
                }
            } catch {}
            Save-Tokens $tok $acct
            $script:Pending = $null
            Log ("login OK: " + $acct.email)
            Send-Json $stream @{ ok = $true; status = "ok"; account = $acct }
        } elseif ($tok.error -eq "authorization_pending" -or $tok.error -eq "slow_down") {
            Send-Json $stream @{ ok = $true; status = "pending" }
        } else {
            $script:Pending = $null
            Send-Json $stream @{ ok = $true; status = "error"; error = ("" + $tok.error_description + $tok.error) }
        }
        return
    }
    if ($path -eq "/api/graph/logout") {
        try { if (Test-Path $TokenFile) { Remove-Item $TokenFile -Force } } catch {}
        $script:Pending = $null; $script:PendingWeb = $null; $script:LastLoginError = $null
        Send-Json $stream @{ ok = $true }
        return
    }
    if ($path -eq "/api/graph/send") {
        $token = Get-AccessToken
        if (-not $token) { Send-Json $stream @{ ok = $false; error = "Nicht angemeldet. Bitte erneut mit Microsoft anmelden." }; return }
        $data = $null
        try { $data = $req.Body | ConvertFrom-Json } catch {}
        if (-not $data -or -not $data.messages) { Send-Json $stream @{ ok = $false; error = "Aucun e-mail." }; return }
        $results = @()
        foreach ($m in $data.messages) {
            $msg = @{
                subject      = $m.subject
                body         = @{ contentType = "HTML"; content = $m.html }
                toRecipients = @($m.to | ForEach-Object { @{ emailAddress = @{ address = $_ } } })
            }
            $deferUntil = $null
            try {
                if ($m.PSObject.Properties.Name -contains "deferUntil" -and $m.deferUntil) {
                    $deferUntil = Normalize-DeferUntil $m.deferUntil
                    $msg["singleValueExtendedProperties"] = @(@{
                        id    = "SystemTime 0x3FEF"
                        value = $deferUntil
                    })
                    Log ("Deferred send requested for message " + $m.id + " -> " + $deferUntil)
                }
            } catch {
                $results += @{ id = $m.id; ok = $false; error = $_.Exception.Message }
                continue
            }
            $atts = @()
            if ($data.logo -and $data.logo.contentBytes -and ($m.html -match 'cid:siglogo')) {
                $atts += @{
                    "@odata.type" = "#microsoft.graph.fileAttachment"
                    name          = $data.logo.name
                    contentType   = $data.logo.mime
                    contentBytes  = $data.logo.contentBytes
                    isInline      = $true
                    contentId     = "siglogo"
                }
            }
            # v163: PDF individuel par message (rappels) ; repli sur l'ancien PDF global (< v163).
            $att = $null
            if ($m.PSObject.Properties.Name -contains "attachment" -and $m.attachment -and $m.attachment.contentBytes) { $att = $m.attachment }
            elseif ($m.kind -eq "reminder" -and $data.PSObject.Properties.Name -contains "reminderAttachment" -and $data.reminderAttachment -and $data.reminderAttachment.contentBytes) { $att = $data.reminderAttachment }
            if ($att) {
                $attName = if ($att.name) { $att.name } else { "rappel.pdf" }
                $attType = if ($att.contentType) { $att.contentType } elseif ($att.mime) { $att.mime } else { "application/pdf" }
                $atts += @{
                    "@odata.type" = "#microsoft.graph.fileAttachment"
                    name          = $attName
                    contentType   = $attType
                    contentBytes  = $att.contentBytes
                }
            }
            if ($atts.Count -gt 0) { $msg["attachments"] = $atts }
            $r = GraphSendMail $token @{ message = $msg; saveToSentItems = $true }
            if ($r.ok) { $results += @{ id = $m.id; ok = $true; deferredUntil = $deferUntil } }
            else { $results += @{ id = $m.id; ok = $false; error = $r.error } }
        }
        Send-Json $stream @{ ok = $true; results = $results }
        return
    }
    if ($path -eq "/api/outlook-signatures") {
        try {
            $sigs = Get-Signatures
            if (-not $sigs -or @($sigs).Count -eq 0) {
                $token = Get-AccessToken
                if ($token) {
                    $t = Graph-SignatureFromMail $token
                    if ($t) { $sigs = @(@{ name = "Depuis un e-mail envoyé"; text = $t }) }
                }
            }
            Send-Json $stream @{ ok = $true; signatures = $sigs }
        } catch { Send-Json $stream @{ ok = $false; error = $_.Exception.Message } }
        return
    }
    if ($path -eq "/api/find-logo") {
        try {
            $logo = Find-Logo
            if (-not $logo) {
                $token = Get-AccessToken
                if ($token) { $logo = Graph-LogoFromMail $token }
            }
            if ($logo) { Send-Json $stream @{ ok = $true; logo = $logo } }
            else { Send-Json $stream @{ ok = $false; error = "Kein Logo gefunden." } }
        } catch { Send-Json $stream @{ ok = $false; error = $_.Exception.Message } }
        return
    }
    if ($path -like "/api/eb/*") {

        Handle-EbRequest $stream $req

        return

    }

    # ---- statische Dateien ----
    if ($path -eq "/" -or $path -eq "") { $path = "/graph.html" }
    $rel = $path.TrimStart('/')
    $file = Join-Path $ScriptDir $rel
    if ((Test-Path $file -PathType Leaf)) {
        $ext = [IO.Path]::GetExtension($file).ToLower()
        $ct = switch ($ext) {
            ".html" { "text/html; charset=utf-8" }
            ".js"   { "application/javascript; charset=utf-8" }
            ".css"  { "text/css; charset=utf-8" }
            ".png"  { "image/png" }
            ".jpg"  { "image/jpeg" }
            ".svg"  { "image/svg+xml" }
            default { "application/octet-stream" }
        }
        Send-Bytes $stream "200 OK" $ct ([IO.File]::ReadAllBytes($file))
    } else {
        Send-Bytes $stream "404 Not Found" "text/plain; charset=utf-8" ([Text.Encoding]::UTF8.GetBytes("Not found"))
    }

    } catch {
        Write-Host ("  SERVERFEHLER: " + $_.Exception.Message)
        try { Send-Json $stream @{ ok = $false; error = ("Serverfehler: " + $_.Exception.Message) } } catch {}
    }
}

# ----------------------------------------------------------- Start
# Lokale Oberfläche sofort starten; GitHub-Aktualisierung danach im Hintergrund.
# v138: arrêt automatique quand l’onglet principal est fermé; une nouvelle ouverture de l’app ne doit pas afficher d’anciennes classes
# e-Bichelchen comme si l’utilisateur était encore connecté.
if ($UpdateUiOnly) {
    Update-UiFromGitHub
    exit 0
}

$preferredPortOpen = Test-EntretienConnectPortOpen
$reusable = $null
if ($preferredPortOpen) {
    $reusable = Get-ReusableEntretienConnectHelper
}
if ($reusable) {
    $url = "http://127.0.0.1:$($reusable.Port)/graph.html"
    Log ("Laufender v" + $reusable.Version + "-Helfer wird sofort wiederverwendet.")
    if (-not $NoAutoOpen) { Open-AppInBrowser $url }
    exit 0
}

$cleanupStarted = Get-Date
if ($preferredPortOpen) {
    Stop-OldEntretienConnectHelpers
} else {
    Log "Kein alter Helfer aktiv; langsame Prozesssuche uebersprungen."
}
Log ("Alte Helfer geprueft nach " + [int](((Get-Date) - $cleanupStarted).TotalMilliseconds) + " ms.")
# v345: Den letzten bestaetigten e-Bichelchen-Stand nicht bei jedem Start loeschen.
# Bei abgelaufener Sitzung ignoriert die Oberflaeche ihn; beim Abmelden/Neuanmelden
# wird er weiterhin ausdruecklich entfernt.

try {
    $started = New-EntretienConnectListener -Preferred $PreferredPort
    $listener = $started.Listener
    $Port = [int]$started.Port
    $url = "http://127.0.0.1:$Port/graph.html"
    Write-EntretienConnectPidFile
    if ($started.Fallback) {
        Log ("Port " + $PreferredPort + " était occupé. EntretienConnect utilise automatiquement le port " + $Port + ".")
    }
    Log ("Lokaler Server bereit nach insgesamt " + [int](((Get-Date) - $script:ProcessStartedAt).TotalMilliseconds) + " ms.")
} catch {
    Write-Host ""
    Write-Host ("EntretienConnect konnte keinen lokalen Port öffnen: " + $_.Exception.Message)
    throw
}

Write-Host "============================================================"
Write-Host ("  EntretienConnect est lancé.   [Version : v" + $script:HelperVersion + " GitHub Starter - sans Python]")
Write-Host "  Dans le navigateur :  $url"
Write-Host "  Laissez cette fenêtre ouverte. La fermer = quitter."
Write-Host "============================================================"
if (-not $NoAutoOpen) {
    # v303: EntretienConnect wieder im Standardbrowser öffnen. Der kontrollierte
    # Chromium-Browser wird nur dann verwendet, wenn Windows ihn für die
    # e-Bichelchen-Automation wirklich benötigt.
    # v336: protokollieren, WAS geöffnet wird. EntretienConnect öffnet den Browser
    # genau einmal - falls dennoch zwei Tabs erscheinen, steht in der Logdatei
    # schwarz auf weiß, dass der zweite nicht von uns stammt.
    Log ("Browser wird EINMAL geöffnet mit: " + $url)
    Open-AppInBrowser $url
    # v348: Sofort ab dem ersten vorhandenen Fensterhandle Größe, Position und Fokus
    # in einem Schritt setzen. Danach wird nie wieder am Fenster gerüttelt.
    $script:RaiseAppAt = Get-Date
    $script:RaiseAppUntil = (Get-Date).AddSeconds(10)
    $script:RaiseAppDone = $false
} else {
    $script:RaiseAppDone = $true
}
$script:UpdateUiAt = (Get-Date).AddSeconds(3)
$script:UpdateUiStarted = $false

while (-not $script:ShutdownRequested) {
    $client = $null
    try {
        if ($listener.Pending()) {
            $client = $listener.AcceptTcpClient()
            $stream = $client.GetStream()
            $req = Read-Request $stream
            if ($req) { Handle-Request $stream $req }
        } else {
            # Während Edge sein Fenster erzeugt, kurz und reaktionsschnell prüfen.
            # Nach der einmaligen Korrektur bleibt der ruhige 250-ms-Serverzyklus.
            if (-not $script:RaiseAppDone) { Start-Sleep -Milliseconds 25 }
            else { Start-Sleep -Milliseconds 250 }
        }
    } catch {
        # einzelne fehlerhafte Anfrage ignorieren, Server weiterlaufen lassen
    } finally {
        if ($client) { try { $client.Close() } catch {} }
    }

    # App-Fenster einmal nach vorne holen; danach nie wieder den Fokus wegnehmen.
    if (-not $script:RaiseAppDone -and (Get-Date) -ge $script:RaiseAppAt) {
        $script:RaiseAppAt = (Get-Date).AddMilliseconds(25)
        $raised = $null
        try { $raised = Raise-AppWindowOnly } catch { Log ("Fenster nach vorne holen fehlgeschlagen: " + $_.Exception.Message) }
        if ($raised -and $raised.focused) {
            $script:RaiseAppDone = $true
            Log ("Fenster steht vorne: " + ($raised | ConvertTo-Json -Compress -Depth 3))
        } else {
            if ((Get-Date) -ge $script:RaiseAppUntil) {
                $script:RaiseAppDone = $true
                Log "Fenster liess sich nicht nach vorne holen - aufgegeben."
            }
        }
    }

    if (-not $script:UpdateUiStarted -and (Get-Date) -ge $script:UpdateUiAt) {
        $script:UpdateUiStarted = $true
        Start-UiUpdateInBackground
    }

    try {
        $now = [DateTime]::UtcNow
        # v167: grand saut d'horloge = sortie de veille. On redonne à l'onglet une fenêtre
        # complète pour renvoyer un battement au lieu d'arrêter le helper immédiatement.
        if ($null -ne $script:LastWatchdogTickUtc -and ($now - $script:LastWatchdogTickUtc).TotalSeconds -gt 60) {
            if ($null -ne $script:LastHeartbeatUtc) { $script:LastHeartbeatUtc = $now }
        }
        $script:LastWatchdogTickUtc = $now
        if (($script:HeartbeatTimeoutSeconds -gt 0) -and ($null -ne $script:LastHeartbeatUtc)) {
            if (($now - $script:LastHeartbeatUtc).TotalSeconds -gt $script:HeartbeatTimeoutSeconds) {
                Log "Kein EntretienConnect-Tab mehr aktiv. Lokaler Helfer beendet sich automatisch."
                break
            }
        } elseif (($script:StartupNoHeartbeatTimeoutSeconds -gt 0) -and (($now - $script:ServerStartedUtc).TotalSeconds -gt $script:StartupNoHeartbeatTimeoutSeconds)) {
            Log "Kein EntretienConnect-Tab gestartet. Lokaler Helfer beendet sich automatisch."
            break
        }
    } catch {}
}
try { $listener.Stop() } catch {}
try { $null = Invoke-EbHelper "shutdown" } catch {}
Remove-EntretienConnectPidFile
Log "EntretienConnect local helper stopped."
