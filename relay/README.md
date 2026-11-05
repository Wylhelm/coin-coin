# duck-relay

Relais asyncio **stdlib uniquement, Python ≥ 3.10**, lancé dans WSL à côté
de `duck-sim`. Une connexion TCP ouvre une connexion Unix indépendante.
Le relais n'active pas le robot et ne remplace pas le gouverneur.

## Démarrage S1

Depuis le worktree S1, dont `.env` contient le jeton partagé sans l'afficher :

```sh
set -a
source .env
set +a
python3 relay/duck_relay.py --state-dir "$HOME/.cache/ds-s1" \
  --host 192.0.2.1 --robot-port 7830 --frame-port 7831
```

Redécouvrir auparavant l'IPv4 `eth0` avec `ip -4 -o addr show dev eth0`.
Utiliser cette même adresse dans `ROBOT_LINK` et `FRAME_LINK`. Le chemin
`host.docker.internal` → loopback WSL ne fonctionne pas sur la machine
mesurée ; voir [ADR-001](../docs/decisions.md).

| Argument | Défaut générique |
|---|---|
| `--state-dir` | `$DUCK_SIM_STATE`, sinon `~/.cache/duck-sim` |
| `--duck` | `duck-a` |
| `--host` | `127.0.0.1` |
| `--robot-port` | `7810` |
| `--frame-port` | `7811` |
| `--token` | variable d'environnement `RELAY_TOKEN`, sinon vide |

Les défauts génériques sont ceux de la spec, **pas** les ressources à
utiliser pour les essais S1. Un hôte non loopback sans jeton est refusé au
démarrage. Préférer `RELAY_TOKEN` dans l'environnement : `--token` existe
pour le contrat CLI mais une valeur passée en argument serait visible dans
la liste des processus et l'historique.

Avec jeton, la première ligne de **chaque** connexion est le jeton brut
suivi de `\n`, sans JSON et sans accusé de réception. Comparaison en temps
constant, délai d'authentification 3 s, rejet avant ouverture du socket Unix.
Les connexions robot **et** images s'authentifient ainsi. Le jeton n'est
jamais journalisé ; les paramètres JSON-RPC ne le sont pas non plus.
Il n'y a pas de chiffrement TLS : réserver ce lien au réseau privé WSL.

## Filtrage et transport

- Port robot → `STATE/duck-a.sock`, liste constante `ROBOT_METHODS`.
- Port images → `STATE/duck-a-frame.sock`, liste constante `FRAME_METHODS`
  limitée à `hello` et `media.frame`.
- Client → Unix : une ligne JSON-RPC 2.0 par message, refus par défaut.
  JSON invalide, batch, enveloppe invalide ou ligne > 64 Kio : `−32600`.
  Méthode hors liste : `−32601`, « refusee par duck-relay ».
  L'identifiant valide est conservé ; une notification interdite est jetée
  sans réponse et journalisée.
- Une ligne trop longue est drainée jusqu'au prochain `\n`, sans conserver
  toute sa taille en mémoire ; la connexion reste utilisable ensuite.
- Unix → client : copie brute, incluant notifications `robot.state` et
  octets binaires d'images. Aucune interprétation ni réécriture.
- Socket absent : fermeture propre et diagnostic stderr explicite.
  Les journaux ne contiennent que connexions, déconnexions, méthodes
  refusées et classes d'erreur, jamais le contenu des paramètres.

SIGINT et SIGTERM ferment les deux listeners et toutes les connexions.
Les écritures sont fermées avant l'attente des tâches pour éviter une fuite
de transport si l'arrêt interrompt déjà une finalisation. Une fermeture
bloquée est journalisée et son transport abandonné après 1 s.

## Vérification hors robot

```sh
uv run pytest tests/unit/test_relay_filter.py \
  tests/integration/test_relay_proxy.py --no-cov
```

Les tests utilisent des ports éphémères loopback et des sockets Unix
temporaires : toutes les méthodes autorisées, familles interdites,
notifications, rejet sans jeton, gros message puis reprise, état poussé,
réponse binaire intacte, socket absent, nettoyage et signaux SIGINT/SIGTERM.
Ils ne touchent aucune instance de simulation.
