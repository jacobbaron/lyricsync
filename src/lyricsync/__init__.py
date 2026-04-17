"""Lyric Aligner — forced alignment for music video captions.

v0 is a thin end-to-end slice: extract audio, run WhisperX forced
alignment against a provided lyric transcript, and emit an SRT plus a
minimal preview MP4 with drawtext-overlaid captions.
"""

__version__ = "0.0.1"
