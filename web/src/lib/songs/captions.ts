// Song-time lyric lines → output-time add_text ops for a cut with a music bed.
// output_t = song_t - bed.song_start (see lib/songs/bed.ts). Lines that fall
// entirely outside the cut are dropped; lines straddling an edge are clipped.

export type LyricLine = { text: string; start: number; end: number };

export const CaptionStyle = {
  // Docs' recommended lyric look: outlined glyphs, no box (lyric_alignment.md).
  size: 54,
  position: "lower",
  wrap: 28,
  color: "white",
  outline: 3,
  box_opacity: 0,
  fade: 0.2,
} as const;

// Shortest caption worth keeping after clipping to the cut.
const MIN_CAPTION_S = 0.2;

export function lyricCaptionOps(
  lines: LyricLine[],
  songStart: number,
  cutDuration: number,
  style: Record<string, unknown> = {},
): Record<string, unknown>[] {
  const ops: Record<string, unknown>[] = [];
  for (const l of lines) {
    const start = Math.max(0, l.start - songStart);
    const end = Math.min(cutDuration, l.end - songStart);
    if (!l.text?.trim() || end - start < MIN_CAPTION_S) continue;
    ops.push({
      op: "add_text",
      ...CaptionStyle,
      ...style,
      text: l.text,
      start: Number(start.toFixed(3)),
      end: Number(end.toFixed(3)),
    });
  }
  return ops;
}
