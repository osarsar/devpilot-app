# DevPilot

Ton tableau de bord de développeur, sur ton PC (http://localhost:5555) : tes projets et leurs
dépôts, les branches (carte dépôt → branche → main), les terminaux et sessions Claude qui
survivent aux redémarrages, l'aperçu local de chaque dépôt (« localhost:port ↗ »), les ports,
les serveurs, le nettoyage du disque.

> **Perdu ? Une seule commande :** `devpilot pc` — l'assistant regarde où en est ce PC, te dit
> quoi faire et le fait (appuie sur **Entrée** pour le choix recommandé).

---

## Sommaire

1. [Les commandes à retenir](#1-les-commandes-à-retenir)
2. [PC vierge : installer DevPilot](#2-pc-vierge--installer-devpilot)
3. [Chaque jour : récupérer / publier](#3-chaque-jour--récupérer--publier)
4. [Préparer un PC pour publier](#4-préparer-un-pc-pour-publier)
5. [Ça ne marche pas ?](#5-ça-ne-marche-pas-)
6. [Pour développer DevPilot](#6-pour-développer-devpilot)

---

## 1. Les commandes à retenir

| Je veux… | Commande |
|---|---|
| savoir quoi faire sur ce PC | `devpilot pc` |
| lancer DevPilot | `devpilot` (ou l'icône DevPilot) |
| récupérer la dernière version publiée | `devpilot update` |
| publier ce que j'ai modifié (pour tous les PC) | `devpilot publish "ce que j'ai changé"` |
| comparer ce PC à GitHub | `devpilot version` |
| l'aide | `devpilot help` |

---

## 2. PC vierge : installer DevPilot

Ubuntu, connecté à internet. Copie-colle :

```bash
sudo apt update && sudo apt install -y git curl
bash <(curl -fsSL https://raw.githubusercontent.com/osarsar/devpilot-app/main/pc.sh)
```

Il annonce « DevPilot n'est pas installé » → **Entrée** (installer), puis **Entrée** (le lancer).
DevPilot s'ouvre sur http://localhost:5555.

Ce qui est installé : le code dans `~/devpilot/.devpilot/app`, un environnement Python isolé
(`.venv`), la commande `devpilot`, l'icône dans le menu. Tes projets vivent dans
`~/devpilot/projects/`.

**Sessions persistantes (recommandé)** — les terminaux et sessions Claude survivent à un
redémarrage de DevPilot :

```bash
sudo apt install -y tmux
```

**Une ancienne installation** sans `devpilot pc` ? Même commande que ci-dessus (`bash <(curl …)`) :
l'assistant propose « Première mise à niveau ».

---

## 3. Chaque jour : récupérer / publier

**Avant de travailler** (récupère ce qui a été publié depuis les autres PC) :

```bash
devpilot update
```

**Quand tu as modifié DevPilot** (publie pour tous les PC) :

```bash
devpilot publish "Carte des branches : flèches plus lisibles"
```

Ce que fait `publish`, dans l'ordre — et il **s'arrête au premier problème sans rien envoyer** :

1. vérifie : pas de secret (clé, jeton, `.env`), pas de données ni de sauvegardes, pas de
   **mot interdit** (ta liste privée, voir section 4), pas de marques de conflit `<<<<<<<` ;
2. lance les tests (~3 min ; `--sans-tests` pour une correction de texte) ;
3. commite sur une branche (jamais directement sur `main`) ;
4. se met à jour avec GitHub si un autre PC a publié entre-temps ;
5. pousse, ouvre la PR, la fusionne dans `main` ;
6. remet ce PC sur `main` et redémarre DevPilot.

**Le dépôt est PUBLIC** : tout ce qui part est lisible par tous, pour toujours. C'est pour ça
que `publish` vérifie autant.

**Fichiers nouveaux** (jamais suivis par git) : rien ne part sans ton choix. L'assistant demande
« Les publier aussi ? » (**Non** par défaut) ; en ligne de commande : `--avec-nouveaux` (les
publier) ou `--sans-nouveaux` (les laisser sur ce PC).

**Deux PC ont modifié les mêmes lignes ?** `publish` s'arrête, n'écrase rien, ne perd rien, et
te dit quoi taper. Corrige le fichier **sans laisser de `<<<<<<<`**, puis relance.

---

## 4. Préparer un PC pour publier

Seulement sur un PC où tu **développes** DevPilot (pas besoin pour `update`) :

```bash
devpilot pc
```

→ choisis **4 « Préparer ce PC pour publier »**. Il fait, en te guidant :

- installe et connecte `gh` (GitHub CLI) à ton compte ;
- crée une clé SSH si besoin et l'ajoute à ton compte GitHub ;
- te demande ta **liste de mots interdits** : les mots qui ne doivent **jamais** partir sur ce
  dépôt public (vraies adresses IP de tes serveurs, domaines de clients…), un par ligne.
  Elle reste sur le PC : `~/.config/devpilot/interdits.txt` — copie-la depuis un autre PC.

Vérifier :

```bash
gh auth status
ssh -T git@github.com        # doit répondre « Hi <ton compte>! »
```

---

## 5. Ça ne marche pas ?

| Symptôme | Que faire |
|---|---|
| Je ne sais pas où j'en suis | `devpilot pc` (Entrée = le choix recommandé) |
| DevPilot ne s'ouvre pas | `devpilot update` (le relance) ; sinon `tail -30 ~/devpilot/.devpilot/data/dashboard.log` |
| `update` : « modifications locales non publiées » | publie-les (`devpilot publish "…"`), ou mets-les de côté : `git -C ~/devpilot/.devpilot/app stash` |
| `update` : « main locale a des commits absents de GitHub » | voir : `git -C ~/devpilot/.devpilot/app log --oneline origin/main..main` — puis décide avant de jeter |
| `publish` : « fichiers NOUVEAUX » | choisis `--avec-nouveaux` ou `--sans-nouveaux` (ou ajoute-les à `.gitignore`) |
| `publish` : « REFUS » | un secret, une donnée ou un mot interdit allait partir : retire-le, relance |
| `publish` : « tests en échec » | lis la fin de `/tmp/devpilot-publish-tests.log`, corrige, relance |
| `publish` : « mêmes lignes qu'une publication d'un autre PC » | suis les commandes affichées ; ne laisse jamais de `<<<<<<<` |
| `publish` : « push refusé » / « gh n'est pas connecté » | `devpilot pc` → 4 |
| Les terminaux disparaissent au redémarrage | `sudo apt install -y tmux`, puis `devpilot update` |
| Un port reste occupé après avoir fermé un projet | dans le projet : barre « En marche » → ✕ sur le port, ou « Fermer le projet » |

---

## 6. Pour développer DevPilot

```bash
cd ~/devpilot/.devpilot/app
.venv/bin/python -m pytest -q          # les tests (~3 min)
DEVPILOT_PORT=5566 .venv/bin/python dashboard.py   # une 2ᵉ instance pour tester, sans arrêter la tienne
```

- `dashboard.py` (Flask) + `templates/dashboard.html` (l'interface, un seul fichier — c'est un
  gabarit Jinja : ne jamais écrire `{{` dans son JavaScript).
- `dev.py` (dépôts, branches, carte), `preview.py` (aperçu local), `persist.py` (sessions tmux),
  `ports.py`, `controller.py` (la console d'un projet), `servers.py`, `sshaccess.py`.
- `publish.sh`, `update.sh`, `pc.sh` : les commandes `devpilot publish / update / pc`.
- On publie avec `devpilot publish` : branche → PR → fusion, jamais de push direct sur `main`.
