# SIMWALK — Diagnostic du test de marche

## Résultat

Le test `tests/sim/test_sim_motion.py::test_move_advances_more_than_fifteen_cm`
attend maintenant une mesure odométrique post-STOP fraîche et stabilisée.
La commande reste exactement `governor.move(0.2, 0, 0, 2)` ; le seuil reste
strictement `forward > 0.15 m`. Aucune relance automatique ou sélection du
meilleur essai n'est utilisée.

La campagne finale contient dix exécutions consécutives de cette version,
chacune lancée une seule fois : **10/10 réussites**, distance minimale
**0,159 m**, maximale **0,175 m**, moyenne **0,1677 m**, médiane **0,1685 m**.
Les sorties individuelles, y compris les poses et valeurs `applied`, sont dans
[`docs/evidence/simwalk/final-motion-runs.txt`](evidence/simwalk/final-motion-runs.txt).
Une vérification pré-campagne à 0,161 m, exécutée avec le même SHA-256 du test,
est consignée séparément et n'entre pas dans ces statistiques.

## Cause étayée

`wait_motion()` revient après l'acquittement RPC de `robot.stop`, mais cette
réponse ne signifie pas que l'EMA de la vitesse appliquée a atteint zéro.
L'ancienne vérification attendait exactement 300 ms puis lisait l'odométrie ;
elle ne vérifiait la vitesse appliquée qu'après l'assertion de distance.

Une trace instrumentée sur la simulation attribuée a observé :

| Temps après retour de `wait_motion()` | Avance projetée depuis le départ | `move.applied.vx` |
|---:|---:|---:|
| 0,000 s | 0,1253 m | 0,2000 m/s |
| 0,026 s | 0,1345 m | 0,1600 m/s |
| 0,127 s | 0,1464 m | 0,0524 m/s |
| 0,253 s | 0,1512 m | 0,0137 m/s |
| 0,380 s | 0,1524 m | 0,0036 m/s |
| 0,607–1,163 s | 0,1502–0,1507 m | inférieur à 0,001 m/s |

La mesure progresse encore après l'acquittement et fluctue légèrement une fois
la vitesse amortie. Un délai fixe ne garantit donc ni une vitesse nulle ni une
odométrie stabilisée au moment de l'échantillonnage. Les campagnes antérieures
S5/S9 rapportaient des résultats des deux côtés du seuil ; S9 avait aussi
observé, dans des snapshots non synchrones, 0,120182 m d'odométrie contre
0,145790 m de déplacement physique projeté, puis 0,134663 m contre 0,173432 m.
Ces écarts confortent le besoin de synchroniser la mesure, mais ne prouvent
pas à eux seuls une erreur d'odométrie.

## Validation

La commande qualité complète, exécutée sans opt-in simulation, est verte :

```text
uv run ruff check .                         All checks passed
uv run ruff format --check .                84 files already formatted
uv run pyright                              0 errors, 0 warnings, 0 informations
uv run pytest                               387 passed, 5 skipped
                                             coverage 90.91%
```

Les cinq tests ignorés sont les deux tests VAD réels et les trois tests
simulation hors opt-in. La campagne de dix exécutions ci-dessus a été lancée
séparément avec le seul test de marche opt-in ; aucun appel OpenAI n'a été fait.

## Correction et protocole

Le test attend maintenant, avec une échéance de 1 s, un état reçu depuis
moins de 200 ms, `abs(applied) < 0.01` sur tous les axes et deux écarts
consécutifs entre mises à jour XY d'au plus 5 mm. Le même critère sert à
prendre le point de départ stationnaire. La distance avant est toujours
projetée sur le yaw odométrique initial. Aucun délai supplémentaire n'est
ajouté à la commande de 2 s.

La version finale a été exécutée dix fois séquentiellement, sans
`--reruns`, boucle de reprise d'un échec ni tri des mesures. Toutes les valeurs
sont conservées dans le journal de campagne. Ressources utilisées :
`~/.cache/ds-s1`, simulation headless sur 7821/7921, relais sur 7830/7831 ;
aucune caméra, aucun Compose et aucun appel OpenAI.

## Limites

Le résultat est une distance d'odométrie stabilisée après la commande de 2 s ;
il inclut le déplacement de décélération post-STOP et n'est pas une
interpolation synchronisée à `t=2,000 s`. Le protocole ne fournit pas de
timestamp interne commun entre l'odométrie et le corps MuJoCo. La campagne
confirme la répétabilité de ce critère odométrique dans cette simulation,
pas l'exactitude de l'odométrie face à une vérité terrain indépendante ni
la généralisation à une autre posture, scène ou politique. Aucun fichier de
`microduck/` n'a été modifié.

## Nettoyage

`duck-sim down` a terminé avec `down`, puis `duck-sim status` a confirmé que
le socket robot et le serveur du corps étaient injoignables. Le relais a été
arrêté par son PID vérifié et son pidfile retiré. `docker compose -p
coincoin-simwalk down --remove-orphans` n'a trouvé aucune ressource. Aucun
pidfile ne subsiste sous `~/.cache/ds-s1`, les ports 7821, 7921, 7830, 7831 et
8771 sont libres, et aucun conteneur du projet SIMWALK n'est présent.
Le lanceur Pollen conserve le blob attendu
`b95192ba23fa78c91ef3c0db8cc250c4a00fd774`.
