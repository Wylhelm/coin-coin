# Changelog

## Pré-réception — procédure assemblée et actifs locaux — 2026-11-05

- Procédure actuelle depuis `main=e7cb24f` : modèles, sons, configuration
  locale sans secrets, contrôles hors réseau et futur lancement/arrêt autorisé.
  Les anciens comptes rendus restent identifiés comme historiques.
- S5/S8 intégrées côté agent, S6/S7 présentes ; `quack` n'est plus un stub
  dans la livraison assemblée. S9 intégrée sous réserves, TCP reste le défaut,
  6 réussites/2 échecs de marche conservés ; critères humains et S10 ouverts.
- Banque S6 copiée sans écrasement vers le clone durable et PRE-DELIVERY :
  83 fichiers dont 82 WAV PCM16, correspondance chemin/SHA-256 complète.
  Modèles et sons restent ignorés par Git ; aucun secret lu ou affiché.
- Aucun changement fonctionnel, service, Docker/Compose, nouvel essai sim
  ou appel externe. Les commandes documentées ne sont pas des essais exécutés.

## PRE-LATENCY — Observation move vers applied reçu — 2026-11-05

- Conserve `move_to_first_command_ms` ; ajoute `move_to_first_applied_ms`,
  horodatée à la réception locale de l'état admissible, préparation comprise.
  `move_id` corrèle appel et métriques. Aucun constat de mouvement physique.
- Départ au repos frais, état post-envoi, consigne bornée correspondante et
  `applied` fini non nul compatible ; indisponibilité explicite sinon.
  Observation bornée à 1 s/durée, invalidée sur STOP, remplacement (même
  consigne comprise), perte ou fermeture, sans tâche supplémentaire.
- Tests déterministes contre un cache à états poussés explicitement :
  retard/péremption, préparation, résidus, axes/valeurs invalides, absence
  d'état, échéance, remplacements et annulations. Transport, scénarios sim,
  seuils d'acceptation et dépendances inchangés ; aucune exécution réelle.
- Vérification ciblée : **99 passed** (`test_move_latency.py` et
  `test_governor.py`, sans couverture globale), Ruff/format et Pyright des
  trois fichiers Python concernés verts. Qualité complète à sérialiser
  avec le coordonnateur, pas encore exécutée pour cette livraison.

## Assemblage protocolaire hors réseau externe — 2026-11-05

- Correctif de revue : reproduction déterministe de l'observation prématurée
  du bec pendant la fin du `coo`. Le test attend désormais la fin des hooks
  et une barrière RPC sur le même TCP avant l'assertion stricte du dernier
  zéro, puis la répète après réarmement du réveil ; aucune modification
  production ni temporisation arbitraire.
- Campagne de stabilité autorisée : **20 suites complètes consécutives**
  avec couverture, chacune **582 réussis, 10 sauts, zéro échec** ;
  couverture **91,72–91,78 %**, Ruff/format/Pyright complets verts.
- Régression des trois features installées ensemble par `App.start()`, vraies
  routes web, `BrowserAudio`, mixer et clients TCP/Realtime/OpenClaw contre
  des pairs locaux sur ports éphémères, caméra injectée au niveau `Frame`.
- Deux variantes : réveil contrôlé, outils/vision/mémoire/audio, interruption
  avec troncature exacte hors cancanements, coucher par `sleep` ou STOP UI
  pendant un mouvement factice, résumé et nettoyage des tâches/sockets.
- WAV/JPEG synthétiques uniquement, isolation globale `FEATURES` conservée,
  aucun changement production/gouverneur, sim, robot, Docker ou API réelle.
  Portée et commande : [docs/offline-assembly.md](docs/offline-assembly.md).
  Aucun critère acoustique/humain ni réserve S9 n'est déclaré validé.

## S9 — Intégration pré-réception autorisée sous réserves — 2026-11-05

- Décision de user : « finissons d'intégrer s9 et tout le reste, tout
  ce qu'on peut avant d'avoir le robot ». Le coordonnateur prépare
  l'intégration ; cette livraison documentaire ne prétend pas une fusion.
- Les deux échecs de marche connus restent ouverts : suite complète
  **6 passed, 2 failed, zéro skip, exit 1**, mesures et seuils inchangés.
  Autoriser l'intégration ne signifie ni critère sim passé ni `agent-done`
  strict. Aucune cause imputée au transport ; TCP reste le défaut.
- Résultats et nettoyage contre-vérifiés ; historique et preuves conservés.
  Aucun changement code/tests, nouvel essai, service, Docker ou appel externe.

## S9 — Revalidation réelle unique après SIMWALK — 2026-11-05

- Suite complète sur `2fc23f9`, base `8de5e3e` : **6 passed, 2 failed,
  551 deselected, zéro skip**, code retour **1**, 58,74 s.
- Marches S1/S9 strictes via WebRTC : **0,137311/0,145489 m**, échec au
  seuil > 0,15 m malgré le helper SIMWALK partagé ; aucune modification de
  scénario, seuil ou transport, aucun rerun. Le critère sim reste ouvert.
- Tours **92,977/94,815°**, regards **0,337/0,298 rad**, caméra **640x360**
  et métriques passent : RTT p95 **0,81 ms**, cadence observée **19,44 Hz**
  (50 updates/2,520 s), gigue absolue p95 **28,99 ms**, aucun deadman observé.
- Sorties et télémétrie de sécurité archivées ; aucun danger détecté.
  Nettoyage S9 vérifié (PID/argv, ports, pidfiles, verrou), Pollen préservé.
  Aucun relais TCP, Docker/Compose ou appel OpenAI/OpenClaw ; attente de
  contre-vérification, Q10 conserve uniquement ses preuves historiques.

## Intégration S9 sur S5-S8/SIMWALK — 2026-11-05

- Rebase sur `main=8de5e3e` en conservant toutes les preuves S9 et les ajouts
  S4-S8/SIMWALK. Les deux marches partagent le helper SIMWALK typé
  `RobotLink`, sans changer `move(0.2,0,0,2)`, le seuil > 0,15 m, tour ou regard.
- Test caméra S7 via fabrique et ciblage explicite communs : connexion vidéo
  WebRTC, capture TCP indépendante, assertions préservées, indisponibilité
  opt-in en échec explicite. Isolation des features main et des secrets
  préservée avec l'override sim de `ROBOT_LINK`.
- Service vocal `RobotFeature` raccordé à la fabrique TCP/WebRTC en conservant
  lien injecté, horloge, paramètres TCP et callbacks. Régressions sur vrais
  transports/pairs locaux : perte/reprise, annonce unique et arrêt propre.
- Aucun changement de transport/Pollen, aucune simulation ou service réel,
  aucun Docker/OpenAI/OpenClaw ni verrou à cette étape. Acceptation S9
  toujours bloquée jusqu'à revue puis revalidation réelle autorisée.
- Qualité complète sérialisée verte : **551 passed, 8 skipped** (uniquement
  les tests sim hors opt-in), couverture **91,20 %**, Ruff/format/Pyright
  verts ; AudioWorklet Node **6/6**. Silero réel et régressions S5/S6/S7,
  STOP/filtrage/cible/secrets conservés et verts.

## S6 — Bec et cancanements — 2026-11-05

- Ajout de `MouthAnimator` avec enveloppe RMS à 25 Hz calée sur l'horloge
  physique, fermeture immédiate puis répétée, et rejet des anciennes positions
  après remise à zéro de la lecture.
- Ajout de `OutputMixer`, `StreamQuack` et `RobotQuack`. L'annulation préserve
  les trames vocales déjà en file ; si un cancanement est déjà mixé avec une
  voix envoyée, seule sa portion non encore envoyée peut être retirée sans
  interrompre cette voix.
- `VoiceFeature` utilise désormais un runtime par application ; les
  automatiques greet/coo/inquire et les hooks d'interruption/STOP sont câblés.
- Tests d'intégration et validations de la livraison en cours ; les critères
  humains (bec MuJoCo, audibilité et absence de faux réveil/barge-in) restent
  à mesurer par user. Aucun résultat de simulation n'est revendiqué.
- Vérification ciblée : **22 tests réussis** (`test_mouth.py`, `test_quack.py`,
  `test_voice_feature.py`) ; ruff/format sur les fichiers S6 touchés et Pyright
  ciblé verts, avant les validations complètes ci-dessous.
- Qualité complète : ruff/format/Pyright verts, **479 tests réussis, 5 ignorés**,
  couverture **91,78 %**. Tests sim autorisés : **3 réussis, 481 désélectionnés** ;
  marche **0,177 m**, tour **87,710°**, regard **0,796 rad**. Ce passage valide la
  motion sim, pas les critères visuels/auditifs S6.
- Simulation et relais arrêtés ; les ports S6 `7861`, `7961`, `7870`, `7871` et
  `8776` sont libres et le state-dir ne contient plus de pidfiles. Docker
  Desktop étant arrêté, Compose et l'essai humain via navigateur restent bloqués
  par l'environnement et ne sont pas déclarés validés.

## S7 — Vision — 2026-11-05

- Outil `see(question?)` sur le lien de `RobotFeature`, capture 2 s, contrôle
  de fraîcheur et une reprise, erreur caméra explicite.
- Message utilisateur Realtime GA `input_image` JPEG + `input_text` facultatif ;
  résultat d'outil et continuation unique conservés dans le lot S5.
- JPEG borné à 640 px de large et 150 000 octets, journal sans pixels/base64,
  interruption, remplacement de session et remplacement de réponse protégés,
  y compris après préparation et attente du verrou d'envoi.
- Tests de barres UYVY et rotations, compression d'images bruitées, vrai relais
  vers faux socket caméra, faux Realtime, caméra absente/périmée et annulation.
  Test sim caméra opt-in avec sauvegarde temporaire puis suppression.
- Régression vrai `App` du tour remplacé : 3 échecs avant correction,
  6 cas verts après (capture, préparation et verrou d'envoi, avec témoins).
- Après rebase S6 (`2298ffb`) : ruff/format/Pyright verts, 508 tests réussis,
  6 ignorés, couverture 91,88 %, Node 6/6.
- Caméra réelle pré-rebase validée : JPEG 360x640, 12 218 octets, sauvegarde
  temporaire vérifiée puis supprimée. Échec streaming distinct du snapshot.
  Suite sim : 3 réussis, 1 échec (marche 0,137 m sous le seuil partagé de
  0,15 m, inchangé). Ressources nettoyées ; contre-vérification sim en attente.

## S5 — Correction de la course d'annonce — 2026-11-05

- Livraison `46bdee4` non acceptée : reproduction déterministe de deux
  continuations après `mouth_close` bloqué, en fin normale comme en échec.
  Une tâche d'annonce ne peut plus lancer de réponse normale ; cette reprise
  est réservée à un lot valide après ses outputs ordonnés. Une nouvelle
  réponse active diffère une annonce sans la perdre ; session et réponse
  sont aussi revérifiées après l'attente de notification UI.
- Une annonce non envoyée survit au barge-in ; une version distingue une
  nouvelle perte pendant l'envoi, même avec le même texte. Une erreur
  d'envoi est explicite et ne crée pas de boucle de réessai. STOP conserve
  sa priorité et invalide les annonces et tâches périmées.
- Douze régressions sans robot/OpenAI : **80 tests ciblés réussis** ;
  ruff/format/Pyright verts, **459 passed, 3 skipped**, couverture **91,78 %** ;
  AudioWorklet **6/6**. Contrats `audio_out` et `send_tool_result` inchangés.
- Preuves brutes ajoutées dans `docs/evidence/s5/`, historique conservé.
  Aucun nouveau passage sim ni démarrage sim/relais/Compose, aucun changement
  des tests sim, seuils ou sources Pollen. S5 reste non intégrée.

## S5 — Revalidation après S8 et SIMWALK — 2026-11-05

- Rebase sur `main=cc30372`, conservation de `BrainFeature` et `RobotFeature`
  dans `FEATURES`, des deux blocs de documentation et des ADR-007/008 ;
  ADR-S5-001 reste distinct. Aucun changement de production supplémentaire.
- Qualité complète : **447 passed, 3 skipped** (uniquement sim sans opt-in),
  couverture **91,72 %**, ruff/format/Pyright verts et AudioWorklet **6/6**.
- Un unique passage complet `COINCOIN_TEST_SIM=1 uv run pytest --no-cov -m sim -s -rs`
  donne **3 passed, 447 deselected**, aucun saut : avance stabilisée **0,162 m**,
  tour **89,222°**, regard **0,795 rad**. Consigne et seuil inchangés ;
  helper commun SIMWALK hérité de main, aucun test additionnel S5 à adapter.
- Blocage S5 levé pour les critères agent ; historique et captures brutes
  précédentes conservés. Services S5 arrêtés, aucun nouvel appel OpenAI,
  Pollen inchangé. Intégration par le coordonnateur et essais humains ouverts.

## S8 — Mémoire OpenClaw — 2026-11-05

- Client HTTPX OpenAI-compatible `OpenClawBrain` (`openclaw/<agent>`, `user`
  stable `coin-coin-voice`, non streamé), gabarits session-start / remember /
  session-end, échéances 3/8/15 s, deux reprises avec backoff/gigue pour réseau,
  429 et 5xx, erreurs typées 401/403 et 409 sans jeton ni corps distant.
- `BrainFeature` inscrit dans `FEATURES` : mémoire chargée au réveil dans
  `{memory_context}` (mode sans mémoire explicite en cas d'échec), outils
  `remember` et `ask_openclaw` (arguments bornés), résumé local du journal de la
  conversation envoyé en tâche de fond au coucher, échecs journalisés.
- Phrase d'attente : consigne dans la description de `ask_openclaw`, sans
  seconde réponse Realtime ; appels > 1,5 s journalisés (ADR-007).
- Faux OpenClaw loopback scriptable (statuts, délais par gabarit, mémoire des
  faits) et tests unitaires/intégration App. Aucune API réelle OpenClaw
  observée, aucun nouvel appel OpenAI ; critères user ouverts.
- Validation : ruff, format et Pyright verts ; 415 tests réussis, 5 ignorés
  (3 sim, 2 assets VAD absents du worktree), couverture 91 % ; AudioWorklet 6/6.

## SIMWALK — Mesure du test de marche — 2026-11-05

- Le test de marche attend maintenant un état odométrique frais et stabilisé
  après STOP, avec `applied < 0,01`; la consigne de 2 s, sa vitesse de
  0,2 m/s et le seuil strict `> 0,15 m` sont inchangés.
- Le rapport et les dix exécutions consécutives non sélectionnées sont
  consignés dans `docs/simwalk-motion.md` et `docs/evidence/simwalk/`.
- Validation finale : commande qualité complète verte, 387 tests réussis,
  5 ignorés, couverture 90,91 % ; campagne sim 10/10 réussites.

## S5 — Outils robot — 2026-11-05 — livraison initiale (historique)

- Branchement des onze outils robot au gouverneur, contrats JSON bornés,
  validation locale des types/arguments, liste dynamique des figures sans
  `sit_toggle`, refus conservés et `NullQuack` explicitement sans son.
- Installation par application, transport reconnectant, cycle STOP/coucher
  et hook `shutdown` fermant gouverneur et liaison. La perte robot est annoncée
  une fois par coupure si une session est ouverte ; les outils refusent avec
  « je ne sens plus mes pattes » jusqu'au retour.
- Annonce Realtime dédiée, sans faux output d'outil et sans seconde reprise
  d'un lot : attente du parent et de tous les résultats ; STOP annule aussi
  les annonces périmées. `audio_out` et `send_tool_result` restent inchangés.
- Actions/refus affichés (20 dernières entrées), résumé de statut court,
  journal complet du statut, latences des appels et de première consigne
  (distinctes du début de mouvement physique).
- Qualité : ruff/format/Pyright verts, 419 tests réussis, 3 sim ignorés hors
  opt-in, couverture 91,33 %, 30 cas S5 et AudioWorklet 6/6.
  Compose S5 healthy sur 8775 et CLI authentifiée vérifiés.
- **Blocage conservé** : premier opt-in 3/3, avance 0,156 m ; dernier opt-in
  1 échec/2 succès, avance 0,120359637 m sous le seuil inchangé > 0,15 m.
  Tour 89,390°, regard 0,797 rad. Pas de cause établie ni de seuil assoupli.
  Ressources S5 arrêtées ; validations humaines ouvertes avec procédure README.

## S4 — Conversation Realtime — 2026-11-05

- Ajout du client WebSocket GA `/v1/realtime`, des évènements normalisés, du
  VAD/transcription FR-EN, des outils asynchrones bornés et des hooks `Feature`
  pour S5–S8. Les échanges ne démarrent qu'après le réveil local ; aucune
  connexion robot n'est faite par la conversation.
- Barge-in : progression et item audio capturés avant le flush navigateur,
  puis annulation et troncature à la durée réellement jouée. Ajout du suivi
  des transcriptions, de l'usage, de la latence VAD→premier audio joué, du
  renouvellement anticipé de session et du résumé de reprise.
- Trace réelle API enregistrée avant l'implémentation, avec audio de tonalité
  non sensible, outil factice `ping` et annulation ; fixture plafonnée à 80 ms
  d'audio et vérifiée sans clé. Modèle `gpt-realtime-2.1`, voix provisoire
  `marin` (écoute humaine encore requise).
- Ajout du faux serveur fixture-backed et des tests de conversation, sommeil,
  reconnexion, renouvellement, journalisation, latence et barge-in.
- Corrections après revue : STOP priorise l'initiation du hook robot et le
  flush, invalide sessions/VAD/audio périmés, conserve l'ordre des trames,
  tronque l'item réellement entendu à l'échantillon près, et traite les outils
  sans bloquer SpeechStarted. Ajout de 15 régressions App/UI/transport,
  notamment un refus TCP local réel, les résultats de refus d'outil, le contexte
  mémoire avant ouverture et les octets FOLLOWUP réellement acceptés.
- Revalidation complète après correction : 366 tests réussis, 3 tests de
  simulation ignorés, couverture totale 90,40 %, ruff/format/Pyright verts ;
  le harness AudioWorklet réel passe 5/5. La trace API unique demeure inchangée ;
  aucun nouvel appel OpenAI, aucune sim et aucun robot.
- Seconde correction après revue de `5cc8a42` : réveil réarmé silencieusement
  après STOP, arrêt robot de service attendu avec échéance explicite, outil
  `sleep` exécuté jusqu'à ses hooks et son résultat, outils corrélés à leur
  réponse et invalidés dès interruption. Plusieurs outils parallèles produisent
  leurs résultats ordonnés puis une seule reprise, jamais après annulation ;
  les erreurs d'envoi sont explicites et terminent l'état de parole.
- Fin de lecture à l'échantillon exact et suivi des générations de page ;
  audio périmé rejeté après remplacement, progression manquante ou timeout
  signalé sans faux succès, frontière entre items tronquée sur le suivant à zéro.
  Hooks `audio_out` déplacés vers l'appel effectif à `play` sans bloquer les
  évènements prioritaires ; deadline étendue à l'upgrade WebSocket ; flush
  STOP restauré en mode hors ligne. Les transcriptions finales sont journalisées
  à leur réception, indépendamment de l'achèvement sonore de la réponse.
- Revalidation de cette seconde correction : 23 nouveaux cas App/UI/transport,
  389 tests réussis, 3 sim ignorés, couverture 90,91 %, ruff/format/Pyright verts,
  harness du vrai AudioWorklet 6/6. Trace réelle conservée à l'identique, aucun
  nouvel appel OpenAI, aucune simulation ni connexion robot.

## S2 — Flush borné — 2026-11-05

- Après la borne initiale d'attente de l'acquittement, l'échéance totale de
  `BrowserAudio.flush()` couvre aussi les verrous et l'envoi UI ; le contrôle
  n'attend pas l'envoi audio en cours. Timeout, erreur d'envoi et annulation
  sont explicites, et la lecture reste refusée jusqu'à resynchronisation par
  un flush acquitté. Cela ne garantit pas le silence physique d'un navigateur
  injoignable.
- Régressions déterministes pour envois bloqués, flushs simultanés, annulation,
  générations périmées, remplacement de page et rejet d'audio ancien. Qualité
  complète et harness AudioWorklet validés sur la branche S2 avant intégration.

## Correctif S9 — 2026-11-05

- Fermeture du contournement du filtrage S1 par `coincoin-cli raw` WebRTC :
  liste de méthodes S1 partagée avec le relais, refus local `-32601` avant
  envoi des appels ou notifications, sans paramètres dans les journaux.
- Régressions sur faux pair : familles interdites, toutes les méthodes
  permises et contrats CLI ; CLI/TCP/relais et exécution stdlib autonome
  préservés. Aucun appel interdit vers une simulation ou un robot.
- Qualité complète verte : **355 passed, 7 skipped hors opt-in**, couverture
  **91,52 %**, Ruff/format/Pyright verts. Q10 WSL et bridge Docker revalidée.
- **Simulation toujours bloquée** : dernière commande complète **4 passed,
  3 failed, zéro saut**, marches S1/S9 0,128577/0,125208 m et regard S9
  0,145847 rad. Nouvelle campagne bornée avec lectures physiques MuJoCo :
  trois échecs sur quatre marches ; télémétrie du premier essai perdue
  après erreur de post-traitement, résultat arrondi conservé sans répétition.
  Temps physique proche du temps réel et écarts physique/odométrie mesurés,
  sans cause racine ni correction légitime S9 démontrée. Tous paramètres
  et critères conservés, mesures et limites dans `docs/webrtc.md`.
- Nettoyage vérifié des seuls processus S9, relais, Compose et verrou caméra.

## S9 — 2026-11-04

- Ajout de `WebRtcLink` avec aiortc, signalisation LAN `mediad`, canal JSON-RPC
  `control`, piste vidéo, reconnexion et sélection par URL `webrtc://`.
- Les tests de mouvement sim sélectionnent le transport configuré ; tests du
  pair WebRTC local et scénarios opt-in de mouvement, RTT et gigue.
- Correction après revue : STOP invalide le mouvement différé ; aucun nouveau
  mouvement n'est admis tant que `bufferedAmount` n'est pas nul. Régressions
  transport/JSON-RPC/gouverneur, 300 updates bloqués, STOP, reprise et timeout
  explicite d'un STOP non livré. Un message déjà admis sur le canal fiable
  ne peut pas être retiré ni dépassé par STOP.
- Les scénarios S1/S9 retrouvent strictement les 2 s et le seuil > 0,15 m ;
  regard maintenu 1 s, contrôle après 0,6 s. Cible `ROBOT_LINK` explicite
  respectée pour la sim opt-in, avec régressions hors réseau.
- Qualité finale sur `main` `0597380` : ruff, format, pyright verts ;
  **351 passed, 7 skipped** hors opt-in, couverture **91,35 %**.
- **Acceptation sim non acquise** : commande complète **6 passed, 1 failed,
  zéro saut** (marche S9 0,143831 m). Dix comparaisons supplémentaires,
  toutes consignées : 2 réussites, 8 échecs, 40 envois par essai et STOP
  après 2,000–2,001 s. Les anciennes preuves à 3 s ne valent pas acceptation.
- Diagnostic final TCP/WebRTC sur la même sim caméra, scénario strict
  inchangé et ordre équilibré préétabli : TCP 3/4 réussites
  (0,143836–0,170451 m), WebRTC 3/4 (0,143790–0,161683 m). 40 moves par
  essai, STOP après 2,000–2,001 s, aucun deadman. Les huit observations
  sont conservées ; aucune cause logicielle ni régression propre à WebRTC
  démontrée, critère toujours bloqué. Relais 7930/7931 et sim S9 nettoyés.
- Cadence observée calculée, non constante : **19,77 Hz**, 50 updates sur
  2,479 s ; cadence d'émission nominale 20 Hz. RTT p50/p95 0,51/0,65 ms,
  intervalle p50 42,17 ms, gigue absolue p95 29,30 ms ; aucun deadman.
- Q10 validée depuis WSL et le bridge Docker par défaut : health HTTP 200 sur
  8779, CLI robot healthy et mouvement Docker de 2 s sur 0,128 m ; aucune
  ouverture du port hôte 8765 ni besoin de `network_mode: host`.
- Mesures détaillées et décision : `docs/webrtc.md` et ADR-S9-001
  (ancienne référence S9 ADR-006).

## Correctif S3 H-POSITIFS — 2026-11-04

- Sessions humaines isolées sous `assets/wake/recorded/`, capture temporaire et
  publication sans écrasement ; reprise explicite conservant les WAV complets
  et les temporaires interrompus.
- Attestation exacte exigée pour les imports humains ; provenance déclarée,
  identité et complétude non vérifiées. Refus des copies exactes des TTS locaux.
- Corpus `assets/wake/` exclus du contexte Docker et sessions humaines ignorées
  par Git. Données existantes conservées, aucune capture micro lancée.
- Validation après rebase sur `19bd4c3` : 329 tests réussis, 3 ignorés, dont 13 nouvelles
  régressions déterministes ; qualité complète verte.

## S2 — 2026-11-04

- Ajout de `BrowserAudio` : micro et lecture PCM16 sur `/ws/audio`, contrôles
  et acquittements sur `/ws/ui`, tampon de lecture limité à 300 ms, flush et
  remplacement de page.
- Ajout de la page navigateur, des AudioWorklets (trames 20 ms, précharge 60 ms,
  compte rendu de lecture et rééchantillonnage selon le taux réel du contexte),
  ainsi que des modes `coincoin --echo` et `coincoin --tone`.
- Ajout du rééchantillonnage SoXR, du mixeur sans délai et du mesureur de latence
  par corrélation ; documentation ADR-002 et procédure AEC manuelle.
- Validation : 55 tests réussis, couverture 93,82 %, ruff et pyright verts.
  Le test humain d'écho/AEC reste à faire par user.

## 0.2.0 — 2026-11-04

- S3 : machine d'états pure, porte audio par état, VAD Silero ONNX avec pré-roll
  et maintien, rééchantillonnage PCM16 et moteurs de réveil Vosk FR/EN.
- Banc hors ligne `scripts/eval_wake.py`, enregistrement via PulseAudio/ffmpeg,
  obtention des modèles et corpus publics documentés ; données audio et modèles
  ignorés par Git.
- Valeurs de mesure, choix du seuil et limites documentés dans
  `docs/wake-eval.md` et l'ADR-003 ; modèles montés en lecture seule dans Compose.
- Correction Silero v5.1.2 : contexte de 64 échantillons, état récurrent et
  contexte réinitialisés ensemble, preuve ONNX réelle parole/silence (±100 ms).
- Lectures voix/cancanement suivies séparément pour conserver le barge-in et
  le délai de réveil quand les deux cycles se chevauchent.
- Rebase S3 sur S2 : API et tests audio conservés, SoXR partagé, montage des
  logs et dépendances des deux sessions préservés ; reprise depuis le repo
  principal et conservation des corpus ignorés documentées.

## S1 — 2026-11-04

- Correction de priorité STOP sur `main` `143e015` : aucun verrou d'action
  ni réponse RPC lente ne retarde son envoi ; RPC d'action invalidés/annulés,
  générations vérifiées avant envoi et après réponse, aucune reprise tardive.
  20 nouveaux cas déterministes ; 314 tests Python réussis, 5 sauts documentés,
  couverture totale 93,04 % (gouverneur 96 %, relais 84 %), qualité verte.
  3 tests sim et 5 AudioWorklet passent ; STOP pendant un regard retardé et
  une marche réelle acquitté en moins de 200 ms (appel transport débuté en
  0,116 ms), consigne zéro, action retardée refusée.
- Rebase sur `main` `c1ee52d` : dépendances et procédures S2/S3 préservées,
  lock combiné régénéré, ADR robot renumérotées 004/005, procédure S1 adaptée
  au dépôt principal et au montage `.cache/logs/`.
- Revalidation combinée : 294 tests réussis, 5 sauts documentés, couverture
  92,95 %, qualité verte ; 3 tests AudioWorklet et 3 tests sim réels réussis
  (avance 0,154 m, tour 88,647°, regard 0,795 rad). Compose reconstruit healthy,
  CLI authentifiée vérifiée, toutes les ressources S1 arrêtées.
- Relais TCP/Unix stdlib autonome, refus par défaut par port, lignes bornées,
  authentification avant connexion Unix et arrêt propre SIGINT/SIGTERM.
- Couche `JsonRpcConnection` réutilisable en S9 : corrélation par identifiant,
  flux d'état sur la même connexion, délais et notifications bornées à 50 ms.
  `TcpLink` authentifié, reconnexion exponentielle avec gigue et callbacks.
- Client images déjà branché dans `TcpLink.frame()` : validation du header,
  lecture binaire exacte, conversion UYVY, rotation et JPEG qualité 80.
- Gouverneur : limites walk/roller et pose, commande à 20 Hz, mouvement
  remplaçable, surveillance 10 Hz, arrêt prioritaire, activation explicite et
  homing, tour en boucle fermée, regard maintenu, posture et skills dynamiques.
- `coincoin-cli` utilisable depuis le conteneur, avec avant/après odométriques,
  erreurs explicites et diagnostic `raw` filtré par le relais.
- Correction vérifiée contre `robotd` : `robot.stop` doit porter un `id` ;
  une notification ne déclenche pas l'arrêt. L'EMA de vitesse de `robotd`
  est prise en compte dans les observations, sans modifier Pollen.
- Qualité : 225 tests hors sim, couverture totale 93,99 %, gouverneur 95 %,
  relais 84 %, ruff/format/pyright verts ; faux robotd TCP/Unix et tests
  de reconnexion, backpressure, erreurs, binaire et signaux.
- Tests sim réels : avance 0,162 m en 2 s, tour 88,813°, regard 0,797 rad.
  Depuis Docker : avance 0,261 m pour une commande de 3 s ; `robot.relax`
  refusé −32601 sans chute ; deadman observé 0,496 s après arrêt du relais.
  Uniquement état/ports/projet Compose S1 ; tous les services arrêtés.

## 0.1.0 — 2026-11-04

- S0 : repo Git indépendant sur `session/S0`, Python 3.12, `uv.lock`,
  ruff, pyright basique et pytest avec tests asynchrones.
- Interfaces figées `Clock`, `AudioIO`, `RobotLink`, `QuackSink`, `Brain` ;
  `FakeClock`, exceptions robot, `Frame` immuable et `NullBrain` explicite.
- Configuration complète de la spec maître, secrets en `SecretStr`,
  journal JSONL UTC avec masquage récursif, rotation quotidienne et purge.
- Page « Coin-Coin — hello », `/healthz`, image non root avec HEALTHCHECK
  et Compose avec port paramétrable et volume de logs.
- Spike réseau rejouable sur le seul port 7899, plan B authentifié,
  nettoyage automatique et tests unitaires avec commandes Docker/IP factices.
- `/healthz` validé depuis WSL et Windows (HTTP 200), conteneur `healthy`
  en UID/GID 1000 ; tous les services S0 sont arrêtés à la livraison.
- Chemin réseau réel B validé sur `eth0` WSL avec jeton obligatoire :
  plan A injoignable, rejet sans jeton puis écho authentifié exact.
- Défaut initial du helper Docker Windows résolu pour les essais par
  préchargement des seules images publiques depuis Windows, sans modifier
  la configuration Docker/WSL ni utiliser de configuration Docker alternative.
- Commande qualité complète verte : 39 tests, couverture 100 %, aucune erreur
  ruff/pyright ; audit Git sans valeur de clé API ni fichier de secrets suivi.
