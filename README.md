# EntretienConnect v345

**Schneller und ohne Fenstersprung unter Windows.** Chromium öffnet das
Browser-App-Fenster direkt mit 1200 × 800 Pixeln. Es wird nicht mehr verzögert
maximiert und nur einmal nicht blockierend in den Vordergrund geholt. Die drei
GitHub-Dateiprüfungen laufen erst nach dem sichtbaren Start im Hintergrund.
Ein bereits laufender v345-Helfer desselben Starter-Ordners wird sofort
weiterverwendet. Das Startprotokoll enthält nun Millisekundenwerte für
PowerShell-Initialisierung, Helferbereinigung und Serverbereitschaft.

**Windows startet immer im Browser-App-Modus.** Edge, Chrome, Brave, Vivaldi
und Opera öffnen EntretienConnect nun bei jedem Start als eigenes App-Fenster –
auch wenn der Browser bereits läuft. Das Fenster wird maximiert, damit die
verfügbare Bildschirmfläche genutzt und die Oberfläche nicht unnötig gekürzt
wird. Firefox öffnet mangels App-Modus stets ein separates normales Fenster.

**e-Bichelchen bleibt beim Moduswechsel angemeldet.** Der reine E-Mail-Modus
deaktiviert lediglich den Kanal in der Oberfläche. Beim späteren Wechsel zu
„Uniquement e-Bichelchen“ oder zum kombinierten Modus wird die gespeicherte
Sitzung nun automatisch und sofort wieder aufgenommen. Abgemeldet wird nur
noch ausdrücklich über „Déconnecter“.

**Windows-Kaltstart ohne leeren zweiten Tab.** Wenn noch kein sichtbares
Edge-/Chrome-Fenster existiert, startet EntretienConnect im App-Modus als
eigenständiges Browserfenster ohne Tab- und Adressleiste. Läuft der Browser
bereits, öffnet sich EntretienConnect weiterhin als normaler neuer Tab.

**Windows-Fenster zuverlässig im Vordergrund.** Das neu gestartete Fenster wird
kurz in die oberste Fensterebene gesetzt, aktiviert und anschließend wieder auf
den normalen Fenstermodus zurückgesetzt. Zwei aufeinanderfolgende Prüfungen
stellen sicher, dass Edge es während der Profilinitialisierung nicht wieder
hinter den Explorer legt.

Die unter Windows geprüfte e-Bichelchen-Sitzungswiederaufnahme aus v339 bleibt
unverändert erhalten. Bei einer noch gültigen gespeicherten Sitzung wird zudem
sofort der letzte erfolgreich gelesene Stand angezeigt; die Prüfung und
Aktualisierung läuft anschließend still im Hintergrund. Dadurch ist
e-Bichelchen beim zweiten Start bereits grün wie auf dem Mac.

**e-Bichelchen reste maintenant connecté sous Windows aussi.** C'était le point resté ouvert en v338, et il a été construit et vérifié directement sur la machine Windows. La cause n'était pas la connexion elle-même, mais ce qu'il en advenait ensuite : Chromium n'écrit jamais sur le disque les cookies de session pure. Ils ne vivaient donc que dans le navigateur auxiliaire — lequel est fermé après la lecture. Au démarrage suivant, la connexion était perdue. Sur Mac, cette perte n'existe plus depuis la v325, parce que les cookies y sont enregistrés après la lecture. Windows procède désormais de la même façon.

Après chaque lecture réussie, les cookies education.lu sont enregistrés, avec la page où ils étaient valables en dernier et l'User-Agent : `%LOCALAPPDATA%\EntretienConnect\eb-session.json`, valable douze heures, accessible au seul utilisateur connecté (héritage désactivé).

Au démarrage suivant, l'interface voit la session mémorisée et se connecte d'elle-même — sans clic et sans fenêtre. Derrière, un navigateur invisible démarre, reçoit les cookies mémorisés **avant** de charger la page, puis exécute exactement la même routine de lecture que la fenêtre visible. C'est pourquoi la catégorie « Message » continue d'être trouvée correctement : elle est découverte dans la page elle-même, elle n'est pas devinée. Les cookies rafraîchis sont réenregistrés dans la foulée, si bien qu'un usage quotidien ne demande plus de se reconnecter sans cesse.

La déconnexion via « Déconnecter » supprime aussi la session mémorisée — sans quoi l'app se reconnecterait silencieusement au démarrage suivant.

Si la connexion silencieuse échoue (session expirée côté serveur, pas de réseau), e-Bichelchen reste simplement gris et un clic sur « Connecter » ouvre la fenêtre de connexion comme avant. La tentative infructueuse dure une dizaine de secondes, se déroule en arrière-plan et ne laisse jamais de fenêtre de navigateur derrière elle. Aucune fenêtre ne s'ouvre jamais d'elle-même.

L'interface savait déjà faire tout cela : depuis la v331, elle interroge la session mémorisée puis appelle `/api/eb/resume`. Sous Windows, cette route n'existait pas — elle répondait « Route e-Bichelchen inconnue ».

Rappel v338 : le dossier de l'Explorateur ne reste plus au premier plan (`AttachThreadInput`, et la tentative est répétée toutes les 0,7 s au lieu d'une seule fois après 2,5 s).

Rappel v337 : boutons de déconnexion pour e-Bichelchen et Microsoft (l'ancien bouton Microsoft se trouvait dans une carte toujours masquée), et le navigateur est lancé directement avec l'adresse au lieu de passer par l'association Windows.
