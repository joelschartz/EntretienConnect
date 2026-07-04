#!/bin/bash
# EntretienConnect – Start Mac
# Doppelklick startet den lokalen Helfer und lädt die Oberfläche von GitHub.
cd "$(dirname "$0")"

# macOS may block downloaded scripts with a quarantine flag; remove it for this folder.
xattr -dr com.apple.quarantine . 2>/dev/null || true

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "Python 3 wurde auf diesem Mac nicht gefunden. Installiere Python 3 oder starte die Windows-Version auf einem PC." buttons {"OK"} default button 1 with icon caution' 2>/dev/null || true
  echo "Python 3 wurde nicht gefunden."
  echo "Bitte Python 3 installieren oder die Windows-Version auf einem PC nutzen."
  read -n 1 -s -r -p "Taste drücken zum Schließen..."
  exit 1
fi

cd "_EntretienConnect" || exit 1
export PYTHONDONTWRITEBYTECODE=1
python3 server.py
