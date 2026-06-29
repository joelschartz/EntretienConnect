# EntretienConnect v111

Application locale pour planifier les entretiens avec les parents et choisir dès le départ le canal le plus simple : **e-Bichelchen**, **e-mail Microsoft 365**, ou **les deux**.

## Nouveauté v95

- Windows démarre sans Python via **EntretienConnect.ps1**.
- Le login Microsoft/e-mail reste géré par PowerShell.
- **Connecter e-Bichelchen** est de nouveau relié à un vrai helper PowerShell : le bouton ouvre Chrome/Edge avec e-Bichelchen, attend le login IAM, puis lit la classe et les élèves.
- En mode **uniquement e-Bichelchen**, les champs e-mail ne sont pas affichés.
- Sur Mac, la fenêtre e-Bichelchen peut se fermer automatiquement après la lecture : la session est mémorisée par le helper local pour la publication.

## Démarrage Windows recommandé

1. Dézipper le dossier.
2. Ouvrir le dossier **EntretienConnect_v95**.
3. Double-cliquer sur **EntretienConnect starten.vbs**.
4. Le navigateur s’ouvre automatiquement.
5. Pour arrêter le serveur local : double-cliquer sur **EntretienConnect beenden.vbs**.

Si quelque chose ne marche pas, utiliser **EntretienConnect-Start.bat** : cette variante affiche la fenêtre PowerShell et donc les messages d’erreur.

## Important pour e-Bichelchen

Quand l’app ouvre e-Bichelchen, se connecter dans la fenêtre Chrome/Edge ouverte par l’app. Après la lecture des élèves, laisser cette fenêtre ouverte tant que des messages doivent être publiés dans e-Bichelchen.

## Fichiers principaux

- `graph.html` : interface de l’app.
- `EntretienConnect.ps1` : serveur local PowerShell pour Microsoft/e-mail et e-Bichelchen.
- `EntretienConnect starten.vbs` : démarrage silencieux.
- `EntretienConnect-Start.bat` : démarrage visible pour diagnostic.
- `EntretienConnect beenden.vbs` : arrêt du helper.


Windows v95: Der normale VBS-Starter startet intern EntretienConnect-Start-Hidden.bat. Bei Problemen EntretienConnect-Start.bat verwenden; dort bleibt die Fehlermeldung sichtbar.


## v111 Hinweis

v111 vereinheitlicht die Reihenfolge im Reiter Envoi: e-Bichelchen zuerst, dann E-mails, dann Rappels. Dieselbe Reihenfolge gilt für Modèles de messages, Aperçu & envoi und die Zusammenfassung.

Start:
1. ZIP komplett entpacken.
2. `0_START_HIER_EntretienConnect.vbs` starten.
3. Wenn das nicht klappt: `0_START_DIAGNOSE_EntretienConnect.bat` starten und Screenshot vom ganzen Fenster schicken.
