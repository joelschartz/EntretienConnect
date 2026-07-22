# EntretienConnect v340

**Windows-Kaltstart ohne zusätzliches leeres Browser-Tab.** Ist der
Standardbrowser noch vollständig geschlossen, startet EntretienConnect ihn nun
gezielt mit der App-Adresse als einzigem Fensterziel. Läuft Edge, Chrome, Brave,
Vivaldi, Opera oder Firefox bereits, bleibt das bisherige Verhalten erhalten:
EntretienConnect öffnet sich als ein neuer Tab im vorhandenen Browser.

**Le dossier de l’Explorateur ne reste plus au premier plan.** La tentative de v336 échouait pour deux raisons. D’abord, Windows refuse `SetForegroundWindow` à un processus qui n’est pas lui-même au premier plan : l’appel restait sans effet. La file d’entrée est désormais rattachée brièvement au thread du premier plan (`AttachThreadInput`), ce qui rend l’appel recevable. Ensuite, il n’y avait qu’**une seule** tentative, 2,5 s après le démarrage : un Edge démarrant à froid n’a alors pas encore chargé la page, la fenêtre ne s’appelle pas encore « EntretienConnect » et n’est donc pas trouvée. La tentative est maintenant répétée toutes les 0,7 s jusqu’à ce que la fenêtre soit réellement devant — au maximum 25 secondes, et plus jamais ensuite, afin de ne pas voler le focus.

**Toujours ouvert : e-Bichelchen ne reste pas connecté sous Windows.** Ce n’est pas un oubli mais une lacune du chemin Windows. Sur Mac, les cookies sont enregistrés après la lecture et la session suivante se poursuit par de simples requêtes HTTP, sans aucune fenêtre. Sous Windows, ce chemin n’existe pas : **tout** y passe par du JavaScript exécuté dans le navigateur connecté (`Invoke-CdpEval`), et la session ne vit que dans ce navigateur — lequel est fermé après la lecture. Elle est donc perdue au démarrage suivant.

C’est rattrapable : enregistrer les cookies après la lecture, puis lancer au démarrage suivant un navigateur invisible avec ces cookies et y exécuter la même routine de lecture. C’est un morceau de PowerShell conséquent que je ne peux pas exécuter depuis le Mac ; je préfère le construire et le vérifier sur la machine Windows plutôt que de le livrer à l’aveugle.

Rappel v337 : boutons de déconnexion pour e-Bichelchen et Microsoft (l’ancien bouton Microsoft se trouvait dans une carte toujours masquée), et le navigateur est lancé directement avec l’adresse au lieu de passer par l’association Windows.
