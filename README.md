# EntretienConnect v312

Correction de v311 : la fenêtre de connexion native ne se fermait plus après l’identification et l’application restait sur « Connexion en cours… ».

- Retire l’appel de permissions ajouté en v311 dans la fenêtre native. Il se trouvait dans le chemin qui ferme la fenêtre après la connexion ; le module natif est de nouveau identique à celui de v310.
- Les droits du fichier de transfert (0600) sont désormais appliqués côté assistant local, qui relit ce fichier chaque seconde de toute façon.
- Ajoute une trace console `[eb] login-ready: …` indiquant l’étape exacte de la fenêtre native, pour diagnostiquer un blocage éventuel.

Repris de v311 :

- Détecte une session e-Bichelchen expirée : l’interface propose immédiatement « Connecter e-Bichelchen » au lieu d’afficher une erreur trompeuse ou de rester sur « Connexion en cours… ».
- Vérifie la session une seule fois avant une publication multiple, pour ne plus s’arrêter au milieu de la liste avec des entrées déjà créées.
- Bloque une deuxième publication simultanée (double-clic ou deuxième onglet) qui pouvait créer des entrées en double.
- Supprime le fichier de transfert dès que la session est en mémoire et aussi au démarrage après un arrêt brutal.
- Mémorise l’adresse des matières qui a fonctionné : la lecture d’une classe demande une requête au lieu de neuf.
- Masque le choix du navigateur sur macOS, où la fenêtre de connexion native est toujours utilisée, et corrige l’infobulle devenue fausse depuis v310.
- N’accepte plus que les accès locaux sur l’assistant local (contrôle de l’en-tête Host).
