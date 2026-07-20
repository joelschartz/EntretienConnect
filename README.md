# EntretienConnect v308

- Die Haupt-App bleibt im Standardbrowser.
- Auf macOS verwendet das e-Bichelchen-Login ein natives WKWebView-Fenster (Apple WebKit), ohne Chrome-/Edge-/Firefox-Abhängigkeit.
- Nach erfolgreichem Einlesen schließt sich das Loginfenster automatisch.
- Die Klassenauswahl findet bei mehreren Klassen ausschließlich in EntretienConnect statt.
- Windows verwendet in v307 weiterhin den bisherigen Chrome-/Edge-Helfer; die WebView2-Umstellung folgt separat.


## v308
- Behebt die fälschliche Meldung, das interne WKWebView-Modul fehle.
- Das Modul wird über mehrere Runtime-Pfade aufgelöst und ist als selbstreparierende Fallback-Kopie im Helfer eingebettet.
