"""Chunk, embed and store the synthetic policy documents.

Run again after editing any file in app/policies/ -- ingestion is idempotent
per document and replaces that document's chunks.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.db.base import Base, engine, session_scope
from app.services import policy_store


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest policy documents.")
    parser.add_argument("--dir", type=Path, default=policy_store.POLICY_DIR)
    args = parser.parse_args()

    Base.metadata.create_all(engine)
    with session_scope() as session:
        results = policy_store.ingest_all(session, args.dir)

    for result in results:
        flagged = result["flagged_sections"]
        suffix = f"  [flagged: {', '.join(flagged)}]" if flagged else ""
        print(
            f"{result['document_id']:<24} {result['version']:<4} "
            f"{result['sections']} sections{suffix}"
        )
    print(f"\n{len(results)} documents ingested.")


if __name__ == "__main__":
    main()
