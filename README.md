# EntretienConnect v328

Robustesse face aux bloqueurs de fenêtres surgissantes.

v327 réglait le cas courant — la fenêtre s’ouvre pendant le clic, donc les navigateurs l’autorisent. Restait le cas d’un blocage général : réglage strict, extension ou politique d’entreprise. `window.open` renvoyait alors `null`, et l’application attendait six minutes en silence, avec une simple mention en petit.

Trois niveaux désormais :

1. **Fenêtre dédiée** (520 × 720), ouverte pendant le clic.
2. **Sinon, un onglet ordinaire** — sans dimensions, une ouverture est bien moins souvent refusée. Mieux vaut un onglet que pas de connexion.
3. **Sinon, message immédiat** : « Votre navigateur a bloqué la fenêtre de connexion », avec un bouton « Ouvrir la connexion Microsoft ». Ce bouton est un vrai lien : le clic est un nouveau geste de l’utilisateur, qu’aucun bloqueur ne refuse. L’interrogation continue en arrière-plan, donc l’identification faite par ce lien termine la connexion normalement.

Vérifié en simulant un blocage total : les deux tentatives d’ouverture ont lieu (avec puis sans dimensions), le message et le lien apparaissent immédiatement, la méthode par code reste proposée, et une identification effectuée via le lien mène bien au bout. Contrôlé aussi que le cas normal n’a pas changé : une seule fenêtre, aux bonnes dimensions, refermée par l’application, sans message d’erreur.

Rappel v327 : la fenêtre s’ouvre pendant le clic (sinon Safari la refuse après les appels réseau). v325 : session e-Bichelchen mémorisée entre deux démarrages, plus d’icône supplémentaire dans le Dock.
