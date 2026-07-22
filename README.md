# EntretienConnect v332

Au démarrage, e-Bichelchen se reconnecte **sans ouvrir aucune fenêtre** — comme Microsoft.

En v331, la fenêtre s’ouvrait brièvement puis disparaissait. Elle n’est plus nécessaire : l’assistant local relit lui-même les classes à partir de la session mémorisée. Mesuré sur un compte réel : **67 ms**, aucune fenêtre.

**Un bogue de ma part, corrigé.** Depuis v311, toute réponse HTML était interprétée comme « session expirée ». Or e-Bichelchen renvoie un HTTP 404 avec page HTML quand on interroge une adresse de matières qui n’existe pas — et l’application en essaie plusieurs. La lecture s’interrompait donc immédiatement, avec un message trompeur. C’est très probablement ce qui avait fait échouer la tentative de v315, que j’avais attribuée à tort à une panne du serveur. L’expiration est désormais reconnue uniquement à la redirection vers l’identification IAM, ce qui est le seul signal fiable.

**Deux autres corrections de fond :**

- Les cookies envoyés sont désormais choisis selon le serveur contacté. La session couvre quatre domaines, et trois noms de cookies s’y répètent avec des valeurs différentes ; seuls les trois cookies valables pour `ssl.education.lu` sont maintenant transmis.
- La catégorie « Message » ne peut pas être devinée : toutes les adresses connues répondent 404, la fenêtre la découvre dans la page. Elle est donc mémorisée par classe lors de la connexion et réutilisée ensuite.

**Important pour le premier essai :** la session déjà enregistrée ne contient pas encore la catégorie mémorisée. Au premier lancement après la mise à jour, la fenêtre s’ouvrira donc encore une fois. À partir du deuxième, tout se fait en silence.

Si la reprise échoue, aucune fenêtre ne s’ouvre d’elle-même : e-Bichelchen reste gris et un clic sur « Connecter » suffit, comme avant.

Ces corrections concernent aussi la publication des messages et le changement de classe, qui empruntent le même chemin — cela reste à confirmer sur un compte réel.
