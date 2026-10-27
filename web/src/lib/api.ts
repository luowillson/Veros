import { z } from "zod";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

const DEFAULT_TIMEOUT_MS = 8_000;
const SEARCH_TIMEOUT_MS = 20_000;
const RANKING_TIMEOUT_MS = 15_000;
const EXPLORE_TIMEOUT_MS = 120_000;

function timeoutSignal(ms: number): AbortSignal | undefined {
  if (typeof AbortSignal.timeout === "function") return AbortSignal.timeout(ms);
  if (typeof AbortController === "undefined") return undefined;
  const c = new AbortController();
  setTimeout(() => c.abort(), ms);
  return c.signal;
}

function combineSignals(signals: (AbortSignal | undefined)[]): AbortSignal | undefined {
  const present = signals.filter((s): s is AbortSignal => s !== undefined);
  if (present.length === 0) return undefined;
  if (present.length === 1) return present[0];
  if (typeof AbortSignal.any === "function") return AbortSignal.any(present);
  const c = new AbortController();
  for (const s of present) {
    if (s.aborted) return (c.abort(), c.signal);
    s.addEventListener("abort", () => c.abort(), { once: true });
  }
  return c.signal;
}

function withTimeout(init: RequestInit, ms = DEFAULT_TIMEOUT_MS): RequestInit {
  return { ...init, signal: combineSignals([init.signal ?? undefined, timeoutSignal(ms)]) };
}

async function readErrorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string" && body.detail.trim()) return body.detail;
  } catch {
    // non-JSON body
  }
  return fallback;
}

const VerdictSchema = z.enum([
  "Strong Accept",
  "Accept",
  "Weak Accept",
  "Borderline",
  "Reject",
  "Insufficient reviews",
]);

const ConsensusStrengthSchema = z.enum(["strong", "moderate", "mixed", "split"]);
const CitationGraphStatusSchema = z.enum(["not_enriched", "enriched", "failed"]);

export const PaperDetailSchema = z.object({
  id: z.string(),
  title: z.string(),
  authors: z.string(),
  venue: z.string().nullable(),
  citations: z.number().nullable(),
  references_count: z.number().nullable(),
  citation_graph_status: CitationGraphStatusSchema,
  openreview_url: z.string().nullable(),
  acceptance: z.string().nullable(),

  score: z.number().nullable(),
  grade: z.string(),
  verdict: VerdictSchema,
  consensus_strength: ConsensusStrengthSchema,
  reviewer_count: z.number(),

  novelty: z.number().nullable(),
  technical: z.number().nullable(),
  clarity: z.number().nullable(),
  impact: z.number().nullable(),

  tldr: z.string().nullable(),
  deep: z.array(z.string()),
  skim: z.array(z.string()),
  reviewers: z.array(
    z.object({
      handle: z.string(),
      rating: z.number(),
      rating_scale_max: z.number().nullable().optional(),
      label: VerdictSchema,
      quote: z.string(),
    }),
  ),
  consensus: z.string().nullable(),

  score_breakdown: z.record(z.string(), z.unknown()).nullable(),
  status: z.enum(["ready", "score_only", "ingested_no_score", "not_found"]),
});

export type PaperDetailDTO = z.infer<typeof PaperDetailSchema>;

export const PaperOutSchema = z.object({
  id: z.string(),
  title: z.string(),
  authors: z.string(),
  venue: z.string().nullable(),
  citations: z.number().nullable(),
  references_count: z.number().nullable(),
  citation_graph_status: CitationGraphStatusSchema,
  openreview_url: z.string().nullable(),
  acceptance: z.string().nullable(),
  score: z.number().nullable(),
  grade: z.string(),
  verdict: VerdictSchema,
  novelty: z.number().nullable(),
  technical: z.number().nullable(),
  clarity: z.number().nullable(),
  impact: z.number().nullable(),
  tldr: z.string().nullable(),
  consensus: z.string().nullable(),
  consensus_strength: ConsensusStrengthSchema,
  reviewer_count: z.number(),
});

export type PaperOutDTO = z.infer<typeof PaperOutSchema>;

export const PaperStatusSchema = z.object({
  paper_id: z.string(),
  ingest: z.enum(["queued", "ready", "failed"]),
  analysis: z.enum(["pending", "ready", "failed"]),
});

export type PaperStatusDTO = z.infer<typeof PaperStatusSchema>;

export const CitationPaperSchema = z.object({
  id: z.string(),
  title: z.string(),
  authors: z.string(),
  venue: z.string().nullable(),
  year: z.number().nullable(),
  citations: z.number().nullable(),
  references_count: z.number().nullable(),
  openreview_url: z.string().nullable(),
  provider_url: z.string().nullable(),
  score: z.number().nullable(),
  grade: z.string(),
  verdict: VerdictSchema,
  novelty: z.number().nullable(),
  technical: z.number().nullable(),
  clarity: z.number().nullable(),
  impact: z.number().nullable(),
  consensus_strength: ConsensusStrengthSchema,
  reviewer_count: z.number(),
  graph_only: z.boolean(),
});

export const CitationGraphSchema = z.object({
  paper_id: z.string(),
  direction: z.enum(["references"]),
  status: CitationGraphStatusSchema,
  generated_at: z.string().datetime(),
  nodes: z.array(CitationPaperSchema),
  edges: z.array(
    z.object({
      source: z.string(),
      target: z.string(),
      edge_type: z.enum(["cites"]),
      weight: z.number(),
    }),
  ),
});

export type CitationPaperDTO = z.infer<typeof CitationPaperSchema>;
export type CitationGraphDTO = z.infer<typeof CitationGraphSchema>;

/** Discriminated union — replaces the old 4-way string-or-DTO return type. */
export type PaperFetchResult =
  | { kind: "ready"; paper: PaperDetailDTO }
  | { kind: "queued" }
  | { kind: "failed" }
  | { kind: "not_found" };

export async function fetchPaper(
  paperId: string,
  init?: RequestInit,
): Promise<PaperFetchResult> {
  const res = await fetch(
    `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}`,
    withTimeout({ cache: "no-store", ...init }),
  );
  if (res.status === 404) return { kind: "not_found" };
  if (res.status === 410) return { kind: "failed" };
  if (res.status === 202) return { kind: "queued" };
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, `API error ${res.status} fetching ${paperId}`));
  }
  return { kind: "ready", paper: PaperDetailSchema.parse(await res.json()) };
}

export async function fetchPaperStatus(paperId: string): Promise<PaperStatusDTO> {
  const res = await fetch(
    `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/status`,
    withTimeout({ cache: "no-store" }),
  );
  if (!res.ok) {
    throw new Error(`API error ${res.status} fetching status for ${paperId}`);
  }
  return PaperStatusSchema.parse(await res.json());
}

export async function fetchPaperCitations(
  paperId: string,
  init?: RequestInit,
): Promise<CitationGraphDTO> {
  const params = new URLSearchParams({ direction: "references", limit: "200" });
  const res = await fetch(
    `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/citations?${params}`,
    withTimeout({ cache: "no-store", ...init }),
  );
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, `Citation API error ${res.status}`));
  }
  return CitationGraphSchema.parse(await res.json());
}

export async function enrichPaperCitations(paperId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/citations/enrich`,
    withTimeout({ method: "POST" }),
  );
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, `Citation enrichment failed ${res.status}`));
  }
}

/** Bulk paper fetch (used by the saved/reading-list page — one round-trip for N ids). */
export async function fetchPapersBatch(ids: string[]): Promise<PaperOutDTO[]> {
  if (ids.length === 0) return [];
  const res = await fetch(`${API_BASE_URL}/papers/batch`, withTimeout({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
    cache: "no-store",
  }));
  if (!res.ok) throw new Error(await readErrorDetail(res, `Batch API error ${res.status}`));
  return z.array(PaperOutSchema).parse(await res.json());
}

export async function analyzePaper(paperId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/papers/${encodeURIComponent(paperId)}/analyze`,
    { method: "POST" },
  );
  if (res.ok) return;
  throw new Error(await readErrorDetail(res, `AI summary failed ${res.status}`));
}

export const LandingGraphNodeSchema = z.object({
  id: z.string(),
  title: z.string(),
  venue: z.string().nullable(),
  score: z.number().nullable(),
  verdict: VerdictSchema,
});

export const LandingGraphEdgeSchema = z.object({
  source: z.string(),
  target: z.string(),
  weight: z.number(),
});

export const LandingGraphSchema = z.object({
  generated_at: z.string().datetime(),
  topic_paper_id: z.string().nullable().optional(),
  topic_title: z.string().nullable().optional(),
  topic_venue: z.string().nullable().optional(),
  nodes: z.array(LandingGraphNodeSchema),
  edges: z.array(LandingGraphEdgeSchema),
});

export type LandingGraphDTO = z.infer<typeof LandingGraphSchema>;

export type SearchSortKey =
  | "relevance"
  | "score"
  | "novelty"
  | "technical"
  | "clarity"
  | "impact";

export type SearchMode = "auto" | "topic" | "specific";

const SearchPageSchema = z.object({
  results: z.array(PaperOutSchema),
  total: z.number(),
});

export type SearchPageDTO = z.infer<typeof SearchPageSchema>;

export async function fetchLandingGraph(init?: RequestInit): Promise<LandingGraphDTO | null> {
  try {
    const res = await fetch(
      `${API_BASE_URL}/landing/graph`,
      withTimeout({ cache: "force-cache", next: { revalidate: 600 }, ...init }, 10_000),
    );
    if (!res.ok) return null;
    return LandingGraphSchema.parse(await res.json());
  } catch {
    return null;
  }
}

export async function fetchSearchPage(
  query: string,
  limit = 20,
  offset = 0,
  mode: SearchMode = "auto",
  sort: SearchSortKey = "relevance",
  init?: RequestInit,
): Promise<SearchPageDTO> {
  const params = new URLSearchParams({
    q: query,
    limit: String(limit),
    offset: String(offset),
    mode,
    sort,
  });
  // Empty-query browse pages are identical for every viewer — let the
  // browser/CDN cache them per the API's Cache-Control header. Real searches
  // stay no-store so users don't see stale or cross-contaminated results.
  const isBrowse = query.trim() === "";
  const cacheInit: RequestInit = isBrowse
    ? { cache: "force-cache", next: { revalidate: 30 } } as RequestInit
    : { cache: "no-store" };
  const res = await fetch(
    `${API_BASE_URL}/search/page?${params}`,
    withTimeout({ ...cacheInit, ...init }, SEARCH_TIMEOUT_MS),
  );
  if (!res.ok) throw new Error(`Search API error ${res.status}`);
  return SearchPageSchema.parse(await res.json());
}

/** Live (debounced) topic-mode fuzzy search; pass an AbortSignal to cancel in-flight calls. */
export async function fetchSearchLive(
  query: string,
  sort: SearchSortKey = "relevance",
  signal?: AbortSignal,
): Promise<PaperOutDTO[]> {
  const params = new URLSearchParams({ q: query, mode: "topic", sort });
  const res = await fetch(
    `${API_BASE_URL}/search?${params}`,
    withTimeout({ cache: "no-store", signal }, SEARCH_TIMEOUT_MS),
  );
  if (!res.ok) throw new Error(`Search API error ${res.status}`);
  return z.array(PaperOutSchema).parse(await res.json());
}

const LookupCandidateSchema = z.object({
  id: z.string(),
  title: z.string(),
  venue: z.string().nullable().optional(),
});

export const SearchLookupResponseSchema = z.object({
  intent: z.enum(["topic", "specific"]),
  top_sim: z.number(),
  paper_id: z.string().nullable(),
  ingest_started: z.boolean(),
  openreview_found: z.boolean(),
  openreview_candidate: LookupCandidateSchema.nullable(),
  results: z.array(PaperOutSchema),
});

export type SearchLookupResponse = z.infer<typeof SearchLookupResponseSchema>;

/** Submit-time classifier: returns intent, optional matched paper id, and optionally results. */
export async function lookupSearch(
  query: string,
  includeResults = true,
): Promise<SearchLookupResponse> {
  const params = new URLSearchParams();
  if (!includeResults) params.set("include_results", "false");
  const url = `${API_BASE_URL}/search/lookup${params.toString() ? `?${params}` : ""}`;
  const res = await fetch(url, withTimeout({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ q: query }),
    cache: "no-store",
  }, SEARCH_TIMEOUT_MS));
  if (!res.ok) throw new Error(`Search lookup API error ${res.status}`);
  return SearchLookupResponseSchema.parse(await res.json());
}

export const AuthorRankingSchema = z.object({
  author: z.string(),
  paper_count: z.number(),
  average_score: z.number(),
  top_paper_id: z.string().nullable(),
  top_paper_title: z.string().nullable(),
  top_score: z.number().nullable(),
  lowest_paper_id: z.string().nullable(),
  lowest_paper_title: z.string().nullable(),
  lowest_score: z.number().nullable(),
});

export type AuthorRankingDTO = z.infer<typeof AuthorRankingSchema>;
export type AuthorRankingOrder = "best" | "worst";

export async function fetchAuthorRankings(
  limit = 100,
  minPapers = 3,
  order: AuthorRankingOrder = "best",
  query = "",
  init?: RequestInit,
): Promise<AuthorRankingDTO[]> {
  const params = new URLSearchParams({
    limit: String(limit),
    min_papers: String(minPapers),
    order,
  });
  if (query.trim()) params.set("q", query.trim());
  const res = await fetch(
    `${API_BASE_URL}/rankings/authors?${params}`,
    withTimeout({ cache: "no-store", ...init }, RANKING_TIMEOUT_MS),
  );
  if (!res.ok) throw new Error(`Rankings API error ${res.status}`);
  return z.array(AuthorRankingSchema).parse(await res.json());
}

/** Saved papers — server-backed via the demo user. */

export async function fetchSaved(init?: RequestInit): Promise<PaperOutDTO[]> {
  const res = await fetch(
    `${API_BASE_URL}/saved`,
    withTimeout({ cache: "no-store", ...init }),
  );
  if (!res.ok) throw new Error(`Saved API error ${res.status}`);
  return z.array(PaperOutSchema).parse(await res.json());
}

export async function fetchSavedStatus(
  paperId: string,
  init?: RequestInit,
): Promise<boolean> {
  const res = await fetch(
    `${API_BASE_URL}/saved/${encodeURIComponent(paperId)}`,
    withTimeout({ cache: "no-store", ...init }),
  );
  if (!res.ok) return false;
  const data = z.object({ saved: z.boolean() }).parse(await res.json());
  return data.saved;
}

export async function savePaper(paperId: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/saved`, withTimeout({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_id: paperId }),
  }));
  if (!res.ok) throw new Error(`Save failed ${res.status}`);
}

export async function unsavePaper(paperId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/saved/${encodeURIComponent(paperId)}`,
    withTimeout({ method: "DELETE" }),
  );
  if (!res.ok) throw new Error(`Unsave failed ${res.status}`);
}

const ExplorePathwayItemSchema = z.object({
  position: z.number(),
  stage: z.string(),
  why_this_paper: z.string(),
  read_focus: z.string(),
  match_quality: z.string(),
  search_query: z.string().nullable(),
  anchor_concepts: z.array(z.string()),
  paper: PaperOutSchema.nullable(),
});

export const ExplorePathwaySchema = z.object({
  id: z.string(),
  title: z.string(),
  rationale: z.string(),
  status: z.string(),
  enrichment_notes: z.record(z.string(), z.unknown()),
  seed_paper_id: z.string().nullable(),
  query_text: z.string().nullable(),
  items: z.array(ExplorePathwayItemSchema),
});

export type ExplorePathwayDTO = z.infer<typeof ExplorePathwaySchema>;
export type ExplorePathwayItemDTO = z.infer<typeof ExplorePathwayItemSchema>;

export async function postExplorePath(
  topic: string,
  force = false,
  signal?: AbortSignal,
): Promise<ExplorePathwayDTO> {
  const params = new URLSearchParams();
  if (force) params.set("force", "true");
  const url = `${API_BASE_URL}/pathways/explore${params.toString() ? `?${params}` : ""}`;
  const res = await fetch(url, withTimeout({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic, limit: 8 }),
    cache: "no-store",
    signal,
  }, EXPLORE_TIMEOUT_MS));
  if (!res.ok) throw new Error(await readErrorDetail(res, `Explore API error ${res.status}`));
  return ExplorePathwaySchema.parse(await res.json());
}

/** Server-side stats for the landing footer. Returned by the API health/stats endpoint. */
export async function fetchStats(init?: RequestInit): Promise<{ paper_count: number; review_count: number }> {
  try {
    const res = await fetch(
      `${API_BASE_URL}/stats`,
      withTimeout({ cache: "force-cache", next: { revalidate: 300 }, ...init }, 5_000),
    );
    if (!res.ok) return { paper_count: 0, review_count: 0 };
    const data = z
      .object({ paper_count: z.number(), review_count: z.number() })
      .parse(await res.json());
    return data;
  } catch {
    return { paper_count: 0, review_count: 0 };
  }
}
