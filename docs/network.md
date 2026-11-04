# Réseau S0 — relevé du 2026-11-04

## Résultat : plan B validé, sans blocage restant

Le moteur Docker Desktop répond depuis WSL : **29.8.2**, Compose **v5.6.0**.
Le premier téléchargement depuis WSL échouait avec :

```text
fork/exec /usr/bin/docker-credential-desktop.exe: exec format error
```

Le défaut a été résolu pour les mesures en préchargeant les deux images
publiques depuis Docker Windows sur le même moteur. Compose et le spike ont
ensuite tourné **depuis WSL** sans autre changement de configuration.
Le plan A est réellement injoignable ; **B est validé sur `192.0.2.1`**,
avec rejet sans jeton puis écho exact. L'accès HTTP au conteneur fonctionne
depuis WSL et Windows. Aucune simulation n'a été lancée.

| Vérification réelle | Résultat |
|---|---|
| `docker version --format '{{.Server.Version}}'` depuis WSL | `29.8.2` |
| `docker compose version` depuis WSL | `v5.6.0` |
| Disponibilité initiale des ports réservés 8770 / 7899 | Aucun listener |
| Premier pull depuis WSL | Erreur du helper d'identifiants, code 1 |
| `docker pull python:3.12-slim` depuis Windows | Image publique chargée, code 0 |
| `docker pull ghcr.io/astral-sh/uv:0.13.7` depuis Windows | Image publique chargée, code 0 |
| `docker compose up --build --detach --wait --wait-timeout 60` depuis WSL | Service `running healthy`, code 0 |
| `curl http://localhost:8770/healthz` depuis WSL | HTTP 200, `{"status": "ok"}` |
| `Invoke-WebRequest http://localhost:8770/healthz` depuis Windows | HTTP 200, `{"status": "ok"}` |
| Page `/` depuis Windows | HTTP 200, titre « Coin-Coin — hello » |
| Identité du conteneur | `uid=1000(coincoin) gid=1000(coincoin)` |
| A : `host.docker.internal` → WSL loopback 7899 | `OSError: [Errno 101] Network is unreachable` |
| B : conteneur → IP eth0 `192.0.2.1:7899` | Rejet sans jeton, écho authentifié exact, `RESULT=B`, code 0 |
| `ss -ltn 'sport = :7899'` après le spike réel | Aucun listener |
| `docker compose down` avec `COMPOSE_PROJECT_NAME=coincoin-s0` | Conteneur et réseau S0 supprimés |
| Contrôle final des ports 7899 / 8770 et du label Compose S0 | Aucun listener ni conteneur restant |

## Rejouer les mesures

Si les images publiques ne sont pas encore locales et le helper WSL échoue,
les précharger depuis PowerShell Windows :

```powershell
docker pull python:3.12-slim
docker pull ghcr.io/astral-sh/uv:0.13.7
```

Ne copier aucun identifiant et ne modifier ni `~/.docker/config.json`, ni la
configuration WSL. En S0, aucune configuration Docker alternative n'a été utilisée.

Depuis WSL, dans `~/projects/microduck/coin-coin/` :

```sh
test -e .env || cp .env.example .env
mkdir -p logs
export COINCOIN_PORT=8770 COMPOSE_PROJECT_NAME=coincoin-s0
docker compose up --build --detach --wait --wait-timeout 60
curl --fail --silent --show-error http://localhost:8770/healthz
docker compose exec -T coin-coin id
docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q coin-coin)"
scripts/net_spike.sh
```

Depuis PowerShell Windows :

```powershell
(Invoke-WebRequest http://localhost:8770/healthz -UseBasicParsing).Content
```

Attendus : `/healthz` retourne `{"status": "ok"}` depuis WSL **et** Windows,
`id` montre UID/GID 1000, état Docker `healthy`, puis le spike confirme l'écho
exact. Sur l'environnement mesuré, il retourne `RESULT=B` ; une machine
différente peut retenir A. Consigner toute nouvelle mesure sans écrire de jeton.

Nettoyer uniquement les ressources S0 :

```sh
COINCOIN_PORT=8770 COMPOSE_PROJECT_NAME=coincoin-s0 docker compose down
ss -ltn 'sport = :7899 or sport = :8770'
```

## Chemins mesurés et configuration retenue

**A — échec réel.** Écho dans WSL sur `127.0.0.1:7899`, client dans
`python:3.12-slim` avec
`--add-host=host.docker.internal:host-gateway` : connexion impossible,
`Network is unreachable`. Ce chemin n'est pas retenu sur cette machine.

**B — retenu après échec réseau réel de A.** Le script découvre l'adresse
IPv4 `eth0` WSL, arrête son écho A et relance sur cette seule adresse,
toujours sur 7899. Un jeton généré en mémoire est exigé sur la première ligne.
Le client vérifie d'abord qu'une connexion sans jeton est rejetée, puis l'écho
authentifié. L'adresse peut changer après redémarrage WSL ; un futur jeton
persistant devra rester exclusivement dans `.env`. Les sessions robot doivent
lier leurs relais à cette IP, configurer `ROBOT_LINK` et `FRAME_LINK` avec
leurs ports réservés, et imposer le jeton avant toute requête.
Les défauts de `Settings`/`.env.example` ne sont pas une validation du plan A.

Le script vérifie la disponibilité du moteur et de l'image **avant** de
démarrer l'écho. Il piège les sorties normales, erreurs, SIGINT et SIGTERM,
arrête uniquement le PID qu'il a créé et retire ses fichiers temporaires.
Un port déjà occupé provoque une erreur explicite, sans arrêter son propriétaire.

## Ce que prouvent les tests unitaires

Les commandes Docker et `ip` sont remplacées par des faux locaux dans
`tests/unit/test_net_spike.py`. Ces tests vérifient le flux du script,
l'écho exact, l'authentification B et le nettoyage sur succès/port occupé.
Ils **ne remplacent pas** les mesures Docker Desktop/WSL ni le test Windows.
