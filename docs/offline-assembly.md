# Régression de l'assemblage hors réseau externe

`tests/integration/test_assembled_app.py` couvre l'installation conjointe de
`BrainFeature`, `RobotFeature` et `VoiceFeature` par `App.start()`, déclenché
par le démarrage des vraies routes web. La liste de production et son ordre
sont vérifiés ; le remplacement de `FEATURES` reste local au test. L'isolation
globale de `tests/conftest.py` est inchangée.

Depuis le worktree, sans `.env`, modèle de réveil, banque de sons ni service
préexistant :

```sh
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/v20.20.1/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
uv run pytest --no-cov -q tests/integration/test_assembled_app.py
```

Le scénario est exécuté deux fois : coucher par l'outil `sleep`, puis STOP par
`/ws/ui` pendant un mouvement factice. Chaque variante vérifie :

- aucun audio Realtime ni appel mémoire avant le réveil contrôlé ; instructions
  enrichies par le contexte mémoire et les figures du faux robot ;
- micro PCM acheminé par `/ws/audio`, vrai `BrowserAudio`, `OutputMixer`,
  vrais clients Realtime/OpenClaw et `TcpLink` vers des pairs locaux ;
- lot `move`, `see`, `remember`, `ask_openclaw`, `quack` : image utilisateur
  avant son résultat, outputs ordonnés et une seule continuation ;
- WAV synthétiques joués par `StreamQuack`, animation du bec, absence de
  `quack_stub` et de `robot.sound`, transcriptions vers la page et le journal ;
- interruption d'une réponse : flush acquitté, cancel, troncature à exactement
  40 ms de voix jouée (les deux cancanements de 200 ms ne comptent pas),
  ancien audio rejeté ;
- arrêt du mouvement factice, bec fermé, `coo` seulement au coucher normal,
  résumé mémoire local, fermeture Realtime, absence d'audio sortant en sommeil ;
- arrêt de l'application et des pairs, aucune nouvelle tâche asyncio survivante,
  aucun client robot ni listener du faux robot, aucun pixel/jeton dans le journal.

## Frontières et limites

Les services écoutent uniquement sur loopback et des ports éphémères. Les
faux réutilisent `fake_realtime` (handshake GA, expiration actualisée pour le
test), `fake_openclaw`, `fake_robotd` et `CameraLink`. La caméra est injectée
à la frontière `TcpLink.frame()` : le JPEG 4x4 est synthétique ; ce test ne
couvre ni le protocole binaire de capture ni un relais caméra. Les tests
`test_see_tool.py` couvrent séparément cette frontière.

Le détecteur de réveil et le VAD sont contrôlés, et le pair navigateur Python
acquitte la progression et les barrières de flush. Ce n'est pas un navigateur
JavaScript, un AudioWorklet, une mesure de latence physique, un test AEC,
une reconnaissance acoustique ou une validation humaine. Aucun transport
WebRTC, simulateur, robot réel, Docker ou appel OpenAI/OpenClaw réel n'est lancé.
Les réserves S9 et les critères humains existants ne sont pas levés.

Le teardown maintient le pair UI pendant `App.stop()` pour acquitter son dernier
flush, ferme explicitement la mémoire avec `BrainFeature.aclose()` (contrat
existant du harness mémoire), puis ferme sockets et serveurs. Il ne prétend
pas ajouter un hook de fermeture mémoire à la production. Les médias et
journaux sont créés uniquement sous `tmp_path`.

## Vérification ciblée du 2026-11-05

```sh
uv run ruff check tests/integration/test_assembled_app.py
uv run ruff format --check tests/integration/test_assembled_app.py
NODE_OPTIONS=--jitless uv run pyright tests/integration/test_assembled_app.py
uv run pytest --no-cov -q -W error::ResourceWarning \
  tests/integration/test_assembled_app.py \
  tests/integration/test_voice_feature.py \
  tests/integration/test_see_tool.py \
  tests/integration/test_memory_flow.py \
  tests/integration/test_ws_audio.py \
  tests/integration/test_tools_e2e_fakes.py
```

Ruff/format verts, Pyright **0 erreur**, **86 tests réussis**. Le premier
Pyright utilisant Node v20.20.1 avec JIT a rencontré un crash natif V8
(`unreachable code`, sortie 251) ; l'option locale `--jitless` permet la
vérification ciblée sans modifier le runtime partagé ni la configuration.
À cette étape initiale, la qualité complète attendait le créneau de coordination.
La campagne de revue autorisée ci-dessous la remplace comme preuve actuelle.

## Correctif de revue : fin des effets et réception TCP

Base assignée : `f799586`, après rebase par la coordination sur `cee02a2`.
L'assertion « dernier `robot.mouth` reçu = 0 au coucher » était exécutée trop
tôt : le passage à `speaking=False` précède les effets `MouthClose` de
`quack_done`. La session Realtime est déjà fermée pendant le `coo` de coucher ;
ni sa fermeture ni la fin des tâches d'arrêt robot ne garantit donc la fin
du cancanement et de ses effets. De plus, `robot.mouth` est une notification :
le retour de l'envoi ne vaut pas acquittement de réception.

La reproduction est désormais déterministe dans la variante `sleep`, sans
temporisation arbitraire ni nouvelle tentative :

1. Le pair navigateur retient le dernier `played` du `coo` jusqu'à réception
   d'une ouverture de bec non nulle sur le faux robot.
2. Après libération de `played`, le faux robot retient la première fermeture
   finale à zéro avant de l'enregistrer. À cette barrière, le test prouve que
   l'ancien prédicat (`speaking=False`, tâches d'arrêt vides) est vrai, que
   la dernière ouverture reçue est encore non nulle, et que le hook final
   n'est pas terminé. C'est l'ordre d'observation fautif, pas une ouverture
   tardive après la fermeture finale.
3. Le test libère la notification. Un témoin `mouth_close`, enregistré après
   les handlers réels des features, attend une réponse `robot.health` sur le
   même flux TCP : les notifications précédentes ont alors été traitées.
   L'assertion stricte « dernier `mouth` reçu = 0 » reste inchangée.
4. Après le réarmement effectif du réveil (temporisation de production
   existante), une seconde barrière RPC vérifie de nouveau le dernier zéro :
   aucune animation restante ne doit rouvrir le bec.

Les barrières sont bornées et libérées aussi en teardown d'échec. STOP utilise
le même témoin de fin et la même barrière TCP. Aucun fichier de production,
seuil, comportement de coucher ou gouverneur n'est modifié.

**Preuve de stabilité autorisée.** Vingt exécutions consécutives de
`uv run pytest`, sans `--no-cov`, relance sur échec ni sélection de tests :
**20/20 vertes**, chacune **582 passed, 10 skipped**, soit 11 640 tests
réussis et aucun échec. Couverture totale **91,72–91,78 %** (seuil 80 %),
durée **33,59–36,05 s** par suite. Les sauts existants concernent les tests
sim hors opt-in et les assets Silero réels absents de ce worktree.

Contrôles complets `uv run ruff check .`, `uv run ruff format --check .`,
`NODE_OPTIONS=--jitless uv run pyright` : verts, **120 fichiers formatés**,
**0 erreur Pyright**. Aucun simulateur ni API réelle lancé. Les assertions
de fin des deux variantes vérifient toujours les tâches, clients et listeners.
Les 20 journaux complets sont conservés dans le dossier d'artefacts de la
session, `files/pre-assembly-review-1/pytest-1.log` à `pytest-20.log`.
