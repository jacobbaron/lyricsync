// SEARCH S10 (#127): golden-query eval harness for GET /api/search.
//
// Runs a curated "golden set" of real verbal queries (built by exploring the
// actual library — see search-eval/golden-queries.json) against live
// /api/search for each ranking mode (keyword / semantic / hybrid) and reports
// whether each query's expected clip(s) appear in the top-k results, plus a
// per-mode precision@k-style summary metric. This is the regression check
// called for by the [SEARCH] epic (#128): re-run it after any ranking change
// (S2/S3/S5 follow-ups) to catch a regression before it ships.
//
// Usage (run from web/, needs Node >=22 for --experimental-strip-types):
//   LYRICSYNC_BASE_URL=https://... LYRICSYNC_API_KEY=lsk_... \
//     node --experimental-strip-types scripts/search-eval.ts [--k 10] \
//       [--golden-set scripts/search-eval/golden-queries.json] \
//       [--report-json scripts/search-eval/last-report.json]
//
// (Same LYRICSYNC_BASE_URL / LYRICSYNC_API_KEY every agent web session already
// has exported — see CLAUDE.md's "Agent operational playbook".) Also runnable
// via `npm run eval:search -- --k 10` (see package.json).
//
// What "hit" means: for a golden query, a mode "hits" if ANY of that query's
// expected_clip_ids appears among the top-k results for that mode. Rank
// reported is the best (lowest) rank among the expected clips that appeared.
// Precision@k here is really "success@k" per query (did we recall an expected
// clip in the topk), averaged over all golden queries — the standard metric
// for this kind of small hand-built golden set (see golden-queries.json's
// per-query notes for why each query's keyword/semantic/hybrid behavior is
// expected to differ).

import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const MODES = ["keyword", "semantic", "hybrid"] as const;
type Mode = (typeof MODES)[number];

interface GoldenQuery {
  query: string;
  expected_clip_ids: string[];
  notes?: string;
}

interface SearchResult {
  clip_id: string;
  project: string | null;
  kind: string;
  score: number;
}

interface SearchResponse {
  count: number;
  results: SearchResult[];
  mode?: string;
  warnings?: unknown;
  error?: string;
}

interface QueryModeResult {
  query: string;
  mode: Mode;
  hit: boolean;
  rank: number | null; // 1-based rank of the best-ranked expected clip, if any
  matchedClipId: string | null;
  resultCount: number;
  warnings: unknown;
  error: string | null;
}

function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}

function parseArgs(argv: string[]) {
  const getFlag = (name: string, fallback: string): string => {
    const idx = argv.indexOf(`--${name}`);
    return idx >= 0 ? argv[idx + 1] ?? fallback : fallback;
  };
  const scriptDir = path.dirname(fileURLToPath(import.meta.url));
  return {
    k: parseInt(getFlag("k", "10"), 10) || 10,
    goldenSetPath: path.resolve(
      getFlag("golden-set", path.join(scriptDir, "search-eval", "golden-queries.json")),
    ),
    reportJsonPath: getFlag("report-json", ""),
  };
}

async function runQuery(
  baseUrl: string,
  apiKey: string,
  query: string,
  mode: Mode,
  k: number,
): Promise<SearchResponse> {
  const url = new URL("/api/search", baseUrl);
  url.searchParams.set("q", query);
  url.searchParams.set("mode", mode);
  url.searchParams.set("limit", String(k));
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  const body = (await res.json()) as SearchResponse;
  if (!res.ok) {
    return { count: 0, results: [], error: body.error ?? `HTTP ${res.status}` };
  }
  return body;
}

function evaluate(
  query: GoldenQuery,
  mode: Mode,
  response: SearchResponse,
): QueryModeResult {
  if (response.error) {
    return {
      query: query.query,
      mode,
      hit: false,
      rank: null,
      matchedClipId: null,
      resultCount: 0,
      warnings: response.warnings ?? null,
      error: response.error,
    };
  }
  const expected = new Set(query.expected_clip_ids);
  let bestRank: number | null = null;
  let matchedClipId: string | null = null;
  response.results.forEach((r, idx) => {
    if (expected.has(r.clip_id) && bestRank === null) {
      bestRank = idx + 1;
      matchedClipId = r.clip_id;
    }
  });
  return {
    query: query.query,
    mode,
    hit: bestRank !== null,
    rank: bestRank,
    matchedClipId,
    resultCount: response.count,
    warnings: response.warnings ?? null,
    error: null,
  };
}

function printReport(golden: GoldenQuery[], results: QueryModeResult[], k: number) {
  console.log(`\nSearch eval report (k=${k}, ${golden.length} golden queries)\n`);

  // Per-query hit/miss table.
  const colWidths = { query: 52, mode: 10, hit: 6, rank: 6 };
  console.log(
    "query".padEnd(colWidths.query) +
      "mode".padEnd(colWidths.mode) +
      "hit".padEnd(colWidths.hit) +
      "rank".padEnd(colWidths.rank),
  );
  console.log("-".repeat(colWidths.query + colWidths.mode + colWidths.hit + colWidths.rank));
  for (const q of golden) {
    for (const mode of MODES) {
      const r = results.find((x) => x.query === q.query && x.mode === mode)!;
      const label = mode === MODES[0] ? q.query : "";
      const status = r.error ? `ERR` : r.hit ? "HIT" : "miss";
      console.log(
        label.slice(0, colWidths.query - 1).padEnd(colWidths.query) +
          mode.padEnd(colWidths.mode) +
          status.padEnd(colWidths.hit) +
          String(r.rank ?? "-").padEnd(colWidths.rank) +
          (r.error ? ` (${r.error})` : r.warnings ? ` (warnings: ${JSON.stringify(r.warnings)})` : ""),
      );
    }
  }

  // Per-mode summary metric: fraction of golden queries with a hit (success@k).
  console.log(`\nSummary — success@${k} per mode (fraction of golden queries where an expected clip appeared in the top ${k}):\n`);
  for (const mode of MODES) {
    const modeResults = results.filter((r) => r.mode === mode);
    const hits = modeResults.filter((r) => r.hit).length;
    const errors = modeResults.filter((r) => r.error).length;
    const pct = ((hits / modeResults.length) * 100).toFixed(1);
    console.log(
      `  ${mode.padEnd(10)} ${hits}/${modeResults.length} (${pct}%)` +
        (errors > 0 ? `  [${errors} error(s)]` : ""),
    );
  }

  const hybridHits = results.filter((r) => r.mode === "hybrid" && r.hit).length;
  const bestSingleChannelHits = Math.max(
    results.filter((r) => r.mode === "keyword" && r.hit).length,
    results.filter((r) => r.mode === "semantic" && r.hit).length,
  );
  console.log(
    `\nHybrid vs. best single channel: hybrid ${hybridHits}/${golden.length} vs. best-of-keyword/semantic ${bestSingleChannelHits}/${golden.length} — ` +
      (hybridHits >= bestSingleChannelHits
        ? "hybrid meets or beats the best single channel (acceptance criterion satisfied)."
        : "hybrid UNDERPERFORMS the best single channel — investigate before shipping a ranking change."),
  );
}

async function main() {
  const { k, goldenSetPath, reportJsonPath } = parseArgs(process.argv.slice(2));
  const baseUrl = requireEnv("LYRICSYNC_BASE_URL");
  const apiKey = requireEnv("LYRICSYNC_API_KEY");

  const golden = JSON.parse(readFileSync(goldenSetPath, "utf-8")) as GoldenQuery[];
  console.log(`Loaded ${golden.length} golden queries from ${goldenSetPath}`);
  console.log(`Target: ${baseUrl}`);

  const results: QueryModeResult[] = [];
  for (const q of golden) {
    for (const mode of MODES) {
      const response = await runQuery(baseUrl, apiKey, q.query, mode, k);
      results.push(evaluate(q, mode, response));
    }
  }

  printReport(golden, results, k);

  if (reportJsonPath) {
    const report = {
      generated_at: new Date().toISOString(),
      base_url: baseUrl,
      k,
      golden_query_count: golden.length,
      results,
      summary: Object.fromEntries(
        MODES.map((mode) => {
          const modeResults = results.filter((r) => r.mode === mode);
          const hits = modeResults.filter((r) => r.hit).length;
          return [mode, { hits, total: modeResults.length, success_at_k: hits / modeResults.length }];
        }),
      ),
    };
    writeFileSync(reportJsonPath, JSON.stringify(report, null, 2));
    console.log(`\nWrote JSON report to ${reportJsonPath}`);
  }

  const anyErrors = results.some((r) => r.error);
  if (anyErrors) {
    console.error("\nOne or more queries errored — see ERR rows above.");
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("search-eval failed:", err);
  process.exit(1);
});
