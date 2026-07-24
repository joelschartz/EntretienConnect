/*
 * EntretienConnect v361 – native macOS dialogs and file selection.
 *
 * This script is invoked by the local helper. It gives the WKWebView app shell
 * the same confirmations, prompts, alerts and file picker behavior that Safari
 * supplies automatically.
 */

function result(value) {
  return JSON.stringify(value || {});
}

function run(argv) {
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;

  const action = String((argv && argv[0]) || '');
  const message = String((argv && argv[1]) || '');
  const defaultValue = String((argv && argv[2]) || '');

  try {
    if (action === 'alert') {
      app.displayDialog(message, {
        buttons: ['OK'],
        defaultButton: 'OK',
        withIcon: 'note'
      });
      return result({ ok: true, value: true });
    }

    if (action === 'confirm') {
      app.displayDialog(message, {
        buttons: ['Annuler', 'OK'],
        defaultButton: 'OK',
        cancelButton: 'Annuler',
        withIcon: 'caution'
      });
      return result({ ok: true, value: true });
    }

    if (action === 'prompt') {
      const response = app.displayDialog(message, {
        defaultAnswer: defaultValue,
        buttons: ['Annuler', 'OK'],
        defaultButton: 'OK',
        cancelButton: 'Annuler'
      });
      return result({ ok: true, value: String(response.textReturned || '') });
    }

    if (action === 'choose-csv') {
      const file = app.chooseFile({
        withPrompt: 'Choisir le fichier CSV à importer',
        ofType: ['public.comma-separated-values-text', 'public.delimited-values-text', 'public.text']
      });
      return result({ ok: true, path: String(file) });
    }

    if (action === 'choose-pdf') {
      const file = app.chooseFile({
        withPrompt: 'Choisir le fichier PDF',
        ofType: ['com.adobe.pdf']
      });
      return result({ ok: true, path: String(file) });
    }

    if (action === 'choose-image') {
      const file = app.chooseFile({
        withPrompt: 'Choisir une image',
        ofType: ['public.image']
      });
      return result({ ok: true, path: String(file) });
    }

    return result({ ok: false, error: 'Action native inconnue.' });
  } catch (error) {
    const number = Number(error && error.errorNumber);
    if (number === -128) return result({ ok: true, cancelled: true });
    return result({
      ok: false,
      error: String(error && (error.message || error) || 'Erreur macOS')
    });
  }
}
