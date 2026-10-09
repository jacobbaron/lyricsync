// Song-time lyric lines → output-time add_text ops for a cut with a music bed.
// output_t = song_t - bed.song_start (see lib/songs/bed.ts). Lines that fall
// entirely outside the cut are dropped; lines straddling an edge are clipped.

export type LyricWord = { text: string; start: number; end: number };
export type LyricLine = {
  text: string;
  start: number;
  end: number;
  // Per-word song-time timings (alignments made after word-level captions
  // shipped). Older alignments omit them and render as whole lines.
  words?: LyricWord[];
};

export const CaptionStyle = {
  // Lyric look: Instrument Serif via libass, the sung word highlighted
  // (modal/captions.py). No box; thin outline + soft shadow.
  font: "instrument-serif",
  anim: "highlight",
  size: 54,
  position: "lower",
  wrap: 28,
  color: "white",
  outline: 1.5,
  box_opacity: 0,
  fade: 0.2,
} as const;

// Shortest caption worth keeping after clipping to the cut.
const MIN_CAPTION_S = 0.2;

const r3 = (n: number) => Number(n.toFixed(3));

// Word timings relative to the caption's (clipped) start, as the timeline's
// text items carry them. Words starting outside the clipped span are dropped.
function relativeWords(
  words: LyricWord[] | undefined,
  songStart: number,
  start: number,
  end: number,
): LyricWord[] | undefined {
  if (!words?.length) return undefined;
  const out: LyricWord[] = [];
  for (const w of words) {
    const s = w.start - songStart;
    const e = w.end - songStart;
    if (s < start || s >= end || !w.text?.trim()) continue;
    out.push({ text: w.text, start: r3(s - start), end: r3(Math.min(e, end) - start) });
  }
  return out.length ? out : undefined;
}

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
    const words = relativeWords(l.words, songStart, start, end);
    ops.push({
      op: "add_text",
      ...CaptionStyle,
      ...style,
      text: l.text,
      start: r3(start),
      end: r3(end),
      ...(words ? { words } : {}),
    });
  }
  return ops;
}
