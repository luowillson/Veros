#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from app.config import get_settings
from app.db.models import Paper, PaperEdge


@dataclass
class ImportStats:
    papers_inserted: int = 0
    papers_updated: int = 0
    edges_inserted: int = 0
    edges_updated: int = 0
    edges_skipped_missing_paper: int = 0


def make_import_engine():
    settings = get_settings()
    return create_engine(
        settings.database_url,
        connect_args={"prepare_threshold": None},
        poolclass=NullPool,
        future=True,
    )


def upsert_papers(db: Session, papers_dict: dict[str, Any], stats: ImportStats, *, quiet: bool) -> None:
    if not papers_dict:
        return

    rows = []
    now = datetime.now(UTC)
    for paper_id, node in papers_dict.items():
        external_url = node.get("url")
        rows.append({
            "id": str(paper_id),
            "title": str(node.get("title") or paper_id),
            "authors": [str(a) for a in (node.get("authors") or [])],
            "year": node.get("year"),
            "venue": str(node.get("venue")) if node.get("venue") else None,
            "citations": node.get("citation_count"),
            "references_count": node.get("reference_count"),
            "abstract": None,
            "openreview_url": str(external_url) if external_url else None,
            "acceptance": None,
            "citation_metadata": {
                "provider": "semantic_scholar",
                "provider_url": str(external_url) if external_url else None,
                "external_ids": node.get("external_ids") or {},
                "graph_only": True,
                "last_seen_at": now.isoformat(),
            },
            "citation_enriched_at": None,
            "ingested_at": now,
            "analyzed_at": None,
            "created_at": now,
        })

    # Chunking to avoid massive single transactions
    chunk_size = 1000
    table = Paper.__table__
    total = len(rows)
    n_chunks = (total + chunk_size - 1) // chunk_size if total else 0
    phase_start = time.monotonic()
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        chunk_idx = i // chunk_size + 1
        stmt = insert(table)

        # We only update non-destructive fields to avoid wiping out OpenReview data
        # if there happens to be a collision (unlikely since these use S2 IDs)
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.id],
            set_={
                "title": stmt.excluded.title,
                "authors": stmt.excluded.authors,
                "year": stmt.excluded.year,
                "venue": stmt.excluded.venue,
                "citations": stmt.excluded.citations,
                "references_count": stmt.excluded.references_count,
                "citation_metadata": table.c.citation_metadata.op("||")(
                    stmt.excluded.citation_metadata
                ),
            },
        )
        t0 = time.monotonic()
        db.execute(stmt, chunk)
        stats.papers_inserted += len(chunk)  # Approximate since it's an upsert
        if not quiet:
            elapsed = time.monotonic() - phase_start
            done = min(i + len(chunk), total)
            rate = done / elapsed if elapsed > 0 else 0.0
            eta_s = (total - done) / rate if rate > 0 else 0.0
            dt = time.monotonic() - t0
            print(
                f"  papers chunk {chunk_idx}/{n_chunks}  "
                f"{done:,}/{total:,}  "
                f"chunk {dt:.1f}s  "
                f"avg {rate:,.0f}/s  "
                f"ETA ~{eta_s/60:.1f}m",
                flush=True,
            )


def upsert_edges(
    db: Session,
    edges_list: list[dict[str, str]],
    known_paper_ids: set[str],
    stats: ImportStats,
    *,
    quiet: bool,
) -> None:
    if not edges_list:
        return

    rows = []
    now = datetime.now(UTC)
    for edge in edges_list:
        src = str(edge["source"])
        dst = str(edge["target"])
        if src not in known_paper_ids or dst not in known_paper_ids:
            stats.edges_skipped_missing_paper += 1
            continue
        rows.append({
            "src_paper_id": src,
            "dst_paper_id": dst,
            "edge_type": "cites",
            "weight": 1.0,
            "edge_metadata": {},
            "created_at": now,
        })

    chunk_size = 2000
    table = PaperEdge.__table__
    total = len(rows)
    n_chunks = (total + chunk_size - 1) // chunk_size if total else 0
    phase_start = time.monotonic()
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        chunk_idx = i // chunk_size + 1
        stmt = insert(table)
        stmt = stmt.on_conflict_do_update(
            index_elements=["src_paper_id", "dst_paper_id", "edge_type"],
            set_={
                "weight": stmt.excluded.weight,
            },
        )
        t0 = time.monotonic()
        db.execute(stmt, chunk)
        stats.edges_inserted += len(chunk)
        if not quiet:
            elapsed = time.monotonic() - phase_start
            done = min(i + len(chunk), total)
            rate = done / elapsed if elapsed > 0 else 0.0
            eta_s = (total - done) / rate if rate > 0 else 0.0
            dt = time.monotonic() - t0
            print(
                f"  edges chunk {chunk_idx}/{n_chunks}  "
                f"{done:,}/{total:,}  "
                f"chunk {dt:.1f}s  "
                f"avg {rate:,.0f}/s  "
                f"ETA ~{eta_s/60:.1f}m",
                flush=True,
            )


def import_crawl_json(source: Path, *, quiet: bool = False) -> ImportStats:
    stats = ImportStats()
    engine = make_import_engine()
    
    print(f"Loading {source} into memory...", flush=True)
    t_load = time.monotonic()
    with source.open("r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"  JSON loaded in {time.monotonic() - t_load:.1f}s", flush=True)
        
    papers = data.get("papers", {})
    edges = data.get("edges", [])
    known_paper_ids = {str(pid) for pid in papers}
    
    print(f"Found {len(papers)} papers and {len(edges)} edges to import.", flush=True)

    with Session(engine) as db:
        # Increase timeout for large bulk inserts
        db.execute(sa_text("SET statement_timeout = 120000"))
        
        print("Upserting papers...", flush=True)
        start_time = time.monotonic()
        upsert_papers(db, papers, stats, quiet=quiet)
        db.commit()
        print(f"  Papers phase done in {time.monotonic() - start_time:.1f}s", flush=True)
        
        print("Upserting citation edges...", flush=True)
        start_time = time.monotonic()
        upsert_edges(db, edges, known_paper_ids, stats, quiet=quiet)
        db.commit()
        print(f"  Edges phase done in {time.monotonic() - start_time:.1f}s", flush=True)
        if stats.edges_skipped_missing_paper:
            print(
                "  Skipped "
                f"{stats.edges_skipped_missing_paper:,} edges whose endpoint paper "
                "was not present in the crawl JSON.",
                flush=True,
            )

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import crawled Semantic Scholar citations JSON into the API database."
    )
    parser.add_argument(
        "--source", 
        required=True, 
        help="Path to the JSON file generated by crawl_citations.py."
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable per-chunk progress lines.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_path = Path(args.source)
    
    if not source_path.exists():
        print(f"Error: File not found at {source_path}")
        return

    result = import_crawl_json(source_path, quiet=args.quiet)

    print("\nImport Complete:")
    print(f"  Papers upserted: {result.papers_inserted:,}")
    print(f"  Edges upserted:  {result.edges_inserted:,}")
    if result.edges_skipped_missing_paper:
        print(f"  Edges skipped:   {result.edges_skipped_missing_paper:,}")


if __name__ == "__main__":
    main()
