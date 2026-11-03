# odil-data-aug-check-app

Application Flask pour vérifier les appariements entre les images sources (ahloma)
et les canvases IIIF candidats listés dans `manuscript_folios/*.json`.

Les livraisons de `manuscript_folios/` peuvent mêler des canvases IIIF Presentation 2 et 3 :
l'app (comme `report_annotations.py` et `training/prepare_data.py`) lit les deux formats,
les canvases IIIF 3 étant normalisés au chargement. Les verdicts, annotations et
marque-pages portant sur des candidats absents d'une nouvelle livraison sont conservés
dans les fichiers TSV, mais ne sont plus affichés ni comptés dans les paires à vérifier.

## Vocabulaire

- **Manuscrit** : un fichier JSON de `manuscript_folios/`, nommé d'après l'identifiant
  du manuscrit (ex. `Q100486`, parfois deux identifiants réunis comme `Q90255|Q90256`).
- **Folio** : une image source d'un manuscrit (`ahloma_…`, folio `12V` par exemple). Un
  folio a zéro, un ou plusieurs canvas IIIF candidats.
- **Sans candidat** : un folio pour lequel aucun canvas n'a été trouvé ; rien à valider.
- **Paire** : un folio associé à l'un de ses candidats, l'unité que l'on valide
  (`valid` / `not_valid`). Un folio à 3 candidats donne 3 paires.
- **Non résolu** : une paire sans verdict.

La page d'accueil affiche ces chiffres et propose trois filtres, mémorisés pour la session :

- **masquer les folios sans candidat** : ils sortent des comptes de folios, et les
  manuscrits qui n'ont que des folios sans candidat disparaissent de la liste ;
- **ne garder que les paires avec score** (`matchResult.score`), activé par défaut : les
  comptes, la liste, le bouton « Continuer » et le passage automatique au folio suivant
  ne tiennent compte que de ces paires. Ce filtre vaut aussi pour la page Marque-pages ;
- **masquer les manuscrits de la Vaticane** (manifestes sur `digi.vatlib.it`) : ils
  sortent de la liste, des comptes et du bouton « Continuer », et le passage automatique
  au folio suivant ne les propose plus.

Sur la page de vérification, le lien « feuilleter le manuscrit » d'un candidat ouvre son
manifeste IIIF dans [Mirador](https://projectmirador.org/), directement sur la page du
candidat, pour parcourir tout le manuscrit.

La page « Non valides » affiche en mosaïque les paires avec score vérifiées
`not_valid`, des scores les plus élevés aux plus bas ; un clic sur une paire ouvre sa
page de vérification.

## Dossier `storage/`

Les fichiers écrits par l'app (`verifications.tsv`, `annotations.tsv`, `bookmarks.tsv`,
`manual_candidates.tsv`) sont rangés dans `storage/`. Au démarrage, l'app crée le
dossier s'il n'existe pas (avec un `.keepfile` vide, seul fichier du dossier suivi par
git), utilise les fichiers qui y sont déjà, y déplace ceux qu'elle trouve encore à la
racine du projet (anciennes versions), et crée les autres avec leur ligne d'en-tête.

## Enregistrement et ajout manuel de candidats

Chaque clic (Valid / Not valid, type d'image, marque-page) est enregistré aussitôt dans
son fichier TSV, sans recharger la page ni passer par « Suivant ». L'app relit les
fichiers modifiés à chaque requête et protège chaque écriture par un verrou
(`storage/.tsv.lock`) commun à ses processus : elle reste cohérente même lancée avec plusieurs
processus (par exemple `gunicorn -w 4`). Les pages ne sont jamais mises en cache.

Quand un folio n'a pas de bon candidat (ou aucun), le panneau « Ajouter un candidat
manuellement » de sa page (ouvert d'office s'il n'a aucun candidat) permet de feuilleter
un manifeste IIIF dans Mirador — par défaut celui des autres candidats du manuscrit — et
d'ajouter la page affichée comme candidat, avec le type de son image. La paire est
enregistrée comme `valid`, et le candidat dans `storage/manual_candidates.tsv` (colonnes
`manuscript`, `source`, `canvas`, `manifest`, `label`, `image_iiif`, `width`, `height`,
`timestamp`) ; il s'affiche ensuite comme les autres, avec un bouton pour le retirer.
Dans `storage/verifications.tsv`, ses lignes ont `manual` à `true`, et son retrait ajoute une
ligne `removed`.
Mirador s'ouvre en vue simple page.

## Configuration

L'accès est protégé par un identifiant et un mot de passe uniques, définis dans
`config.yml` (non versionné) :

```bash
cp config.example.yml config.yml   # puis changer username, password et secret_key
```

## Lancer

```bash
uv sync
uv run flask --app app run --debug
```

ou sans uv : `pip install -r requirements.txt && flask --app app run`.

Puis ouvrir <http://127.0.0.1:5000>.

## Résultats

Chaque validation est ajoutée à la fin de `storage/verifications.tsv`, qui garde donc tout
l'historique dans l'ordre chronologique : pour une paire validée plusieurs fois, c'est
la dernière ligne qui compte.

| colonne        | contenu                                  |
|----------------|------------------------------------------|
| `manuscript`   | nom du fichier JSON (ex. `Q100486`)      |
| `source`       | identifiant de l'image source (`ahloma_…`) |
| `target`       | `@id` du canvas IIIF candidat            |
| `score`        | score d'appariement (`matchResult.score`), vide s'il n'y en a pas |
| `verification` | `valid` ou `not_valid` ; `removed` quand un candidat ajouté à la main est retiré (la paire n'a alors plus de verdict) |
| `timestamp`    | date et heure de la validation (ISO 8601, ex. `2026-10-06T10:56:02+02:00`) |
| `manual`       | `true` si la cible est un candidat ajouté à la main, `false` sinon |

Pour repartir de zéro, ne garder que la ligne d'en-tête du fichier puis relancer l'app.

### Marque-pages

Sur la page de vérification, le bouton « Mettre de côté » ajoute le folio (avec une
note facultative) à `storage/bookmarks.tsv`, sans le retirer du parcours de vérification. La
page « Marque-pages » liste les folios mis de côté, avec le meilleur score de leurs
candidats ; le filtre « paires avec score » n'y garde que les folios qui en ont une.

| colonne      | contenu                                     |
|--------------|---------------------------------------------|
| `manuscript` | nom du fichier JSON                         |
| `source`     | identifiant de l'image source (`ahloma_…`)  |
| `folio`      | folio de l'image source (ex. `12V`)         |
| `note`       | note facultative                            |
| `timestamp`  | date et heure de la mise de côté (ISO 8601) |

Le fichier contient les marque-pages actuels, le plus récent en dernier : retirer un
marque-page supprime sa ligne, mettre à jour sa note le replace en dernier.

### Annotations des images

Sous l'image source et sous chaque image candidate, trois cases indiquent le type
d'image : « Plain », « Détail » ou « Double page ». Une image a un type au plus : cocher
une case décoche les autres, décocher la case cochée retire l'annotation. Chaque clic
est enregistré aussitôt dans `storage/annotations.tsv`. L'annotation porte sur l'image : elle
apparaît sur tous les folios qui ont cette image.

Pour une image source, l'identifiant enregistré est celui que la visionneuse a
réellement ouvert sur le serveur IIIF de l'École des chartes (la bonne extension :
`.jpg`, `.jpeg`…).

Le panneau Statistiques de l'accueil suit les annotations : nombre de Plain / Détail /
Double page pour les images sources et pour les images candidates, et couverture de
chacune.

Dans la liste des manuscrits, la colonne « Images annotées » donne pour chacun le nombre
d'images annotées sur son nombre total d'images (sources de ses folios et candidats
distincts), selon les filtres actifs ; le détail sources / candidats s'affiche au survol,
et un ✓ indique un manuscrit entièrement annoté. Au-dessus de la liste, le même décompte
est donné pour tous les manuscrits affichés.

| colonne      | contenu                                                        |
|--------------|----------------------------------------------------------------|
| `image_iiif` | identifiant IIIF de l'image (source ou `@id` du service image du candidat) |
| `annotation` | `plain`, `detail` ou `double_page` (`folio`, ancien nom de `plain`, est converti à la lecture) |

Le fichier contient les annotations actuelles, la plus récente en dernier. Pour un
rapport rapide (nombre de chaque type pour les sources et les candidats, part,
couverture) :

```bash
uv run python report_annotations.py              # storage/annotations.tsv par défaut
uv run python report_annotations.py autre.tsv    # un autre fichier
```

## Dev

```bash
uv run ruff format . && uv run ruff check .
uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt
```
