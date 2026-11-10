# Notificateur logements Île-de-France (Crous + ARPEJ)

Toutes les 10 minutes, le script vérifie :
- **Crous** : tous les logements disponibles en Île-de-France sur trouverunlogement.lescrous.fr (Paris, Créteil, Versailles)
- **ARPEJ** : les résidences d'Île-de-France (75, 77, 78, 91, 92, 93, 94, 95) qui ont des chambres libres

Tu reçois une notification pour chaque **nouveau logement**, avec le prix, la surface, l'adresse et le lien direct. Si tu appuies dessus, la page de réservation s'ouvre.

## 1. Recevoir les notifs sur ton téléphone (2 min)

1. Installe l'app **ntfy** (iOS / Android, gratuite, sans compte).
2. Appuie sur **+** et abonne-toi à un nom de canal unique et difficile à deviner, par exemple `logement-mouad-7k3q9x`. Toute personne qui connaît ce nom peut lire les notifs, donc évite un nom simple.
3. Ce nom, c'est ton `NTFY_TOPIC`.

Pour Telegram en plus (optionnel) : crée un bot avec @BotFather → `TELEGRAM_BOT_TOKEN`, envoie-lui un message, puis récupère ton `chat_id` sur `https://api.telegram.org/bot<TOKEN>/getUpdates` → `TELEGRAM_CHAT_ID`.

## 2. Le faire tourner 24h/24 (recommandé) : GitHub Actions, gratuit

1. Crée un dépôt **privé** sur GitHub et mets-y ces fichiers (avec le dossier `.github`).
2. Dans le dépôt, va dans **Settings → Secrets and variables → Actions → New repository secret** et ajoute `NTFY_TOPIC`.
3. Onglet **Actions** → « Vérif logements IDF » → **Run workflow** pour lancer un premier test.
   Tu dois recevoir « Notificateur logement activé » avec le nombre d'offres actuelles.
4. Ensuite, la vérification tourne toute seule toutes les 10 min, même quand ton Mac est éteint.

Filtres optionnels (onglet **Variables** au même endroit) :
| Variable | Exemple | Effet |
|---|---|---|
| `MAX_RENT` | `600` | ignore ce qui coûte plus de 600 € |
| `CROUS_MODES` | `alone` | Crous : seul uniquement (`house_sharing` = coloc, `couple`) |
| `ARPEJ_SKIP_RESTRICTED` | `1` | ignore les résidences ARPEJ réservées aux écoles partenaires (PSB n'en fait pas partie) |

## 2 bis. Ou sur ton Mac

```bash
cd chemin/vers/logement-notifier
NTFY_TOPIC=ton-topic python3 notifier.py --test      # notif de test
NTFY_TOPIC=ton-topic python3 notifier.py --dry-run   # affiche les offres actuelles
NTFY_TOPIC=ton-topic python3 notifier.py --demo      # envoie 1 vraie offre Crous + 1 ARPEJ au format alerte
```
Pour l'automatiser : modifie `fr.logement.notifier.plist` (chemin + topic), copie-le dans `~/Library/LaunchAgents/`, puis
`launchctl load ~/Library/LaunchAgents/fr.logement.notifier.plist`. Il ne tourne que quand le Mac est allumé et réveillé.

## Bon à savoir
- L'identifiant d'outil du Crous change chaque année (48 pour 2026-27) : le script le détecte tout seul sur la page d'accueil. Pour le forcer, utilise `CROUS_TOOL_ID`.
- Les logements Crous partent en quelques minutes : garde ton compte messervices.etudiant.gouv.fr connecté sur ton téléphone pour réserver vite.
- Pour ARPEJ, tu es notifié quand une résidence apparaît ou quand son nombre de chambres libres augmente.
- Si GitHub désactive le workflow après 60 jours sans activité, réactive-le dans l'onglet Actions.
