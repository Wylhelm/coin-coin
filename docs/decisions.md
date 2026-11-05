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
À S0, le seuil de réveil n'était pas choisi ; S3 publie désormais un seuil
Vosk provisoire mesuré en ADR-003. Le modèle et la voix Realtime restent
explicitement non sélectionnés (chaînes vides) jusqu'à S4.

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

L'appel `flush()` applique une échéance monotone unique de 100 ms couvrant
l'attente de son verrou, l'envoi de la commande UI et l'acquittement. Il ne prend
pas le verrou d'envoi audio : les trames déjà en transit sont invalidées par
génération et numéro de séquence. Un timeout, une erreur d'envoi ou une
annulation est signalé à l'appelant ; la page est alors considérée
désynchronisée et les nouveaux `play()` échouent jusqu'à un flush acquitté.
Cela ne garantit pas l'arrêt physique de l'audio si le navigateur ou sa voie UI
ne répond pas.

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

L'interpolation linéaire conserve sa phase fractionnaire relativement au premier
échantillon restant après chaque quantum : elle interpole ce point avec le
suivant, sans réutiliser l'échantillon consommé du quantum précédent. Le
harness compare la rampe complète du vrai worklet à la référence calculée à
16, 24, 44,1 et 48 kHz, y compris au-delà de plusieurs blocs, entre deux
fragments et après une sous-alimentation suivie d'une reprise. Un contre-exemple
à 48 kHz vérifie aussi qu'avec les échantillons −30000, 0, 30000 aux indices
63–65, le rendu n°129 interpole bien à +15000/32768.

Les doubles WebSocket déterministes vérifient également qu'un `send_bytes`
bloqué ne retient pas le flush UI, qu'un `send_json` bloqué échoue dans son
échéance, et que deux flushs simultanés respectent chacun leur propre borne.
Les régressions couvrent l'annulation, l'acquittement de génération périmée, le
remplacement de page, le rejet des nouveaux `play()` après échec et le filtrage
d'une ancienne trame reçue après un flush acquitté.

**Validation de la correction rouverte.** La commande complète
`uv run ruff check . && uv run ruff format --check . && uv run pyright &&
uv run pytest` est verte sur la base `main=0597380` : 332 tests réussis,
5 ignorés, couverture totale de 93,16 %, aucun diagnostic pyright. Les tests
ciblés WebSocket réussissent (14 tests), et le harness AudioWorklet séparé
réussit ses 5 tests sous Node.js. Le README donne la procédure d'essai et de
nettoyage Compose depuis le clone principal `~/projects/microduck/coin-coin`,
après la suppression du worktree S2.

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

## ADR-003 — Vosk provisoire, seuil 0.90

Date : 2026-11-04. Statut : compromis mesuré, validation humaine en attente.

**Contexte.** S3 devait mesurer un moteur de réveil bilingue avec au moins 90 %
de rappel par langue, au plus un faux réveil par heure et aucun déclenchement
sur les cancanements. Un modèle openWakeWord personnalisé n'était envisagé
qu'après la mesure Vosk. Les positifs de user n'étaient pas enregistrés.

**Mesures.** Le banc `scripts/eval_wake.py` a évalué 40 positifs TTS synthétiques
par langue (près/loin), 30 minutes de FLEURS français, 30 minutes de
LibriSpeech test-clean anglais, 82 cancanements copiés en lecture seule et
20 phrases TTS contenant « Coin-Coin » dans un contexte. Positifs et audio
téléchargés/synthétisés restent ignorés par Git. À `WAKE_THRESHOLD=0.90` :

- rappel synthétique : FR 15/40 (37,5 %), EN 21/40 (52,5 %) ;
- 0 faux réveil/h sur FLEURS + LibriSpeech + banque de sons, 0 sur la banque ;
- 20 déclenchements sur 20 phrases TTS en contexte ; 19,30 faux réveils/h en
  comptant tous les négatifs, donc la cible globale n'est pas atteinte ;
- la grille 0,30–0,90 ne change pas le rappel ; 0,90 réduit le plus les faux
  positifs en discours public parmi les seuils mesurés ;
- les deux recognizers ont évalué le corpus en 2 min 15,5 s dans l'image Docker
  S3 isolée du réseau (environ 1,07 h d'audio au total, environ 28× le temps réel).

**Décision.** Garder Vosk small FR 0.22 et EN-US 0.15 comme référence exécutable,
grammaire stricte `["coin coin", "[unk]"]`, et exposer `WAKE_THRESHOLD=0.90`
comme **meilleur compromis provisoire**, pas comme seuil validé. Le
`OpenWakeWordEngine` reste un stub explicite : aucun modèle candidat n'a été
mesuré comme meilleur, et les phrases où le mot de réveil est prononcé
intentionnellement ne peuvent pas être séparées par un simple seuil Vosk.

**Conséquences.** S4 peut charger un moteur et un seuil configurables, mais
l'activation réelle reste conditionnée à la vérification des vrais positifs
par user. Le rapport final est `docs/wake-eval.md`; procédure de reprise
dans `assets/wake/README.md`. Les critères FR/EN et faux réveils totaux restent
ouverts tant que les positifs réels ne sont pas enregistrés et le compromis
réévalué.

**Correctifs et intégration après revue.** S3 est rebasée sur S2 corrigée
(`81a4e0d`, flush et continuité audio) et réutilise
son module `audio/resample.py` sans changement de son API ; ses tests audio et
WebSocket, le montage `.cache/logs` et ses dépendances sont conservés.
Le wrapper Silero v5.1.2 suit désormais
[`OnnxWrapper.__call__`](https://github.com/snakers4/silero-vad/blob/v5.1.2/src/silero_vad/utils_vad.py) :
64 échantillons de contexte + 512 nouveaux (entrée 576), contexte et état
récurrent remis à zéro ensemble. Les tests unitaires vérifient les entrées
ONNX, le contexte de la fenêtre précédente et les resets explicite/fin de
parole. Le test réel `tests/integration/test_vad_real.py` constate un écart
maximal de probabilité de 0 sur 51 fenêtres et un replay identique après reset.
Le WAV parole/silence produit 1,248 s pour le début et 2,496 s pour la fin
avec maintien (attendus 1,200 s et 2,508 s, tolérance ±100 ms). Modèle et WAV
restent ignorés ; commande, empreintes et conservation dans `assets/wake/README.md`.
Le banc Vosk et son compromis ne sont pas modifiés.

Les cycles `response_active`/`quack_active` sont indépendants : terminer un
cancanement ne termine pas une réponse, et inversement. `speaking` reste actif,
la bouche n'est pas fermée par la fin du seul premier cycle, le barge-in annule
les deux lectures et le réveil ne reprend que 500 ms après la dernière fin.
La fin du cancanement ne réarme pas le délai de suivi démarré par la réponse.

**Seconde correction : H-POSITIFS et provenance.** Les enregistrements humains
vont désormais dans `assets/wake/recorded/session-01`, séparés des anciens TTS
sous `positives/` et `vad/`, qui restent intacts. Le script refuse un répertoire
existant sans `--resume`, enregistre dans un temporaire unique, valide le WAV
et publie sans écrasement par lien dur. Une interruption conserve les prises
complètes et le temporaire ; la reprise explicite avec le même plan enregistre
uniquement les emplacements manquants. Les WAV invalides ou inattendus sont
signalés sans suppression. Le plan est une intention d'enregistrement et non
une preuve d'identité ou de contenu.

Le seul choix `--positive-source recorded` n'est plus suffisant : le banc exige
l'attestation exacte documentée dans `assets/wake/README.md` pour tous les WAV
du dossier. Le rapport la cite comme déclaration utilisateur non vérifiée,
indique les comptes par langue et la couverture des noms près/loin sans
certifier la complétude H-POSITIFS. Il ne prétend pas identifier user,
vérifier la phrase ou la distance. Les imports humains restent possibles.
Les dossiers TTS connus et leurs copies strictement identiques par SHA-256
sont refusés comme humains ; ce contrôle local n'est pas un détecteur général
de parole synthétique. Les prises doivent être écoutées et attestées par la
personne réalisant la validation. Le rapport historique Vosk synthétique et
ses mesures ne sont pas recalculés. `recorded/` est ignoré par Git et tout
`assets/wake/` exclu de Docker pour ne pas envoyer de prises personnelles
dans le contexte de construction.

## ADR-004 — requête `robot.stop`, pas notification ; observer l'EMA

Date : 2026-11-04. Statut : accepté, vérifié contre la simulation réelle.

**Contexte.** Les commandes continues `move`, `head`, `pose`, `mouth`
acceptent des notifications. Cela n'est pas vrai de tous les intents :
`robotd/src/main.rs` route une notification uniquement vers `apply_intent`.
`RobotStop` n'y figure pas ; seul `dispatch` appelle `intents.stop()` et
renvoie `IntentResult`. Un faux serveur trop permissif masquerait ce défaut.

**Décision.** Tous les arrêts du gouverneur sont des requêtes avec `id`,
bornées à 100 ms pour l'accusé de réception. Un refus est remonté tel quel ;
un timeout est explicite. Le bec est fermé sur arrêt et à la connexion.
Une annulation de remplacement ne produit pas d'arrêt tardif de l'ancienne
tâche. STOP invalide aussi les actions encore en attente du homing.
Le faux reproduit l'absence d'effet d'une notification `robot.stop` et les
tests vérifient que l'arrêt effectif est une requête.

**Observation importante.** `robot.stop` met immédiatement la **consigne**
à zéro, mais `state.move.applied` contient l'EMA interne de `robotd`, pas
la consigne brute. De même, le deadman porte la cible à zéro après 500 ms ;
`limited_by=["deadman"]` apparaît avant disparition complète de l'EMA.
La CLI attend au plus 1 s que chaque axe appliqué soit < 0,01 après la fin
de la tâche, sans réémettre `move`. Le test de marche conserve son seuil
> 0,15 m sur la commande de 2 s et vérifie séparément la consigne nulle puis
l'amortissement. La preuve deadman emploie le critère prévu par la spec :
présence de `deadman` en moins d'une seconde (mesuré : 0,496 s).

**Conséquences.** Ni firmware ni code Pollen ne sont modifiés. On ne prétend
pas que l'EMA ou le corps physique passe instantanément à zéro. Un nouvel
essai doit conserver les vrais seuils de distance/angle, pas les remplacer
par la seule réussite d'un RPC.

## ADR-005 — transport partageable et tâches de sécurité indépendantes

Date : 2026-11-04. Statut : accepté.

**Décision.** `JsonRpcConnection` dépend seulement d'un lecteur de lignes et
d'un écrivain asynchrone ; `TcpLink` possède connexion, authentification,
handshake, callbacks et reconnexion. La caméra utilise une connexion
distincte avec la même authentification, car son en-tête est suivi de
binaire. Sa validation reprend `MediaFrameHeader::valid_uyvy` (16 Mio,
géométrie, rotation) avant allocation/décodage ; numpy et Pillow produisent
le JPEG demandé par S7.

Le gouverneur possède un surveillant à 10 Hz, une tâche de mouvement et une
tâche de maintien du regard. La lecture du mode, potentiellement lente,
est une tâche séparée et ne bloque pas la surveillance. Le mode est relu
toutes les 30 s et après reconnexion ; un changement observé interrompt le
mouvement pour ne jamais continuer avec les anciennes bornes.

**Correction de priorité STOP.** La séparation des tâches ne suffisait pas :
`look`, les skills et la revérification de posture attendaient des RPC sous
le verrou partagé avec STOP. Une réponse lente empêchait donc l'envoi de
l'arrêt. STOP ne prend désormais jamais ce verrou. Il incrémente la génération,
annule immédiatement les RPC d'action et les tâches mouvement/regard, puis
envoie la requête d'arrêt avant d'attendre leur nettoyage et de fermer le bec.
Le délai de réponse de l'arrêt reste 100 ms ; la fermeture du bec est tentée
même si l'arrêt est refusé.

Les appels d'action vérifient sécurité et génération juste avant le transport
et après la réponse ; une annulation par STOP devient un refus explicite.
La vérification après attente du verrou empêche aussi une ancienne action en
file de reprendre. Les réponses déjà en vol ne peuvent plus créer de maintien,
enchaîner un skill ou activer le robot. Une commande déjà traitée côté robot
ne peut pas être retirée ; STOP la suit et l'EMA reste observable (ADR-004).
Le verrou sérialise toujours les actions ordinaires, notamment les toggles
assis/debout et les remplacements, mais n'est plus une barrière à la sécurité.

Les tests par événements retiennent les réponses `look`, `skills`, `stop`
avant skill, `do`, `policies` (y compris sous verrou) et `enable`, avec ou
sans annulation effective de la réponse. STOP doit terminer sans libérer la
réponse retenue (< 100 ms), aucune commande d'action ne suit ; watchdog et
action en attente du verrou sont aussi couverts. La reproduction réelle avec
marche et réponse regard retenue mesure l'entrée dans `link.call` STOP en
0,116 ms et vérifie son acquittement en moins de 200 ms, consigne zéro et
refus de l'ancienne action. Le premier délai n'est pas une mesure de
réception réseau ou d'immobilisation physique.

**Conséquences.** Les interfaces S0 sont inchangées ; S9 peut adapter les
flux WebRTC sans recopier corrélation/état. S5 utilise le gouverneur, jamais
la CLI `raw`. Les tests contre la simulation sont en opt-in explicite
(`COINCOIN_TEST_SIM=1`) et lisent alors uniquement le `.env` du dépôt exécuté.
La suite hors sim reste indépendante de tous les vrais secrets.

## ADR-006 — Conversation vocale via Realtime WebSocket

Date : 2026-11-05. Statut : implémenté ; validation vocale humaine en attente.

**Contexte.** S4 doit fournir une conversation vocale bilingue et interruptible
sans exposer de clé dans le navigateur, et enregistrer une trace réelle avant
d'implémenter son client. L'identifiant ADR-004 étant déjà attribué à l'arrêt
robot, cette décision prend le prochain numéro libre plutôt que de dupliquer
ou renommer une décision existante.

**Décision.** Utiliser l'API GA `wss://api.openai.com/v1/realtime` avec la clé
uniquement côté serveur. La trace réelle, capturée avant l'implémentation du
client, utilise le modèle `gpt-realtime-2.1`, la voix provisoire `marin`,
l'audio PCM16 mono 24 kHz, la transcription d'entrée et un outil `ping`
factice. Elle a confirmé ces événements serveur : `session.created`,
`session.updated`, `input_audio_buffer.committed`,
`conversation.item.added`, `conversation.item.done`,
`conversation.item.input_audio_transcription.completed`,
`response.created`, `response.function_call_arguments.delta`,
`response.function_call_arguments.done`, `response.output_audio.delta`,
`response.output_audio.done`, `response.output_audio_transcript.delta`,
`response.output_audio_transcript.done`, `response.output_item.added`,
`response.output_item.done`, `response.done` et
`conversation.item.truncated`, ainsi que les événements de partie de contenu
et de limite de débit. Le client reconnaît aussi les évènements VAD GA
`input_audio_buffer.speech_started` et `input_audio_buffer.speech_stopped`,
conformément à la documentation Realtime ; le WAV de trace utilisait un
commit explicite et ne les a donc pas émis. Après un résultat d'outil, le
client demande explicitement la réponse suivante. En interruption, il capture
l'item et sa progression avant le flush, envoie `response.cancel`, puis tronque
à la durée réellement jouée.

La fixture `tests/fakes/fixtures/realtime_trace.jsonl` conserve les 77
événements serveur de la trace ; la sortie audio est limitée à 80 ms et
aucune trame micro, clé ou donnée personnelle n'y est enregistrée. L'interruption
de la trace API a utilisé `audio_end_ms=0`, car aucun son n'était lu par le
script ; cela confirme le protocole, pas le silence physique d'un haut-parleur.

**Conséquences.** La session est ouverte uniquement après le réveil local ;
aucun audio n'est envoyé en sommeil et la conversation n'appelle aucun outil
robot. Les features ajoutent leurs outils, hooks et contextes d'instructions
via `App` et la liste `FEATURES`. Une perte de connexion déclenche une
reconnexion bornée ; une session est renouvelée une minute avant expiration
avec un résumé de conversation court et non fiable injecté comme contexte.
La voix `marin` est un choix de travail, pas une sélection humaine validée.
FR/EN, le barge-in sous 500 ms et le retour en sommeil après 7 minutes restent
à écouter/vérifier par user.

**Corrections après revue S4.** Le hook `stop` est initié avant le flush,
l'interface et l'annulation Realtime ; le flush et l'annulation ne dépendent pas
de leur achèvement. Les ouvertures et travaux audio/VAD anciens sont invalidés
au coucher, les trames micro gardent leur ordre, et la troncature utilise
l'identité d'item et le nombre d'échantillons réellement joués capturés avant
flush. Les outils s'exécutent hors du consommateur d'évènements ; leurs refus
valides sont conservés. Une perte TCP locale a été testée avec un port refusé,
sans appel externe. Les régressions App/UI/transport et la suite complète
valident les compteurs audio uniquement après acceptation par le transport,
l'état de parole synchronisé à la connexion, les hooks de sommeil et les
erreurs UI bornées. Les vérifications humaines ci-dessus restent ouvertes.

**Seconde correction après revue de `5cc8a42`.** STOP ne joue aucun son et
réarme le réveil après 500 ms, même s'il était déjà en sommeil. À l'arrêt du
service, le hook `stop` a une échéance de 1 s, indépendante de l'UI et de Realtime ;
son timeout/échec est journalisé avant l'annulation générale des tâches.
L'invalidation n'annule pas sa propre tâche : l'outil `sleep` termine les hooks,
le coucher et le journal de résultat sans reprendre une session fermée.

`response.output_item.done.response_id`, présent dans la trace initiale, est
conservé pour corréler chaque outil au parent. Une interruption invalide
immédiatement les appels en cours et les appels tardifs. Un lot d'outils ne
reprend la génération qu'après `response.done completed` du parent et tous
les résultats, transmis dans l'ordre initial des appels, avec un unique
`response.create`. `send_tool_result()` envoie seulement l'output ;
`create_response()` est désormais explicite. La
[documentation Realtime actuelle](https://developers.openai.com/api/docs/guides/realtime-conversations#function-calling)
confirme le séquencement `function_call_output` puis `response.create` ;
aucune nouvelle trace API n'a été créée pour tester les ordonnancements.
Les faux couvrent les deux ordres de fin lent/rapide, leur interruption et
l'échec d'envoi d'un résultat. Les transcriptions finales sont journalisées
dès leur réception, y compris pour un parent avec outils : ce sont des textes
générés, pas une preuve de ce qui a été entendu.

L'extension optionnelle `TrackedPlayback` de `AudioIO` expose
`PlaybackProgress(epoch, sent_samples, played_samples)`. BrowserAudio change
d'epoch au flush et au remplacement/débranchement de page ; le message worklet
`played` transporte aussi `samples`, relayé par la vraie route `/ws/ui`.
Les anciens rapports millisecondes restent acceptés, mais les queues
sous-milliseconde exigent le champ exact de la page/worklet mis à jour.
La fin de réponse n'arrondit plus sa cible au-dessus du compteur disponible
et n'annonce pas un succès sur timeout ou perte de page. Les intervalles
d'items sont semi-ouverts : à la frontière exacte, le suivant est à zéro.
Un bloc déjà en file ou un delta tardif d'une ancienne page n'est pas rejoué
sur la nouvelle ; une nouvelle réponse utilise ses nouveaux compteurs.

Le hook `audio_out(pcm, play_at_ms)` est initié à l'appel effectif `AudioIO.play`,
avec la position prévue dans la file de lecture, dans une tâche indépendante.
Un hook lent n'arrête ni le consommateur Realtime ni le flush ; les hooks
anciens sont annulés à l'interruption. L'upgrade WebSocket est lui aussi
soumis à `_OPEN_TIMEOUT_S`, avec fermeture et erreur réseau normalisée avant
le backoff. STOP hors ligne conserve `BrowserAudio.flush()` comme écho/tone.

**Preuves de seconde correction.** Commande qualité complète verte :
389 tests réussis, 3 simulations ignorées, couverture 90,91 %, aucun diagnostic
ruff/Pyright. Les 23 cas de `tests/integration/test_s4_followup.py` utilisent
App, BrowserAudio, les routes UI/audio, le faux serveur Realtime et une socket
TCP locale bloquant l'upgrade. Le harness du vrai worklet passe 6/6 et vérifie
explicitement 25 échantillons joués pour seulement 1 ms entière rapportée.
La trace réelle initiale reste inchangée ; aucune sim, connexion robot ou
nouvelle requête OpenAI. Cela ne lève pas les validations humaines ni la
limite du silence physique d'un navigateur injoignable.

## ADR-007 — Mémoire OpenClaw : client testable, hooks non bloquants, phrase d'attente

Date : 2026-11-05. Statut : retenu (S8, sur `main=051eb33`).

**Contexte.** Le contrat OpenAI-compatible d'OpenClaw est documenté
publiquement, mais la gateway privée de user n'est pas configurée. Les
tests ne doivent ni dépendre du réseau externe ni prétendre à une observation
réelle. S4 fournit `Feature`, les hooks `brain_session_start` (attendu avant
l'ouverture Realtime) et `brain_session_end`, le registre d'outils et
`instructions_context`. `app.py` appartient à S2/S4 : S8 ne le modifie pas.

**Décision.**

1. Client HTTPX asynchrone sur `POST /v1/chat/completions`, modèle
   `openclaw/<id>`, `user` stable `coin-coin-voice`, testé seulement contre un
   faux aiohttp loopback. `NullBrain` sans jeton. Échéances 3/8/15 s ; réseau,
   429 et 5xx : deux reprises, backoff exponentiel borné avec gigue ; 401/403 et
   409 sont typés et deviennent des phrases explicites ; le dernier échec est
   exposé (`last_failure`) pour bâtir le contexte sans mémoire.
2. `BrainFeature` n'utilise que l'API publique S4. Au réveil il borne
   `session_start` et capture toute exception : la mémoire ne peut jamais
   empêcher l'ouverture Realtime. Au coucher, le hook ne fait que planifier une
   tâche conservée par la feature (`drain`/`aclose` pour l'attendre).
3. Le résumé de fin est construit localement depuis le journal : un marqueur
   `type=brain conversation=<id>` est écrit au réveil, puis la tâche relit les
   transcriptions finales et `tool_result` postérieurs du même processus. Cela
   évite de lire l'historique privé de l'App, qui couvre aussi les sessions
   précédentes, et d'éditer `app.py`. Le `summary` de l'effet S4 (vide
   aujourd'hui) est préfixé s'il est fourni.
4. Phrase d'attente : Realtime ne permet pas de remplacer un
   `function_call_output` déjà envoyé, et S4 n'envoie qu'une reprise par lot
   après `response.done`. Un résultat intermédiaire « je réfléchis » serait donc
   la réponse définitive de l'outil, et une réponse parallèle déclenchée à 1,5 s
   entrerait en concurrence avec le lot d'outils, le barge-in et STOP. La
   description de `ask_openclaw` demande au modèle de dire une courte phrase
   d'attente dans la même réponse, avant l'appel ; elle est prononcée pendant
   l'attente, mais pas conditionnée à 1,5 s. Les appels > 1,5 s sont
   journalisés (`status=slow`).

**Sécurité.** Le bearer token est un credential opérateur complet : agent à
outils minimaux, gateway privée ou tunnel SSH. Aucun jeton ni corps d'erreur
distant n'est journalisé (tests). Les notes mémoire sont injectées comme faits
d'arrière-plan, pas comme instructions.

**Conséquences.** Les transcriptions des sessions terminées sont relues depuis
`logs/` ; si le journal est illisible, le résumé ne contient que le `summary`
fourni, et un résumé vide n'est pas envoyé. Un arrêt du service pendant la
tâche de fond peut perdre ce résumé. L'API réelle reste non observée : la forme
réelle de `/v1/models` et d'une complétion doit être consignée dans
[`docs/openclaw.md`](openclaw.md) après la procédure de user.

## ADR-008 — Mesurer la marche après stabilisation de l'odométrie

**Statut : accepté.** `governor.wait_motion()` attend l'acquittement de
`robot.stop`, pas la fin de l'amortissement de la vitesse appliquée. Le test
de marche lisait l'odométrie après un délai fixe de 300 ms, puis vérifiait
seulement ensuite `applied < 0,01`. Une trace instrumentée a observé
`applied=0,2 m/s` au retour de `wait_motion()`, puis une décroissance jusqu'à
moins de `0,01` environ 0,4 s plus tard, pendant que l'odométrie avançait et
se stabilisait.

**Décision.** Avant et après la commande inchangée `move(0.2, 0, 0, 2)`,
attendre un état reçu depuis moins de 200 ms, une vitesse appliquée sous
`0,01` sur chaque axe et deux écarts consécutifs entre mises à jour XY d'au
plus 5 mm. La limite d'attente reste bornée à 1 s. Le test conserve le seuil strict
`> 0,15 m`; il n'ajoute ni relance ni sélection d'essai.

**Conséquences et limites.** Le test rapporte le déplacement odométrique
stabilisé après une commande de 2 s, ce qui inclut la décélération après STOP ;
il ne prétend pas interpoler la pose à l'instant exact `t=2,000 s`. Les
lectures de corps MuJoCo et l'odométrie ne sont pas horodatées de façon
synchronisée par le protocole actuel. Aucun changement de code Pollen, de
politique, de vitesse, de durée ou de seuil n'est autorisé par cette décision.
La campagne documentée a réussi 10 exécutions sur 10 (0,159–0,175 m) ; cela
fiabilise ce contrôle odométrique sans constituer une mesure indépendante du
corps physique.

## ADR-S5-001 — Outils gouvernés et annonces dans la reprise unique

Date : 2026-11-05. Statut : implémenté ; correctif de course vérifié, contre-vérification requise.

**Décision.** `RobotFeature` installe un runtime propre à chaque application,
avec `TcpLink`, le gouverneur existant et ses tâches. Les onze outils S5
délèguent l'actuation au gouverneur ; aucun appel brut robot n'est exposé.
Les schémas précisent les bornes, directions et variantes de regard. Les
arguments inconnus, booléens numériques, valeurs non finies et formes
incomplètes sont refusés avant transport. Les valeurs numériques hors plage
restent bornées une seconde fois par le gouverneur, qui rend `clamped`.
Les figures du contexte et de l'enum sont relues à chaque ouverture de session ;
un nom absent rend la liste disponible, sans exposer `sit_toggle`.
`NullQuack` journalise explicitement le stub, sans son ni `robot.sound`.

**Extensions S4 nécessaires, acceptées par le coordonnateur.** App publie les
actions/refus via un envoi UI borné et émet `shutdown` après son arrêt prioritaire
et l'annulation des tâches de conversation. Le runtime ferme alors gouverneur
et transport. Le registre par défaut est résolu lors de la construction de
l'application ; les anciens tests hors robot le remplacent par une liste vide
pour ne jamais contacter une ressource réelle. Les tests S5 installent leur
propre feature avec des transports locaux.

Une perte de lien est journalisée et annoncée une seule fois par coupure.
`RealtimeSession.announce` utilise un `response.create` avec instructions de
réponse et `tool_choice:none` selon le
[contrat des événements client](https://developers.openai.com/api/reference/resources/realtime/client-events#response.create).
Si une réponse est active, App garde l'annonce en attente : soit elle remplace
les instructions de l'unique reprise après tous les outputs du lot et la fin
du parent, soit elle attend la fin de lecture de la réponse ordinaire.
Un parent échoué peut être suivi de l'annonce, sans rejouer ses outils.
L'annonce n'invente jamais un `function_call_output` et ne modifie pas
`send_tool_result`. STOP invalide l'annonce en attente et annule aussi un
envoi déjà bloqué. Les hooks `audio_out` et le transport audio sont inchangés.
Cette annonce peut être différée par une réponse/lecture en cours ; on ne
prétend pas produire du son si le navigateur ou Realtime est indisponible.

**Extension S1 directement liée à S5.** `Governor.status` garde son contrat
`ToolResult`, mais rend un résumé court FR/EN, notamment tombé/soulevé/périmé
et mouvement actif, au lieu de lire un dictionnaire de santé à voix haute.
Le détail complet reste dans `robot_status`. « Tout va bien » exige santé,
politique active/homée, mode connu et état frais sans drapeau de danger.
`move_to_first_command_ms` mesure l'appel du gouverneur jusqu'à l'envoi de la
première consigne ; ce n'est pas une mesure de locomotion physique.
Aucune fréquence, durée, borne de sécurité, mécanique d'arrêt ou seuil sim
n'a changé.

**Preuves et limites.** Ruff/format/Pyright verts ; 419 réussis/3 sim ignorés,
couverture 91,33 %, dont 30 cas S5 ; deux tests Silero réels et vrai worklet 6/6.
La trace S4 est inchangée, sans nouvel appel réel OpenAI.
Le premier passage réel S5 donne 3/3 (0,156 m, 89,491°, 0,796 rad) ;
le dernier donne 1 échec/2 succès : 0,120359637 m au lieu de > 0,15 m,
89,390° et 0,797 rad. Aucun diagnostic causal n'est établi. On conserve
le blocage et les seuils, sans campagne S9 ni correction Pollen.
Tous les services S5 sont arrêtés. Les essais humains restent ouverts.

## ADR-S6-001 — Mixer partagé et runtime vocal par application

Date : 2026-11-05. Statut : implémenté ; validation automatisée et humaine en cours.

**Contexte.** Les cancanements simulés doivent rester dans la sortie du navigateur
pour appartenir à la référence AEC, sans fausser le compte d'échantillons de la
voix Realtime ni retarder son démarrage. L'annulation d'un son ne doit pas vider
les trames vocales déjà mises en file.

**Décision.** `OutputMixer` additionne la voix et `StreamQuack` à la sortie
physique. Son horloge virtuelle expose à Realtime uniquement les échantillons de
voix. L'annulation supprime le son non envoyé et ne vide la sortie physique que
s'il n'y a pas de voix en attente ; un cancanement déjà mixé dans des trames
émises peut donc subsister jusqu'à leur lecture si une voix est en file. STOP et
barge-in utilisent le flush global existant. `RobotQuack` n'est choisi que par
`QUACK_SINK=robot`.

Chaque installation `VoiceFeature` crée un `_VoiceRuntime` propre à son `App`,
comme `RobotFeature`. Les positions du bec proviennent du PCM effectivement
émis, sont différées selon `played_ms` et les positions en retard sont écartées
au lieu d'être rejouées en rafale. Une remise à zéro de l'horloge vide aussi les
anciennes fenêtres.

**Conséquences et validation restante.** Une banque absente journalise une seule
erreur et n'empêche pas la conversation. Aucun seuil d'enveloppe ou de cadence
n'est modifié. Les tests ciblés d'enveloppe, de mixage, d'annulation et du cycle
de vie App passent : **22 réussis** ; ruff/format sur les fichiers touchés et
Pyright ciblé sont verts. La commande qualité complète passe : ruff/format/
Pyright verts, **479 tests réussis, 5 ignorés**, couverture **91,78 %**.
Le passage sim autorisé réussit **3 tests** (motion : 0,177 m, 87,710°,
0,796 rad), mais ne mesure pas le bec ni les cancanements.

La première commande `duck-sim down` n'avait pas reçu `DUCK_SIM_PORT=7861` et
a laissé le body server. Son PID et sa ligne de commande S6 ont été vérifiés
avant l'arrêt ciblé ; `down` a ensuite été rejoué avec le port attribué. Le
relais a également été arrêté. Vérification finale : aucun listener sur les
ports S6 `7861`, `7961`, `7870`, `7871`, `8776`, aucun pidfile à la racine de
`ds-s6`. Docker Desktop étant arrêté, Compose n'a pas été lancé. L'essai MuJoCo
requis par la spec doit confirmer visuellement le bec et audiblement les trois
automatiques, sans faux réveil ni faux barge-in ; aucun critère humain n'est
déclaré réussi avant mesure.

**Revalidation après rebase sur `cc30372` (S8 et ADR-008 SIMWALK).** Le blocage
historique ci-dessus est levé par un passage complet unique, sans sélection
ni rejeu : **3 passed, 447 deselected**, avance **0,162 m**, tour **89,222°**,
regard **0,795 rad**. Le helper test-only de main attend une odométrie fraîche
et stabilisée après STOP, consigne 0,2 m/s pendant 2 s et seuil > 0,15 m
inchangés. Aucun autre test sim S5 ne nécessitait une adaptation.
Qualité complète : **447 passed, 3 skipped**, couverture **91,72 %** ;
ruff/format/Pyright verts, worklet 6/6, deux tests Silero réels exécutés.
Les deux features S8/S5 sont conservées et les ADR-007/008 restent intacts.
Les preuves initiales et la revalidation sont dans `docs/evidence/s5/`.

**Correction après refus de `46bdee4`.** La transition de fin de réponse rend
`response_active=False` avant d'attendre `mouth_close`. Pendant cette attente,
`announce()` et la fin normale/échouée pouvaient programmer deux reprises ;
la seconde, après consommation de l'annonce, émettait une réponse normale.
La reprise sans annonce est désormais réservée à un `_ToolTurn` valide.
Une tâche d'annonce sans message ou avec une réponse déjà active ne fait rien.
Le changement d'état `response_started` réserve la réponse avant son premier
`await`, sans verrou qui retarderait STOP.
Après l'attente UI de cette transition, l'identité de session, sa génération
et l'identité de réponse sont revérifiées avant l'envoi : une nouvelle
réponse/session garde l'annonce en attente au lieu de créer une réponse
concurrente ou d'envoyer sur l'ancienne session.

Le message n'est acquitté qu'après l'envoi réussi. Une annulation par barge-in
avant cet envoi le conserve pour la prochaine réponse ; STOP le supprime et
annule toujours la tâche. Un numéro de version empêche un ancien envoi,
réussi ou échoué, d'effacer une nouvelle perte portant le même texte.
Une erreur de transport efface uniquement sa propre annonce et reste
journalisée : pas de nouvelle boucle de réessai. Le lot conserve son ordre,
son unique continuation et les annulations S4 ; ni `send_tool_result`, ni
`audio_out`, ni les tests/seuils sim ne changent.

Douze régressions supplémentaires sans réseau robot/OpenAI, dont l'ordonnancement
exact du bec bloqué : reproduction rouge **2 échecs** avant correction ;
**80 tests ciblés réussis** après correction. Qualité complète : **459 passed,
3 skipped**, couverture **91,78 %**, ruff/format/Pyright verts et Node **6/6**.
Pas de nouvelle sim : preuve 3/3 SIMWALK conservée, contre-vérification sim
du coordonnateur également rapportée (0,162 m, 89,033°, 0,795 rad).
S5 reste non intégrée et les essais humains ouverts.
Ressources S5 arrêtées, aucun nouvel appel OpenAI, Pollen inchangé.
Cette mesure reste odométrique et inclut la décélération ; intégration et
validation vocale humaine ne sont pas revendiquées.

## ADR-S7-001 — Image utilisateur et continuation du lot d'outils

Date : 2026-11-05. Statut : accepté ; validation caméra sim en attente.

Le guide officiel
[Realtime conversations — image inputs](https://developers.openai.com/api/docs/guides/realtime-conversations)
a été consulté avant l'implémentation : `conversation.item.create` avec un
message utilisateur `input_image`, `image_url: data:image/jpeg;base64,...`,
et `input_text` facultatif. `RealtimeSession.add_image` ajoute seulement cet
item ; il ne crée pas de réponse. Le lot S5 envoie ensuite les outputs ordonnés
et demande une seule réponse après la fin du parent. Aucun nouvel appel réel
OpenAI : validation exclusivement contre le faux WebSocket.

`see` est installé par le runtime `RobotFeature`, avec son `RobotLink` existant,
sans deuxième connexion de commande ni nouvelle feature singleton. Chaque
capture attend au maximum 2 s ; une image périmée ou datée du futur provoque
une seule nouvelle capture. La compression travaille hors de la boucle asyncio,
réduit la largeur puis la qualité et, si nécessaire, les dimensions pour garantir
150 000 octets même sur une image bruitée très haute. L'identité de session et
l'état actif sont revérifiés après ces attentes ; l'annulation est propagée.
Le correctif de revue lie aussi la capture à la génération de session et au
tour S5 d'origine : une nouvelle réponse dans la même session invalide l'image.
`add_image` accepte un garde facultatif, évalué sous son verrou d'envoi avant
`send_json`, et signale explicitement un rejet à l'outil. Les contrats App/S5
restent inchangés. Une fois le message remis au transport, il n'est pas rétractable.
Six régressions avec le vrai App couvrent capture, préparation et attente du
verrou, chacune avec remplacement de réponse et témoin inchangé :
3 échecs/3 succès avant correction, puis 6 succès.
Le journal ne reçoit jamais les octets ni l'URL de données de l'image :
`LOG_IMAGES=false` est la politique de cette version, sans stockage opt-in.

## ADR-006 — WebRTC LAN pour le robot, TCP relay inchangé

Date : 2026-11-04. Statut : Q10 acceptée ; acceptation S9 bloquée sur la
distance de marche stricte de 2 s (après correction de revue).

**Contexte.** S9 doit piloter le `mediad` réel avant la réception du robot,
réutiliser les contrats JSON-RPC de S1 et vérifier l'accessibilité ICE depuis
WSL et un conteneur Docker Desktop. La signalisation LAN de `mediad` n'a pas
d'authentification ; le relais TCP S1, lui, exige son jeton.

**Décision.** Choisir le transport à partir du schéma de `ROBOT_LINK` :
`webrtc://host:8443` utilise `WebRtcLink`; `tcp://host:port` conserve
`TcpLink`, le relais et le jeton. L'adaptation WebRTC alimente le
`JsonRpcConnection` de S1, conserve `robot.stop` comme requête à ID et n'ajoute
pas de file d'attente historique pour les `robot.move` : un seul envoi récent
est conservé tant que `bufferedAmount` n'est pas nul. STOP invalide cet envoi
avant son propre envoi ; les demandes explicites postérieures restent
possibles. Le seuil antérieur de 16 KiB est retiré car il permettait
l'accumulation dans le tampon aiortc lui-même. Un message déjà admis ne peut
pas être rétracté sur le canal fiable ; la livraison ou l'arrêt physique ne
sont pas garantis en saturation. L'absence d'acquittement STOP dans les
100 ms produit un échec explicite du gouverneur, jamais une réussite.
La réponse SDP aiortc est `passive`; l'`active` par défaut a atteint ICE mais a échoué au
handshake DTLS contre le `webrtcsink` de la simulation.

**Correction de frontière S1, 2026-11-05.** Contrairement au lien TCP,
WebRTC ne traverse pas `duck-relay`. La datachannel applique donc désormais
sa liste de méthodes avant transmission, y compris pour `coincoin-cli raw`
et les notifications. La liste S1 inchangée est extraite dans un module
stdlib partagé, livré dans le paquet Coin-Coin ; le relais conserve son
exécution autonome dans le dépôt. Un refus est explicite (`-32601`) sans
journaliser de paramètres. Tests intégrés sur faux pair pour les méthodes
interdites et permises, et CLI/TCP/relais inchangés. Ce contrôle n'est pas un
gouverneur de paramètres et ne sécurise pas les autres clients de `mediad`.

**Mesures rectifiées après revue.** Base `main` `0597380`. La marche est
revenue à 2 s pour les deux scénarios, sans modifier le seuil > 0,15 m ;
regard maintenu 1 s et contrôlé après 0,6 s. Commande complète sim :
**6 passed, 1 failed, aucun saut** ; S1 avance 0,160 m, S9 0,143831 m
(échec), tours 89,645°/89,536°, regards 0,352/0,610 rad. Les dix marches
comparatives supplémentaires donnent 2 réussites et 8 échecs malgré
40 envois par essai et STOP après 2,000–2,001 s, sans backlog observé ;
aucune cause logicielle n'est démontrée. La preuve antérieure à 3 s est
retirée comme critère d'acceptation.

La comparaison finale des transports réels sur la même sim caméra, avec
ordre équilibré et scénario inchangé, donne TCP 3/4 réussites
(0,143836–0,170451 m) et WebRTC 3/4 (0,143790–0,161683 m). Chaque essai
émet 40 moves avec STOP après 2,000–2,001 s, sans deadman. Le seuil reste
non fiable dans les deux transports. Conditions au repos comparables,
sans reset physique ou de phase ; cette petite série ne prouve pas une
équivalence statistique ni une cause, et ne justifie aucun changement
logiciel spéculatif. Le diagnostic et ses limites sont dans `webrtc.md`.

RTT `robot.health` p50/p95 0,51/0,65 ms ; nominal 20 Hz, cadence observée
**19,77 Hz** calculée sur 50 changements de consigne pendant 2,479 s ;
intervalle p50 42,17 ms et gigue absolue p95 29,30 ms, aucun `deadman`.
WSL et le bridge Docker par défaut ont réussi `status`; le déplacement
Docker de 2 s a avancé de 0,128 m (contrôle réseau, pas acceptation distance). HTTP
/healthz répond 200 sur 8779. `network_mode: host` n'a pas été nécessaire.
Le mapping `127.0.0.1:8779->8765/tcp` n'ouvre pas le port hôte 8765.

Le détail de signalisation, l'échec NVENC et le rangage logiciel requis sur
cet hôte, les commandes et le nettoyage sont dans [docs/webrtc.md](webrtc.md).
