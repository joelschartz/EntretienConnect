# EntretienConnect v334

Un ancien assistant local ne bloque plus la mise à jour.

Le message « le helper local actif est encore v332 » avait une cause simple : un EntretienConnect plus ancien tournait encore et occupait le port standard. Le nouveau démarrage se rabattait silencieusement sur un port de remplacement — et laissait donc l’ancien exactement là où il était. Or les deux partagent le même cache pour l’interface : le nouveau démarrage le renouvelait, l’ancien assistant servait ensuite la **nouvelle** interface tout en annonçant son **ancienne** version. C’est précisément là que la connexion échouait.

Désormais, le nouveau démarrage demande à un assistant plus ancien de s’arrêter et reprend le port standard. Seul un EntretienConnect plus ancien est remplacé : une instance de même génération ou plus récente, ainsi que tout service étranger sur ce même port, restent intacts (vérifié). Si le port est libre, la vérification ne coûte aucun temps mesurable.

Le message lui-même nommait par ailleurs « EntretienConnect_MAC.app » sous Windows. Il indique maintenant le bon lanceur et la vraie raison — un ancien EntretienConnect encore ouvert.

**Ce qui n’a pas changé, et pourquoi.** L’onglet vide au démarrage sous Windows : l’assistant confie l’adresse au navigateur par défaut ; si celui-ci démarre à froid, il affiche en plus sa propre page d’accueil. C’est le comportement du navigateur. On ne pourrait le changer qu’en lançant le navigateur directement avec l’adresse — une modification que je ne fais pas sans pouvoir tester Windows. De même, la connexion ouvre sous Windows une instance séparée de Chrome ou Edge au lieu d’une petite fenêtre comme sur macOS ; mais après le premier login, plus aucun navigateur ne s’ouvre aux démarrages suivants.

Rappel v333 : la session e-Bichelchen est mémorisée sous Windows aussi, et les cookies y sont conservés individuellement avec leur domaine — auparavant, à nom identique, le dernier écrasait les précédents, alors que trois noms se répètent sur des serveurs différents avec des valeurs différentes.
