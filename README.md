# EntretienConnect v331

e-Bichelchen se connecte désormais tout seul au démarrage, comme Microsoft — quand une session mémorisée est disponible.

Depuis v330, la session survit à la fermeture de l’application : la fenêtre s’ouvre, reconnaît la session et se referme sans rien demander. Il fallait cependant encore cliquer sur « Connecter », alors que Microsoft est vert dès le lancement. La différence tenait à ceci : pour Microsoft, l’assistant local peut vérifier le jeton sur le disque ; pour e-Bichelchen, les classes ne peuvent être lues que par la fenêtre.

L’application demande donc au démarrage si une session est mémorisée et, le cas échéant, lance la connexion d’elle-même. La fenêtre s’ouvre brièvement, reprend la session et se referme — puis e-Bichelchen est vert, sans aucun clic.

Volontairement prudent, et vérifié :

| Situation | Comportement |
|---|---|
| Aucune session mémorisée | rien ne s’ouvre |
| Session mémorisée | une seule connexion automatique |
| `ebFetchStatus` appelé plusieurs fois | toujours une seule tentative |
| Mode « Uniquement e-mail » | rien ne s’ouvre |

Aucune donnée ancienne n’est réaffichée : la classe est relue par la fenêtre, comme lors d’une connexion manuelle. La session mémorisée expire après 12 h et une reconnexion volontaire l’efface — dans les deux cas, plus d’ouverture automatique.

Sous Windows, rien ne change : la session mémorisée n’existe que pour la fenêtre native de macOS.

Rappel v330 : la session mémorisée est enfin utilisée, parce que la fenêtre retourne à l’adresse où la session était valable au lieu de la page d’identification.
