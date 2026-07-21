# EntretienConnect v320

Deux mesures contre l’alerte « Es konnte keine Verbindung zum Server erstellt werden » affichée par e-Bichelchen dans la fenêtre de connexion.

**1. User-Agent complet.** La fenêtre s’annonçait comme
`Mozilla/5.0 (Macintosh; …) AppleWebKit/605.1.15 (KHTML, like Gecko)` — sans `Version/… Safari/…`. Pour e-Bichelchen, ou pour une protection placée devant, ce n’est pas un navigateur reconnaissable. La fenêtre envoie désormais un User-Agent Safari complet (`applicationNameForUserAgent`, avec `setCustomUserAgent` en second recours). Mesuré sur banc d’essai, en-tête reçu par le serveur et `navigator.userAgent` dans la page :
`Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15`

**2. Solution de secours.** Le choix du navigateur est de nouveau visible sur macOS, et surtout il agit. « Fenêtre native » (par défaut) ouvre la fenêtre intégrée comme avant ; « Google Chrome » ou « Microsoft Edge » repasse par la voie Chromium, complète et éprouvée. À utiliser si e-Bichelchen refuse de fonctionner dans la fenêtre intégrée. Jusqu’ici cette liste était masquée sur macOS parce qu’elle n’avait aucun effet.

Vérifié : la page d’identification ne reçoit toujours aucune requête avant la connexion (acquis de v317), la lecture après identification fonctionne, et l’aiguillage `auto` → fenêtre native / `chrome`,`edge` → voie Chromium a été testé.

Honnêtement : je ne peux pas prouver que le User-Agent était la cause, faute d’accès au vrai serveur. Si l’alerte persiste, choisissez « Google Chrome » dans la liste — c’est précisément à cela que sert la solution de secours.

Rappel : v317 a supprimé les requêtes lancées pendant l’identification, v316 a annulé la modification de v315 (les cookies exportés ne suffisent pas pour un appel HTTP autonome), v314 a retiré ~900 lignes de code inaccessible, v313 a réparé la fenêtre native.
