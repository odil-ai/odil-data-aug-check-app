#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask app to check pairings between source images and IIIF canvases.

Each JSON file in ``manuscript_folios/`` describes one manuscript: a list of folios
(source images served by the École des chartes IIIF server) together with the IIIF
canvases that were automatically reconciled with them. The app shows each source image
next to its candidate canvases and lets the user mark every pair as ``valid`` or
``not_valid``.

The data written by the app is kept in the ``storage/`` directory, created at start-up
if needed (see :func:`prepare_storage`).

Verdicts are appended to ``verifications.tsv`` with the columns ``manuscript``,
``source``, ``target``, ``score`` (matching score from the JSON files, empty if unknown),
``verification``, ``timestamp`` and ``manual`` (``true`` when the target is a candidate
added by hand). The file keeps the whole history in chronological order: the last row of
a pair gives its current verdict, ``removed`` meaning that a candidate added by hand was
removed (the pair then has no verdict).

Folios judged special can also be bookmarked, with an optional note, without leaving the
verification flow. Bookmarks are stored in ``bookmarks.tsv`` with the columns
``manuscript``, ``source``, ``folio``, ``note`` and ``timestamp`` (the most recent last).

Each source or candidate image can be annotated with its kind: ``plain``, ``detail`` or
``double_page`` (one at most; ``folio``, the former name of ``plain``, is converted when
read). Annotations are stored in ``annotations.tsv`` with the
columns ``image_iiif`` (IIIF image identifier) and ``annotation``, the most recent last;
the home page follows them up and ``report_annotations.py`` counts them.

When a folio has no right candidate, a page found by hand in a IIIF manifest (leafed
through in Mirador) can be added to it as a candidate, with the kind of its image: the
pair is then saved as ``valid``. Such candidates are stored in ``manual_candidates.tsv``
with the columns ``manuscript``, ``source``, ``canvas``, ``manifest``, ``label``,
``image_iiif``, ``width``, ``height`` and ``timestamp``, and are shown as the others.

Access is protected by a single username / password pair read from ``config.yml``,
together with the secret key used to sign the session cookie (see
``config.example.yml``).

Usage::

    uv run flask --app app run --debug

The verdicts, bookmarks, annotations and manual candidates are kept in memory and read
again from their files whenever these change on disk, and every change is made under a
lock shared by the processes of the app: several processes (e.g. ``gunicorn -w 4``) thus
show the same, up-to-date data.
"""

import csv
import fcntl
import functools
import gzip
import hmac
import io
import json
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
# Directory of the data written by the app (see prepare_storage()).
STORAGE_DIR = BASE_DIR / "storage"
# Empty file that keeps the storage directory in git, its other files being ignored.
KEEP_FILE = STORAGE_DIR / ".keepfile"
RESULTS_TSV = STORAGE_DIR / "verifications.tsv"
FIELDS = ["manuscript", "source", "target", "score", "verification", "timestamp", "manual"]
VERDICTS = {"valid", "not_valid"}
# Value of the "verification" column when a candidate added by hand is removed: the pair
# then has no verdict any more (it no longer exists).
REMOVED = "removed"
# Labels of the verdicts on the home page, "unresolved" being a pair without verdict.
VERDICT_LABELS = {"valid": "Valid", "not_valid": "Not valid", "unresolved": "Non résolu"}
BOOKMARKS_TSV = STORAGE_DIR / "bookmarks.tsv"
BOOKMARK_FIELDS = ["manuscript", "source", "folio", "note", "timestamp"]
ANNOTATIONS_TSV = STORAGE_DIR / "annotations.tsv"
ANNOTATION_FIELDS = ["image_iiif", "annotation"]
# Kinds of candidate image, as stored in the TSV file, and their labels on the folio page.
ANNOTATION_LABELS = {"plain": "Plain", "detail": "Détail", "double_page": "Double page"}
# Former names of kinds of image, converted when read (files saved before the renaming).
LEGACY_ANNOTATIONS = {"folio": "plain"}
MANUAL_TSV = STORAGE_DIR / "manual_candidates.tsv"
MANUAL_FIELDS = [
    "manuscript",
    "source",
    "canvas",
    "manifest",
    "label",
    "image_iiif",
    "width",
    "height",
    "timestamp",
]
# Lock file shared by the processes of the app, around every change of the data files.
LOCK_FILE = STORAGE_DIR / ".tsv.lock"

SOURCE_IIIF = "https://iiif.chartes.psl.eu/images/ahloma_images"
# Host of the IIIF manifests of the Vatican Library, which the home page can hide.
VATICAN_HOST = "digi.vatlib.it"
# Extensions tried in order when the source image cannot be found.
EXTENSIONS = [".jpg", ".jpeg", ".jpg2", ".jp2", ".png", ".tif", ".tiff"]
# Size of the images of the mosaic of the pairs not valid (IIIF Image API size parameter).
THUMB_SIZE = "!300,300"

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

# Serialises the changes of the data files between the threads of a process (the lock
# file does it between processes, see exclusive()).
lock = threading.Lock()


def load_config() -> Json:
    """Load the login settings from :data:`CONFIG_FILE`.

    :return: Settings with the keys ``username``, ``password`` and ``secret_key``.
    :rtype: Json
    """
    return yaml.safe_load(CONFIG_FILE.read_text())


def normalize_canvas(canvas: Json) -> Json:
    """Give an IIIF Presentation 3 canvas the fields of a Presentation 2 canvas.

    The deliveries mix both versions, while the app reads the Presentation 2 fields: the
    canvas ``@id``, ``images[0].resource`` with its image ``service``, a text or list
    ``label`` and a ``thumbnail`` object. A Presentation 2 canvas is returned unchanged;
    a Presentation 3 canvas gets these fields from its ``id``, from the body of its first
    painting annotation (``items[0].items[0].body``) and from its first image service,
    its label (language map) and its first thumbnail, its own fields being kept.

    :param canvas: IIIF Presentation 2 or 3 canvas.
    :type canvas: Json
    :return: The canvas, with the Presentation 2 fields.
    :rtype: Json
    """
    if "images" in canvas or "items" not in canvas:
        return canvas
    body = canvas["items"][0]["items"][0].get("body", {})
    services = body.get("service") or []
    service = services[0] if isinstance(services, list) and services else services
    resource = {"@id": body.get("id"), "width": body.get("width"), "height": body.get("height")}
    if service:
        resource["service"] = {"@id": service.get("id") or service.get("@id")}
    thumbnails = canvas.get("thumbnail") or []
    thumbnail = thumbnails[0] if isinstance(thumbnails, list) and thumbnails else thumbnails
    label = canvas.get("label", "")
    if isinstance(label, dict):  # language map: {"en": ["fol. 1v", ...]}
        label = [value for values in label.values() for value in values]
    return {
        **canvas,
        "@id": canvas.get("id"),
        "@type": "sc:Canvas",
        "label": label,
        "images": [{"resource": resource}],
        "thumbnail": {"@id": thumbnail.get("id") or thumbnail.get("@id")} if thumbnail else None,
    }


def load_manuscripts() -> dict[str, list[Json]]:
    """Load every manuscript JSON file from :data:`DATA_DIR`.

    The canvases are normalised to IIIF Presentation 2 (see :func:`normalize_canvas`).

    :return: Folio entries (``folio``, ``canvases``, ...) keyed by manuscript name
        (the JSON file stem, e.g. ``Q100486``), sorted by name.
    :rtype: dict[str, list[Json]]
    """
    manuscripts = {}
    for path in sorted(DATA_DIR.glob("*.json")):
        items = json.loads(path.read_text())
        for item in items:
            item["canvases"] = [normalize_canvas(canvas) for canvas in item["canvases"]]
        manuscripts[path.stem] = items
    return manuscripts


def match_score(canvas: Json) -> float | None:
    """Return the matching score of a candidate canvas.

    :param canvas: IIIF Presentation 2 canvas, possibly with a ``matchResult`` entry.
    :type canvas: Json
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
        ``(manuscript, source, target)``; empty if the file does not exist yet. A pair
        whose latest row is :data:`REMOVED` (candidate added by hand, then removed) has no
        verdict.
    :rtype: dict[ResultKey, str]
    """
    if not RESULTS_TSV.exists():
        return {}
    with RESULTS_TSV.open(newline="") as f:
        rows = csv.DictReader(f, delimiter="\t")
        latest = {(r["manuscript"], r["source"], r["target"]): r["verification"] for r in rows}
    return {key: verdict for key, verdict in latest.items() if verdict != REMOVED}


def upgrade_results(text: str) -> str:
    """Give the rows of a verifications file written before the ``manual`` column that column.

    :param text: Content of :data:`RESULTS_TSV`.
    :type text: str
    :return: The same content if its header is :data:`FIELDS`; otherwise every row kept as
        is, with the missing columns added: ``manual`` is ``true`` for a pair whose target
        is a candidate added by hand (see :data:`MANUAL`), ``false`` otherwise.
    :rtype: str
    """
    if text.split("\n", 1)[0].split("\t") == FIELDS:
        return text
    out = io.StringIO()
    writer = csv.DictWriter(out, FIELDS, delimiter="\t", lineterminator="\n", restval="")
    writer.writeheader()
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        if not row.get("manual"):
            key = (row["manuscript"], row["source"], row["target"])
            row["manual"] = "true" if key in MANUAL else "false"
        writer.writerow({field: row.get(field) or "" for field in FIELDS})
    return out.getvalue()


def append_result(key: ResultKey, verdict: str, manual: bool | None = None) -> None:
    """Append a timestamped verdict at the end of :data:`RESULTS_TSV`.

    The file is a log: a new verdict on an already checked pair adds a new row, so the
    history is kept in chronological order. The header is written when the file is
    created or empty, and a file written before the ``manual`` column gets it (see
    :func:`upgrade_results`). The matching score of the pair is taken from :data:`SCORES`.

    The existing rows and the new one are written to a temporary file which then
    replaces the TSV file. This only needs write access to the directory, not to the
    file itself (``git pull`` may recreate it with another owner than the app's user),
    and the file is never left half-written.

    :param key: ``(manuscript, source, target)`` of the pair.
    :type key: ResultKey
    :param verdict: ``valid``, ``not_valid`` or :data:`REMOVED`.
    :type verdict: str
    :param manual: True if the target is a candidate added by hand; by default, whether it
        is in :data:`MANUAL`.
    :type manual: bool | None
    :return: None
    :rtype: None
    """
    if manual is None:
        manual = key in MANUAL
    rows = RESULTS_TSV.read_text() if RESULTS_TSV.exists() else ""
    if rows:
        rows = upgrade_results(rows)
    if rows and not rows.endswith("\n"):
        rows += "\n"
    tmp = RESULTS_TSV.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        f.write(rows)
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        if not rows:
            writer.writerow(FIELDS)
        writer.writerow([*key, SCORES.get(key, ""), verdict, now(), str(manual).lower()])
    tmp.replace(RESULTS_TSV)
    STAMPS[RESULTS_TSV] = file_stamp(RESULTS_TSV)


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
    STAMPS[BOOKMARKS_TSV] = file_stamp(BOOKMARKS_TSV)


def load_annotations() -> dict[str, str]:
    """Load the annotations of the candidate images from :data:`ANNOTATIONS_TSV`.

    :return: Kind of each annotated image (``plain``, ``detail`` or ``double_page``; the
        former names of :data:`LEGACY_ANNOTATIONS` are converted, and saved under their
        new name at the next save), keyed by IIIF image identifier, in the order of the
        file; empty if the file does not exist yet.
    :rtype: dict[str, str]
    """
    if not ANNOTATIONS_TSV.exists():
        return {}
    with ANNOTATIONS_TSV.open(newline="") as f:
        rows = csv.DictReader(f, delimiter="\t")
        return {
            r["image_iiif"]: LEGACY_ANNOTATIONS.get(r["annotation"], r["annotation"]) for r in rows
        }


def save_annotations() -> None:
    """Write all annotations to :data:`ANNOTATIONS_TSV`, the most recent last.

    As in :func:`append_result`, the rows are written to a temporary file which then
    replaces the TSV file, so that only write access to the directory is needed.

    :return: None
    :rtype: None
    """
    tmp = ANNOTATIONS_TSV.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(ANNOTATION_FIELDS)
        writer.writerows(ANNOTATIONS.items())
    tmp.replace(ANNOTATIONS_TSV)
    STAMPS[ANNOTATIONS_TSV] = file_stamp(ANNOTATIONS_TSV)


def load_manual() -> dict[ResultKey, dict[str, str]]:
    """Load the candidates added by hand from :data:`MANUAL_TSV`.

    :return: Rows (see :data:`MANUAL_FIELDS`) keyed by ``(manuscript, source, canvas)``,
        in the order of the file; empty if the file does not exist yet.
    :rtype: dict[ResultKey, dict[str, str]]
    """
    if not MANUAL_TSV.exists():
        return {}
    with MANUAL_TSV.open(newline="") as f:
        rows = csv.DictReader(f, delimiter="\t")
        return {(r["manuscript"], r["source"], r["canvas"]): r for r in rows}


def save_manual() -> None:
    """Write all the candidates added by hand to :data:`MANUAL_TSV`, the most recent last.

    As in :func:`append_result`, the rows are written to a temporary file which then
    replaces the TSV file, so that only write access to the directory is needed.

    :return: None
    :rtype: None
    """
    tmp = MANUAL_TSV.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, MANUAL_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(MANUAL.values())
    tmp.replace(MANUAL_TSV)
    STAMPS[MANUAL_TSV] = file_stamp(MANUAL_TSV)


def manual_canvas(row: dict[str, str]) -> Json:
    """Build the canvas of a candidate added by hand, as the delivered ones are read.

    :param row: Row of :data:`MANUAL_TSV`.
    :type row: dict[str, str]
    :return: IIIF Presentation 2 canvas (see :func:`normalize_canvas`) marked ``manual``.
    :rtype: Json
    """
    width, height = (int(row[k]) if row.get(k, "").isdigit() else None for k in ("width", "height"))
    resource = {"@id": None, "width": width, "height": height}
    if row.get("image_iiif"):
        resource["service"] = {"@id": row["image_iiif"]}
    return {
        "@id": row["canvas"],
        "@type": "sc:Canvas",
        "label": row.get("label", ""),
        "width": width,
        "height": height,
        "images": [{"resource": resource}],
        "thumbnail": None,
        "manifest_url": row["manifest"],
        "manual": True,
    }


def apply_manual() -> None:
    """Put the candidates of :data:`MANUAL` among the candidates of their folio.

    The candidates added by hand before are first taken out, so that the folios match
    :data:`MANUAL` exactly; they come after the delivered candidates. The cached counts
    that depend on the candidates are cleared.

    :return: None
    :rtype: None
    """
    folios = {}
    for name, items in MANUSCRIPTS.items():
        for item in items:
            item["canvases"] = [c for c in item["canvases"] if not c.get("manual")]
            folios[name, item["folio"]["id"]] = item
    for (name, source, _), row in MANUAL.items():
        if item := folios.get((name, source)):
            item["canvases"].append(manual_canvas(row))
    for cached in ("image_counts", "vatican_manuscripts"):
        if cached in globals():
            globals()[cached].cache_clear()


def file_stamp(path: Path) -> tuple[int, int] | None:
    """Return what tells whether a file has changed: its modification time and size.

    :param path: File.
    :type path: Path
    :return: ``(mtime_ns, size)``, or None if the file does not exist.
    :rtype: tuple[int, int] | None
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def refresh_data() -> None:
    """Read again the data files changed on disk since they were read.

    A file changes when another process of the app (or a person) writes it. The data in
    memory is updated in place, so that every reference to it stays valid.

    :return: None
    :rtype: None
    """
    for path, data, load in (
        (RESULTS_TSV, RESULTS, load_results),
        (BOOKMARKS_TSV, BOOKMARKS, load_bookmarks),
        (ANNOTATIONS_TSV, ANNOTATIONS, load_annotations),
        (MANUAL_TSV, MANUAL, load_manual),
    ):
        stamp = file_stamp(path)
        if stamp != STAMPS.get(path, "unread"):
            data.clear()
            data.update(load())
            STAMPS[path] = stamp
            if path == MANUAL_TSV:
                apply_manual()


@contextmanager
def exclusive() -> Iterator[None]:
    """Make a change of the data files alone, with up-to-date data.

    The change is serialised with the other threads (:data:`lock`) and with the other
    processes of the app (lock on :data:`LOCK_FILE`), and the files changed meanwhile are
    read again first (see :func:`refresh_data`).

    :return: A context in which to change the data and save it.
    :rtype: Iterator[None]
    """
    with lock, LOCK_FILE.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            refresh_data()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


CONFIG = load_config()
app.secret_key = CONFIG["secret_key"]
MANUSCRIPTS = load_manuscripts()
SCORES = load_scores()


def prepare_storage() -> None:
    """Make sure that the storage directory and its data files exist.

    :data:`STORAGE_DIR` is created if needed, with an empty :data:`KEEP_FILE`. Each data
    file that is already in it is used as it is. A missing one is taken from the app
    directory, where the data files were kept before (it is moved, so that nothing is
    lost), or else created with its header row only. A file found in both places is
    taken from the storage directory, the other one being left untouched (with a
    warning).

    :return: None
    :rtype: None
    """
    STORAGE_DIR.mkdir(exist_ok=True)
    KEEP_FILE.touch(exist_ok=True)
    for path, fields in (
        (RESULTS_TSV, FIELDS),
        (BOOKMARKS_TSV, BOOKMARK_FIELDS),
        (ANNOTATIONS_TSV, ANNOTATION_FIELDS),
        (MANUAL_TSV, MANUAL_FIELDS),
    ):
        former = BASE_DIR / path.name
        if path.exists():
            if former.exists():
                app.logger.warning("%s is used; %s is left aside, unused", path, former)
            continue
        try:
            former.rename(path)
        except FileNotFoundError:  # no former file (or another process just moved it)
            if not path.exists():
                with path.open("w", newline="") as f:
                    csv.writer(f, delimiter="\t", lineterminator="\n").writerow(fields)


# Data of the files, read by refresh_data() at start-up and whenever a file changes.
RESULTS: dict[ResultKey, str] = {}
BOOKMARKS: dict[FolioKey, dict[str, str]] = {}
ANNOTATIONS: dict[str, str] = {}
MANUAL: dict[ResultKey, dict[str, str]] = {}
# What each data file was like when last read or written (see file_stamp()).
STAMPS: dict[Path, tuple[int, int] | None] = {}
prepare_storage()
refresh_data()


def check_credentials(username: str, password: str) -> bool:
    """Compare submitted credentials with the ones of :data:`CONFIG_FILE`.

    :param username: Submitted username.
    :type username: str
    :param password: Submitted password.
    :type password: str
    :return: True if both the username and the password match.
    :rtype: bool
    """
    same_user = hmac.compare_digest(username.encode(), str(CONFIG["username"]).encode())
    same_password = hmac.compare_digest(password.encode(), str(CONFIG["password"]).encode())
    return same_user and same_password


def source_extensions(folio: Json) -> list[str]:
    """Return the extensions to try for a source image on :data:`SOURCE_IIIF`.

    :param folio: Folio metadata (``id``, ``filename``, ...).
    :type folio: Json
    :return: The extensions of :data:`EXTENSIONS`, starting with the one of the folio
        filename (if any).
    :rtype: list[str]
    """
    ext = Path(folio["filename"]).suffix
    return [e for e in dict.fromkeys([ext, *EXTENSIONS]) if e]


def source_images(folio: Json) -> list[str]:
    """Return the possible IIIF identifiers of a source image, one per extension.

    Which one exists on :data:`SOURCE_IIIF` is only known once the browser has opened
    it: the folio page then sends that one with the annotation of the image.

    :param folio: Folio metadata (``id``, ``filename``, ...).
    :type folio: Json
    :return: IIIF image identifiers, in the order of :func:`source_extensions`.
    :rtype: list[str]
    """
    return [f"{SOURCE_IIIF}/{folio['id']}{e}" for e in source_extensions(folio)]


def source_tiles(folio: Json) -> list[TileSource]:
    """Build the OpenSeadragon tile sources of a source image.

    :param folio: Folio metadata (``id``, ``filename``, ...).
    :type folio: Json
    :return: info.json URLs to try in order, one per possible identifier (see
        :func:`source_images`).
    :rtype: list[TileSource]
    """
    return [f"{image}/info.json" for image in source_images(folio)]


def source_thumbnails(folio: Json) -> list[str]:
    """Build the URLs of a small version of a source image.

    :param folio: Folio metadata (``id``, ``filename``, ...).
    :type folio: Json
    :return: Image URLs (size :data:`THUMB_SIZE`) to try in order, one per extension (see
        :func:`source_extensions`).
    :rtype: list[str]
    """
    return [
        f"{SOURCE_IIIF}/{folio['id']}{e}/full/{THUMB_SIZE}/0/default.jpg"
        for e in source_extensions(folio)
    ]


def image_id(canvas: Json) -> str:
    """Return the IIIF identifier of the image of a canvas, the key of its annotation.

    :param canvas: IIIF Presentation 2 canvas.
    :type canvas: Json
    :return: The @id of its IIIF image service, or the @id of the image itself (or of the
        canvas) when it has no service.
    :rtype: str
    """
    resource = canvas["images"][0]["resource"]
    return resource.get("service", {}).get("@id") or resource.get("@id") or canvas["@id"]


def target_tiles(canvas: Json) -> list[TileSource]:
    """Build the OpenSeadragon tile sources of a candidate canvas.

    :param canvas: IIIF Presentation 2 canvas.
    :type canvas: Json
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


def target_thumbnails(canvas: Json) -> list[str]:
    """Build the URLs of a small version of the image of a candidate canvas.

    :param canvas: IIIF Presentation 2 canvas.
    :type canvas: Json
    :return: Image URLs to try in order: the IIIF image service at :data:`THUMB_SIZE`
        (Image API 2, then 1.1), the thumbnail of the canvas, then its raw image.
    :rtype: list[str]
    """
    resource = canvas["images"][0]["resource"]
    urls = []
    if service := resource.get("service", {}).get("@id"):
        urls += [
            f"{service}/full/{THUMB_SIZE}/0/{quality}.jpg" for quality in ("default", "native")
        ]
    urls += [u for u in ((canvas.get("thumbnail") or {}).get("@id"), resource.get("@id")) if u]
    return urls


def label(canvas: Json) -> str:
    """Return the label of a canvas as text.

    :param canvas: IIIF Presentation 2 canvas (see :func:`normalize_canvas`).
    :type canvas: Json
    :return: The label; values of a multi-valued label are joined with a dash.
    :rtype: str
    """
    value = canvas.get("label", "")
    return " — ".join(map(str, value)) if isinstance(value, list) else str(value)


def candidates(item: Json, scored_only: bool = False) -> list[Json]:
    """Return the candidate canvases of a folio that are taken into account.

    :param item: Folio entry (``folio`` and ``canvases``).
    :type item: Json
    :param scored_only: True to keep only the candidates with a matching score.
    :type scored_only: bool
    :return: The candidate canvases, all of them or only those with a score.
    :rtype: list[Json]
    """
    if scored_only:
        return [c for c in item["canvases"] if match_score(c) is not None]
    return item["canvases"]


def is_done(manuscript: str, item: Json, scored_only: bool = False) -> bool:
    """Tell whether every candidate of a folio taken into account has a verdict.

    :param manuscript: Manuscript name.
    :type manuscript: str
    :param item: Folio entry (``folio`` and ``canvases``).
    :type item: Json
    :param scored_only: True to take into account only the candidates with a score.
    :type scored_only: bool
    :return: True if all these pairs are checked (always True for a folio without such
        candidate).
    :rtype: bool
    """
    source = item["folio"]["id"]
    return all((manuscript, source, c["@id"]) in RESULTS for c in candidates(item, scored_only))


def next_todo(
    manuscript: str,
    index: int = -1,
    scored_only: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> tuple[str, int] | None:
    """Find the next folio to check, after ``index`` in this manuscript, then in the next ones.

    :param manuscript: Manuscript to start from.
    :type manuscript: str
    :param index: Position of the current folio; the search starts right after it
        (``-1`` to start at the first folio).
    :type index: int
    :param scored_only: True to look only for pairs with a score left to check.
    :type scored_only: bool
    :param exclude: Manuscripts to skip (except the one to start from).
    :type exclude: frozenset[str]
    :return: ``(manuscript, index)`` of the next folio to check, or None if all are checked.
    :rtype: tuple[str, int] | None
    """
    names = list(MANUSCRIPTS)
    start = names.index(manuscript)
    for name in names[start:] + names[:start]:
        if name != manuscript and name in exclude:
            continue
        items = MANUSCRIPTS[name]
        first = index + 1 if name == manuscript else 0
        for i in range(first, len(items)):
            if not is_done(name, items[i], scored_only):
                return name, i
    return None


def folio_index(manuscript: str, source: str) -> int | None:
    """Find the position of a folio in its manuscript.

    :param manuscript: Manuscript name.
    :type manuscript: str
    :param source: Source image id of the folio.
    :type source: str
    :return: Position of the folio (0-based), or None if it is not in the data anymore.
    :rtype: int | None
    """
    items = MANUSCRIPTS.get(manuscript, [])
    return next((i for i, it in enumerate(items) if it["folio"]["id"] == source), None)


def pie_slices(counts: Counter, total: int) -> list[dict[str, Any]]:
    """Build the slices of the pie chart of the pairs by verdict.

    Each non-empty verdict gets a slice sized after its share of the pairs, in percent of
    the circle; a share under 0.5 % is drawn at 0.5 % so that it stays visible (the exact
    figures are given by the legend).

    :param counts: Number of pairs by verdict (``valid``, ``not_valid``, ``unresolved``).
    :type counts: Counter
    :param total: Number of pairs.
    :type total: int
    :return: Slices in the order of :data:`VERDICT_LABELS`, with ``key``, ``label``,
        ``count``, ``start`` and ``size`` (both in percent of the circle).
    :rtype: list[dict[str, Any]]
    """
    if not total:
        return []
    sizes = {key: max(100 * counts[key] / total, 0.5) for key in VERDICT_LABELS if counts[key]}
    scale = 100 / sum(sizes.values())
    slices, start = [], 0.0
    for key, size in sizes.items():
        size *= scale
        slices.append(
            {
                "key": key,
                "label": VERDICT_LABELS[key],
                "count": counts[key],
                "start": round(start, 3),
                "size": round(size, 3),
            }
        )
        start += size
    return slices


@functools.cache
def image_counts() -> tuple[int, int]:
    """Count the source images and the distinct candidate images (computed once).

    :return: Number of folios with a source image file, and number of distinct candidate
        images (see :func:`image_id`).
    :rtype: tuple[int, int]
    """
    items = [item for items in MANUSCRIPTS.values() for item in items]
    sources = sum(item["folio"]["filename"] != "null" for item in items)
    candidates = {image_id(c) for item in items for c in item["canvases"]}
    return sources, len(candidates)


def annotation_stats() -> dict[str, Any]:
    """Count the annotations by kind, for the source images and the candidate images.

    An annotated image is a source image when its identifier is on :data:`SOURCE_IIIF`,
    a candidate image otherwise. The counts ignore the filters of the home page.

    :return: ``kinds`` (one dict per kind of :data:`ANNOTATION_LABELS`: ``key``,
        ``label``, ``source`` and ``candidate`` counts), the totals ``source`` and
        ``candidate``, and the numbers of images ``source_images`` and
        ``candidate_images``.
    :rtype: dict[str, Any]
    """
    counts = Counter(
        (kind, image.startswith(SOURCE_IIIF + "/")) for image, kind in ANNOTATIONS.items()
    )
    kinds = [
        {"key": key, "label": label, "source": counts[key, True], "candidate": counts[key, False]}
        for key, label in ANNOTATION_LABELS.items()
    ]
    source_images_count, candidate_images_count = image_counts()
    return {
        "kinds": kinds,
        "source": sum(k["source"] for k in kinds),
        "candidate": sum(k["candidate"] for k in kinds),
        "source_images": source_images_count,
        "candidate_images": candidate_images_count,
    }


@functools.cache
def vatican_manuscripts() -> frozenset[str]:
    """Return the manuscripts of the Vatican Library (computed once).

    :return: Names of the manuscripts with a candidate whose IIIF manifest is on
        :data:`VATICAN_HOST`.
    :rtype: frozenset[str]
    """
    return frozenset(
        name
        for name, items in MANUSCRIPTS.items()
        if any(
            urlparse(c.get("manifest_url") or c["@id"]).netloc == VATICAN_HOST
            for item in items
            for c in item["canvases"]
        )
    )


def hidden_manuscripts() -> frozenset[str]:
    """Read the "Vatican manuscripts" filter for the current request.

    The query parameter ``vatican`` (``hide`` or ``show``) sets the filter, which is then
    kept in the session; the Vatican manuscripts are shown by default.

    :return: The manuscripts to hide (see :func:`vatican_manuscripts`), or none.
    :rtype: frozenset[str]
    """
    if "vatican" in request.args:
        session["hide_vatican"] = request.args["vatican"] == "hide"
    return vatican_manuscripts() if session.get("hide_vatican", False) else frozenset()


def annotated_source_folios() -> set[str]:
    """Return the ids of the folios whose source image is annotated.

    The identifier of a source image is ``<SOURCE_IIIF>/<folio id><extension>`` (see
    :func:`source_images`), whatever the extension it was saved with.

    :return: Ids of the folios with an annotated source image.
    :rtype: set[str]
    """
    prefix = SOURCE_IIIF + "/"
    return {Path(image[len(prefix) :]).stem for image in ANNOTATIONS if image.startswith(prefix)}


def scored_filter() -> bool:
    """Read the "pairs with a score only" filter for the current request.

    The query parameter ``scored`` (``only`` or ``all``) sets the filter, which is then
    kept in the session; otherwise the value of the session is used, the filter being on
    by default. It applies to the home page, the bookmarks and the verification flow.

    :return: True when only the pairs with a matching score are taken into account.
    :rtype: bool
    """
    if "scored" in request.args:
        session["scored_only"] = request.args["scored"] == "only"
    return session.get("scored_only", True)


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
    :type manuscript: str
    :param index: Position of the folio in the manuscript (0-based).
    :type index: int
    :return: All folio entries of the manuscript and the requested one.
    :rtype: tuple[list[Json], Json]
    :raises NotFound: If the manuscript or the folio does not exist.
    """
    items = MANUSCRIPTS.get(manuscript)
    if not items or not 0 <= index < len(items):
        abort(404)
    return items, items[index]


@app.before_request
def read_changed_files() -> None:
    """Read again the data files changed on disk (see :func:`refresh_data`).

    :return: None
    :rtype: None
    """
    refresh_data()


@app.before_request
def require_login() -> Response | None:
    """Send users who are not logged in to the login page.

    :return: Redirection to the login page, or None to go on with the request.
    :rtype: Response | None
    """
    if not session.get("logged_in") and request.endpoint not in ("login", "static"):
        return redirect(url_for("login"))
    return None


@app.after_request
def no_store(response: Response) -> Response:
    """Forbid keeping the pages in a cache, so that they always show the saved data.

    :param response: Response about to be sent.
    :type response: Response
    :return: The response, with ``Cache-Control: no-store`` for the HTML pages.
    :rtype: Response
    """
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def compress(response: Response) -> Response:
    """Compress HTML pages with gzip when the browser accepts it.

    The home page weighs about 1.4 MB of HTML (thousands of rows) but less than 60 KB once
    compressed, which makes a large difference on a remote connection. A response that is
    already encoded (e.g. by a reverse proxy) is left as is.

    :param response: Response about to be sent.
    :type response: Response
    :return: The response, compressed if possible.
    :rtype: Response
    """
    if (
        response.mimetype == "text/html"
        and not response.direct_passthrough
        and "Content-Encoding" not in response.headers
        and "gzip" in request.headers.get("Accept-Encoding", "")
    ):
        response.set_data(gzip.compress(response.get_data(), compresslevel=6))
        response.headers["Content-Encoding"] = "gzip"
        response.vary.add("Accept-Encoding")
    return response


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
    """Home page: corpus figures, pairs by verdict and the list of manuscripts.

    Pairs are counted by current verdict: ``valid``, ``not_valid`` or ``unresolved``
    (no verdict yet), overall and for each manuscript. For each manuscript, the images
    already annotated (plain, detail or double page) are counted too, out of the source
    images of its folios and its distinct candidate images, as shown by the filters; and
    so are they for all the manuscripts listed.

    The query parameter ``empty`` (``show`` or ``hide``) shows or hides the folios without
    candidate. Once hidden, they are left out of the folio counts, and the manuscripts
    whose folios all lack candidates are left out of the list.

    The query parameter ``scored`` (``only`` or ``all``) keeps only the pairs with a
    matching score, or all of them: the counts, the list and the "Continuer" link then
    take into account only these pairs (and the folios without candidate are hidden, as
    they have none). The query parameter ``vatican`` (``hide`` or ``show``) hides or shows
    the manuscripts of the Vatican Library, for the list, the counts and the "Continuer"
    link (see :func:`hidden_manuscripts`). The choices are kept in the session; only the
    pairs with a score are kept by default (see :func:`scored_filter`).

    :return: Rendered ``index.html`` page.
    :rtype: str
    """
    if "empty" in request.args:
        session["hide_empty"] = request.args["empty"] == "hide"
    scored_only = scored_filter()
    hide_empty = session.get("hide_empty", False) or scored_only
    excluded = hidden_manuscripts()
    annotated_sources = annotated_source_folios()
    shown_sources, shown_images = [], set()
    rows = []
    for name, items in MANUSCRIPTS.items():
        if name in excluded:
            continue
        kept = [it for it in items if candidates(it, scored_only)] if hide_empty else items
        if not kept:
            continue
        pairs = [(it["folio"]["id"], c["@id"]) for it in kept for c in candidates(it, scored_only)]
        counts = Counter(RESULTS.get((name, s, t), "unresolved") for s, t in pairs)
        sources = [it["folio"]["id"] for it in kept if it["folio"]["filename"] != "null"]
        images = {image_id(c) for it in kept for c in candidates(it, scored_only)}
        sources_left = sum(folio not in annotated_sources for folio in sources)
        images_left = sum(image not in ANNOTATIONS for image in images)
        shown_sources += sources
        shown_images |= images
        rows.append(
            {
                "name": name,
                "folios": len(kept),
                "empty": sum(not it["canvases"] for it in items),
                "pairs": len(pairs),
                "done": counts["valid"] + counts["not_valid"],
                "counts": counts,
                "to_annotate": {
                    "left": sources_left + images_left,
                    "total": len(sources) + len(images),
                    "sources_left": sources_left,
                    "sources": len(sources),
                    "candidates_left": images_left,
                    "candidates": len(images),
                },
            }
        )
    first = next((name for name in MANUSCRIPTS if name not in excluded), None)
    todo = next_todo(first, scored_only=scored_only, exclude=excluded) if first else None
    totals = sum((r["counts"] for r in rows), Counter())
    return render_template(
        "index.html",
        rows=rows,
        todo=todo,
        hide_empty=hide_empty,
        scored_only=scored_only,
        hide_vatican=bool(excluded),
        images_annotated=sum(f in annotated_sources for f in shown_sources)
        + sum(image in ANNOTATIONS for image in shown_images),
        images_total=len(shown_sources) + len(shown_images),
        manuscripts=len(rows),
        hidden=len(MANUSCRIPTS) - len(rows),
        folios=sum(r["folios"] for r in rows),
        empty=sum(not it["canvases"] for items in MANUSCRIPTS.values() for it in items),
        done=totals["valid"] + totals["not_valid"],
        total=sum(r["pairs"] for r in rows),
        totals=totals,
        verdicts=VERDICT_LABELS,
        slices=pie_slices(totals, sum(r["pairs"] for r in rows)),
        annotations=annotation_stats(),
    )


@app.route("/<manuscript>/")
def manuscript(manuscript: str) -> Response:
    """Open a manuscript at its first folio left to check (its first folio if all are done).

    Only the pairs with a score are taken into account when the home page keeps only them.

    :param manuscript: Manuscript name.
    :type manuscript: str
    :return: Redirection to the folio page.
    :rtype: Response
    :raises NotFound: If the manuscript does not exist.
    """
    items = MANUSCRIPTS.get(manuscript) or abort(404)
    scored_only = scored_filter()
    index = next((i for i, it in enumerate(items) if not is_done(manuscript, it, scored_only)), 0)
    return redirect(url_for("folio", manuscript=manuscript, index=index))


@app.route("/<manuscript>/<int:index>")
def folio(manuscript: str, index: int) -> str:
    """Comparison page: the source image of a folio next to its candidate canvases.

    :param manuscript: Manuscript name.
    :type manuscript: str
    :param index: Position of the folio in the manuscript (0-based).
    :type index: int
    :return: Rendered ``folio.html`` page.
    :rtype: str
    :raises NotFound: If the manuscript or the folio does not exist.
    """
    items, item = get_item(manuscript, index)
    source = item["folio"]["id"]
    # The identifier of the source image already annotated, if any, else the most likely.
    images = source_images(item["folio"])
    source_image = next((image for image in images if image in ANNOTATIONS), images[0])
    candidates = [
        {
            "id": c["@id"],
            "label": label(c),
            "tiles": target_tiles(c),
            "manifest": c.get("manifest_url"),
            "score": match_score(c),
            "verdict": RESULTS.get((manuscript, source, c["@id"])),
            "annotation": ANNOTATIONS.get(image_id(c)),
            "manual": c.get("manual", False),
        }
        for c in item["canvases"]
    ]
    # Manifests of the candidates of the manuscript, the most frequent first: the ones to
    # leaf through to add a candidate by hand.
    manifests = Counter(
        c["manifest_url"] for it in items for c in it["canvases"] if c.get("manifest_url")
    )
    return render_template(
        "folio.html",
        manuscript=manuscript,
        index=index,
        count=len(items),
        folio=item["folio"],
        source_tiles=source_tiles(item["folio"]),
        source_image=source_image,
        source_annotation=ANNOTATIONS.get(source_image),
        candidates=candidates,
        bookmark=BOOKMARKS.get((manuscript, source)),
        annotation_labels=ANNOTATION_LABELS,
        manifests=[url for url, _ in manifests.most_common()],
    )


@app.post("/verify")
def verify() -> Response:
    """Save the verdict of one (source, target) pair sent by a folio page form.

    Expects the form fields ``manuscript``, ``index``, ``target`` and ``verification``.
    When this verdict completes the folio, the next page is the next folio to check;
    otherwise (or when correcting an already checked folio) it is the same page. When the
    home page keeps only the pairs with a score, only these pairs are taken into account;
    when it hides the Vatican manuscripts, the next folio is not taken from them.

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
    scored_only = scored_filter()
    key = (manuscript, item["folio"]["id"], target)
    with exclusive():
        was_done = is_done(manuscript, item, scored_only)
        RESULTS[key] = verdict
        append_result(key, verdict)
    next_url = None
    if (
        not was_done
        and is_done(manuscript, item, scored_only)
        and (todo := next_todo(manuscript, index, scored_only, hidden_manuscripts()))
    ):
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
    with exclusive():
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


@app.post("/annotate")
def annotate() -> Response:
    """Save the kind of a source or candidate image (or remove it), sent by a folio page.

    Expects the form fields ``manuscript``, ``index``, ``annotation`` (one of
    :data:`ANNOTATION_LABELS`, or none to remove the annotation) and either ``target``
    (candidate canvas) or ``image`` (identifier of the source image opened by the page,
    one of :func:`source_images`; an annotation saved under another identifier of the
    same source image is replaced). An image has one kind at most: if several are sent
    (form submitted without JavaScript), the one that differs from the saved kind wins.
    The annotation belongs to the image, so it is shared by all the folios that have
    this image.

    :return: For the page's JavaScript, JSON ``{"annotation": ...}`` with the saved kind,
        or null once removed; otherwise a redirection to the same folio page.
    :rtype: Response
    :raises NotFound: If the manuscript or the folio does not exist.
    :raises BadRequest: If a field is missing, or the image or the kind is invalid.
    """
    manuscript = request.form["manuscript"]
    index = request.form.get("index", -1, type=int)
    _, item = get_item(manuscript, index)
    if "target" in request.form:
        canvas = next((c for c in item["canvases"] if c["@id"] == request.form["target"]), None)
        keys = [image_id(canvas)] if canvas else []
    else:
        image = request.form.get("image")
        images = source_images(item["folio"])
        keys = [image, *(other for other in images if other != image)] if image in images else []
    kinds = [LEGACY_ANNOTATIONS.get(kind, kind) for kind in request.form.getlist("annotation")]
    if not keys or any(kind not in ANNOTATION_LABELS for kind in kinds):
        abort(400)
    with exclusive():
        current = next((ANNOTATIONS[key] for key in keys if key in ANNOTATIONS), None)
        new = [kind for kind in kinds if kind != current]
        kind = new[-1] if new else (kinds[0] if kinds else None)
        for key in keys:
            ANNOTATIONS.pop(key, None)
        if kind:
            ANNOTATIONS[keys[0]] = kind
        save_annotations()
    if wants_json():
        return jsonify(annotation=kind)
    return redirect(url_for("folio", manuscript=manuscript, index=index))


def is_url(value: str) -> bool:
    """Tell whether a form value is a web address that can be saved.

    :param value: Value sent by a form.
    :type value: str
    :return: True for an http(s) URL without white space, of reasonable length.
    :rtype: bool
    """
    return (
        value.startswith(("http://", "https://"))
        and len(value) < 2000
        and not any(char.isspace() for char in value)
    )


@app.post("/manual")
def add_manual() -> Response:
    """Add to a folio a candidate found by hand in a IIIF manifest, as a valid pair.

    Expects the form fields ``manuscript``, ``index``, ``canvas`` and ``manifest`` (URLs),
    and the ``label``, ``image`` (IIIF image service), ``width`` and ``height`` of the
    canvas as read in the manifest by the page, and an optional ``annotation`` (kind of
    the image, see :data:`ANNOTATION_LABELS`). The candidate is saved in
    :data:`MANUAL_TSV`, the pair gets the verdict ``valid``, and the image its kind.

    :return: For the page's JavaScript, JSON ``{"canvas": ...}``; otherwise a redirection to
        the same folio page.
    :rtype: Response
    :raises NotFound: If the manuscript or the folio does not exist.
    :raises BadRequest: If a field is missing or invalid.
    :raises Conflict: If the canvas is already a candidate of the folio.
    """
    manuscript = request.form["manuscript"]
    index = request.form.get("index", -1, type=int)
    _, item = get_item(manuscript, index)
    canvas, manifest = request.form["canvas"].strip(), request.form["manifest"].strip()
    image = request.form.get("image", "").strip()
    kind = LEGACY_ANNOTATIONS.get(
        request.form.get("annotation", ""), request.form.get("annotation", "")
    )
    sizes = [request.form.get(name, "").strip() for name in ("width", "height")]
    if (
        not is_url(canvas)
        or not is_url(manifest)
        or (image and not is_url(image))
        or (kind and kind not in ANNOTATION_LABELS)
        or any(size and not size.isdigit() for size in sizes)
    ):
        abort(400)
    key = (manuscript, item["folio"]["id"], canvas)
    with exclusive():
        if canvas in {c["@id"] for c in item["canvases"]}:
            abort(409)
        MANUAL[key] = {
            "manuscript": manuscript,
            "source": key[1],
            "canvas": canvas,
            "manifest": manifest,
            # Tabs and line breaks would split the TSV row: collapse whitespace.
            "label": " ".join(request.form.get("label", "").split())[:300],
            "image_iiif": image,
            "width": sizes[0],
            "height": sizes[1],
            "timestamp": now(),
        }
        save_manual()
        apply_manual()
        RESULTS[key] = "valid"
        append_result(key, "valid")
        if image and kind:
            ANNOTATIONS.pop(image, None)
            ANNOTATIONS[image] = kind
            save_annotations()
    if wants_json():
        return jsonify(canvas=canvas)
    return redirect(url_for("folio", manuscript=manuscript, index=index))


@app.post("/manual/remove")
def remove_manual() -> Response:
    """Remove a candidate added by hand to a folio.

    Expects the form fields ``manuscript``, ``index`` and ``canvas``. A row
    :data:`REMOVED` is appended to :data:`RESULTS_TSV`, so that the pair no longer has a
    verdict there either; its former verdicts stay as history, and the annotation of the
    image in its file.

    :return: Redirection to the same folio page.
    :rtype: Response
    :raises NotFound: If the folio does not exist or has no such candidate added by hand.
    """
    manuscript = request.form["manuscript"]
    index = request.form.get("index", -1, type=int)
    _, item = get_item(manuscript, index)
    key = (manuscript, item["folio"]["id"], request.form["canvas"])
    with exclusive():
        if MANUAL.pop(key, None) is None:
            abort(404)
        save_manual()
        apply_manual()
        RESULTS.pop(key, None)
        append_result(key, REMOVED, manual=True)
    return redirect(url_for("folio", manuscript=manuscript, index=index))


@app.route("/bookmarks")
def bookmarks() -> str:
    """Bookmarks page: the folios set aside, the most recent last.

    With the "pairs with a score only" filter (see :func:`scored_filter`), only the folios
    with at least one candidate with a score are listed.

    :return: Rendered ``bookmarks.html`` page.
    :rtype: str
    """
    scored_only = scored_filter()
    rows = []
    for b in BOOKMARKS.values():
        index = folio_index(b["manuscript"], b["source"])
        canvases = MANUSCRIPTS[b["manuscript"]][index]["canvases"] if index is not None else []
        scores = [score for c in canvases if (score := match_score(c)) is not None]
        if scored_only and not scores:
            continue
        rows.append({**b, "index": index, "score": max(scores, default=None)})
    return render_template(
        "bookmarks.html", rows=rows, total=len(BOOKMARKS), scored_only=scored_only
    )


@app.route("/rejected")
def rejected() -> str:
    """Mosaic of the pairs with a score checked as not valid, the highest scores first.

    Each tile shows the source image next to the candidate and links to the folio page,
    on that candidate.

    :return: Rendered ``rejected.html`` page.
    :rtype: str
    """
    tiles = []
    for name, items in MANUSCRIPTS.items():
        for index, item in enumerate(items):
            for position, canvas in enumerate(item["canvases"]):
                score = match_score(canvas)
                key = (name, item["folio"]["id"], canvas["@id"])
                if score is None or RESULTS.get(key) != "not_valid":
                    continue
                tiles.append(
                    {
                        "manuscript": name,
                        "index": index,
                        "position": position,
                        "folio": item["folio"]["folio"],
                        "label": label(canvas),
                        "score": score,
                        "source": source_thumbnails(item["folio"]),
                        "target": target_thumbnails(canvas),
                    }
                )
    tiles.sort(key=lambda tile: tile["score"], reverse=True)
    return render_template("rejected.html", tiles=tiles)


@app.route("/<manuscript>/<int:index>/<int:position>/mirador")
def mirador(manuscript: str, index: int, position: int) -> str:
    """Mirador viewer on the IIIF manifest of a candidate, opened at its canvas.

    The whole digitised manuscript can then be leafed through, starting from the page
    matched with the source image.

    :param manuscript: Manuscript name.
    :type manuscript: str
    :param index: Position of the folio in the manuscript (0-based).
    :type index: int
    :param position: Position of the candidate among the candidates of the folio.
    :type position: int
    :return: Rendered ``mirador.html`` page.
    :rtype: str
    :raises NotFound: If the folio or the candidate does not exist, or if the candidate
        has no manifest.
    """
    _, item = get_item(manuscript, index)
    if not 0 <= position < len(item["canvases"]):
        abort(404)
    canvas = item["canvases"][position]
    if not canvas.get("manifest_url"):
        abort(404)
    return render_template(
        "mirador.html",
        manifest=canvas["manifest_url"],
        canvas=canvas["@id"],
        title=f"{manuscript} — {item['folio']['folio']} — {label(canvas)}",
    )


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
