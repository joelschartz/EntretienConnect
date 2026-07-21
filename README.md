# EntretienConnect v313

Répare la fenêtre de connexion native sur macOS. Elle restait ouverte après l’identification et l’application restait sur « Connexion en cours… » — en réalité depuis v310, jamais seulement depuis v311.

Deux erreurs dans le module natif, la première masquant la seconde :

- Une erreur ObjC absente (`nil`) arrive en JXA sous la forme d’un objet non vide. Le test `if (error)` prenait donc la branche d’échec à **chaque** appel réussi, le résultat n’était jamais lu et la phase n’atteignait jamais « ready ». C’est le fichier d’état bloqué sur `stage: navigation` / `detail: [id nil]`. Désormais c’est le résultat qui décide ; l’objet d’erreur ne sert plus qu’à produire un message.
- La lecture des cookies appelait `getAllCookiesWithCompletionHandler`, qui n’existe pas ; le sélecteur de `WKHTTPCookieStore` est `getAllCookies:`. L’appel levait une exception non rattrapée qui terminait la fenêtre de connexion. Corrigé, et protégé pour qu’une évolution de l’API donne un message lisible au lieu d’un arrêt silencieux.

Repris de v311 / v312 :

- Détecte une session e-Bichelchen expirée : l’interface propose immédiatement « Connecter e-Bichelchen ».
- Vérifie la session une seule fois avant une publication multiple, et bloque une deuxième publication simultanée.
- Fichier de transfert de session en droits 0600, supprimé dès que la session est en mémoire et au démarrage après un arrêt brutal.
- Mémorise l’adresse des matières qui a fonctionné : une requête au lieu de neuf.
- Masque le choix du navigateur sur macOS et corrige l’infobulle devenue fausse depuis v310.
- N’accepte plus que les accès locaux sur l’assistant local (contrôle de l’en-tête Host).
- Trace console `[eb] login-ready: …` indiquant l’étape exacte de la fenêtre native.
