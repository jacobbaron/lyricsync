"""Unit tests for the story-generation worker logic.

These tests cover _build_messages and the core worker flow using mocked
dependencies — no Modal, Supabase, R2, or Anthropic calls are made.

Loads modal/app.py by path so the `modal` import name-collision is avoided
and no actual Modal initialisation happens at import time.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── module loading ────────────────────────────────────────────────────────────
# We need transcript on sys.path before loading app.py so its top-level import
# (`from transcript import ...`) resolves to our local file.
import sys
_MODAL_DIR = Path(__file__).resolve().parent.parent / "modal"
if str(_MODAL_DIR) not in sys.path:
    sys.path.insert(0, str(_MODAL_DIR))

_APP_PATH = _MODAL_DIR / "app.py"


def _load_app():
    """Load modal/app.py without triggering real Modal initialisation."""
    # Stub the `modal` package so imports succeed but nothing is registered.
    modal_stub = types.ModuleType("modal")
    modal_stub.App = MagicMock(return_value=MagicMock())
    modal_stub.Image = MagicMock()
    modal_stub.Secret = MagicMock()
    modal_stub.fastapi_endpoint = lambda **kw: (lambda f: f)
    modal_stub.Mount = MagicMock()
    modal_stub.FilePatternMatcher = MagicMock()
    modal_stub.NetworkFileSystem = MagicMock()
    modal_stub.CloudBucketMount = MagicMock()
    modal_stub.cloud_bucket_mount = MagicMock()
    modal_stub.file_io = MagicMock()
    modal_stub.file_pattern_matcher = MagicMock()
    modal_stub.network_file_system = MagicMock()
    # app.function decorator — just returns the function unchanged
    modal_stub.App.return_value.function = lambda **kw: (lambda f: f)
    sys.modules.setdefault("modal", modal_stub)
    sys.modules.setdefault("modal.mount", MagicMock())

    # Stub heavy deps that aren't installed in the test env
    for mod in ("fastapi", "fastapi.responses", "whisperx", "boto3", "supabase"):
        sys.modules.setdefault(mod, MagicMock())

    spec = importlib.util.spec_from_file_location("lyricsync_app", _APP_PATH)
    app_module = importlib.util.module_from_spec(spec)
    sys.modules["lyricsync_app"] = app_module
    spec.loader.exec_module(app_module)
    return app_module


app = _load_app()
_build_messages = app._build_messages
_generate_worker = app._generate_worker


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_sb(stories_by_round: dict[str, list[dict]] | None = None):
    """Build a mock Supabase client whose .table().select()...execute() returns
    pre-canned story data keyed by generation_round_id."""
    stories_by_round = stories_by_round or {}

    def _execute_for_stories(round_id):
        resp = MagicMock()
        resp.data = stories_by_round.get(round_id, [])
        return resp

    def _table(name):
        tbl = MagicMock()
        # Chain: .select().eq().order().execute()
        chain = MagicMock()
        chain.execute = MagicMock(return_value=MagicMock(data=[]))
        # Each chaining method returns the same chain so .execute() is reachable
        for _m in ("eq", "order", "lt", "limit", "not_", "in_", "maybeSingle"):
            getattr(chain, _m).return_value = chain
        tbl.select.return_value = chain
        tbl.update.return_value = chain
        tbl.delete.return_value = chain

        # Special-case: stories table queries by generation_round_id
        if name == "stories":
            def _select(*args, **kwargs):
                inner = MagicMock()
                def _eq(col, val):
                    inner2 = MagicMock()
                    inner2.order = lambda *a, **kw: MagicMock(
                        execute=lambda: _execute_for_stories(val)
                    )
                    inner2.execute = lambda: _execute_for_stories(val)
                    return inner2
                inner.eq = _eq
                inner.order = lambda *a, **kw: inner
                return inner
            tbl.select = _select

        return tbl

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


def _round(id_: str, round_num: int, prompt: str | None = None) -> dict:
    return {"id": id_, "round": round_num, "prompt": prompt}


def _story(title: str) -> dict:
    return {
        "title": title,
        "description": "desc",
        "estimated_duration_secs": 10.0,
        "ranges_json": [{"source": "a.mov", "start": 0.0, "end": 5.0, "text": title}],
    }


# ── _build_messages ───────────────────────────────────────────────────────────

class TestBuildMessages:
    def test_first_round_no_history(self):
        sb = _make_sb()
        current = _round("r1", 1)
        msgs = _build_messages("TRANSCRIPT", current, [], sb)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "TRANSCRIPT" in msgs[0]["content"]

    def test_first_round_with_prompt(self):
        sb = _make_sb()
        current = _round("r1", 1, prompt="Make it punchy")
        msgs = _build_messages("T", current, [], sb)
        assert "Make it punchy" in msgs[0]["content"]

    def test_always_ends_with_user_turn(self):
        """Messages must end with user for the Claude API to accept them."""
        stories = [_story("S1"), _story("S2"), _story("S3")]
        sb = _make_sb({"r1": stories})
        prev = [_round("r1", 1)]
        current = _round("r2", 2)  # null prompt
        msgs = _build_messages("T", current, prev, sb)
        assert msgs[-1]["role"] == "user"

    def test_null_prompt_uses_default_followup(self):
        stories = [_story("S1"), _story("S2"), _story("S3")]
        sb = _make_sb({"r1": stories})
        prev = [_round("r1", 1)]
        current = _round("r2", 2, prompt=None)
        msgs = _build_messages("T", current, prev, sb)
        last_user = msgs[-1]["content"]
        assert "Generate 3 new story options" in last_user

    def test_alternates_roles(self):
        stories = [_story("S1"), _story("S2"), _story("S3")]
        sb = _make_sb({"r1": stories, "r2": stories})
        prev = [_round("r1", 1), _round("r2", 2)]
        current = _round("r3", 3)
        msgs = _build_messages("T", current, prev, sb)
        roles = [m["role"] for m in msgs]
        for a, b in zip(roles, roles[1:]):
            assert a != b, f"Consecutive {a!r} messages found: {roles}"

    def test_skips_aborted_rounds(self):
        """Rounds with no completed stories must be silently skipped."""
        stories = [_story("S1"), _story("S2"), _story("S3")]
        # r2 was aborted — no stories
        sb = _make_sb({"r1": stories, "r2": [], "r3": stories})
        prev = [_round("r1", 1), _round("r2", 2), _round("r3", 3)]
        current = _round("r4", 4)
        msgs = _build_messages("T", current, prev, sb)
        roles = [m["role"] for m in msgs]
        for a, b in zip(roles, roles[1:]):
            assert a != b, f"Consecutive {a!r} after skipping aborted round: {roles}"

    def test_three_prior_rounds_null_prompts(self):
        """Regression: the original bug — 3 prior rounds, null prompts."""
        stories = [_story("S1"), _story("S2"), _story("S3")]
        sb = _make_sb({"r1": stories, "r2": stories, "r3": stories})
        prev = [_round("r1", 1), _round("r2", 2), _round("r3", 3)]
        current = _round("r4", 4, prompt=None)
        msgs = _build_messages("T", current, prev, sb)
        assert msgs[-1]["role"] == "user"
        roles = [m["role"] for m in msgs]
        for a, b in zip(roles, roles[1:]):
            assert a != b, f"Consecutive {a!r}: {roles}"

    def test_custom_prompt_appears_in_followup(self):
        stories = [_story("S1"), _story("S2"), _story("S3")]
        sb = _make_sb({"r1": stories})
        prev = [_round("r1", 1)]
        current = _round("r2", 2, prompt="Go darker")
        msgs = _build_messages("T", current, prev, sb)
        assert "Go darker" in msgs[-1]["content"]


# ── _generate_worker — integration-style with all deps mocked ─────────────────

def _make_words():
    return [
        {"text": w, "source": "clip.mov",
         "global_start": i * 0.5, "global_end": i * 0.5 + 0.4,
         "local_start": i * 0.5, "local_end": i * 0.5 + 0.4}
        for i, w in enumerate(
            "we love building things at the home depot store today".split()
        )
    ]


def _make_anthropic_response(stories: list[dict]):
    """Fake Anthropic response that calls propose_stories."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {"stories": stories}
    response = MagicMock()
    response.content = [tool_block]
    return response


class TestGenerateWorker:
    def _run(self, anthropic_response, extra_env=None):
        """Run _generate_worker with fully mocked deps."""
        project_id = "proj-1"
        round_id = "round-1"
        words = _make_words()

        # Mock R2
        r2 = MagicMock()
        r2.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({"words": words}).encode())
        }

        # Mock Supabase
        def make_execute_result(data):
            r = MagicMock()
            r.data = data
            return r

        sb = MagicMock()
        sb.table.return_value.select.return_value \
            .eq.return_value.limit.return_value \
            .execute.return_value = make_execute_result(
                [{"id": round_id, "round": 1, "prompt": None}]
            )
        sb.table.return_value.select.return_value \
            .eq.return_value.lt.return_value \
            .order.return_value.execute.return_value = make_execute_result([])
        sb.table.return_value.select.return_value \
            .eq.return_value.order.return_value \
            .execute.return_value = make_execute_result(
                [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
            )
        sb.table.return_value.update.return_value \
            .eq.return_value.execute.return_value = make_execute_result([])

        # Mock Anthropic
        anthropic_mod = MagicMock()
        anthropic_mod.Anthropic.return_value.messages.create.return_value = (
            anthropic_response
        )

        env = {
            "R2_BUCKET_NAME": "bucket",
            "ANTHROPIC_API_KEY": "key",
            "SUPABASE_URL": "http://sb",
            "SUPABASE_SERVICE_ROLE_KEY": "srk",
            **(extra_env or {}),
        }
        with patch.dict("os.environ", env), \
             patch("lyricsync_app._supabase", return_value=sb), \
             patch("lyricsync_app._r2", return_value=r2), \
             patch.dict(sys.modules, {"anthropic": anthropic_mod}):
            _generate_worker(project_id, round_id)

        return sb, r2

    def test_successful_generation_marks_project_ready(self):
        stories = [
            {
                "title": f"Story {i}",
                "description": "desc",
                "segments": [{"source": "clip.mov", "quote": "we love building things"}],
            }
            for i in range(3)
        ]
        sb, _ = self._run(_make_anthropic_response(stories))
        # Project should be updated to stories_ready
        updates = [
            call for call in sb.table.return_value.update.call_args_list
        ]
        assert any(
            "stories_ready" in str(c) for c in updates
        ), "Project was not advanced to stories_ready"

    def test_no_tool_call_sets_error(self):
        no_tool_response = MagicMock()
        no_tool_response.content = []  # no tool_use block
        sb, _ = self._run(no_tool_response)
        updates = [str(c) for c in sb.table.return_value.update.call_args_list]
        assert any("error" in u for u in updates), \
            "Project not set to error when Claude skipped the tool"

    def test_unresolvable_quote_sets_error(self):
        stories = [
            {
                "title": "Bad Story",
                "description": "desc",
                "segments": [{"source": "clip.mov", "quote": "xyzzy plugh frobnicate"}],
            }
        ] * 3
        sb, _ = self._run(_make_anthropic_response(stories))
        updates = [str(c) for c in sb.table.return_value.update.call_args_list]
        assert any("error" in u for u in updates), \
            "Project not set to error on unresolvable quote"

    def test_unknown_source_sets_error(self):
        stories = [
            {
                "title": "T",
                "description": "d",
                "segments": [{"source": "ghost.mov", "quote": "some text"}],
            }
        ] * 3
        sb, _ = self._run(_make_anthropic_response(stories))
        updates = [str(c) for c in sb.table.return_value.update.call_args_list]
        assert any("error" in u for u in updates), \
            "Project not set to error on unknown source"
