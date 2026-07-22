# EntretienConnect v333

Sous Windows, la session e-Bichelchen est désormais mémorisée elle aussi : après un premier login, les démarrages suivants se reconnectent en silence, sans ouvrir de navigateur.

Jusqu’ici, tout ce qui a été construit depuis v325 ne valait que pour la fenêtre native de macOS. Sous Windows, e-Bichelchen repassait donc au gris à chaque redémarrage. La session capturée sur ce chemin est maintenant enregistrée de la même manière (fichier lisible par vous seul), et la reprise silencieuse fonctionne sur les deux systèmes — elle repose sur de simples requêtes HTTP, indépendantes du système.

Une correction de fond au passage : les cookies capturés sous Windows étaient rassemblés en une seule ligne, où le dernier cookie d’un même nom écrasait les précédents. Or trois noms se répètent sur des serveurs différents avec des valeurs différentes. Les cookies sont désormais conservés individuellement avec leur domaine, et seuls ceux valables pour le serveur interrogé sont envoyés. Cela concerne aussi la publication des messages, qui empruntait la même ligne rassemblée.

**Ce qui n’a pas changé, et pourquoi.** Sous Windows, la connexion n’ouvre pas une petite fenêtre comme sur macOS : l’assistant lance une instance séparée de Chrome ou Edge, avec son propre profil. D’où une fenêtre de navigateur avec un onglet vide en plus, et l’application qui passe à l’arrière-plan. Ce n’est pas voulu, mais je ne peux pas tester Windows d’ici, et je préfère ne pas modifier à l’aveugle un chemin qui fonctionne. En pratique la gêne disparaît largement : après le premier login, plus aucun navigateur ne s’ouvre aux démarrages suivants.

**Au premier lancement après la mise à jour**, la connexion s’ouvrira encore une fois — la session déjà enregistrée ne contient pas les informations nouvellement mémorisées. À partir du deuxième démarrage, tout se fait en silence.

Rappel v332 : sur macOS, reprise sans aucune fenêtre (67 ms mesurées) et correction de la détection de session expirée, qui prenait toute page HTML — y compris un simple 404 — pour une déconnexion.
