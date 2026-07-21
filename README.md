# EntretienConnect v314

Nettoyage sans changement de comportement. Seul du code devenu inaccessible a été retiré, environ 900 lignes.

- Suppression complète de la couche Firefox WebDriver BiDi (Firefox piloté à distance) : la classe de contrôle, la couche WebSocket dédiée, la création d’un profil Firefox et la lecture de session par cette voie. Son point d’entrée n’était plus appelé depuis v306.
- Suppression de la couche intermédiaire devenue inutile, qui ne faisait que transmettre chaque appel. Il reste deux couches au lieu de trois : la voie Chromium/CDP (Windows) et la fenêtre native (macOS).
- Suppression de quelques fonctions sans appelant (préchauffage du navigateur, ouverture d’onglet à distance, deux aides JSON inutilisées).

La voie Windows a été comparée fonction par fonction avec l’état précédent : les neuf points d’entrée sont restés identiques au caractère près. Volontairement conservés : la voie « Firefox actuel » (toujours sélectionnable sous Windows) et les outils de création d’entrées de test.

Rappel des correctifs précédents :

- v313 — la fenêtre de connexion native fonctionne enfin : une erreur ObjC absente (`nil`) arrive en JXA sous forme d’objet non vide, si bien que `if (error)` prenait la branche d’échec à chaque appel réussi ; et la lecture des cookies appelait une méthode inexistante (`getAllCookies:` est le bon sélecteur).
- v311 / v312 — détection de session expirée, vérification unique avant publication multiple, blocage d’une publication simultanée, fichier de transfert en droits 0600 supprimé dès la reprise en mémoire, mémorisation de l’adresse des matières, choix du navigateur masqué sur macOS, accès local uniquement (en-tête Host).
