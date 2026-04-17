"""Forced alignment stage — WhisperX wav2vec2.

We call WhisperX's ``align()`` directly with the provided lyrics as a
fixed transcript, NOT with its Whisper transcription. This is the
"known-text" path documented in m-bain/whisperX issues #939 and #1308:
construct a single segment whose ``text`` is the full flattened
transcript, and hand that to ``align()`` alongside the audio.

v0 hardcodes the English wav2vec2 phoneme model. Languages, models, and
device selection become flags in v1.
"""

from __future__ import annotations

from pathlib import Path

from .alignment import AlignmentResult, aggregate_words_to_lines
from .lyrics import LyricsDoc


def run_whisperx_align(
    audio_path: Path,
    lyrics: LyricsDoc,
    device: str = "cpu",
    language: str = "en",
) -> AlignmentResult:
    """Force-align ``lyrics`` against ``audio_path`` using WhisperX.

    Imported lazily because ``whisperx`` pulls in torch and is slow to
    import; we don't want ``--help`` and tests paying that cost.
    """
    import whisperx  # type: ignore

    # 1. Load audio (WhisperX reads the 16kHz mono wav we produced).
    audio = whisperx.load_audio(str(audio_path))

    # 2. Load the wav2vec2 alignment model (no ASR model needed — we
    #    already have the transcript).
    model_a, metadata = whisperx.load_align_model(
        language_code=language, device=device
    )

    # 3. Build a single "segment" whose text is the flattened lyrics.
    #    WhisperX's align() treats each segment's text as the known
    #    transcript for the audio window [start, end]. With a single
    #    segment spanning the whole audio, it aligns the whole lyric
    #    against the whole track. See whisperX #939 / #1308.
    transcript_text = " ".join(lyrics.flat_words)
    duration = float(len(audio)) / 16000.0
    segments = [
        {"text": transcript_text, "start": 0.0, "end": duration}
    ]

    aligned = whisperx.align(
        segments,
        model_a,
        metadata,
        audio,
        device,
        return_char_alignments=False,
    )

    word_timings = aligned.get("word_segments", [])
    return aggregate_words_to_lines(lyrics, word_timings)
