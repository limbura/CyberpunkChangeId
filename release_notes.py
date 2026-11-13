"""Generate GitHub Release notes from versions embedded in the built Change ID ZIP.

Read BUILD_INFO.json from the actual release artifact, not a fresh GitHub API lookup.
This ensures that advertised library versions match the sources used by the builder.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

BUILD_INFO_PATH = "BUILD_INFO.json"
TAG_PATTERN = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


def library_release_notes(archive_path: Path, release_tag: str) -> str:
    if not TAG_PATTERN.fullmatch(release_tag):
        raise ValueError(f"Expected release tag vX.Y.Z, got {release_tag!r}")
    try:
        with ZipFile(archive_path) as archive:
            info = json.loads(archive.read(BUILD_INFO_PATH))
    except (OSError, BadZipFile, KeyError, ValueError) as error:
        raise ValueError(
            f"Release ZIP has no valid {BUILD_INFO_PATH}: {archive_path}"
        ) from error
    if not isinstance(info, dict) or info.get("application") != "change_id":
        raise ValueError("Invalid Change ID build metadata")
    if info.get("version") != release_tag[1:]:
        raise ValueError("Change ID version in ZIP does not match release tag")

    versions = {}
    for key in ("reader_version", "printer_version"):
        value = info.get(key)
        if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
            raise ValueError(f"Missing or invalid {key} in {BUILD_INFO_PATH}")
        versions[key] = value
    timini = info.get("timini_revision")
    if not isinstance(timini, str) or not re.fullmatch(r"[0-9a-f]{40}", timini):
        raise ValueError(f"Missing or invalid timini_revision in {BUILD_INFO_PATH}")

    return (
        "## Библиотеки в этой сборке\n\n"
        f"- **Считыватель:** `wcopy_reader` v{versions['reader_version']}\n"
        f"- **Принтер:** `cyberwall_id_printer` v{versions['printer_version']}\n"
        f"- **TiMini-Print:** ревизия `{timini}`\n\n"
        "Версии взяты из `BUILD_INFO.json` внутри опубликованного ZIP-архива.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    notes = library_release_notes(args.archive, args.tag)
    args.output.write_text(notes, encoding="utf-8")
    print(notes)


if __name__ == "__main__":
    main()
