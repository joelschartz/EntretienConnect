# EntretienConnect v335

Les deux désagréments signalés sous Windows.

**L’onglet vide dans la fenêtre de connexion.** Sous Windows, la connexion était lancée avec `--new-window`. Au démarrage à froid, Chromium ouvre alors d’abord sa propre fenêtre de départ (page vide / nouvel onglet), *puis* la page de connexion — d’où les deux onglets. On utilise désormais `--app=` : exactement une fenêtre, sans barre d’onglets ni barre d’adresse, avec une taille et une position fixes. C’est ce que fait le chemin macOS depuis toujours, raison pour laquelle le problème n’y est jamais apparu.

**L’application qui « se minimise » après la connexion.** Une fois la connexion terminée, EntretienConnect ramène le focus et appelait à cette occasion `ShowWindow(SW_RESTORE)` sur la fenêtre de l’application. Or, sur une fenêtre **maximisée**, SW_RESTORE rétablit la taille précédente, plus petite : la fenêtre rétrécissait donc à chaque fois. L’appel n’a désormais lieu que si la fenêtre est réellement réduite. Corrigé aux trois endroits concernés : serveur PowerShell, assistant e-Bichelchen et chemin Python.

**L’onglet vide au démarrage de l’application est autre chose.** Il n’est pas ouvert par EntretienConnect : l’application se contente de confier l’adresse au navigateur par défaut, et celui-ci affiche en plus sa propre page de démarrage lorsqu’il démarre à froid. Vérification rapide : lancez Edge ou Chrome seul depuis la barre des tâches, sans EntretienConnect. Si l’onglet vide apparaît aussi, il s’agit du réglage du navigateur (Edge : Paramètres > Démarrage, page d’accueil et nouveaux onglets).

**Correction importante concernant v333.** J’avais écrit que la session mémorisée valait aussi sous Windows à partir de v333. C’est faux. Sous Windows, c’est `EntretienConnect.ps1` — une implémentation PowerShell distincte — qui s’exécute, et non `server.py`/`ebichelchen.py`. Or la reprise de session (v330–v333) se trouve dans le chemin Python et n’est donc jamais exécutée sous Windows : e-Bichelchen y reste gris après un redémarrage. Cela peut être ajouté, mais c’est un travail à part entière, pas un effet secondaire.

Rappel v334 : un assistant local plus ancien est désormais remplacé au lieu d’être laissé sur le port standard (chemin Python ; la version PowerShell nettoyait déjà les anciennes instances).
