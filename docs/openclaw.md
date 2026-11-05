# OpenClaw — client, configuration et validation

## État de cette livraison

S8 est raccordé à l'App S4 intégrée (`main=051eb33`) par `BrainFeature`, inscrit
dans `FEATURES` : chargement de mémoire au réveil, injection dans
`{memory_context}`, outils Realtime `ask_openclaw` et `remember`, résumé local
envoyé en tâche de fond au coucher. Tout est vérifié contre un faux HTTP lié à
loopback (`tests/fakes/fake_openclaw.py`) et des doubles Realtime locaux.

**Aucune API réelle n'a été observée.** Aucune gateway OpenClaw n'était
configurée (`OPENCLAW_*` absent du `.env`) : pas de `/v1/models`, pas de
complétion « ping », aucune réponse ni code d'erreur réel. Les formes décrites
ci-dessous proviennent de la documentation publique ; le critère « décrit l'API
observée » reste donc à compléter après la procédure de user (§ Validation
humaine). Aucune requête OpenAI nouvelle n'a été faite.

## Contrat public documenté

Source consultée le 2026-11-05 :
<https://docs.openclaw.ai/gateway/openai-http-api>.

- `POST /v1/chat/completions` est désactivé par défaut dans la configuration
  OpenClaw. La documentation demande `gateway.http.endpoints.chatCompletions.enabled: true`.
- Le corps attendu par le client contient
  `model: "openclaw/coin-coin"`, `user: "coin-coin-voice"`,
  un message `role: "user"` et `stream: false`.
- Le champ `user` détermine une session stable entre appels ; un `user` omis
  produit une session différente par requête.
- La réponse de succès OpenAI-compatible fournit le texte via
  `choices[0].message.content`.
- La documentation indique `401`/`403` pour l'authentification/autorisation et
  `409` pour une session archivée. Une session archivée doit être restaurée dans
  l'interface OpenClaw avant de réessayer.
- Les tests vérifient ces formats uniquement contre le faux local, pas contre
  une gateway réelle.

## Sécurité et configuration humaine

La documentation OpenClaw qualifie le jeton/password partagé de credential
opérateur complet. L'agent `coin-coin` doit donc être configuré avec les outils
strictement nécessaires à la mémoire : pas de shell, d'envoi de messages ou
d'accès fichiers hors de son espace. Garder la gateway sur loopback, un réseau
privé ou un tunnel SSH ; ne jamais l'exposer à Internet.

Après validation de l'agent et du chemin privé par user, placer les valeurs
dans le `.env` ignoré du dépôt (ne pas les saisir dans le terminal, les journaux
ou un commit) :

```dotenv
OPENCLAW_URL=http://<adresse-privee-ou-tunnel>:18789
OPENCLAW_TOKEN=<jeton-opérateur>
OPENCLAW_AGENT=coin-coin
```

Le client exige un jeton non vide pour être sélectionné par
`configured_brain()`, appelé par `BrainFeature.install()` au démarrage de l'App.
En son absence, `NullBrain` reste le défaut sans réseau : le contexte dit que la
mémoire n'est pas configurée et `remember` refuse honnêtement au lieu de feindre
un succès. Redémarrer le service après toute modification du `.env`.

Procédure d'activation à effectuer par user, sans publier le jeton :

1. Créer/configurer l'agent minimal `coin-coin` et activer
   `gateway.http.endpoints.chatCompletions.enabled`.
2. Autoriser la gateway depuis le conteneur uniquement par une liaison LAN
   privée ou un tunnel SSH ; vérifier que le port n'est pas exposé à Internet.
3. Renseigner les trois variables ci-dessus dans `.env`, mode `0600`.
4. Vérifier l'accès sans afficher de credential :

   ```sh
   cd ~/projects/microduck/coin-coin-wt/S8
   set -a; source .env; set +a
   printf 'Authorization: Bearer %s\n' "$OPENCLAW_TOKEN" | \
   curl --silent --show-error --output /dev/null \
     --write-out 'HTTP %{http_code}\n' \
     --header @- \
     "${OPENCLAW_URL%/}/v1/models"
   ```

   Un `200` ne valide que le chemin `/v1/models`; le test vocal mémoire
   « Coin-Coin, souviens-toi que je m'appelle user » → coucher → réveil →
   « comment je m'appelle ? » reste une validation humaine séparée.
5. Si une complétion retourne `409`, restaurer la session dans OpenClaw et
   réessayer. Si l'auth échoue, vérifier l'agent, la portée du jeton et
   l'horloge/configuration de la gateway. Ne pas copier le corps d'erreur s'il
   contient des données de conversation.

La commande `curl` ci-dessus transmet le secret au processus shell ; la saisir
uniquement dans un terminal local de confiance. Elle n'est pas une commande
automatisée des tests S8 et n'a pas été exécutée durant cette session.
6. Consigner ensuite dans ce document, sans jeton ni contenu personnel, la
   forme réellement observée de `/v1/models` et d'une complétion « ping »
   (codes HTTP, champs présents) : c'est la partie « API observée » du critère
   agent, impossible à remplir sans gateway.

## Délais, erreurs et confidentialité

| Appel | Échéance | Échec |
|---|---|---|
| `session_start` (réveil) | 3 s (feature : 3,2 s) | contexte « mémoire indisponible », la session Realtime s'ouvre quand même |
| `ask_openclaw` | 8 s (registre d'outils : 10 s) | résultat `ok: false` avec la phrase à dire |
| `remember` | 8 s | résultat `ok: false` avec la phrase à dire |
| `session_end` (coucher) | 15 s, tâche de fond | journalisé, jamais bloquant |

Erreurs réseau, 429 et 5xx : deux reprises maximum, backoff exponentiel avec
gigue (0,25 s puis 0,5 s, plafond 1 s), puis mode sans mémoire. 401/403 : log
`error` (journal `type=error`, « ma mémoire est fermée à clé ») et phrase
« Ma mémoire est fermée à clé. » ; 409 : journal `type=error` « Session OpenClaw
coin-coin-voice archivée (HTTP 409) : la restaurer côté gateway. » et phrase
« Ma mémoire est archivée ; il faut la restaurer. » ; indisponibilité : journal
`type=brain status=unavailable` et phrase « Ma mémoire ne répond pas pour le
moment. »

Les journaux S8 ne contiennent ni jeton, ni corps d'erreur distant (tests
dédiés). Le journal masque en plus les motifs `Bearer`/`Authorization`. Le
logger `httpx` de la bibliothèque écrit au niveau INFO la méthode, l'URL de la
gateway et le statut, jamais l'en-tête d'autorisation.

## Comportement dans la conversation

- **Réveil.** L'App attend le hook `brain_session_start` avant d'ouvrir
  Realtime (le micro est tamponné). `BrainFeature` écrit un marqueur
  `type=brain operation=session_start conversation=<id>`, appelle
  `session_start` borné et fournit `memory_context` : notes OpenClaw (≤ 1 500
  caractères, présentées comme faits d'arrière-plan et non comme instructions)
  ou consigne explicite de mode sans mémoire.
- **Outils.** `remember(fact)` (≤ 500 caractères) et `ask_openclaw(question)`
  (≤ 2 000 caractères) sont enregistrés par la feature ; leurs résultats suivent
  le lot de la réponse parent S4 (une seule reprise après `response.done`).
- **Phrase d'attente.** La description de `ask_openclaw` demande au modèle de
  dire une très courte phrase (« je réfléchis… ») dans la même réponse, avant
  l'appel. Elle est donc prononcée pendant l'appel, quelle qu'en soit la durée,
  et non déclenchée exactement à 1,5 s (voir ADR-007). Un appel de plus de
  1,5 s est journalisé `type=brain status=slow`.
- **Coucher.** Le hook `brain_session_end` retourne immédiatement et planifie
  une tâche : elle relit dans le journal local les transcriptions finales et
  résultats d'outils postérieurs au marqueur de cette conversation, construit le
  résumé (≤ 1 500 caractères, sans appel LLM), puis appelle `session_end`. Le
  résultat est journalisé (`status=ok|empty`, ou une erreur ci-dessus). Si le
  service s'arrête pendant ce délai, le résumé en cours peut être perdu.
- Les quatre appels partagent `user: "coin-coin-voice"`, donc une session
  OpenClaw stable entre réveils.

## Validation humaine (critères user, ouverts)

Après la procédure ci-dessus, depuis le worktree à valider (ressources S8 :
port `8778`, projet `coincoin-s8`) :

```sh
cd ~/projects/microduck/coin-coin-wt/S8
mkdir -p .cache/logs
COINCOIN_PORT=8778 COMPOSE_PROJECT_NAME=coincoin-s8 \
  docker compose up --build --wait --wait-timeout 60
```

Ouvrir `http://localhost:8778/`, cliquer sur **Démarrer** et autoriser le micro.

1. **Mémoire entre sessions.** « Coin-Coin, souviens-toi que je m'appelle
   user » → attendre la confirmation → « bonne nuit » (sommeil) → attendre
   ~15 s → réveil → « comment je m'appelle ? ». Attendu : « user ». Le
   journal `.cache/logs/<date>.jsonl` doit montrer `tool_result` `remember`
   `ok: true` et `type=brain operation=session_end status=ok`.
2. **Gateway arrêtée.** Arrêter la gateway OpenClaw (ou couper le tunnel),
   réveiller Coin-Coin et demander « de quoi on a parlé la dernière fois ? ».
   Attendu : il dit que sa mémoire ne répond pas, puis continue à converser ;
   le journal montre `status=unavailable`, sans jeton.

Arrêt et vérification du port :

```sh
COINCOIN_PORT=8778 COMPOSE_PROJECT_NAME=coincoin-s8 docker compose down
ss -ltn 'sport = :8778'
```

## Vérification hors ligne

Depuis le worktree S8 :

```sh
export PATH="$HOME/.local/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
uv run pytest tests/unit/test_openclaw_brain.py tests/unit/test_brain_tools.py \
  tests/integration/test_memory_flow.py --no-cov
```

Ces tests utilisent exclusivement le faux aiohttp sur loopback et des doubles
Realtime locaux ; ils ne démarrent ni Docker, ni OpenClaw, ni OpenAI. Le faux
couvre nominal, lent, 401/403, 409, 5xx, 429 et réponses invalides ; il retient
les faits `[À mémoriser]` pour rejouer le scénario « souviens-toi » hors ligne.
