# EntretienConnect v336

**La fenêtre de l’application passe désormais au premier plan au démarrage.** Sous Windows, le dossier de l’Explorateur depuis lequel on lance l’application restait devant, l’application derrière. Windows n’autorise pas un processus d’arrière-plan à prendre le premier plan : la fenêtre du navigateur s’ouvrait donc derrière. Elle est maintenant ramenée au premier plan une seule fois, peu après le chargement de la page — et de manière non bloquante, car la page ne peut se charger que si la boucle de requêtes tourne.

**À propos des deux onglets au démarrage.** J’ai vérifié le lanceur : le `.exe` Windows est un petit programme Go dont les fonctions se limitent à démarrer PowerShell, attendre que le serveur réponde et afficher les erreurs — **il n’ouvre aucun navigateur**. Côté PowerShell, il n’y a qu’un seul `Start-Process`. L’application ouvre donc le navigateur exactement une fois, et le journal l’indique désormais explicitement (`Browser wird EINMAL geöffnet mit: …`, dans `%LOCALAPPDATA%\EntretienConnect\EntretienConnect-log.txt`).

Le second onglet vient donc du navigateur lui-même — page de démarrage à froid, ou restauration de la session précédente (auquel cas l’onglet « vide » est en réalité l’ancien onglet EntretienConnect, dont le port n’existe plus).

Rappel v335 : la fenêtre de connexion e-Bichelchen utilise `--app=` au lieu de `--new-window` (plus d’onglet vide), et `SW_RESTORE` n’est plus appliqué qu’à une fenêtre réellement réduite — c’était la cause du rétrécissement de la fenêtre après la connexion.

Toujours ouvert sous Windows : la reprise de session e-Bichelchen (v330–v333) n’existe que dans le chemin Python et n’est donc pas exécutée sous Windows ; e-Bichelchen y reste gris après un redémarrage.
