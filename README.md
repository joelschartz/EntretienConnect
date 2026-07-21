# EntretienConnect v325

Deux améliorations de la fenêtre de connexion e-Bichelchen sur macOS.

**Ne plus se reconnecter à chaque démarrage.** La session e-Bichelchen tient à un cookie de session, qui ne vit que dans la mémoire du processus de la fenêtre — dès que celle-ci se fermait, il était perdu, d’où une nouvelle identification à chaque lancement de l’application. Les cookies education.lu du dernier login sont désormais enregistrés (fichier lisible par vous seul, droits 0600) et réinjectés dans la fenêtre **avant** le chargement de la page. Si la session IAM est encore valable côté serveur, vous arrivez directement connecté.

Vérifié de bout en bout sur banc d’essai : au 1er lancement les deux cookies sont capturés et enregistrés ; au 2ᵉ, le cookie de session — celui qui ne survivait pas — est bien renvoyé au serveur dès la première requête. Garde-fous testés : la session expire après 12 h et est alors supprimée ; une reconnexion volontaire (« recommencer ») efface la session mémorisée ; une session vide n’est jamais enregistrée. C’est le même principe que le jeton Microsoft, déjà conservé sur le disque.

**Plus d’icône « osascript » dans le Dock.** La fenêtre s’annonce désormais comme accessoire : l’icône supplémentaire dans le Dock disparaît. La fenêtre reste pleinement utilisable — fenêtre active, saisie au clavier et **⌘V** fonctionnent (vérifié : le WKWebView est premier répondeur et gère paste:). Un menu « Édition » a été ajouté exprès pour que les raccourcis Couper/Copier/Coller marchent, utile quand le mot de passe vient d’un gestionnaire.

Une limite honnête : le mot **« osascript »** peut encore apparaître dans la barre de menus en haut, mais uniquement tant que la fenêtre de connexion est au premier plan. Ce nom vient du programme réellement exécuté (`/usr/bin/osascript`) et ne peut pas être renommé sans envelopper le tout dans une véritable application — un changement bien plus lourd, écarté pour l’instant. L’icône du Dock, elle, disparaît vraiment.

Rappel v324 : la connexion Microsoft réagit aussi vite que celle d’e-Bichelchen (attente après identification réduite de 4,5 s à moins d’1,5 s). La connexion Microsoft reste dans le navigateur système à dessein — Microsoft bloque souvent l’authentification en fenêtre intégrée.
