ObjC.import('Cocoa');
ObjC.import('WebKit');
ObjC.import('Foundation');

/*
 * EntretienConnect v332 – native macOS e-Bichelchen login window.
 * Runs through /usr/bin/osascript -l JavaScript and uses WKWebView (Safari/WebKit),
 * so no Chrome, Edge or remotely controlled Firefox is required.
 */

let EC_APP = null;
let EC_WINDOW = null;
let EC_WEBVIEW = null;
let EC_TIMER = null;
let EC_BUSY = false;
let EC_FINISHED = false;
let EC_STATE_PATH = '';
let EC_READ_EXPRESSION = '';
let EC_START_URL = '';
let EC_RESTORE_COOKIES_PATH = '';
let EC_STARTED_AT = Date.now();
// v320: Safari-Kennung für den User-Agent des Loginfensters.
const EC_APP_NAME_FOR_UA = 'Version/17.4 Safari/605.1.15';

// v325: Eigene Menüleiste. Ohne sie gibt es keine Tastenkürzel – und ⌘V braucht
// man im Loginfenster, wenn das Passwort aus einem Passwortmanager kommt. Das
// WKWebView ist erster Responder und behandelt paste:/copy:/cut: selbst; die
// Menüpunkte reichen die Kürzel nur an die Responder-Kette weiter.
function installEditMenu() {
  try {
    const main = $.NSMenu.alloc.init;
    const appItem = $.NSMenuItem.alloc.init; main.addItem(appItem);
    const appMenu = $.NSMenu.alloc.initWithTitle($('EntretienConnect'));
    appMenu.addItemWithTitleActionKeyEquivalent($('Fenster schließen'), 'performClose:', $('w'));
    appMenu.addItemWithTitleActionKeyEquivalent($('Beenden'), 'terminate:', $('q'));
    appItem.setSubmenu(appMenu);
    const editItem = $.NSMenuItem.alloc.init; main.addItem(editItem);
    const edit = $.NSMenu.alloc.initWithTitle($('Bearbeiten'));
    edit.addItemWithTitleActionKeyEquivalent($('Annuler'), 'undo:', $('z'));
    edit.addItemWithTitleActionKeyEquivalent($('Ausschneiden'), 'cut:', $('x'));
    edit.addItemWithTitleActionKeyEquivalent($('Kopieren'), 'copy:', $('c'));
    edit.addItemWithTitleActionKeyEquivalent($('Einfügen'), 'paste:', $('v'));
    edit.addItemWithTitleActionKeyEquivalent($('Alles auswählen'), 'selectAll:', $('a'));
    editItem.setSubmenu(edit);
    $.NSApplication.sharedApplication.setMainMenu(main);
  } catch (_) {}
}

// v325: Eine noch gültige e-Bichelchen-Sitzung aus einem früheren App-Start wird
// zurückgeschrieben, BEVOR die Seite geladen wird. War die IAM-Sitzung nicht
// abgelaufen, landet der Benutzer direkt angemeldet. Das Sitzungs-Cookie lebt
// sonst nur im Speicher dieses Prozesses und ist bei jedem Neustart weg – das war
// der Grund, warum bisher jedes Mal eine neue Anmeldung nötig war.
function injectCookiesThenLoad(webview, url, cookiesPath) {
  let arr = [];
  if (cookiesPath) {
    try {
      const raw = $.NSString.stringWithContentsOfFileEncodingError($(cookiesPath), $.NSUTF8StringEncoding, Ref());
      const txt = String(jsValue(raw) || '');
      if (txt) arr = JSON.parse(txt);
    } catch (_) { arr = []; }
  }
  const request = $.NSURLRequest.requestWithURL(url);
  if (!Array.isArray(arr) || !arr.length) { webview.loadRequest(request); return; }
  let store = null;
  try { store = webview.configuration.websiteDataStore.httpCookieStore; } catch (_) { store = null; }
  if (!store || typeof store.setCookieCompletionHandler !== 'function') { webview.loadRequest(request); return; }
  let remaining = arr.length;
  let loaded = false;
  const loadOnce = () => { if (!loaded) { loaded = true; try { webview.loadRequest(request); } catch (_) {} } };
  const done = () => { remaining -= 1; if (remaining <= 0) loadOnce(); };
  // Sicherheitsnetz: falls ein Completion-Handler ausbleibt, wird trotzdem geladen.
  $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(2.0, false, function(_) { loadOnce(); });
  arr.forEach(function(c) {
    try {
      const props = $.NSMutableDictionary.alloc.init;
      props.setObjectForKey($(String(c.name || '')), $.NSHTTPCookieName);
      props.setObjectForKey($(String(c.value || '')), $.NSHTTPCookieValue);
      props.setObjectForKey($(String(c.domain || 'ssl.education.lu')), $.NSHTTPCookieDomain);
      props.setObjectForKey($(String(c.path || '/')), $.NSHTTPCookiePath);
      if (c.secure) props.setObjectForKey($('TRUE'), $.NSHTTPCookieSecure);
      const cookie = $.NSHTTPCookie.cookieWithProperties(props);
      if (cookie && !cookie.isNil()) { store.setCookieCompletionHandler(cookie, function() { done(); }); }
      else { done(); }
    } catch (_) { done(); }
  });
}

function jsValue(value) {
  try {
    if (value === undefined || value === null) return null;
    if (value.js !== undefined) return value.js;
  } catch (_) {}
  try { return ObjC.deepUnwrap(value); } catch (_) {}
  try { return ObjC.unwrap(value); } catch (_) {}
  return String(value);
}

// v313: Returns a usable message for a REAL error, and '' for an ObjC nil that
// JXA hands over as a truthy wrapper (its String() form is "[id nil]").
function errorText(err) {
  if (!err) return '';
  let desc = null;
  try { desc = jsValue(err.localizedDescription); } catch (_) { desc = null; }
  if (desc !== null && desc !== undefined) {
    const text = String(desc).trim();
    return (!text || text === 'null' || text === 'undefined') ? '' : text;
  }
  let raw = '';
  try { raw = String(err).trim(); } catch (_) { raw = ''; }
  if (!raw || raw === '[id nil]' || raw === 'null' || raw === 'undefined') return '';
  return raw;
}

function writeState(obj) {
  try {
    obj = obj || {};
    if (!obj.updatedAt) obj.updatedAt = new Date().toISOString();
    const text = $(JSON.stringify(obj));
    const err = Ref();
    const ok = text.writeToFileAtomicallyEncodingError($(EC_STATE_PATH), true, $.NSUTF8StringEncoding, err);
    return Boolean(ok);
  } catch (_) {
    return false;
  }
}

function finishWithError(message, detail) {
  if (EC_FINISHED) return;
  EC_FINISHED = true;
  writeState({
    status: 'error',
    error: String(message || 'WKWebView error'),
    detail: String(detail || ''),
    startedAt: new Date(EC_STARTED_AT).toISOString()
  });
  try { if (EC_TIMER) EC_TIMER.invalidate; } catch (_) {}
  try { if (EC_WINDOW) EC_WINDOW.orderOut(null); } catch (_) {}
  $.NSThread.sleepForTimeInterval(0.12);
  try { EC_APP.terminate(null); } catch (_) {}
}

function educationCookies(cookies) {
  const out = [];
  try {
    const count = Number(cookies.count || 0);
    for (let i = 0; i < count; i++) {
      const c = cookies.objectAtIndex(i);
      const domain = String(jsValue(c.domain) || '');
      const name = String(jsValue(c.name) || '');
      if (!name || domain.toLowerCase().indexOf('education.lu') < 0) continue;
      out.push({
        name: name,
        value: String(jsValue(c.value) || ''),
        domain: domain,
        path: String(jsValue(c.path) || '/'),
        secure: Boolean(jsValue(c.secure)),
        httpOnly: Boolean(jsValue(c.HTTPOnly))
      });
    }
  } catch (_) {}
  return out;
}

function finalizePayload(payload, pageUrl) {
  if (EC_FINISHED) return;
  EC_FINISHED = true;

  // v313: The WKHTTPCookieStore selector is `getAllCookies:`, so the JXA name is
  // `getAllCookies`. The previous `getAllCookiesWithCompletionHandler` did not
  // exist and raised an uncaught NSException that killed the whole login window.
  // It never surfaced because the nil-error bug above meant this line was never
  // reached. Kept behind a try/catch so a future API change degrades into a
  // readable error state instead of a silent crash.
  let store = null;
  try {
    store = EC_WEBVIEW.configuration.websiteDataStore.httpCookieStore;
  } catch (e) {
    finishWithError('The e-Bichelchen session could not be read.', String(e));
    return;
  }
  if (!store || typeof store.getAllCookies !== 'function') {
    finishWithError('The e-Bichelchen session could not be read.', 'WKHTTPCookieStore.getAllCookies unavailable');
    return;
  }
  store.getAllCookies(function(cookies) {
    let cookieRows = educationCookies(cookies);
    EC_WEBVIEW.evaluateJavaScriptCompletionHandler($('navigator.userAgent || "Mozilla/5.0"'), function(uaValue, uaError) {
      const ua = String(jsValue(uaValue) || 'Mozilla/5.0 (Macintosh) AppleWebKit');
      const merged = {};
      cookieRows.forEach(function(c) { merged[c.name] = c.value; });
      const cookieHeader = Object.keys(merged).map(function(k) { return k + '=' + merged[k]; }).join('; ');
      if (!cookieHeader) {
        EC_FINISHED = false;
        writeState({
          status: 'waiting',
          stage: 'session',
          message: 'e-Bichelchen is open, but the authenticated session is not available yet.',
          pageUrl: String(pageUrl || '')
        });
        return;
      }
      try { if (EC_TIMER) EC_TIMER.invalidate; } catch (_) {}
      writeState({
        status: 'ready',
        pageUrl: String(pageUrl || ''),
        data: payload,
        session: {
          cookieHeader: cookieHeader,
          cookieNames: Object.keys(merged).sort(),
          cookies: cookieRows,
          userAgent: ua,
          capturedAt: new Date().toISOString(),
          targetUrl: String(pageUrl || ''),
          browser: 'macOS WKWebView v332'
        },
        engine: 'WKWebView-v332',
        startedAt: new Date(EC_STARTED_AT).toISOString()
      });
      try { EC_WINDOW.orderOut(null); } catch (_) {}
      $.NSThread.sleepForTimeInterval(0.18);
      try { EC_APP.terminate(null); } catch (_) {}
    });
  });
}

function buildControllerScript() {
  // The read expression is an async IIFE. We launch it once and store its result
  // in a page-global object. evaluateJavaScript itself only returns a synchronous
  // status snapshot, which works on older WKWebView versions as well.
  return `(() => {
    const href = String(location.href || '');
    const onEb = href.indexOf('/ebichelchen/app/') >= 0;
    if (!onEb) return JSON.stringify({phase:'login',url:href});
    if (!window.__entretienConnectNative332) {
      window.__entretienConnectNative332 = {phase:'starting',url:href,error:'',data:null,startedAt:Date.now()};
      const s = window.__entretienConnectNative332;
      s.phase = 'reading';
      Promise.resolve(${EC_READ_EXPRESSION})
        .then(v => { s.data = v; s.phase = 'ready'; s.url = String(location.href || href); })
        .catch(e => {
          s.error = String(e && (e.message || e) || 'unknown error');
          s.phase = 'waiting';
          s.url = String(location.href || href);
          setTimeout(() => { try { delete window.__entretienConnectNative332; } catch (_) {} }, 1200);
        });
    }
    const s = window.__entretienConnectNative332;
    return JSON.stringify({phase:s.phase,url:String(s.url||href),error:String(s.error||''),data:s.data||null,age:Date.now()-Number(s.startedAt||Date.now())});
  })()`;
}

function pollWebView() {
  if (EC_FINISHED || EC_BUSY || !EC_WEBVIEW) return;
  try {
    if (EC_WINDOW && !Boolean(EC_WINDOW.isVisible)) {
      EC_FINISHED = true;
      writeState({status:'closed', message:'Login window closed by user.'});
      try { EC_APP.terminate(null); } catch (_) {}
      return;
    }
  } catch (_) {}

  EC_BUSY = true;
  const script = buildControllerScript();
  EC_WEBVIEW.evaluateJavaScriptCompletionHandler($(script), function(result, error) {
    EC_BUSY = false;
    if (EC_FINISHED) return;
    // v313: A nil NSError arrives in JXA as a TRUTHY wrapper object, so the old
    // `if (error)` took the failure branch on every single successful call and
    // the result was never parsed — the login window stayed open forever and the
    // state file was stuck on stage "navigation" / detail "[id nil]".
    // The result now decides; the error object is only used for its message.
    let outer = null;
    try { outer = JSON.parse(String(jsValue(result) || '{}')); } catch (_) { outer = null; }
    if (!outer || !outer.phase) {
      const detail = errorText(error);
      if (detail) writeState({status:'waiting', stage:'navigation', detail:detail, pageUrl:''});
      return;
    }
    const phase = String(outer.phase || 'waiting');
    const pageUrl = String(outer.url || '');
    if (phase === 'login') {
      writeState({status:'open', stage:'login', pageUrl:pageUrl, engine:'WKWebView'});
      return;
    }
    if (phase === 'reading' || phase === 'starting') {
      writeState({status:'open', stage:'reading', pageUrl:pageUrl, engine:'WKWebView'});
      return;
    }
    if (phase === 'waiting') {
      writeState({status:'open', stage:'loading', pageUrl:pageUrl, detail:String(outer.error || ''), engine:'WKWebView'});
      return;
    }
    if (phase === 'ready' && outer.data) {
      let payload = null;
      try {
        payload = typeof outer.data === 'string' ? JSON.parse(outer.data) : outer.data;
      } catch (e) {
        finishWithError('The e-Bichelchen data could not be decoded.', String(e));
        return;
      }
      finalizePayload(payload, pageUrl);
    }
  });
}

function run(argv) {
  try {
    if (!argv || argv.length < 3) throw new Error('Missing native login arguments.');
    EC_STATE_PATH = String(argv[0]);
    const expressionPath = String(argv[1]);
    EC_START_URL = String(argv[2]);
    // v325: Optionaler 4. Parameter – Pfad zu den zurückzuschreibenden Cookies.
    EC_RESTORE_COOKIES_PATH = (argv.length > 3) ? String(argv[3]) : '';
    const readErr = Ref();
    const readObj = $.NSString.stringWithContentsOfFileEncodingError($(expressionPath), $.NSUTF8StringEncoding, readErr);
    EC_READ_EXPRESSION = String(jsValue(readObj) || '');
    if (!EC_READ_EXPRESSION) throw new Error('The e-Bichelchen read script is empty.');

    writeState({status:'starting', engine:'WKWebView', startedAt:new Date(EC_STARTED_AT).toISOString()});

    EC_APP = $.NSApplication.sharedApplication;
    // v325: Als Zubehör statt als vollwertiges Programm anmelden. Damit
    // verschwindet das zusätzliche Dock-Symbol; das Fenster bleibt voll bedienbar
    // (gemessen: Key-Window, Eingabefokus, ⌘V funktionieren). Der Prozessname wird
    // als Beste-Bemühung gesetzt – den fettgedruckten Menüleistennamen bestimmt
    // aber das laufende Programm (osascript), das lässt sich hier nicht umbenennen.
    try { $.NSProcessInfo.processInfo.setProcessName($('EntretienConnect')); } catch (_) {}
    EC_APP.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    installEditMenu();

    const rect = $.NSMakeRect(0, 0, 1080, 760);
    const style = $.NSWindowStyleMaskTitled | $.NSWindowStyleMaskClosable | $.NSWindowStyleMaskMiniaturizable | $.NSWindowStyleMaskResizable;
    EC_WINDOW = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(rect, style, $.NSBackingStoreBuffered, false);
    EC_WINDOW.setTitle($('e-Bichelchen – EntretienConnect'));
    EC_WINDOW.setReleasedWhenClosed(false);
    EC_WINDOW.center;

    const config = $.WKWebViewConfiguration.alloc.init;
    config.websiteDataStore = $.WKWebsiteDataStore.defaultDataStore;
    // v320: Ohne diese Zeile meldet sich das Fenster als
    //   "Mozilla/5.0 (Macintosh; …) AppleWebKit/605.1.15 (KHTML, like Gecko)"
    // – ohne "Version/… Safari/…". Für e-Bichelchen (bzw. eine vorgelagerte
    // Schutzschicht) ist das kein erkennbarer Browser; die Seite meldete
    // daraufhin "Es konnte keine Verbindung zum Server erstellt werden".
    // applicationNameForUserAgent wird von WebKit an die echte Engine-Kennung
    // angehängt, das Ergebnis ist ein vollwertiger Safari-User-Agent.
    try {
      config.applicationNameForUserAgent = $(EC_APP_NAME_FOR_UA);
    } catch (_) {}
    EC_WEBVIEW = $.WKWebView.alloc.initWithFrameConfiguration(rect, config);
    // Zweiter Weg, falls applicationNameForUserAgent nicht greift.
    try {
      const ua = String(jsValue(EC_WEBVIEW.valueForKey($('userAgent'))) || '');
      if (ua && ua.indexOf('Safari/') < 0) {
        EC_WEBVIEW.setCustomUserAgent($(ua.trim() + ' ' + EC_APP_NAME_FOR_UA));
      }
    } catch (_) {}
    EC_WEBVIEW.setAllowsBackForwardNavigationGestures(true);
    EC_WINDOW.setContentView(EC_WEBVIEW);
    EC_WINDOW.makeKeyAndOrderFront(null);
    EC_APP.activateIgnoringOtherApps(true);

    const url = $.NSURL.URLWithString($(EC_START_URL));
    if (!url) throw new Error('Invalid e-Bichelchen URL.');
    injectCookiesThenLoad(EC_WEBVIEW, url, EC_RESTORE_COOKIES_PATH);

    EC_TIMER = $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(0.8, true, function(_) { pollWebView(); });
    writeState({status:'open', stage:'login', engine:'WKWebView', pageUrl:EC_START_URL});
    EC_APP.run;
  } catch (e) {
    finishWithError('The macOS login window could not be started.', String(e && (e.message || e) || e));
  }
}
