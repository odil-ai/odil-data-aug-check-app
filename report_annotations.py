#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report on the annotations of the images saved by the app.

Reads ``storage/annotations.tsv`` (columns ``image_iiif`` and ``annotation``) and prints the
number of annotated images by kind (plain, detail, double page), for the source images
and the candidate images, with the share of each kind. When the ``manuscript_folios/``
directory is available, it also gives how many of the source and candidate images are
annotated.

An image is a source image when its identifier is on the IIIF server of the source
images (:data:`SOURCE_IIIF`), a candidate image otherwise, as on the home page of the app.

Only the standard library is used, so that the report can be made from the TSV file
alone, without the app or its configuration.

Usage::

    uv run python report_annotations.py [storage/annotations.tsv] [--data manuscript_folios]
"""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).parent
# Same values as in app.py: IIIF server of the source images, labels of the kinds of
# image in display order.
SOURCE_IIIF = "https://iiif.chartes.psl.eu/images/ahloma_images"
LABELS = {"plain": "Plain", "detail": "Détail", "double_page": "Double page"}
# Former names of kinds of image, counted under their new name (as in app.py).
LEGACY = {"folio": "plain"}


def read_annotations(path: Path) -> Counter:
    """Count the annotated images by kind and by origin (source or candidate image).

    :param path: TSV file of the annotations.
    :type path: Path
    :return: Number of images keyed by ``(kind, is_source)``.
    :rtype: Counter
    """
    with path.open(newline="") as f:
        return Counter(
            (
                LEGACY.get(row["annotation"], row["annotation"]),
                row["image_iiif"].startswith(SOURCE_IIIF + "/"),
            )
            for row in csv.DictReader(f, delimiter="\t")
        )


def image_id(canvas: dict) -> str | None:
    """Return the IIIF identifier of the image of a canvas, as the app does (``app.image_id``).

    Both IIIF Presentation 2 (``images[0].resource``) and 3 (``items[0].items[0].body``)
    canvases are read: the @id or id of the first image service, or else of the image, or
    else of the canvas.

    :param canvas: IIIF Presentation 2 or 3 canvas.
    :type canvas: dict
    :return: The image identifier, or None if the canvas has none.
    :rtype: str | None
    """
    if "images" in canvas:
        resource = canvas["images"][0]["resource"]
    else:
        resource = canvas["items"][0]["items"][0].get("body", {})
    services = resource.get("service") or []
    service = services[0] if isinstance(services, list) and services else services
    service_id = (service.get("@id") or service.get("id")) if service else None
    return (
        service_id
        or resource.get("@id")
        or resource.get("id")
        or canvas.get("@id")
        or canvas.get("id")
    )


def count_images(data_dir: Path) -> tuple[int, int]:
    """Count the source images and the distinct candidate images of the manuscripts.

    Source images are the folios with an image file; candidate images are identified as
    in the app (see :func:`image_id`).

    :param data_dir: Directory of the manuscript JSON files.
    :type data_dir: Path
    :return: Number of source images and number of distinct candidate images.
    :rtype: tuple[int, int]
    """
    sources, candidates = 0, set()
    for path in data_dir.glob("*.json"):
        for item in json.loads(path.read_text()):
            sources += item["folio"]["filename"] != "null"
            candidates.update(image_id(canvas) for canvas in item["canvases"])
    return sources, len(candidates)


def num(n: int) -> str:
    """Format a number with a narrow no-break space as thousands separator.

    :param n: Number to format.
    :type n: int
    :return: The formatted number, e.g. ``31 960``.
    :rtype: str
    """
    return f"{n:,}".replace(",", " ")


def pct(n: int, total: int) -> str:
    """Format the share of a number in a total as a percentage.

    :param n: Part of the total.
    :type n: int
    :param total: Total (0 gives 0 %).
    :type total: int
    :return: The share with one decimal and a decimal comma, e.g. ``12,5 %``; a share that
        would round to 0 or 100 without being so is given as ``< 0,1 %`` or ``> 99,9 %``
        (as on the home page of the app).
    :rtype: str
    """
    share = 100 * n / total if total else 0
    if 0 < share < 0.05:
        return "< 0,1 %"
    if 99.95 <= share < 100:
        return "> 99,9 %"
    return f"{share:.1f} %".replace(".", ",")


def report(counts: Counter, images: tuple[int, int] | None) -> str:
    """Build the text of the report.

    :param counts: Number of annotated images keyed by ``(kind, is_source)``.
    :type counts: Counter
    :param images: Number of source images and of candidate images, or None if unknown.
    :type images: tuple[int, int] | None
    :return: A table with one row per kind (the known kinds first, in order) and the
        columns Sources, Candidats, Total and share of the total; then the totals and,
        when the numbers of images are known, the coverage.
    :rtype: str
    """
    kinds = list(LABELS) + sorted({kind for kind, _ in counts} - set(LABELS))
    sources = sum(n for (_, is_source), n in counts.items() if is_source)
    candidates = sum(counts.values()) - sources
    total = sources + candidates
    row = "  {:<14}{:>9}{:>11}{:>9}  {:>8}"
    lines = [row.format("", "Sources", "Candidats", "Total", "Part")]
    for kind in kinds:
        n_source, n_candidate = counts[kind, True], counts[kind, False]
        n = n_source + n_candidate
        lines.append(
            row.format(
                LABELS.get(kind, kind), num(n_source), num(n_candidate), num(n), pct(n, total)
            )
        )
    lines.append("  " + "-" * 53)
    lines.append(row.format("Total", num(sources), num(candidates), num(total), ""))
    if images is not None:
        lines.append(
            row.format("Couverture", pct(sources, images[0]), pct(candidates, images[1]), "", "")
        )
        lines.append(
            f"  (sur {num(images[0])} images sources et {num(images[1])} images candidates)"
        )
    return "\n".join(lines)


def main() -> None:
    """Read the command line arguments and print the report.

    :return: None
    :rtype: None
    """
    parser = argparse.ArgumentParser(description="Rapport sur les annotations des images.")
    parser.add_argument(
        "tsv",
        nargs="?",
        type=Path,
        default=BASE_DIR / "storage" / "annotations.tsv",
        help="fichier TSV des annotations (défaut : storage/annotations.tsv)",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=BASE_DIR / "manuscript_folios",
        help="dossier des JSON des manuscrits, pour la couverture (défaut : manuscript_folios)",
    )
    args = parser.parse_args()
    if not args.tsv.exists():
        print(f"Aucune annotation pour l'instant : {args.tsv} n'existe pas.")
        return
    images = count_images(args.data) if args.data.is_dir() else None
    print(f"Annotations : {args.tsv}")
    print(report(read_annotations(args.tsv), images))


if __name__ == "__main__":
    main()
