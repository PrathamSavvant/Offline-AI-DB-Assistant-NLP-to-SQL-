"""Create the database knowledge cache on first offline startup."""
from __future__ import annotations

import json
from pathlib import Path

from core.db_context import db_context
from core.knowledge_files import REQUIRED_KNOWLEDGE_FILES
from core.knowledge_readiness import inspect_cached_artifacts


def invalid_knowledge_files(directory: Path) -> list[str]:
    invalid: list[str] = []
    for name in REQUIRED_KNOWLEDGE_FILES:
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            invalid.append(name)
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                json.load(handle)
        except (OSError, ValueError):
            invalid.append(name)
    return invalid


def cache_issues(directory: Path) -> list[str]:
    """The launcher needs complete schema, fingerprint, and table profiles."""
    return list(dict.fromkeys(
        invalid_knowledge_files(directory) + inspect_cached_artifacts(directory)["issues"]
    ))


def main() -> int:
    knowledge_dir = db_context.get_knowledge_dir()
    invalid = cache_issues(knowledge_dir)
    if not invalid:
        print(f"Knowledge cache ready: {knowledge_dir}")
        return 0

    print("Knowledge cache is missing or incomplete: " + ", ".join(invalid))
    print("Running the first-time database scan. This can take several minutes for a large database.")
    from main import run_phase_1

    run_phase_1(force=True)
    remaining = cache_issues(knowledge_dir)
    if remaining:
        raise RuntimeError("Knowledge bootstrap did not produce: " + ", ".join(remaining))
    print(f"Knowledge bootstrap complete: {knowledge_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
