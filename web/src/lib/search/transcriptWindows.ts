// SEARCH S2 (#119): splits a clip's ordered transcript words into small,
// non-overlapping windows for indexing as `clip_search_docs` rows (kind =
// 'transcript'). Each window becomes one Postgres full-text document, so
// ts_rank_cd naturally ranks *which part* of a long transcript matched, and
// the window's anchor word gives a real timestamp — the FTS-native
// replacement for the S1 MVP's hand-rolled "±5-word snippet centered on the
// match."
//
// Pure, dependency-free (no supabase/R2 imports) so it can be imported both
// from the Next.js merge route (which already has the words in memory after
// building merged.json) and from the standalone backfill script (which reads
// merged.json from R2 directly, outside the Next.js runtime) via a plain
// relative import — no path-alias resolution required.

export const TRANSCRIPT_WINDOW_SIZE = 16;

export interface WindowableWord {
  text: string;
  local_start: number;
}

export interface TranscriptWindow {
  window_idx: number;
  source_text: string;
  timestamp: number;
}

/**
 * Chunk `words` (assumed already ordered by local_start) into fixed-size
 * windows. Empty/whitespace-only chunks are skipped. The window's timestamp
 * anchors on its middle word, approximating the S1 MVP's "center the snippet
 * on the match" behavior without needing to know the match position (that's
 * now Postgres's job via ts_rank_cd).
 */
export function windowTranscript(
  words: WindowableWord[],
  windowSize: number = TRANSCRIPT_WINDOW_SIZE,
): TranscriptWindow[] {
  if (words.length === 0) return [];
  const windows: TranscriptWindow[] = [];
  let windowIdx = 0;
  for (let i = 0; i < words.length; i += windowSize) {
    const chunk = words.slice(i, i + windowSize);
    const text = chunk
      .map((w) => w.text ?? "")
      .join(" ")
      .trim();
    if (!text) continue;
    const mid = chunk[Math.floor(chunk.length / 2)];
    windows.push({
      window_idx: windowIdx,
      source_text: text,
      timestamp: mid.local_start,
    });
    windowIdx += 1;
  }
  return windows;
}
