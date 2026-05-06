"""Entry writer.

Writes schema-valid JSON to disk under <id>.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Entry
from .validate import validate_entry


class EntryWriter:
    """Writes validated entries as <id>.json in the configured output dir."""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, entry: Entry) -> Path:
        data = entry.model_dump(mode="json", exclude_none=False)
        validate_entry(data)
        out = self.output_dir / f"{entry.id}.json"
        with out.open("w") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        return out
