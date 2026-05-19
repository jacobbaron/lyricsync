# shorten — POC

Cut a spoken-word video down by transcribing it, picking which segments
to keep, and concatenating them back together with ffmpeg.

This is a standalone POC that lives next to `lyricsync` but isn't part
of the package. The "pick which segments to keep" step is done by a
human (or an LLM in this chat) reading the transcript — there's no
automated content selection.

## Pipeline

```
video ──► transcribe.py ──► transcript.json ──► (human/LLM picks ranges)
                │                                       │
                ▼                                       ▼
            audio.mp3 ──► align.py ──► transcript_aligned.json
                                                        │
                                  ranges.json ◄─────────┘
                                        │
                                        ▼
                                     cut.py ──► short.mp4
                                        │
                                        ▼
                                  critique.py (GPT-4o audio QA)
```

## Setup

Put your OpenAI key in `.env` at the repo root:

```
OPENAI_API_KEY=sk-...
```

`ffmpeg` and `uv` on `PATH`. WhisperX comes from the parent project's
`pyproject.toml`.

## Usage

```bash
# 1. Transcribe (Whisper API, word + segment timestamps).
uv run shorten/transcribe.py path/to/video.mp4
# → shorten/out/<stem>/{audio.mp3, transcript.json, transcript.txt}

# Optional: keep disfluencies (um/uh/like) instead of letting Whisper drop them.
uv run shorten/transcribe.py path/to/video.mp4 --keep-fillers
# → shorten/out/<stem>/transcript_fillers.{json,txt}

# 2. (Optional but recommended) Re-align with WhisperX for phoneme-tight
#    word timestamps. Whisper API word timings drift by ±200-500ms;
#    forced alignment is what you want if you're cutting near word edges.
uv run python shorten/align.py shorten/out/<stem>/
# → shorten/out/<stem>/transcript_aligned.json

# 3. Decide what to keep. Write a ranges file like:
cat > shorten/out/<stem>/ranges.json <<'EOF'
[
  {"start": 12.40, "end": 15.46},
  {"start": 20.92, "end": 32.72}
]
EOF

# 4. Cut.
uv run shorten/cut.py path/to/video.mp4 shorten/out/<stem>/ranges.json \
    -o shorten/out/<stem>/short.mp4

# 5. (Optional) QA the cut with GPT-4o audio.
uv run shorten/critique.py shorten/out/<stem>/short.mp4
```

## Notes

- **Whisper word timestamps are loose.** Use `align.py` for any cut
  that needs to land near a word boundary (e.g., punching out fillers).
  Whisper API drift is large enough to clip neighboring syllables.
- **Filler punch-outs produce splice artifacts.** Removing in-segment
  fillers ("um", filler "like") leaves a small audio glitch GPT-4o
  reliably picks up. Whole-segment cuts at sentence boundaries sound
  much cleaner. Tradeoff.
- **HDR sources.** `cut.py` forces 8-bit yuv420p BT.709 output so iPhone
  HDR (HLG / BT.2020 / 10-bit) sources play in QuickTime / Quick Look.
  Without proper tone-mapping (homebrew ffmpeg has no `zscale`),
  highlights may look a touch bright — fine for review, not for final
  delivery.
- **25 MB Whisper upload limit.** At 64 kbps mono mp3 that's ~50 minutes
  of audio. Longer needs chunking; not implemented.
- **Output dir is `shorten/out/`** and is gitignored. Transcripts and
  cuts stay local.

## Files

```
shorten/
  transcribe.py   # video → audio → Whisper → transcript.json
  align.py        # transcript + audio → WhisperX → phoneme-tight timings
  cut.py          # video + ranges.json → concat'd short.mp4
  critique.py     # short.mp4 → GPT-4o audio review
  out/            # (gitignored) per-video working dir
```
