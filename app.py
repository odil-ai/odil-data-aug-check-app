#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask app to check pairings between source images and IIIF canvases.

Each JSON file in ``manuscript_folios/`` describes one manuscript: a list of folios
(source images served by the École des chartes IIIF server) together with the IIIF
canvases that were automatically reconciled with them. The app shows each source image
next to its candidate canvases and lets the user mark every pair as ``valid`` or
``not_valid``.

Verdicts are appended to ``verifications.tsv`` with the columns ``manuscript``,
``source``, ``target``, ``score`` (matching score from the JSON files, empty if unknown),
``verification`` and ``timestamp``. The file keeps the whole history in chronological
order: the last row of a pair gives its current verdict.

Folios judged special can also be bookmarked, with an optional note, without leaving the
verification flow. Bookmarks are stored in ``bookmarks.tsv`` with the columns
``manuscript``, ``source``, ``folio``, ``note`` and ``timestamp`` (the most recent last).

Access is protected by a single username / password pair read from ``config.yml``,
together with the secret key used to sign the session cookie (see
``config.example.yml``).

Usage::

    uv run flask --app app run --debug

The current verdicts and bookmarks are kept in memory, so the app must run in a single
process.
"""

import csv
import hmac
import json
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response

# A JSON object: folio entry, folio metadata or IIIF canvas.
type Json = dict[str, Any]
# Key of a verdict: (manuscript, source image id, target canvas @id).
type ResultKey = tuple[str, str, str]
# Key of a bookmark: (manuscript, source image id).
type FolioKey = tuple[str, str]
# OpenSeadragon tile source: an info.json URL or a simple image {"type": "image", "url": ...}.
type TileSource = str | dict[str, str]

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yml"
DATA_DIR = BASE_DIR / "manuscript_folios"
RESULTS_TSV = BASE_DIR / "verifications.tsv"
FIELDS = ["manuscript", "source", "target", "score", "verification", "timestamp"]
VERDICTS = {"valid", "not_valid"}
# Labels of the verdicts on the home page, "unresolved" being a pair without verdict.
VERDICT_LABELS = {"valid": "Valid", "not_valid": "Not valid", "unresolved": "Non résolu"}
BOOKMARKS_TSV = BASE_DIR / "bookmarks.tsv"
BOOKMARK_FIELDS = ["manuscript", "source", "folio", "note", "timestamp"]

SOURCE_IIIF = "https://iiif.chartes.psl.eu/images/ahloma_images"
# Extensions tried in order when the source image cannot be found.
EXTENSIONS = [".jpg", ".jpeg", ".jpg2", ".jp2", ".png", ".tif", ".tiff"]

app = Flask(__name__)

# Behind the reverse proxy, the URL prefix comes from the X-Forwarded-Prefix header.
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_prefix=1,
)

# The session cookie keeps the default path "/" so that it is sent back whatever the URL
# prefix (direct access or behind the proxy); a specific name avoids clashing with the
# cookie of another Flask app on the same domain.
app.config["SESSION_COOKIE_NAME"] = "odil_session"

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


def now() -> str:
    """Return the current local time, used to timestamp verdicts and bookmarks.

    :return: Date and time in ISO 8601 format with the UTC offset, to the second.
    :rtype: str
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_results() -> dict[ResultKey, str]:
    """Load the current verdicts from the history saved in :data:`RESULTS_TSV`.

    :return: Latest verdict (``valid`` or ``not_valid``) of each pair, keyed by
        ``(manuscript, source, target)``; empty if the file does not exist yet.
    :rtype: dict[ResultKey, str]
    """
    if not RESULTS_TSV.exists():
        return {}
    with RESULTS_TSV.open(newline="") as f:
        rows = csv.DictReader(f, delimiter="\t")
        return {(r["manuscript"], r["source"], r["target"]): r["verification"] for r in rows}


def append_result(key: ResultKey, verdict: str) -> None:
    """Append a timestamped verdict at the end of :data:`RESULTS_TSV`.

    The file is a log: a new verdict on an already checked pair adds a new row, so the
    history is kept in chronological order. The header is written when the file is
    created or empty. The matching score of the pair is taken from :data:`SCORES`.

    The existing rows and the new one are written to a temporary file which then
    replaces the TSV file. This only needs write access to the directory, not to the
    file itself (``git pull`` may recreate it with another owner than the app's user),
    and the file is never left half-written.

    :param key: ``(manuscript, source, target)`` of the pair.
    :param verdict: ``valid`` or ``not_valid``.
    :return: None
    :rtype: None
    """
    rows = RESULTS_TSV.read_text() if RESULTS_TSV.exists() else ""
    if rows and not rows.endswith("\n"):
        rows += "\n"
    tmp = RESULTS_TSV.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        f.write(rows)
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        if not rows:
            writer.writerow(FIELDS)
        writer.writerow([*key, SCORES.get(key, ""), verdict, now()])
    tmp.replace(RESULTS_TSV)


def load_bookmarks() -> dict[FolioKey, dict[str, str]]:
    """Load the bookmarked folios from :data:`BOOKMARKS_TSV`.

    :return: Bookmark rows (``manuscript``, ``source``, ``folio``, ``note``,
        ``timestamp``) keyed by ``(manuscript, source)``, in the order of the file;
        empty if the file does not exist yet.
    :rtype: dict[FolioKey, dict[str, str]]
    """
    if not BOOKMARKS_TSV.exists():
        return {}
    with BOOKMARKS_TSV.open(newline="") as f:
        return {(r["manuscript"], r["source"]): r for r in csv.DictReader(f, delimiter="\t")}


def save_bookmarks() -> None:
    """Write all bookmarks to :data:`BOOKMARKS_TSV`, the most recent last.

    As in :func:`append_result`, the rows are written to a temporary file which then
    replaces the TSV file, so that only write access to the directory is needed.

    :return: None
    :rtype: None
    """
    tmp = BOOKMARKS_TSV.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, BOOKMARK_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(BOOKMARKS.values())
    tmp.replace(BOOKMARKS_TSV)


CONFIG = load_config()
app.secret_key = CONFIG["secret_key"]
MANUSCRIPTS = load_manuscripts()
SCORES = load_scores()
RESULTS = load_results()
BOOKMARKS = load_bookmarks()


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


def folio_index(manuscript: str, source: str) -> int | None:
    """Find the position of a folio in its manuscript.

    :param manuscript: Manuscript name.
    :param source: Source image id of the folio.
    :return: Position of the folio (0-based), or None if it is not in the data anymore.
    :rtype: int | None
    """
    items = MANUSCRIPTS.get(manuscript, [])
    return next((i for i, it in enumerate(items) if it["folio"]["id"] == source), None)


def wants_json() -> bool:
    """Tell whether the current request comes from the folio page's JavaScript.

    The script asks for JSON so that the page is updated in place; a plain form
    submission (without JavaScript) asks for HTML and gets a redirection.

    :return: True if JSON is the preferred response type.
    :rtype: bool
    """
    return request.accept_mimetypes.best == "application/json"


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

    Pairs are counted by current verdict: ``valid``, ``not_valid`` or ``unresolved``
    (no verdict yet), overall and for each manuscript.

    :return: Rendered ``index.html`` page.
    :rtype: str
    """
    rows = []
    for name, items in MANUSCRIPTS.items():
        pairs = [(it["folio"]["id"], c["@id"]) for it in items for c in it["canvases"]]
        counts = Counter(RESULTS.get((name, s, t), "unresolved") for s, t in pairs)
        rows.append(
            {
                "name": name,
                "folios": len(items),
                "empty": sum(not it["canvases"] for it in items),
                "pairs": len(pairs),
                "done": counts["valid"] + counts["not_valid"],
                "counts": counts,
            }
        )
    todo = next_todo(next(iter(MANUSCRIPTS))) if MANUSCRIPTS else None
    totals = sum((r["counts"] for r in rows), Counter())
    return render_template(
        "index.html",
        rows=rows,
        todo=todo,
        done=totals["valid"] + totals["not_valid"],
        total=sum(r["pairs"] for r in rows),
        totals=totals,
        verdicts=VERDICT_LABELS,
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
        bookmark=BOOKMARKS.get((manuscript, source)),
    )


@app.post("/verify")
def verify() -> Response:
    """Save the verdict of one (source, target) pair sent by a folio page form.

    Expects the form fields ``manuscript``, ``index``, ``target`` and ``verification``.
    When this verdict completes the folio, the next page is the next folio to check;
    otherwise (or when correcting an already checked folio) it is the same page.

    :return: For the page's JavaScript, JSON ``{"verdict": ..., "next": ...}`` where
        ``next`` is the URL of the next folio, or null to stay on the page; otherwise a
        redirection to the next page.
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
    key = (manuscript, item["folio"]["id"], target)
    with lock:
        RESULTS[key] = verdict
        append_result(key, verdict)
    next_url = None
    if not was_done and is_done(manuscript, item) and (todo := next_todo(manuscript, index)):
        next_url = url_for("folio", manuscript=todo[0], index=todo[1])
    if wants_json():
        return jsonify(verdict=verdict, next=next_url)
    return redirect(next_url or url_for("folio", manuscript=manuscript, index=index))


@app.post("/bookmark")
def bookmark() -> Response:
    """Add, update or remove the bookmark of a folio, sent by a folio page form.

    Expects the form fields ``manuscript``, ``index``, ``action`` (``add`` or ``remove``)
    and, for ``add``, an optional ``note``. Adding an already bookmarked folio updates its
    note and moves it to the end of the list. The folio stays in the verification flow.

    :return: For the page's JavaScript, JSON ``{"bookmark": ...}`` with the saved
        bookmark, or null once removed; otherwise a redirection to the same folio page.
    :rtype: Response
    :raises NotFound: If the manuscript or the folio does not exist.
    :raises BadRequest: If a field is missing or the action is invalid.
    """
    manuscript = request.form["manuscript"]
    index = request.form.get("index", -1, type=int)
    action = request.form["action"]
    _, item = get_item(manuscript, index)
    if action not in ("add", "remove"):
        abort(400)
    key = (manuscript, item["folio"]["id"])
    with lock:
        BOOKMARKS.pop(key, None)
        if action == "add":
            BOOKMARKS[key] = {
                "manuscript": manuscript,
                "source": key[1],
                "folio": item["folio"]["folio"],
                # Tabs and line breaks would split the TSV row: collapse whitespace.
                "note": " ".join(request.form.get("note", "").split()),
                "timestamp": now(),
            }
        save_bookmarks()
    if wants_json():
        return jsonify(bookmark=BOOKMARKS.get(key))
    return redirect(url_for("folio", manuscript=manuscript, index=index))


@app.route("/bookmarks")
def bookmarks() -> str:
    """Bookmarks page: the folios set aside, the most recent last.

    :return: Rendered ``bookmarks.html`` page.
    :rtype: str
    """
    rows = [{**b, "index": folio_index(b["manuscript"], b["source"])} for b in BOOKMARKS.values()]
    return render_template("bookmarks.html", rows=rows)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
