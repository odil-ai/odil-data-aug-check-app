# odil-data-aug-check-app

Application Flask pour vérifier les appariements entre les images sources (ahloma)
et les canvases IIIF candidats listés dans `manuscript_folios/*.json`.

## Vocabulaire

- **Manuscrit** : un fichier JSON de `manuscript_folios/`, nommé d'après l'identifiant
  du manuscrit (ex. `Q100486`, parfois deux identifiants réunis comme `Q90255|Q90256`).
- **Folio** : une image source d'un manuscrit (`ahloma_…`, folio `12V` par exemple). Un
  folio a zéro, un ou plusieurs canvas IIIF candidats.
- **Sans candidat** : un folio pour lequel aucun canvas n'a été trouvé ; rien à valider.
- **Paire** : un folio associé à l'un de ses candidats, l'unité que l'on valide
  (`valid` / `not_valid`). Un folio à 3 candidats donne 3 paires.
- **Non résolu** : une paire sans verdict.

La page d'accueil affiche ces chiffres et permet de masquer les folios sans candidat :
ils sortent alors des comptes de folios, et les manuscrits qui n'ont que des folios
sans candidat disparaissent de la liste.

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

Chaque validation est ajoutée à la fin de `verifications.tsv`, qui garde donc tout
l'historique dans l'ordre chronologique : pour une paire validée plusieurs fois, c'est
la dernière ligne qui compte.

| colonne        | contenu                                  |
|----------------|------------------------------------------|
| `manuscript`   | nom du fichier JSON (ex. `Q100486`)      |
| `source`       | identifiant de l'image source (`ahloma_…`) |
| `target`       | `@id` du canvas IIIF candidat            |
| `score`        | score d'appariement (`matchResult.score`), vide s'il n'y en a pas |
| `verification` | `valid` ou `not_valid`                   |
| `timestamp`    | date et heure de la validation (ISO 8601, ex. `2026-10-06T10:56:02+02:00`) |

Pour repartir de zéro, ne garder que la ligne d'en-tête du fichier puis relancer l'app.

### Marque-pages

Sur la page de vérification, le bouton « Mettre de côté » ajoute le folio (avec une
note facultative) à `bookmarks.tsv`, sans le retirer du parcours de vérification. La
page « Marque-pages » liste les folios mis de côté.

| colonne      | contenu                                     |
|--------------|---------------------------------------------|
| `manuscript` | nom du fichier JSON                         |
| `source`     | identifiant de l'image source (`ahloma_…`)  |
| `folio`      | folio de l'image source (ex. `12V`)         |
| `note`       | note facultative                            |
| `timestamp`  | date et heure de la mise de côté (ISO 8601) |

Le fichier contient les marque-pages actuels, le plus récent en dernier : retirer un
marque-page supprime sa ligne, mettre à jour sa note le replace en dernier.

## Dev

```bash
uv run ruff format . && uv run ruff check .
uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt
```
