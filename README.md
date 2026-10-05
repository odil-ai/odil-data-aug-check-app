# odil-data-aug-check-app

Petite app Flask pour vérifier les appariements entre les images sources (ahloma)
et les canvases IIIF candidats listés dans `manuscript_folios/*.json`.

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

Les vérifications sont écrites dans `verifications.tsv` :

| colonne        | contenu                                  |
|----------------|------------------------------------------|
| `manuscript`   | nom du fichier JSON (ex. `Q100486`)      |
| `source`       | identifiant de l'image source (`ahloma_…`) |
| `target`       | `@id` du canvas IIIF candidat            |
| `score`        | score d'appariement (`matchResult.score`), vide s'il n'y en a pas |
| `verification` | `valid` ou `not_valid`                   |

## Dev

```bash
uv run ruff format . && uv run ruff check .
uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt
```
