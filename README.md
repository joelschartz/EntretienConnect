# EntretienConnect v324

La connexion Microsoft réagit maintenant aussi vite que celle d’e-Bichelchen.

Le procédé était déjà le bon : code d’autorisation avec PKCE, aucun code à recopier (la méthode par code d’appareil ne sert plus que de secours). Ce qui donnait l’impression de lenteur, c’était l’attente **après** l’identification :

| | e-Bichelchen | Microsoft avant | Microsoft maintenant |
|---|---|---|---|
| Cadence d’interrogation | 850–1400 ms | 3000 ms | 900 ms |
| Fermeture de l’onglet de retour | — | 1500 ms | 400 ms |
| Attente cumulée | — | **jusqu’à 4,5 s** | **moins d’1,5 s** |

Une interrogation coûte 0,85 ms, mesuré : elle s’adresse à l’assistant local, la cadence plus courte ne charge rien.

**Pourquoi la connexion Microsoft ne se fait pas dans la fenêtre native, comme e-Bichelchen :** Microsoft déconseille et bloque fréquemment l’authentification dans une fenêtre intégrée, et les stratégies d’accès conditionnel d’un établissement peuvent la refuser. Le navigateur du système est ici la voie prévue et sûre. Comme la connexion actuelle fonctionne, la déplacer serait un pari — et un pari de ce genre a déjà coûté une version aujourd’hui (v315). Le trajet reste donc le même ; seule l’attente inutile a disparu.

Rappel v323 : avertissement en cas de variable mal orthographiée dans un modèle, contrôle des adresses renforcé (chevrons, points doublés).

Reste ouvert dans la partie e-mail : la taille des pièces jointes n’est pas vérifiée avant l’envoi, et le jeton d’accès n’est lu qu’une fois avant la boucle d’envoi.

L’assistant e-Bichelchen et la fenêtre de connexion native restent inchangés depuis v320.
