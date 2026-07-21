# EntretienConnect v311

- Détecte une session e-Bichelchen expirée : l’interface propose immédiatement « Connecter e-Bichelchen » au lieu d’afficher une erreur trompeuse ou de rester sur « Connexion en cours… ».
- Vérifie la session une seule fois avant une publication multiple, pour ne plus s’arrêter au milieu de la liste avec des entrées déjà créées.
- Bloque une deuxième publication simultanée (double-clic ou deuxième onglet) qui pouvait créer des entrées en double.
- Protège le fichier de transfert de la session (droits 0600), le supprime dès que la session est en mémoire et aussi au démarrage après un arrêt brutal.
- Mémorise l’adresse des matières qui a fonctionné : la lecture d’une classe demande une requête au lieu de neuf.
- Masque le choix du navigateur sur macOS, où la fenêtre de connexion native est toujours utilisée, et corrige l’infobulle devenue fausse depuis v310.
- N’accepte plus que les accès locaux sur l’assistant local (contrôle de l’en-tête Host).
