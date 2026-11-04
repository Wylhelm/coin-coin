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
