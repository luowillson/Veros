#!/usr/bin/env python3
"""Recursive citation crawler using the Semantic Scholar API.

Usage
-----
    # By Semantic Scholar paper ID
    python scripts/crawl_citations.py 649def34f8be52c8b66281af98ae884c09aef38b

    # By DOI
    python scripts/crawl_citations.py --doi 10.48550/arXiv.1706.03762

    # By ArXiv ID
    python scripts/crawl_citations.py --arxiv 1706.03762

    # Control depth and output
    python scripts/crawl_citations.py 649def34f8be52c8b66281af98ae884c09aef38b \
        --max-depth 3 \
        --max-papers 500 \
        --output citations.json

    # Use an API key for higher rate limits
    python scripts/crawl_citations.py 649def34f8be52c8b66281af98ae884c09aef38b \
        --api-key YOUR_KEY

    # Resume (skip re-fetching references already fully pulled in a prior run)
    python scripts/crawl_citations.py 649def34f8be52c8b66281af98ae884c09aef38b \
        --output citations.json --resume
    python scripts/crawl_citations.py 649def34f8be52c8b66281af98ae884c09aef38b \
        --resume citations.json --output citations.json

The script performs a breadth-first crawl: starting from the seed paper, it
fetches all references, then all references of those references, and so on.
It respects the Semantic Scholar rate limit (1 request per second by default)
and deduplicates papers by their S2 paper ID.

Output is a JSON file containing:
  - papers: dict mapping paper_id → paper metadata
  - edges:  list of {source, target} citation edges
  - stats:  crawl statistics (depth reached, papers found, etc.)
  - metadata.expanded_paper_ids: S2 ids whose reference lists were fully fetched
    (used by --resume to avoid duplicate Semantic Scholar calls)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPANDED_METADATA_KEY = "expanded_paper_ids"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

S2_BASE = "https://api.semanticscholar.org/graph/v1"

PAPER_FIELDS = ",".join([
    "paperId", "corpusId", "title", "authors", "year", "venue",
    "citationCount", "referenceCount", "externalIds", "url",
])

REFERENCE_FIELDS = ",".join([
    "citedPaper.paperId", "citedPaper.corpusId", "citedPaper.title",
    "citedPaper.authors", "citedPaper.year", "citedPaper.venue",
    "citedPaper.citationCount", "citedPaper.referenceCount",
    "citedPaper.externalIds", "citedPaper.url",
])


@dataclass
class PaperNode:
    paper_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    depth: int = 0  # BFS depth from the seed


@dataclass
class Edge:
    source: str  # paper that cites
    target: str  # paper being cited


def _paper_node_from_json(obj: dict[str, Any]) -> PaperNode:
    """Rebuild a PaperNode from crawl JSON (asdict shape)."""
    return PaperNode(
        paper_id=str(obj.get("paper_id") or ""),
        title=str(obj.get("title") or ""),
        authors=[str(a) for a in (obj.get("authors") or []) if a],
        year=obj.get("year"),
        venue=obj.get("venue"),
        citation_count=obj.get("citation_count"),
        reference_count=obj.get("reference_count"),
        external_ids=dict(obj.get("external_ids") or {}),
        url=obj.get("url"),
        depth=int(obj.get("depth") or 0),
    )


def _edge_key(source: str, target: str) -> tuple[str, str]:
    return (source, target)


def load_crawl_json(path: Path) -> tuple[dict[str, PaperNode], list[Edge], set[str], str | None] | None:
    """Load papers, edges, expanded ids, and stored seed_id from a prior crawl file."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read resume file %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    papers_raw = raw.get("papers")
    edges_raw = raw.get("edges")
    if not isinstance(papers_raw, dict) or not isinstance(edges_raw, list):
        return None

    papers: dict[str, PaperNode] = {}
    for pid, node in papers_raw.items():
        if not isinstance(node, dict):
            continue
        n = _paper_node_from_json(node)
        if n.paper_id:
            papers[n.paper_id] = n

    edges: list[Edge] = []
    seen_e: set[tuple[str, str]] = set()
    for row in edges_raw:
        if not isinstance(row, dict):
            continue
        s, t = row.get("source"), row.get("target")
        if not isinstance(s, str) or not isinstance(t, str) or not s or not t:
            continue
        k = _edge_key(s, t)
        if k in seen_e:
            continue
        seen_e.add(k)
        edges.append(Edge(source=s, target=t))

    meta = raw.get("metadata")
    expanded: set[str] = set()
    stored_seed: str | None = None
    if isinstance(meta, dict):
        stored_seed = meta.get("seed_id") if isinstance(meta.get("seed_id"), str) else None
        exp = meta.get(EXPANDED_METADATA_KEY)
        if isinstance(exp, list):
            expanded = {str(x) for x in exp if isinstance(x, str) and x}
        elif exp is None:
            # Legacy JSON (no expanded_paper_ids): re-fetch /references so we do
            # not risk skipping partially written graphs.
            if edges:
                logger.info(
                    "Resume file has no %s; reference lists will be re-fetched for "
                    "non-expanded papers (one-time until you save with this version).",
                    EXPANDED_METADATA_KEY,
                )
            expanded = set()

    return papers, edges, expanded, stored_seed


def _build_pending_queue(
    papers: dict[str, PaperNode],
    expanded: set[str],
    *,
    max_depth: int,
) -> deque[tuple[str, int]]:
    """Papers that still need a /references fetch, in ascending BFS depth order."""
    pending: list[tuple[int, str]] = []
    for pid, node in papers.items():
        if pid in expanded:
            continue
        if (node.reference_count or 0) <= 0:
            continue
        d = node.depth
        if max_depth >= 0 and d >= max_depth:
            continue
        pending.append((d, pid))
    pending.sort(key=lambda x: (x[0], x[1]))
    return deque((pid, d) for d, pid in pending)


class SemanticScholarCrawler:
    """BFS citation crawler with rate limiting."""

    def __init__(
        self,
        *,
        api_key: str = "",
        min_interval: float = 1.05,
        timeout: float = 15.0,
        max_refs_per_paper: int = 500,
    ) -> None:
        self._api_key = api_key
        self._min_interval = min_interval
        self._timeout = timeout
        self._last_request_at = 0.0
        self._request_count = 0
        self._max_refs = max_refs_per_paper

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self._rate_limit()
        self._last_request_at = time.monotonic()
        self._request_count += 1
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full_url)
        if self._api_key:
            req.add_header("x-api-key", self._api_key)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
                return data if isinstance(data, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code == 429:
                wait = float(exc.headers.get("Retry-After", "5"))
                logger.warning("Rate limited — waiting %.1fs", wait)
                time.sleep(wait)
                return self._get_json(url, params)  # retry
            logger.warning("HTTP %d: %s", exc.code, exc)
            return None
        except Exception as exc:
            logger.warning("Request failed: %s", exc)
            return None

    def resolve_paper(self, identifier: str) -> dict[str, Any] | None:
        """Look up a paper by S2 ID, DOI:xxx, or ARXIV:xxx."""
        return self._get_json(
            f"{S2_BASE}/paper/{identifier}",
            params={"fields": PAPER_FIELDS},
        )

    def search_paper(self, title: str) -> dict[str, Any] | None:
        """Search for a paper by title and return the best match."""
        data = self._get_json(
            f"{S2_BASE}/paper/search",
            params={"query": title, "fields": PAPER_FIELDS, "limit": 1},
        )
        if not data:
            return None
        papers = data.get("data", [])
        return papers[0] if papers else None

    def fetch_references(self, paper_id: str) -> list[dict[str, Any]]:
        """Fetch all references for a paper, paginating if needed."""
        all_refs: list[dict[str, Any]] = []
        offset = 0
        limit = min(self._max_refs, 1000)

        while True:
            data = self._get_json(
                f"{S2_BASE}/paper/{paper_id}/references",
                params={
                    "fields": REFERENCE_FIELDS,
                    "limit": limit,
                    "offset": offset,
                },
            )
            if not data:
                break

            batch = data.get("data", [])
            if not batch:
                break

            for row in batch:
                cited = row.get("citedPaper")
                if isinstance(cited, dict) and cited.get("paperId"):
                    all_refs.append(cited)

            # Check if there are more pages
            total = data.get("total", 0)
            offset += len(batch)
            if offset >= total or offset >= self._max_refs:
                break

        return all_refs

    def crawl(
        self,
        seed_id: str,
        *,
        max_depth: int = -1,
        max_papers: int = -1,
        resume_papers: dict[str, PaperNode] | None = None,
        resume_edges: list[Edge] | None = None,
        resume_expanded: set[str] | None = None,
    ) -> tuple[dict[str, PaperNode], list[Edge], set[str], str]:
        """BFS crawl starting from seed_id.

        Args:
            seed_id: Semantic Scholar paper ID (or DOI:/ARXIV: prefixed).
            max_depth: Maximum BFS depth. -1 means unlimited.
            max_papers: Maximum papers to discover. -1 means unlimited.
            resume_*: optional state from a prior JSON file (see --resume).

        Returns:
            (papers dict, edges list, expanded_paper_ids, resolved_seed_paper_id)
        """
        papers: dict[str, PaperNode] = dict(resume_papers or {})
        edges: list[Edge] = list(resume_edges or [])
        expanded: set[str] = set(resume_expanded or ())
        edge_seen: set[tuple[str, str]] = {_edge_key(e.source, e.target) for e in edges}

        # Resolve the seed paper (skip network if raw id already present from resume)
        logger.info("Resolving seed paper: %s", seed_id)
        seed_data: dict[str, Any] | None = None
        seed_paper_id: str | None = None

        if seed_id in papers and not seed_id.startswith("TITLE:"):
            seed_paper_id = seed_id
            seed_data = None
            logger.info("Using seed paper from resume file (no lookup request).")
        elif seed_id.startswith("TITLE:"):
            seed_data = self.search_paper(seed_id[6:])
        else:
            seed_data = self.resolve_paper(seed_id)

        if seed_data is not None and seed_data.get("paperId"):
            seed_paper_id = str(seed_data["paperId"])
        elif seed_paper_id is None:
            logger.error("Could not resolve seed paper: %s", seed_id)
            return papers, edges, expanded, ""

        assert seed_paper_id is not None

        if seed_data is not None:
            seed_node = _to_node(seed_data, depth=0)
            papers[seed_paper_id] = seed_node
        else:
            if seed_paper_id not in papers:
                logger.error("Seed %s missing from resume data", seed_paper_id)
                return papers, edges, expanded, ""
            papers[seed_paper_id] = replace(papers[seed_paper_id], depth=0)

        seed_node = papers[seed_paper_id]
        queue = _build_pending_queue(papers, expanded, max_depth=max_depth)

        if resume_papers:
            logger.info(
                "Resume: %d papers, %d edges, %d already expanded (skip reference refetch)",
                len(papers),
                len(edges),
                len(expanded),
            )

        logger.info(
            "Seed: %s — \"%s\" (%d references reported)",
            seed_paper_id,
            seed_node.title,
            seed_node.reference_count or 0,
        )

        if max_papers >= 0 and len(papers) >= max_papers:
            logger.info("Already at max papers (%d); nothing to do.", max_papers)
            return papers, edges, expanded, seed_paper_id

        try:
            while queue:
                paper_id, depth = queue.popleft()
                next_depth = depth + 1

                if max_depth >= 0 and next_depth > max_depth:
                    continue

                if paper_id in expanded:
                    continue

                logger.info(
                    "[depth=%d] Fetching references for %s (%d papers found, %d in queue)",
                    depth,
                    paper_id,
                    len(papers),
                    len(queue),
                )

                refs = self.fetch_references(paper_id)
                new_count = 0
                stopped_by_paper_cap = False

                for ref_data in refs:
                    ref_id = ref_data.get("paperId")
                    if not ref_id:
                        continue

                    ek = _edge_key(paper_id, ref_id)
                    if ek not in edge_seen:
                        edge_seen.add(ek)
                        edges.append(Edge(source=paper_id, target=ref_id))

                    if ref_id in papers:
                        continue

                    if max_papers >= 0 and len(papers) >= max_papers:
                        logger.info("Reached max papers limit (%d)", max_papers)
                        queue.clear()
                        stopped_by_paper_cap = True
                        break

                    node = _to_node(ref_data, depth=next_depth)
                    papers[ref_id] = node
                    new_count += 1

                    ref_count = ref_data.get("referenceCount") or 0
                    if ref_count > 0 and (max_depth < 0 or next_depth < max_depth):
                        if ref_id not in expanded:
                            queue.append((ref_id, next_depth))

                logger.info(
                    "  → %d references fetched, %d new papers (total: %d)",
                    len(refs),
                    new_count,
                    len(papers),
                )
                if not stopped_by_paper_cap:
                    expanded.add(paper_id)
        except KeyboardInterrupt:
            logger.warning("Crawl interrupted by user. Saving progress so far...")

        logger.info(
            "Crawl complete: %d papers, %d edges, %d expanded, %d API requests",
            len(papers),
            len(edges),
            len(expanded),
            self._request_count,
        )
        return papers, edges, expanded, seed_paper_id


def _to_node(data: dict[str, Any], depth: int) -> PaperNode:
    authors = []
    for a in (data.get("authors") or []):
        if isinstance(a, dict) and a.get("name"):
            authors.append(a["name"])

    external_ids: dict[str, str] = {}
    raw_ids = data.get("externalIds") or {}
    if isinstance(raw_ids, dict):
        for key in ("DOI", "ArXiv", "CorpusId"):
            if raw_ids.get(key):
                external_ids[key.lower()] = str(raw_ids[key])

    return PaperNode(
        paper_id=data.get("paperId") or "",
        title=data.get("title") or "",
        authors=authors,
        year=data.get("year"),
        venue=data.get("venue") or None,
        citation_count=data.get("citationCount"),
        reference_count=data.get("referenceCount"),
        external_ids=external_ids,
        url=data.get("url"),
        depth=depth,
    )


import signal

def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt()

def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    parser = argparse.ArgumentParser(
        description="Recursively crawl citation references via Semantic Scholar.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "paper_id",
        nargs="?",
        help="Semantic Scholar paper ID (hex hash). Omit if using --doi or --arxiv.",
    )
    parser.add_argument("--doi", help="Resolve the seed paper by DOI.")
    parser.add_argument("--arxiv", help="Resolve the seed paper by ArXiv ID.")
    parser.add_argument("--title", help="Resolve the seed paper by title search.")
    parser.add_argument(
        "--max-depth", type=int, default=-1,
        help="Maximum BFS depth (-1 = unlimited). Default: unlimited.",
    )
    parser.add_argument(
        "--max-papers", type=int, default=-1,
        help="Maximum papers to discover (-1 = unlimited). Default: unlimited.",
    )
    parser.add_argument(
        "--max-refs-per-paper", type=int, default=500,
        help="Max references to fetch per paper. Default: 500.",
    )
    parser.add_argument(
        "--output", "-o", default="citations_crawl.json",
        help="Output JSON file. Default: citations_crawl.json",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help=(
            "Continue from an existing crawl JSON: reuse papers/edges and skip "
            f"Semantic Scholar /references calls for ids in metadata.{EXPANDED_METADATA_KEY}. "
            "With no FILE, uses the same path as --output. "
            "Put the seed paper_id before --resume (e.g. ID -o out.json --resume), "
            "not after --resume, or the id would be treated as the resume file path."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""),
        help="Semantic Scholar API key. Default: $SEMANTIC_SCHOLAR_API_KEY env var.",
    )
    parser.add_argument(
        "--rate-limit", type=float, default=1.05,
        help="Minimum seconds between API requests. Default: 1.05",
    )
    args = parser.parse_args()

    # Resolve the seed identifier
    if args.doi:
        seed_id = f"DOI:{args.doi}"
    elif args.arxiv:
        seed_id = f"ARXIV:{args.arxiv}"
    elif args.title:
        seed_id = f"TITLE:{args.title}"
    elif args.paper_id:
        seed_id = args.paper_id
    else:
        parser.error("Provide a paper_id, --doi, --arxiv, or --title.")
        return

    crawler = SemanticScholarCrawler(
        api_key=args.api_key,
        min_interval=args.rate_limit,
        max_refs_per_paper=args.max_refs_per_paper,
    )

    resume_papers: dict[str, PaperNode] | None = None
    resume_edges: list[Edge] | None = None
    resume_expanded: set[str] | None = None
    stored_seed: str | None = None
    if args.resume is not None:
        resume_path = Path(args.resume) if args.resume else Path(args.output)
        loaded = load_crawl_json(resume_path)
        if loaded is not None:
            resume_papers, resume_edges, resume_expanded, stored_seed = loaded
            logger.info("Loaded resume snapshot from %s", resume_path)
            if stored_seed and stored_seed != seed_id:
                logger.warning(
                    "Resume file seed_id %r differs from current seed %r — "
                    "continuing anyway; graph may be inconsistent.",
                    stored_seed,
                    seed_id,
                )
        else:
            logger.info("No usable resume data at %s; starting a fresh crawl.", resume_path)

    start = time.monotonic()
    papers, edges, expanded, resolved_seed = crawler.crawl(
        seed_id,
        max_depth=args.max_depth,
        max_papers=args.max_papers,
        resume_papers=resume_papers,
        resume_edges=resume_edges,
        resume_expanded=resume_expanded,
    )
    elapsed = time.monotonic() - start

    if not resolved_seed:
        logger.error("Seed paper could not be resolved; no output written.")
        sys.exit(1)

    # Build output
    output = {
        "metadata": {
            "seed_id": seed_id,
            "resolved_seed_paper_id": resolved_seed,
            "crawled_at": datetime.now(tz=timezone.utc).isoformat(),
            "max_depth": args.max_depth,
            "max_papers": args.max_papers,
            "elapsed_seconds": round(elapsed, 1),
            EXPANDED_METADATA_KEY: sorted(expanded),
        },
        "stats": {
            "total_papers": len(papers),
            "total_edges": len(edges),
            "max_depth_reached": max((p.depth for p in papers.values()), default=0),
            "papers_by_depth": _count_by_depth(papers),
        },
        "papers": {pid: asdict(node) for pid, node in papers.items()},
        "edges": [asdict(e) for e in edges],
    }

    out_path = Path(args.output)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    logger.info("Output written to %s", out_path)

    # Summary
    print(f"\n{'='*60}")
    print(f"  Crawl Summary")
    print(f"{'='*60}")
    print(f"  Seed:           {seed_id}")
    if resolved_seed in papers:
        print(f"  Title:          {papers[resolved_seed].title}")
    print(f"  Papers found:   {len(papers):,}")
    print(f"  Citation edges: {len(edges):,}")
    print(f"  Max depth:      {output['stats']['max_depth_reached']}")
    print(f"  Elapsed:        {elapsed:.1f}s")
    print(f"  Output:         {out_path}")
    print(f"{'='*60}")

    # Depth breakdown
    depth_counts = output["stats"]["papers_by_depth"]
    if depth_counts:
        print(f"\n  Papers by depth:")
        for depth_str in sorted(depth_counts, key=int):
            count = depth_counts[depth_str]
            bar = "█" * min(count, 60)
            print(f"    depth {depth_str:>2}: {count:>6,}  {bar}")
        print()


def _count_by_depth(papers: dict[str, PaperNode]) -> dict[str, int]:
    counts: dict[int, int] = {}
    for p in papers.values():
        counts[p.depth] = counts.get(p.depth, 0) + 1
    return {str(k): v for k, v in sorted(counts.items())}


if __name__ == "__main__":
    main()
