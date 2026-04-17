"""API tests for the timing editor server (no ffmpeg)."""

from pathlib import Path

from starlette.testclient import TestClient

from lyricsync.alignment import AlignedLine, AlignedWord, AlignmentResult
from lyricsync.alignment_json import write_alignment_json
from lyricsync.serve_app import create_app


def _minimal_result() -> AlignmentResult:
    return AlignmentResult(
        lines=(
            AlignedLine(
                text="hi",
                start=0.0,
                end=0.5,
                words=(AlignedWord(text="hi", start=0.0, end=0.5),),
            ),
        )
    )


def test_get_put_alignment_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "project"
    out.mkdir()
    write_alignment_json(
        _minimal_result(),
        out / "alignment.json",
        meta={"source_video": "x.mp4"},
    )
    (out / "audio.wav").write_bytes(b"dummy")

    client = TestClient(create_app(out))
    r = client.get("/api/alignment")
    assert r.status_code == 200
    data = r.json()
    assert data["schema_version"] == 1
    assert data["meta"]["source_video"] == "x.mp4"
    assert len(data["lines"]) == 1

    data["lines"][0]["words"][0]["start"] = 0.1
    data["lines"][0]["words"][0]["end"] = 0.6
    r2 = client.put("/api/alignment", json=data)
    assert r2.status_code == 200

    r3 = client.get("/api/alignment")
    assert r3.json()["lines"][0]["words"][0]["start"] == 0.1


def test_audio_served(tmp_path: Path) -> None:
    out = tmp_path / "p"
    out.mkdir()
    write_alignment_json(_minimal_result(), out / "alignment.json")
    (out / "audio.wav").write_bytes(b"RIFF....")

    client = TestClient(create_app(out))
    r = client.get("/api/audio")
    assert r.status_code == 200
    assert r.content.startswith(b"RIFF")
