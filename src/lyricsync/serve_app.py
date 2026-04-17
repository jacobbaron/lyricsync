"""Local ASGI app for the timing editor (loads alignment + serves audio + static UI)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .alignment_json import alignment_from_dict, alignment_to_dict, read_alignment_json
from .alignment_json import write_alignment_json as write_alignment_json_file


def _json_error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def create_app(project_dir: Path) -> Starlette:
    """Serve one project directory (must contain ``alignment.json`` and ``audio.wav``)."""
    project_dir = project_dir.resolve()
    alignment_path = project_dir / "alignment.json"
    audio_path = project_dir / "audio.wav"
    static_dir = Path(__file__).resolve().parent / "editor_static"

    async def api_get_alignment(_: Request) -> Response:
        if not alignment_path.is_file():
            return _json_error(404, "alignment.json not found — run `lyricsync align` first")
        try:
            result = read_alignment_json(alignment_path)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            return _json_error(400, str(e))
        meta: dict[str, Any] = {}
        try:
            raw = json.loads(alignment_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("meta"), dict):
                meta = raw["meta"]
        except OSError:
            pass
        payload = alignment_to_dict(result, meta=meta or None)
        return JSONResponse(payload)

    async def api_put_alignment(request: Request) -> Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "invalid JSON body")
        if not isinstance(body, dict):
            return _json_error(400, "body must be a JSON object")
        try:
            result = alignment_from_dict(body)
        except ValueError as e:
            return _json_error(400, str(e))
        meta = body.get("meta")
        meta_dict = meta if isinstance(meta, dict) else None
        try:
            write_alignment_json_file(result, alignment_path, meta=meta_dict)
        except OSError as e:
            return _json_error(500, str(e))
        return JSONResponse({"ok": True, "path": str(alignment_path)})

    async def api_get_audio(request: Request) -> Response:
        if not audio_path.is_file():
            return _json_error(404, "audio.wav not found")
        return FileResponse(
            audio_path,
            media_type="audio/wav",
            filename="audio.wav",
        )

    routes: list[Route | Mount] = [
        Route("/api/alignment", api_get_alignment, methods=["GET"]),
        Route("/api/alignment", api_put_alignment, methods=["PUT"]),
        Route("/api/audio", api_get_audio, methods=["GET"]),
    ]

    if static_dir.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=str(static_dir), html=True)))

    app = Starlette(routes=routes)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app


def run_server(project_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(
        create_app(project_dir),
        host=host,
        port=port,
        log_level="info",
    )
