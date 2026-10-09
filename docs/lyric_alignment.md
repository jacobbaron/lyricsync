# Lyric forced alignment

Turn lyrics you already have into per-line timings for a clip, so captions land
on the beat instead of being placed by hand.

## Why this is separate from transcription

The existing pipeline (`POST /api/clips/[id]/transcribe` → `POST
/api/projects/[id]/align`) asks *"what was said, and when?"*. On sung audio the
first half of that question goes badly wrong:

- words are misheard ("five more drinks" → "5 more days")
- whole lines vanish under loud instrumentation
- Whisper loops on repeated refrains, emitting the same phrase a dozen times,
  every word stamped at the same instant
- a held vowel becomes one "word" twenty seconds long

None of that is fixable by a better decoder prompt, because the text is already
known. Forced alignment inverts the problem: the lyrics are fixed input, and
only timing is solved for. Every word in the result comes from the lyrics you
submitted — the aligner cannot invent, drop, or reorder a line.

## Using it

```bash
BASE="${LYRICSYNC_BASE_URL%/}"
curl -sS -X POST -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
  -H 'Content-Type: application/json' \
  "$BASE/api/clips/$CLIP/align-lyrics" \
  -d "{\"lyrics\": $(jq -Rs . < lyrics.txt)}"
# → 202 {"alignmentId": "...", "status": "accepted"}

curl -sS -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
  "$BASE/api/clips/$CLIP/align-lyrics"   # poll until status == "ready"
```

The lyrics are plain text, one caption line per line. Blank lines and bracketed
section headings (`[Chorus]`) are ignored, so a lyric sheet can be pasted as-is.

`result` when ready:

```json
{"lines": [{"text": "…", "start": 15.45, "end": 18.42, "score": 0.52}],
 "windows": 26, "anchored_windows": 22, "coverage": 1.0, "mean_score": 0.59}
```

`start`/`end` are clip-local seconds — the same frame of reference a timeline
text item wants, so the lines drop straight onto a text track:

```json
{"ops": [{"op": "add_text", "text": "…", "start": 15.3, "end": 18.7,
          "size": 54, "position": "lower", "wrap": 28,
          "color": "white", "outline": 3, "box_opacity": 0, "fade": 0.2}]}
```

Scores are wav2vec2 confidences. On sung audio over a full mix, 0.5–0.75 is
normal and is not a sign of a bad alignment — compare lines against each other
rather than against an absolute bar.

## Aligning against a song master (preferred for music videos)

A clip's phone audio is the worst input wav2vec2 gets: room noise, a distant
mic, the band bleeding in. When the project has the clean mix as a song
(project page → "Upload song / audio master"), align against that instead:

```bash
curl -sS -X POST -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
  -H 'Content-Type: application/json' \
  "$BASE/api/songs/$SONG/align-lyrics" \
  -d "{\"lyrics\": $(jq -Rs . < lyrics.txt)}"
curl -sS -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
  "$BASE/api/songs/$SONG/align-lyrics"   # poll until status == "ready"
```

Same worker, same result shape, plus `"time_base": "song"`: `start`/`end` are
**song time**, not clip time. Songs have no transcript of their own, so the
first alignment against a song runs it through Whisper once (cached at
`songs.transcript_r2_key`) purely for anchors; if that fails, planning falls
back to the single whole-track window.

To put the lines on a cut whose music bed is that song, in one step:

```bash
curl -sS -X POST -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
  -H 'Content-Type: application/json' \
  "$BASE/api/stories/$STORY/lyric-captions" \
  -d '{"alignment_id": "<id>"}'
# → {"status":"accepted","added":31,"dropped":4,"revision":7}
```

It converts each line with `output_t = song_t - bed.song_start`, drops lines
outside the cut (clipping ones that straddle an edge), adds them as `add_text`
items in the lyric style below (override any field with `style: {...}`), and
re-renders. `dry_run: true` returns the ops without applying them. Captions are
added, not replaced; remove old ones with `remove_text` first. The cut page's
"Music bed" panel does the same from the UI.

## Write the lyrics as performed, not as written

The aligner distributes exactly the text you give it across exactly the audio
that exists. A sheet that disagrees with the take will produce confident,
wrong timings:

- if the take repeats the chorus eight times, list it eight times
- if a take drops a couplet, drop it from the sheet
- alternate takes need their own sheet

Two symptoms in the result point at this: a `coverage` below 1.0 (lines the
aligner could not place at all) and lines whose duration is wildly out of step
with their neighbours.

## How it works

`modal/lyric_align.py` (pure, unit-tested) plans the work; `_align_lyrics_worker`
in `modal/app.py` runs wav2vec2 over the plan.

**Windowing is the whole trick.** Handing wav2vec2 the entire lyric sheet as one
segment spanning the whole track does not work: with long instrumental passages
and held notes it has too much freedom, and any line next to a window edge gets
dragged toward it by seconds. (Interior lines are stable — it is an edge
effect, and it is reproducible: widening a window by two seconds moved the
first line of a section by 3.4s while every interior line moved by 0.00s.)

So the audio is windowed first, using the clip's existing raw transcript as
*anchors*. Wherever the ASR independently heard the same word the lyric sheet
claims, that timestamp is evidence — even when the words around it were
misheard. Each line then gets its own few-second window, which wav2vec2 pins
down tightly.

Three things make anchoring survive bad ASR:

- **A match must be a run.** A single shared token is chance, not evidence, in
  a lyric full of repeated words (`MIN_ANCHOR_RUN`).
- **Repetition loops and held vowels are discarded** before matching
  (`clean_asr_words`) — they are the two characteristic Whisper failures on
  singing, and both produce anchors that are confidently wrong.
- **A repeated refrain is never anchored line-by-line.** Every repeat of
  "chorus line" matches the ASR equally well, so the matcher pairs them
  arbitrarily and the repeats scatter across the section. Identical adjacent
  lines instead share one window, and the aligner distributes them evenly
  across exactly the audio between the distinct lines on either side.

Lines the ASR missed wholesale get the leftover audio between the surrounding
anchors, and a window too short for its text is merged into a neighbour rather
than left to starve (which shows up as a sub-second line with a near-zero
score).

A clip with no transcript at all still works — planning falls back to a single
whole-track window. It is simply less precise, so transcribe first when you can.

### Window boundaries (why captions used to land late)

Each anchored line is aligned inside `[first ASR word − PAD_HEAD_S, last ASR
word + PAD_TAIL_S]`, and the aligner cannot place a word before its window
starts. When two lines are close, the previous line's tail pad overlaps the
next line's head pad. That overlap used to be resolved in the previous line's
favour, so the next window could start *after* the singer had begun: its
opening words were crammed against the window edge, up to ~1 s late ("I let
myself" squeezed into half a second). Overlaps now give up the tail pad first,
never cutting into the previous line's own ASR evidence. And when the ASR
missed a line's opening words, the head pad grows by `LEAD_TOKEN_S` per
unheard word, since the line really starts that many words before its anchor.

`result.window_spans` records each window; a line whose first word sits
exactly on its window's start is the signature of this failure.

## Caption styling

Text items take `color` (a name or `#RRGGBB`), `outline` + `outline_color`, and
`fade` alongside `size`/`position`/`wrap`/`box_opacity`. For lyric captions,
an outlined glyph over bare picture usually reads better than the default
black box — set `box_opacity: 0` and give it an `outline` of 3–4. Colors are
whitelisted, since they are interpolated into an ffmpeg filter string.

**Word-level captions.** Each aligned line also carries `words:
[{text, start, end}]` (same time base as the line). `POST
/api/stories/[id]/lyric-captions` passes them through, so its default look is
Instrument Serif with the sung word highlighted (`font: "instrument-serif"`,
`anim: "highlight"`); pass `style: {anim: "pop"}` / `{anim: "word"}` /
`{font: "playfair"}` etc. to change it. Alignments made before word timings
were stored have no `words` — re-run align-lyrics to get them.

See `docs/timeline_editing.md` for the full text-item reference.
