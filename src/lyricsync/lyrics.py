"""Lyrics file parsing.

One caption line per non-blank text line. Blank lines are preserved as
explicit gaps so we can aggregate word-level alignment back up to line
granularity without losing the structural boundaries in the original
lyrics file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LyricLine:
    """A single caption-worthy line from the lyrics file."""

    index: int  # zero-based position among non-blank lines
    text: str
    # words are the whitespace-split tokens we hand to the aligner
    words: tuple[str, ...]


@dataclass(frozen=True)
class LyricsDoc:
    """Parsed lyrics document.

    `lines` holds only the non-blank lines (each one a caption).
    `line_breaks` records, for each line, whether a blank line followed
    it — useful later for grouping into verse/chorus chunks, but not
    needed by v0's SRT writer.
    """

    lines: tuple[LyricLine, ...]
    line_breaks: tuple[bool, ...]  # parallel to `lines`

    @property
    def flat_words(self) -> list[str]:
        """All words, flattened, in document order.

        This is what WhisperX's align() wants as the fixed transcript.
        """
        return [w for line in self.lines for w in line.words]


def parse_lyrics(path: Path) -> LyricsDoc:
    """Read a lyrics text file into a structured document."""
    raw = Path(path).read_text(encoding="utf-8")
    return parse_lyrics_text(raw)


def parse_lyrics_text(raw: str) -> LyricsDoc:
    """Parse lyrics from an in-memory string.

    - Lines are split on '\\n'.
    - Leading/trailing whitespace is stripped per line.
    - Blank lines are not emitted as caption lines, but we record that a
      break followed the preceding caption line.
    """
    lines: list[LyricLine] = []
    line_breaks: list[bool] = []

    idx = 0
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            # Mark that the most recent caption line was followed by a
            # blank. Leading/consecutive blanks are simply ignored.
            if lines:
                line_breaks[-1] = True
            continue

        words = tuple(stripped.split())
        lines.append(LyricLine(index=idx, text=stripped, words=words))
        line_breaks.append(False)  # flipped to True later if a blank follows
        idx += 1

    return LyricsDoc(lines=tuple(lines), line_breaks=tuple(line_breaks))
