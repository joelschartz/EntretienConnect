ObjC.import('Cocoa');
ObjC.import('WebKit');
ObjC.import('Foundation');

/*
 * EntretienConnect v362 – native macOS Microsoft login window.
 *
 * The login stays in a separate WKWebView window. Once the local OAuth
 * callback has finished and displays the success page, this process closes its
 * own window. This avoids the browser restriction that prevents a tab opened
 * through macOS `open` from closing itself with window.close().
 */

let EC_APP = null;
let EC_WINDOW = null;
let EC_WEBVIEW = null;
let EC_TIMER = null;
let EC_BUSY = false;
let EC_FINISHED = false;
const EC_APP_NAME_FOR_UA = 'Version/17.4 Safari/605.1.15';

function jsValue(value) {
  try {
    if (value === undefined || value === null) return null;
    if (value.js !== undefined) return value.js;
  } catch (_) {}
  try { return ObjC.deepUnwrap(value); } catch (_) {}
  try { return ObjC.unwrap(value); } catch (_) {}
  return String(value);
}

function installMenus() {
  try {
    const main = $.NSMenu.alloc.init;
    const appItem = $.NSMenuItem.alloc.init;
    main.addItem(appItem);
    const appMenu = $.NSMenu.alloc.initWithTitle($('EntretienConnect'));
    appMenu.addItemWithTitleActionKeyEquivalent($('Fenster schließen'), 'performClose:', $('w'));
    appMenu.addItemWithTitleActionKeyEquivalent($('Beenden'), 'terminate:', $('q'));
    appItem.setSubmenu(appMenu);

    const editItem = $.NSMenuItem.alloc.init;
    main.addItem(editItem);
    const editMenu = $.NSMenu.alloc.initWithTitle($('Bearbeiten'));
    editMenu.addItemWithTitleActionKeyEquivalent($('Widerrufen'), 'undo:', $('z'));
    editMenu.addItemWithTitleActionKeyEquivalent($('Wiederholen'), 'redo:', $('Z'));
    editMenu.addItem($.NSMenuItem.separatorItem);
    editMenu.addItemWithTitleActionKeyEquivalent($('Ausschneiden'), 'cut:', $('x'));
    editMenu.addItemWithTitleActionKeyEquivalent($('Kopieren'), 'copy:', $('c'));
    editMenu.addItemWithTitleActionKeyEquivalent($('Einfügen'), 'paste:', $('v'));
    editMenu.addItemWithTitleActionKeyEquivalent($('Alles auswählen'), 'selectAll:', $('a'));
    editItem.setSubmenu(editMenu);
    EC_APP.setMainMenu(main);
  } catch (_) {}
}

function finish() {
  if (EC_FINISHED) return;
  EC_FINISHED = true;
  try { if (EC_TIMER) EC_TIMER.invalidate; } catch (_) {}
  try { if (EC_WINDOW) EC_WINDOW.orderOut(null); } catch (_) {}
  $.NSThread.sleepForTimeInterval(0.12);
  try { EC_APP.terminate(null); } catch (_) {}
}

function pollLoginWindow() {
  if (EC_FINISHED || EC_BUSY || !EC_WEBVIEW) return;
  try {
    if (EC_WINDOW && !Boolean(EC_WINDOW.isVisible)) {
      finish();
      return;
    }
  } catch (_) {}

  EC_BUSY = true;
  const script = `JSON.stringify({
    href:String(location.href||''),
    title:String(document.title||''),
    text:String(document.body&&document.body.innerText||'').slice(0,1200)
  })`;
  EC_WEBVIEW.evaluateJavaScriptCompletionHandler($(script), function(result, _) {
    EC_BUSY = false;
    if (EC_FINISHED) return;
    let page = null;
    try { page = JSON.parse(String(jsValue(result) || '{}')); } catch (_) { page = null; }
    if (!page) return;
    const href = String(page.href || '').toLowerCase();
    const text = String(page.text || '');
    const localCallback =
      href.indexOf('http://localhost:') === 0 ||
      href.indexOf('http://127.0.0.1:') === 0;
    if (localCallback && text.indexOf('Connexion réussie') >= 0) {
      finish();
    }
  });
}

function run(argv) {
  try {
    if (!argv || argv.length < 1) throw new Error('Die Microsoft-Anmeldeadresse fehlt.');
    const startUrl = String(argv[0] || '');
    const parsed = $.NSURL.URLWithString($(startUrl));
    if (!parsed) throw new Error('Die Microsoft-Anmeldeadresse ist ungültig.');
    const host = String(ObjC.unwrap(parsed.host) || '').toLowerCase();
    if (
      host !== 'login.microsoftonline.com' &&
      host.indexOf('.login.microsoftonline.com') < 0 &&
      host !== 'login.microsoft.com' &&
      host.indexOf('.login.microsoft.com') < 0
    ) {
      throw new Error('Die Microsoft-Anmeldeadresse ist nicht erlaubt.');
    }

    EC_APP = $.NSApplication.sharedApplication;
    try { $.NSProcessInfo.processInfo.setProcessName($('EntretienConnect')); } catch (_) {}
    EC_APP.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    installMenus();

    const width = 560;
    const height = 760;
    const rect = $.NSMakeRect(0, 0, width, height);
    const style = $.NSWindowStyleMaskTitled |
      $.NSWindowStyleMaskClosable |
      $.NSWindowStyleMaskMiniaturizable |
      $.NSWindowStyleMaskResizable;
    EC_WINDOW = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
      rect,
      style,
      $.NSBackingStoreBuffered,
      false
    );
    EC_WINDOW.setTitle($('Microsoft 365 – EntretienConnect'));
    EC_WINDOW.setReleasedWhenClosed(false);
    EC_WINDOW.setMinSize($.NSMakeSize(480, 640));
    EC_WINDOW.center;

    const config = $.WKWebViewConfiguration.alloc.init;
    config.websiteDataStore = $.WKWebsiteDataStore.defaultDataStore;
    try { config.applicationNameForUserAgent = $(EC_APP_NAME_FOR_UA); } catch (_) {}
    EC_WEBVIEW = $.WKWebView.alloc.initWithFrameConfiguration(rect, config);
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
    EC_WEBVIEW.loadRequest($.NSURLRequest.requestWithURL(parsed));

    EC_TIMER = $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(0.3, true, function(_) {
      pollLoginWindow();
    });
    EC_APP.run;
  } catch (error) {
    try {
      const alert = $.NSAlert.alloc.init;
      alert.setMessageText($('Microsoft-Anmeldung konnte nicht geöffnet werden.'));
      alert.setInformativeText($(String(error && (error.message || error) || error)));
      alert.addButtonWithTitle($('OK'));
      alert.runModal;
    } catch (_) {}
    finish();
  }
}
