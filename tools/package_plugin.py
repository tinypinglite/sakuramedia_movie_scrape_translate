#!/usr/bin/env python3
"""Build the installable SakuraMedia plugin archive."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

PLUGIN_ID = "sakuramedia_movie_scrape_translate"
RUNTIME_FILES = (
    "manifest.json",
    "__init__.py",
    "dmm.py",
    "jobs.py",
    "plugin.py",
    "settings.py",
    "state.py",
    "translation.py",
    "prompts/movie_desc_translation.md",
    "prompts/movie_title_translation.md",
    "README.md",
)
ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    version = json.loads((ROOT / "manifest.json").read_text())["version"]
    output_dir = ROOT / "dist"
    output_dir.mkdir(exist_ok=True)
    archive_path = output_dir / f"{PLUGIN_ID}-{version}.zip"
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for member in RUNTIME_FILES:
            archive.write(ROOT / member, member)
    print(archive_path.relative_to(ROOT))


if __name__ == "__main__":
    main()
