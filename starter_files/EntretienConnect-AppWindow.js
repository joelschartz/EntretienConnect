ObjC.import('Cocoa');
ObjC.import('WebKit');
ObjC.import('Foundation');

/*
 * EntretienConnect v362 – native macOS main window.
 *
 * This script is launched by EntretienConnect_MAC.app through the system JXA
 * runtime. It embeds the local EntretienConnect interface in WKWebView without
 * opening Safari and without installing any additional component.
 */

let EC_APP = null;
let EC_WINDOW = null;
let EC_WEBVIEW = null;
let EC_TIMER = null;
let EC_BASE_URL = '';
let EC_CLOSING = false;

function installMenus() {
  try {
    const main = $.NSMenu.alloc.init;

    const appItem = $.NSMenuItem.alloc.init;
    main.addItem(appItem);
    const appMenu = $.NSMenu.alloc.initWithTitle($('EntretienConnect'));
    appMenu.addItemWithTitleActionKeyEquivalent($('EntretienConnect beenden'), 'terminate:', $('q'));
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

    const windowItem = $.NSMenuItem.alloc.init;
    main.addItem(windowItem);
    const windowMenu = $.NSMenu.alloc.initWithTitle($('Fenster'));
    windowMenu.addItemWithTitleActionKeyEquivalent($('Minimieren'), 'performMiniaturize:', $('m'));
    windowMenu.addItemWithTitleActionKeyEquivalent($('Schließen'), 'performClose:', $('w'));
    windowItem.setSubmenu(windowMenu);

    EC_APP.setMainMenu(main);
  } catch (_) {}
}

function postShutdown() {
  if (!EC_BASE_URL) return;
  try {
    const url = $.NSURL.URLWithString($(EC_BASE_URL + '/api/app/shutdown'));
    if (!url) return;
    const request = $.NSMutableURLRequest.requestWithURL(url);
    request.setHTTPMethod($('POST'));
    request.setTimeoutInterval(1.0);
    const response = Ref();
    const error = Ref();
    $.NSURLConnection.sendSynchronousRequestReturningResponseError(request, response, error);
  } catch (_) {}
}

function closeApp() {
  if (EC_CLOSING) return;
  EC_CLOSING = true;
  try { if (EC_TIMER) EC_TIMER.invalidate; } catch (_) {}
  postShutdown();
  try { EC_APP.terminate(null); } catch (_) {}
}

function showFailure(message) {
  try {
    const alert = $.NSAlert.alloc.init;
    alert.setMessageText($('EntretienConnect konnte nicht geöffnet werden.'));
    alert.setInformativeText($(String(message || 'Unbekannter Fehler')));
    alert.addButtonWithTitle($('OK'));
    alert.runModal;
  } catch (_) {}
  closeApp();
}

function run(argv) {
  try {
    if (!argv || argv.length < 1) throw new Error('Die lokale App-Adresse fehlt.');
    const startUrl = String(argv[0] || '');
    const parsed = $.NSURL.URLWithString($(startUrl));
    if (!parsed) throw new Error('Die lokale App-Adresse ist ungültig.');

    const scheme = String(ObjC.unwrap(parsed.scheme) || '').toLowerCase();
    const host = String(ObjC.unwrap(parsed.host) || '').toLowerCase();
    if (scheme !== 'http' || (host !== '127.0.0.1' && host !== 'localhost')) {
      throw new Error('Aus Sicherheitsgründen darf das App-Fenster nur die lokale EntretienConnect-Adresse öffnen.');
    }
    const port = Number(ObjC.unwrap(parsed.port) || 8765);
    EC_BASE_URL = 'http://127.0.0.1:' + String(port);

    EC_APP = $.NSApplication.sharedApplication;
    try { $.NSProcessInfo.processInfo.setProcessName($('EntretienConnect')); } catch (_) {}
    EC_APP.setActivationPolicy($.NSApplicationActivationPolicyRegular);
    installMenus();

    let width = 1200;
    let height = 780;
    let left = 80;
    let bottom = 80;
    try {
      const visible = $.NSScreen.mainScreen.visibleFrame;
      const screenWidth = Number(visible.size.width);
      const screenHeight = Number(visible.size.height);
      // v360: ähnlich GrilleÉval deutlich bildschirmfüllender öffnen. Die
      // verfügbaren macOS-Ränder (Menüleiste/Dock) werden weiterhin respektiert.
      width = Math.min(Math.max(Math.round(screenWidth * 0.90), 1200), screenWidth - 24);
      height = Math.min(Math.max(Math.round(screenHeight * 0.90), 780), screenHeight - 24);
      left = Number(visible.origin.x) + Math.max(0, Math.round((screenWidth - width) / 2));
      bottom = Number(visible.origin.y) + Math.max(0, Math.round((screenHeight - height) / 2));
    } catch (_) {}

    const rect = $.NSMakeRect(left, bottom, width, height);
    const style = $.NSWindowStyleMaskTitled |
      $.NSWindowStyleMaskClosable |
      $.NSWindowStyleMaskMiniaturizable |
      $.NSWindowStyleMaskResizable |
      $.NSWindowStyleMaskFullSizeContentView;
    EC_WINDOW = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
      rect,
      style,
      $.NSBackingStoreBuffered,
      false
    );
    EC_WINDOW.setTitle($('EntretienConnect'));
    // v362: Die macOS-Fensterknöpfe bleiben erhalten, Titeltext und abgesetzter
    // heller Balken verschwinden. Der Webinhalt läuft wie bei GrilleÉval bis
    // unter die transparente Titelleiste.
    try { EC_WINDOW.setTitleVisibility($.NSWindowTitleHidden); } catch (_) {}
    try { EC_WINDOW.setTitlebarAppearsTransparent(true); } catch (_) {}
    try { EC_WINDOW.setTitlebarSeparatorStyle($.NSTitlebarSeparatorStyleNone); } catch (_) {}
    EC_WINDOW.setReleasedWhenClosed(false);
    EC_WINDOW.setMinSize($.NSMakeSize(980, 650));

    const config = $.WKWebViewConfiguration.alloc.init;
    config.websiteDataStore = $.WKWebsiteDataStore.defaultDataStore;
    EC_WEBVIEW = $.WKWebView.alloc.initWithFrameConfiguration(
      $.NSMakeRect(0, 0, width, height),
      config
    );
    EC_WEBVIEW.setAllowsBackForwardNavigationGestures(false);
    EC_WINDOW.setContentView(EC_WEBVIEW);
    EC_WINDOW.makeKeyAndOrderFront(null);
    EC_APP.activateIgnoringOtherApps(true);

    EC_WEBVIEW.loadRequest($.NSURLRequest.requestWithURL(parsed));

    EC_TIMER = $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(0.4, true, function(_) {
      try {
        if (EC_WINDOW && !Boolean(EC_WINDOW.isVisible)) closeApp();
      } catch (_) {}
    });

    EC_APP.run;
  } catch (error) {
    showFailure(String(error && (error.message || error) || error));
  }
}
