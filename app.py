#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask app to check pairings between source images and IIIF canvases.

Each JSON file in ``manuscript_folios/`` describes one manuscript: a list of folios
(source images served by the École des chartes IIIF server) together with the IIIF
canvases that were automatically reconciled with them. The app shows each source image
next to its candidate canvases and lets the user mark every pair as ``valid`` or
``not_valid``.

Verdicts are stored in ``verifications.tsv`` with the columns ``manuscript``,
``source``, ``target``, ``score`` (matching score from the JSON files, empty if unknown)
and ``verification`` (one row per pair, the latest verdict wins).

Access is protected by a single username / password pair read from ``config.yml``,
together with the secret key used to sign the session cookie (see
``config.example.yml``).

Usage::

    uv run flask --app app run --debug

Verdicts are kept in memory and the TSV file is rewritten on each change, so the app
must run in a single process.
"""

import csv
import hmac
import json
import threading
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, abort, redirect, render_template, request, session, url_for
from werkzeug.wrappers import Response

# A JSON object: folio entry, folio metadata or IIIF canvas.
type Json = dict[str, Any]
# Key of a verdict: (manuscript, source image id, target canvas @id).
type ResultKey = tuple[str, str, str]
# OpenSeadragon tile source: an info.json URL or a simple image {"type": "image", "url": ...}.
type TileSource = str | dict[str, str]

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yml"
DATA_DIR = BASE_DIR / "manuscript_folios"
RESULTS_TSV = BASE_DIR / "verifications.tsv"
FIELDS = ["manuscript", "source", "target", "score", "verification"]
VERDICTS = {"valid", "not_valid"}

SOURCE_IIIF = "https://iiif.chartes.psl.eu/images/ahloma_images"
# Extensions tried in order when the source image cannot be found.
EXTENSIONS = [".jpg", ".jpeg", ".jpg2", ".jp2", ".png", ".tif", ".tiff"]

app = Flask(__name__)
# Serialises writes to the TSV file between request threads.
lock = threading.Lock()


def load_config() -> Json:
    """Load the login settings from :data:`CONFIG_FILE`.

    :return: Settings with the keys ``username``, ``password`` and ``secret_key``.
    :rtype: Json
    """
    return yaml.safe_load(CONFIG_FILE.read_text())


def load_manuscripts() -> dict[str, list[Json]]:
    """Load every manuscript JSON file from :data:`DATA_DIR`.

    :return: Folio entries (``folio``, ``canvases``, ...) keyed by manuscript name
        (the JSON file stem, e.g. ``Q100486``), sorted by name.
    :rtype: dict[str, list[Json]]
    """
    return {p.stem: json.loads(p.read_text()) for p in sorted(DATA_DIR.glob("*.json"))}


def match_score(canvas: Json) -> float | None:
    """Return the matching score of a candidate canvas.

    :param canvas: IIIF Presentation 2 canvas, possibly with a ``matchResult`` entry.
    :return: The score of the automatic matching, or None if the canvas has none.
    :rtype: float | None
    """
    return (canvas.get("matchResult") or {}).get("score")


def load_scores() -> dict[ResultKey, float]:
    """Collect the matching scores of all the pairs that have one.

    :return: Matching score keyed by ``(manuscript, source, target)``.
    :rtype: dict[ResultKey, float]
    """
    return {
        (name, item["folio"]["id"], canvas["@id"]): score
        for name, items in MANUSCRIPTS.items()
        for item in items
        for canvas in item["canvases"]
        if (score := match_score(canvas)) is not None
    }


def load_results() -> dict[ResultKey, str]:
    """Load the verdicts already saved in :data:`RESULTS_TSV`.

    :return: Verdict (``valid`` or ``not_valid``) keyed by ``(manuscript, source,
        target)``; empty if the file does not exist yet.
    :rtype: dict[ResultKey, str]
    """
    if not RESULTS_TSV.exists():
        return {}
    with RESULTS_TSV.open(newline="") as f:
        rows = csv.DictReader(f, delimiter="\t")
        return {(r["manuscript"], r["source"], r["target"]): r["verification"] for r in rows}


def save_results() -> None:
    """Write all verdicts to :data:`RESULTS_TSV`, sorted by key.

    The matching score of each pair is taken from :data:`SCORES`. The rows are written
    to a temporary file which then replaces the TSV file, so that
    it is never left half-written.

    :return: None
    :rtype: None
    """
    tmp = RESULTS_TSV.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(FIELDS)
        for key, verdict in sorted(RESULTS.items()):
            writer.writerow([*key, SCORES.get(key, ""), verdict])
    tmp.replace(RESULTS_TSV)


CONFIG = load_config()
app.secret_key = CONFIG["secret_key"]
MANUSCRIPTS = load_manuscripts()
SCORES = load_scores()
RESULTS = load_results()


def check_credentials(username: str, password: str) -> bool:
    """Compare submitted credentials with the ones of :data:`CONFIG_FILE`.

    :param username: Submitted username.
    :param password: Submitted password.
    :return: True if both the username and the password match.
    :rtype: bool
    """
    same_user = hmac.compare_digest(username.encode(), str(CONFIG["username"]).encode())
    same_password = hmac.compare_digest(password.encode(), str(CONFIG["password"]).encode())
    return same_user and same_password


def source_tiles(folio: Json) -> list[TileSource]:
    """Build the OpenSeadragon tile sources of a source image.

    The image is looked up on :data:`SOURCE_IIIF` with each extension of
    :data:`EXTENSIONS`, starting with the one of the folio filename (if any).

    :param folio: Folio metadata (``id``, ``filename``, ...).
    :return: info.json URLs to try in order.
    :rtype: list[TileSource]
    """
    ext = Path(folio["filename"]).suffix
    exts = [e for e in dict.fromkeys([ext, *EXTENSIONS]) if e]
    return [f"{SOURCE_IIIF}/{folio['id']}{e}/info.json" for e in exts]


def target_tiles(canvas: Json) -> list[TileSource]:
    """Build the OpenSeadragon tile sources of a candidate canvas.

    :param canvas: IIIF Presentation 2 canvas.
    :return: Tile sources to try in order: the IIIF image service, then the raw image
        and the thumbnail as simple images.
    :rtype: list[TileSource]
    """
    resource = canvas["images"][0]["resource"]
    tiles = []
    if service := resource.get("service", {}).get("@id"):
        tiles.append(f"{service}/info.json")
    for url in (resource.get("@id"), (canvas.get("thumbnail") or {}).get("@id")):
        if url:
            tiles.append({"type": "image", "url": url})
    return tiles


def label(canvas: Json) -> str:
    """Return the label of a canvas as text.

    :param canvas: IIIF Presentation 2 canvas.
    :return: The label; values of a multi-valued label are joined with a dash.
    :rtype: str
    """
    value = canvas.get("label", "")
    return " — ".join(map(str, value)) if isinstance(value, list) else str(value)


def is_done(manuscript: str, item: Json) -> bool:
    """Tell whether every candidate of a folio has a verdict.

    :param manuscript: Manuscript name.
    :param item: Folio entry (``folio`` and ``canvases``).
    :return: True if all pairs are checked (always True for a folio without candidate).
    :rtype: bool
    """
    source = item["folio"]["id"]
    return all((manuscript, source, c["@id"]) in RESULTS for c in item["canvases"])


def next_todo(manuscript: str, index: int = -1) -> tuple[str, int] | None:
    """Find the next folio to check, after ``index`` in this manuscript, then in the next ones.

    :param manuscript: Manuscript to start from.
    :param index: Position of the current folio; the search starts right after it
        (``-1`` to start at the first folio).
    :return: ``(manuscript, index)`` of the next folio to check, or None if all are checked.
    :rtype: tuple[str, int] | None
    """
    names = list(MANUSCRIPTS)
    start = names.index(manuscript)
    for name in names[start:] + names[:start]:
        items = MANUSCRIPTS[name]
        first = index + 1 if name == manuscript else 0
        for i in range(first, len(items)):
            if not is_done(name, items[i]):
                return name, i
    return None


def get_item(manuscript: str, index: int) -> tuple[list[Json], Json]:
    """Get a folio entry, aborting with a 404 error if it does not exist.

    :param manuscript: Manuscript name.
    :param index: Position of the folio in the manuscript (0-based).
    :return: All folio entries of the manuscript and the requested one.
    :rtype: tuple[list[Json], Json]
    :raises NotFound: If the manuscript or the folio does not exist.
    """
    items = MANUSCRIPTS.get(manuscript)
    if not items or not 0 <= index < len(items):
        abort(404)
    return items, items[index]


@app.before_request
def require_login() -> Response | None:
    """Send users who are not logged in to the login page.

    :return: Redirection to the login page, or None to go on with the request.
    :rtype: Response | None
    """
    if not session.get("logged_in") and request.endpoint not in ("login", "static"):
        return redirect(url_for("login"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login() -> str | Response:
    """Login page: check the submitted username and password.

    :return: Redirection to the home page once logged in, otherwise the rendered
        ``login.html`` page (with an error message after a failed attempt).
    :rtype: str | Response
    """
    error = None
    if request.method == "POST":
        if check_credentials(request.form.get("username", ""), request.form.get("password", "")):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Identifiant ou mot de passe incorrect."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout() -> Response:
    """Log out and go back to the login page.

    :return: Redirection to the login page.
    :rtype: Response
    """
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index() -> str:
    """Home page: overall progress and verification progress of each manuscript.

    :return: Rendered ``index.html`` page.
    :rtype: str
    """
    rows = []
    for name, items in MANUSCRIPTS.items():
        pairs = [(it["folio"]["id"], c["@id"]) for it in items for c in it["canvases"]]
        rows.append(
            {
                "name": name,
                "folios": len(items),
                "empty": sum(not it["canvases"] for it in items),
                "pairs": len(pairs),
                "done": sum((name, s, t) in RESULTS for s, t in pairs),
            }
        )
    todo = next_todo(next(iter(MANUSCRIPTS))) if MANUSCRIPTS else None
    return render_template(
        "index.html",
        rows=rows,
        todo=todo,
        done=sum(r["done"] for r in rows),
        total=sum(r["pairs"] for r in rows),
    )


@app.route("/<manuscript>/")
def manuscript(manuscript: str) -> Response:
    """Open a manuscript at its first folio left to check (its first folio if all are done).

    :param manuscript: Manuscript name.
    :return: Redirection to the folio page.
    :rtype: Response
    :raises NotFound: If the manuscript does not exist.
    """
    items = MANUSCRIPTS.get(manuscript) or abort(404)
    index = next((i for i, it in enumerate(items) if not is_done(manuscript, it)), 0)
    return redirect(url_for("folio", manuscript=manuscript, index=index))


@app.route("/<manuscript>/<int:index>")
def folio(manuscript: str, index: int) -> str:
    """Comparison page: the source image of a folio next to its candidate canvases.

    :param manuscript: Manuscript name.
    :param index: Position of the folio in the manuscript (0-based).
    :return: Rendered ``folio.html`` page.
    :rtype: str
    :raises NotFound: If the manuscript or the folio does not exist.
    """
    items, item = get_item(manuscript, index)
    source = item["folio"]["id"]
    candidates = [
        {
            "id": c["@id"],
            "label": label(c),
            "tiles": target_tiles(c),
            "manifest": c.get("manifest_url"),
            "score": match_score(c),
            "verdict": RESULTS.get((manuscript, source, c["@id"])),
        }
        for c in item["canvases"]
    ]
    return render_template(
        "folio.html",
        manuscript=manuscript,
        index=index,
        count=len(items),
        folio=item["folio"],
        source_tiles=source_tiles(item["folio"]),
        candidates=candidates,
    )


@app.post("/verify")
def verify() -> Response:
    """Save the verdict of one (source, target) pair sent by a folio page form.

    Expects the form fields ``manuscript``, ``index``, ``target`` and ``verification``.
    When this verdict completes the folio, redirects to the next folio to check;
    otherwise (or when correcting an already checked folio) goes back to the same page.

    :return: Redirection to a folio page.
    :rtype: Response
    :raises NotFound: If the manuscript or the folio does not exist.
    :raises BadRequest: If a field is missing or the verdict or the target is invalid.
    """
    manuscript = request.form["manuscript"]
    index = request.form.get("index", -1, type=int)
    target = request.form["target"]
    verdict = request.form["verification"]
    items, item = get_item(manuscript, index)
    if verdict not in VERDICTS or target not in {c["@id"] for c in item["canvases"]}:
        abort(400)
    was_done = is_done(manuscript, item)
    with lock:
        RESULTS[(manuscript, item["folio"]["id"], target)] = verdict
        save_results()
    if not was_done and is_done(manuscript, item) and (todo := next_todo(manuscript, index)):
        manuscript, index = todo
    return redirect(url_for("folio", manuscript=manuscript, index=index))


if __name__ == "__main__":
    app.run(debug=False, 
            host="0.0.0.0", 
            port=5000)
