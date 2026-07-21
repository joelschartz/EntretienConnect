# EntretienConnect v316

Annule la modification de v315 sur macOS. Elle empêchait la connexion : juste après l’identification, l’application affichait « La session e-Bichelchen a expiré ».

v315 ne laissait plus à la fenêtre native qu’une petite sonde de connexion et lisait ensuite les classes en Python, par HTTP, avec les cookies récupérés. En pratique cette requête aboutissait sur la page d’identification IAM — alors que la même requête venait de réussir quelques secondes plus tôt *dans* la fenêtre. Dans la page, le navigateur envoie la session complète, cookies `HttpOnly` compris ; `WKHTTPCookieStore.getAllCookies` ne les restitue apparemment pas tous, si bien que les cookies exportés ne suffisent pas pour un appel HTTP autonome.

- La lecture de la première classe repasse par la fenêtre de connexion, exactement comme en v314.
- La sonde reste dans le code, désactivée et documentée : quiconque voudra réunifier la lecture devra d’abord résoudre ce problème de cookies.

Le chemin macOS a été comparé fonction par fonction avec v314 : `_mac_wk_read_payload`, `check_login_ready`, `debug_browser_running` et `_mac_wk_launch` sont identiques, seuls des commentaires diffèrent.

À noter : la même limite concerne probablement l’écriture des messages (`direct_ebichelchen_request` utilise les mêmes cookies) et le changement de classe sans nouvelle identification. Ces deux points restent à vérifier sur un compte réel.

Conservé de v314 : suppression d’environ 900 lignes de code inaccessible. De v313 : la fenêtre native fonctionne enfin. De v311/v312 : détection de session expirée, vérification avant publication multiple, blocage des publications simultanées, accès local uniquement.
