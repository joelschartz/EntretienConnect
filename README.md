# EntretienConnect v317

Plus aucune requête vers e-Bichelchen tant que l’identification n’est pas terminée.

La fenêtre de connexion s’ouvre sur `…/ebichelchen/app/login`, et cette adresse satisfaisait déjà la condition « contient /ebichelchen/app/ ». Le script de lecture démarrait donc dès la première seconde et lançait des requêtes non authentifiées vers l’API, renouvelées toutes les deux secondes pendant toute la durée de l’identification. e-Bichelchen affichait alors sa propre alerte « Es konnte keine Verbindung zum Server erstellt werden ».

Mesuré sur banc d’essai, fenêtre restée sur la page d’identification pendant 12 secondes :

- avant : 5 requêtes API (soit une cinquantaine pour une identification de deux minutes avec MFA)
- après : aucune

Après l’identification, la lecture fonctionne comme avant — classes, élèves et catégorie « Message » sont lus normalement. Une sécurité est prévue : si e-Bichelchen restait malgré tout sur une adresse `/login` après connexion, la lecture démarre quand même au bout de 90 secondes, exactement comme avant.

Rappel : v316 a annulé la modification de v315 (les cookies exportés depuis la fenêtre native ne suffisent pas pour un appel HTTP autonome — il manque les cookies `HttpOnly`). v314 a retiré ~900 lignes de code inaccessible, v313 a réparé la fenêtre native.

Reste à vérifier sur un compte réel : la publication des messages (`direct_ebichelchen_request` utilise les mêmes cookies exportés) et le changement de classe sans nouvelle identification.
