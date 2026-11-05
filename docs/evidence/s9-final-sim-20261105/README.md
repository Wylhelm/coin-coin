# S9 : suite réelle unique après intégration SIMWALK

Exécution autorisée après revue du HEAD `2fc23f94e8d5dea97f8d5d6be4dabbc88eacff30`,
base `8de5e3ebcc16734053d7cf96b5dbb3f75847c543`, le 2026-11-05 à 20:49 UTC.

```sh
ROBOT_LINK=webrtc://192.0.2.1:8443 COINCOIN_TEST_SIM=1 \
  uv run pytest --no-cov -m sim -s -rs
```

**6 passed, 2 failed, 551 deselected in 58.74s ; zéro skip ; exit 1.**
Les deux marches exactes `move(0.2,0,0,2)` échouent au seuil > 0,15 m :
S1 0,13731089587383769 m, S9 0,14548907224457483 m.
Aucun ciblage, rerun ou nouvel essai. Les deux utilisent WebRTC et le même
helper SIMWALK ; aucune cause n'est inférée.

| Fichier anonymisé | Contenu |
|---|---|
| `pytest.log` | Sortie complète et traces des deux échecs ; caméra, tours, regards, RTT/cadence/gigue/deadman |
| `result.json` | HEAD, IP eth0, code retour, verrou propriétaire/date, ressources et PID/argv avant arrêt, vérifications après arrêt |
| `startup.log` | Démarrage caméra, santé et posture initiale |
| `safety.jsonl` | 237 lectures sans contrôle du corps S9 pendant la suite, aucun danger détecté |
| `cleanup.log` | Arrêt protégé S9, retour 0 |

La commande de 2 s n'a pas été instrumentée séparément ; les durées globales
et de fenêtre de sécurité ne sont pas les durées individuelles de marche.
La cadence observée provient des transitions des états robotd reçus, et non
d'un instrument interne au daemon. Détails et limites dans
[`../../webrtc.md`](../../webrtc.md).

Nettoyage vérifié : aucun PID démarré survivant, aucun pidfile `ds-s9`,
ports 7911/8001/7930/7931/8443/8080/8779 libres, verrou S9 retiré.
Aucun relais, Docker/Compose, OpenAI/OpenClaw ou serveur Coin-Coin lancé.
Les chemins de jeton dans les argv sont des noms de fichiers, jamais leur contenu.
Le critère sim S9 reste ouvert ; attente de contre-vérification du coordonnateur.
