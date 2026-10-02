"""Filename-derived colour associations must be part of content identity too."""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from bot.services.color_matrix import color_key


def colors_for_file(path: Path, colors: Iterable[str]) -> list[str]:
    stem = color_key(re.sub(r"^\d+[\s._-]*", "", path.stem))
    return [str(color) for color in colors if (wanted := color_key(str(color))) and stem and wanted in stem]
