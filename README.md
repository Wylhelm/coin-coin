# Coin-Coin

**Un compagnon vocal pour donner vie à un petit robot.**

Coin-Coin réunit conversation en temps réel, vision et commandes robotiques
dans une interface web. L'objectif : pouvoir parler naturellement à son
compagnon, lui demander ce qu'il voit et interagir avec son environnement.

> [!WARNING]
> **Projet en construction et expérimental.** Le fonctionnement sur un robot
> physique n'a pas encore été validé. Certains essais de déplacement en
> simulation échouent encore. Ne pilotez pas de matériel sans validation
> préalable et surveillance adaptée.

## Ce que fait Coin-Coin

- **Écouter et discuter** : audio depuis le navigateur, réveil local et
  conversation vocale en français ou en anglais avec OpenAI Realtime.
- **Interrompre naturellement** : la parole de l'utilisateur peut interrompre
  une réponse en cours ; un bouton STOP permet aussi d'arrêter la conversation.
- **Agir et regarder** : outils de mouvement, de regard et de vision, avec un
  gouverneur qui encadre les commandes envoyées au robot.
- **Donner du caractère** : animation du bec et cancanements synchronisés avec
  l'audio, lorsque les ressources nécessaires sont disponibles.
- **Se souvenir** : intégration optionnelle à OpenClaw pour la mémoire à long
  terme. Elle est actuellement testée avec un service simulé, pas avec une
  gateway OpenClaw réelle.
- **Explorer les transports** : connexion TCP avec relais authentifié ou liaison
  WebRTC expérimentale. Le simulateur robot est un projet séparé.

## Prérequis

- Python 3.12 ou plus récent et [uv](https://docs.astral.sh/uv/).
- Un navigateur récent, un microphone et des haut-parleurs pour l'audio.
- Une clé OpenAI pour la conversation vocale ; elle n'est pas nécessaire pour
  le mode écho ni pour les tests hors ligne.
- Pour la conversation complète : les modèles de réveil et de détection de
  parole, ainsi qu'une configuration adaptée des services utilisés.
- Pour les fonctions robotiques : un simulateur compatible et sa configuration
  réseau. Le robot physique reste à valider.

Docker et Docker Compose sont facultatifs : une configuration Compose est
fournie, mais l'assemblage complet reste à revalider.

## Démarrage rapide

```sh
git clone https://github.com/Wylhelm/coin-coin.git
cd coin-coin
uv sync --locked
test -e .env || cp .env.example .env
```

Le mode écho permet d'essayer l'interface audio sans clé API, sans modèle vocal
et sans liaison robot :

```sh
uv run coincoin --echo
```

Ouvrir <http://localhost:8765>, démarrer l'écoute et autoriser le microphone.
Arrêter le service avec `Ctrl+C`. Le mode `uv run coincoin --tone` propose
également un test par tonalité.

> [!IMPORTANT]
> Le serveur lancé directement écoute sur toutes les interfaces réseau.
> Utilisez-le uniquement sur un réseau de confiance et ne l'exposez pas à
> Internet. Le relais TCP n'offre pas de chiffrement TLS et la signalisation
> WebRTC n'est pas authentifiée.

### Conversation et robot

1. Renseigner les paramètres nécessaires dans `.env`, notamment
   `OPENAI_API_KEY`. Ne jamais publier ce fichier ni partager ses secrets.
2. Installer les modèles Vosk et Silero avec
   `scripts/download_wake_models.sh`. Ce script télécharge les modèles et
   remplace le fichier Silero existant.
3. Configurer `ROBOT_LINK`, `FRAME_LINK` et, pour le relais TCP,
   `RELAY_TOKEN` selon l'environnement utilisé. La mémoire OpenClaw est
   facultative et se configure séparément.
4. Préparer une banque de sons si les cancanements sont souhaités : sans
   banque, cette fonction est désactivée.
5. Après avoir configuré les services nécessaires, lancer `uv run coincoin`.

Le mode normal peut se connecter au robot configuré et appeler des services
externes. Les modèles, les sons et les secrets restent locaux : ils ne sont
pas inclus dans le dépôt. La documentation technique se trouve dans `docs/` ;
la [procédure de livraison](docs/delivery.md) détaille les dépendances et les
précautions de lancement.

## Développement

Les contrôles suivants utilisent des doubles locaux pour les services externes :

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Les tests du lecteur audio navigateur nécessitent également Node.js :

```sh
node --test tests/js/play_worklet.test.mjs
```

Les tests qui demandent un simulateur ou des modèles supplémentaires peuvent
être ignorés lorsque ces ressources ne sont pas présentes. Leur réussite ne
remplace pas les essais vocaux et matériels.

## Organisation

- `src/coincoin/` : service vocal, interface web, outils robot et mémoire.
- `relay/` : relais entre le service et les sockets du robot ou du simulateur.
- `tests/` : tests unitaires, intégration et scénarios de simulation.
- `docs/` : documentation technique, décisions et résultats d'essais.

## Licence

Aucune licence n'a encore été définie pour ce dépôt.
