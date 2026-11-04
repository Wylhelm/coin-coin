# Décisions

## ADR-000 — aiohttp pour le socle web asynchrone

Date : 2026-11-04. Statut : accepté.

**Contexte.** S0 doit fournir HTTP et des contrats asyncio ; S2 ajoutera les
WebSockets audio/UI. Il faut éviter un deuxième serveur ou une abstraction
de transport inutile.

**Décision.** Utiliser `aiohttp`, l'une des options de S0, pour HTTP et les
futurs WebSockets. `create_app()` ne construit aucun client externe et se teste
avec `aiohttp.test_utils`. L'entrée `coincoin` charge configuration et journal,
puis sert le port interne fixe 8765. Le port publié et le nom de projet sont
propres à chaque session ; S0 réserve 8770 et `coincoin-s0`.

**Conséquences.** Une seule dépendance web runtime. Le conteneur s'exécute
en UID/GID 1000 et son `/healthz` vérifie la vie du service, pas des API externes.
L'absence de secrets n'empêche pas de lancer le socle.

Les signatures prescrites restent intactes. L'unique exemption ruff
`ASYNC109` est locale au `timeout` imposé par `RobotLink.call`.
Les valeurs non encore sélectionnées par S3/S4 restent explicitement
non sélectionnées (`None` pour le seuil, chaînes vides pour modèle/voix),
plutôt que d'inventer un modèle, une voix ou un seuil.

## ADR-001 — plan B : IP eth0 WSL et jeton sur la première ligne

Date : 2026-11-04. Statut : accepté, mesures réelles.

**Contexte.** Docker Engine 29.8.2 et Compose v5.6.0 répondent depuis WSL.
Le premier téléchargement de `python:3.12-slim` échouait avant toute mesure :

```text
error getting credentials - err: fork/exec
/usr/bin/docker-credential-desktop.exe: exec format error
```

Les deux images publiques ont été préchargées via Docker Windows sur ce même
moteur : `python:3.12-slim` et `ghcr.io/astral-sh/uv:0.13.7`. Les commandes
Compose et le spike ont ensuite été exécutés normalement depuis WSL, sans
modifier le helper, la configuration globale ni `DOCKER_CONFIG`.

**Mesures.** Le conteneur S0 sert `/healthz` depuis WSL et Windows
(HTTP 200, `{"status": "ok"}`), avec HEALTHCHECK `healthy` et UID/GID 1000.
Le spike réel donne :

```text
Testing A: host.docker.internal -> WSL 127.0.0.1:7899
OSError: [Errno 101] Network is unreachable
A failed; testing B on eth0 with a temporary shared token
PASS: fallback rejects unauthenticated connections
PASS: exact TCP echo from the Docker container
RESULT=B: use tcp://192.0.2.1:<relay-port>, bind eth0,
          require RELAY_TOKEN on the first line
```

Code de sortie : 0. Après sortie, aucun listener 7899 ni fichier temporaire
du spike ne reste. Le jeton éphémère n'est ni affiché ni conservé.

**Décision.** Retenir **B** pour les relais TCP des sessions suivantes :
écoute sur l'IP IPv4 `eth0` WSL, liens TCP utilisant cette IP, authentification
par `RELAY_TOKEN` obligatoire sur la première ligne de chaque connexion.
Les ports restent ceux attribués par COORDINATION.md. L'IP mesurée
`192.0.2.1` doit être redécouverte après un redémarrage WSL.

**Conséquences.** Le relais S1 doit implémenter le plan B et refuser les
connexions sans jeton avant tout transfert. Un jeton persistant appartient
uniquement au `.env` de la session concernée, jamais au repo ou aux logs.
S0 n'en crée aucun : son jeton sert seulement au test d'écho.
`Settings` et `.env.example` conservent les défauts de la spec maître ;
la configuration effective des sessions robot remplace ces liens selon B.

## ADR-002 — audio navigateur sur WebSocket et AudioWorklet

Date : 2026-11-04. Statut : retenu pour S2 ; validation humaine AEC en attente.

**Contexte.** L'audio de simulation doit rester dans la page navigateur pour que
l'annulation d'écho du navigateur puisse utiliser la sortie comme référence.
Le transport doit aussi éviter ICE et les candidats injoignables depuis Docker
Desktop sur localhost.

**Décision.** Utiliser un WebSocket binaire `/ws/audio` pour les trames PCM16
mono little-endian 24 kHz de 20 ms, et `/ws/ui` pour les contrôles. Le micro
utilise les contraintes navigateur echo cancellation, noise suppression et auto
gain ; les AudioWorklets rééchantillonnent l'entrée si nécessaire, tamponnent
la lecture avec 60 ms de précharge, rapportent la progression toutes les
100 ms et vident la lecture sur commande. Le mode écho vérifie le trajet audio ;
`--tone` émet un bip de 1 kHz et mesure sa présence par corrélation.

Chaque paquet audio descendant préfixe son PCM d'un numéro de séquence uint64
little-endian. Le contrôle de flush annonce le dernier numéro antérieur ; le
client élimine ces séquences même si elles arrivent après le message UI de flush.
Le serveur suspend aussi les nouveaux appels `play()` jusqu'à l'acquittement du
worklet. Les paquets transportent leur longueur PCM exacte : les fragments
incomplets ne sont ni complétés par du silence ni retenus, et le worklet les
concatène avant lecture.

**Preuve automatisée.** `uv run pytest tests/integration/test_ws_audio.py`
envoie 1 s de PCM au faux client et vérifie l'écho octet par octet, la limite
de 300 ms d'avance, le remplacement d'une page et le flush acquitté en moins
de 100 ms. Les tests unitaires vérifient la conversion/rééchantillonnage SoXR
24→16 kHz, la longueur exacte sur trames, la conservation de la fréquence et
de l'énergie (±1 %), le mixage sans délai et la corrélation du bip. `node
--test tests/js/play_worklet.test.mjs` exécute le vrai worklet et la vraie
barrière de séquence : trames livrées en retard après deux flushs, trames
fraîches après flush, fragments de 400+400+3 échantillons sans trou, et premier
rapport de lecture autour de 100 ms à 16, 24 et 48 kHz.

**Validation de la correction rouverte.** La commande complète
`uv run ruff check . && uv run ruff format --check . && uv run pyright &&
uv run pytest` est verte : 57 tests Python réussis, couverture totale de
94,29 %, aucun diagnostic pyright. Le harness AudioWorklet séparé réussit ses
3 tests sous Node.js. Le README donne la procédure d'essai et de nettoyage
Compose depuis le clone principal `~/projects/microduck/coin-coin`, après la
suppression du worktree S2.

**Mesure humaine AEC (à faire par user).** Pas encore exécutée : aucun
résultat de latence ou d'atténuation n'est revendiqué. Suivre la procédure
« Vérifier l'audio » du README : haut-parleurs sans casque, écouter le mode
`--echo` (retard ressenti faible, pas de larsen), puis lancer `--tone`, parler
par-dessus et noter le retard affiché ainsi que la présence du bip au vumètre.
Si le bip est nettement visible ou le système larsène, noter Q9 en échec.

**Alternative si l'AEC échoue.** Ouvrir une décision séparée sur aiortc et
`RTCPeerConnection` côté page, en configurant ICE vers `127.0.0.1` et un port
UDP publié. Elle n'est pas implémentée dans S2.

**Conséquences.** Les sons de Coin-Coin doivent être mixés et joués par
`BrowserAudio`, jamais via `robot.sound` en simulation, afin de rester dans
la référence AEC. Le choix reste à confirmer par le test live avant S4.
