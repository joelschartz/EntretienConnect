# EntretienConnect v315

Sur macOS, la fenêtre de connexion native n’a plus qu’une seule tâche : constater que l’identification est terminée et transmettre la session. Tout le reste — classes, élèves, catégorie « Message » — est lu par l’assistant local en Python, avec exactement les mêmes cookies.

Pourquoi : la même analyse existait en double, environ 430 lignes de JavaScript injectées dans la page d’un côté, le code Python de l’autre. La première lecture venait du JavaScript, le choix de classe du Python. Deux implémentations du même résultat, à maintenir en parallèle à chaque évolution d’e-Bichelchen.

- Le script injecté dans la fenêtre passe de ~430 lignes à 27 : une seule requête qui vérifie que la session répond.
- Un seul chemin de lecture pour la première lecture comme pour le choix de classe.
- Le fichier de transfert ne contient plus aucune donnée d’élève, seulement les cookies et un compteur de classes.
- Si la première lecture échoue, elle peut être relancée sans nouvelle identification : la session reste en mémoire.

Le gros lecteur JavaScript reste en place et inchangé — la voie Windows (Chromium/CDP) l’utilise toujours.

Vérifications : les objets produits par les deux voies ont été comparés champ par champ sur les mêmes données ; élèves et classe sont identiques au caractère près, et tous les champs lus par l’interface (`group`, `groups`, `students`, `messageSubject`, `needsGroupSelection`) concordent. Les deux cas ont été testés de bout en bout : une seule classe (sélection automatique) et plusieurs classes (choix dans EntretienConnect, puis relecture).

Rappel : v313 a réparé la fenêtre native (erreur ObjC `nil` traitée comme un échec, et sélecteur de cookies inexistant) ; v314 a retiré ~900 lignes de code inaccessible ; v311/v312 ont apporté la détection de session expirée, la vérification avant publication multiple, le blocage des publications simultanées et l’accès local uniquement.
