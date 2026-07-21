# EntretienConnect v318

Le blocage de connexion e-Bichelchen sur macOS est corrigé.

- La lecture ne démarre plus sur l’écran public de connexion, qu’il soit affiché sous `/app/login` ou directement sous `/app/`.
- Si e-Bichelchen refuse malgré tout la fenêtre WebKit native et affiche son message de connexion au serveur, EntretienConnect ferme cette fenêtre et ouvre automatiquement une seule fenêtre isolée Chrome/Edge.
- Le même verrou de connexion protège également ce mode de secours.
- L’annulation, la fermeture et une nouvelle tentative fonctionnent correctement après le basculement automatique.
