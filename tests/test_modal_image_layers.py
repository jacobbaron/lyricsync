"""Guard against Modal's "build step after add_local_*" image error.

Modal refuses an image that layers a build step (run_commands, pip_install, …)
on top of one that has already mounted local files with `add_local_*`:

    An image tried to run a build step after using `image.add_local_*` to
    include local files.

That check runs server-side during `modal deploy`, inside the object resolver —
so importing modal/app.py does NOT catch it, and neither does any local test
using the modal library. The first signal is a failed deploy on main, after
merge. It has bitten once already: lyric_image was originally derived from
align_image, which ends with its local-file mounts.

The mistake is visible in the source, so this checks the source: it walks the
image builder chains in modal/app.py with ast and flags a build step applied to
an image that already carries mount layers. Passing `copy=True` to add_local_*
makes it a real build layer, which is legal, so that is honored.
"""

import ast
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "modal" / "app.py"

# Methods that add a build layer. Anything here is illegal once an image is
# carrying un-copied mount layers.
BUILD_METHODS = frozenset({
    "apt_install", "pip_install", "pip_install_from_requirements",
    "pip_install_private_repos", "poetry_install_from_file", "run_commands",
    "run_function", "dockerfile_commands", "env", "workdir", "entrypoint",
    "shell", "cmd",
})


def _chain(node: ast.AST) -> tuple[ast.AST, list[ast.Call]]:
    """Unwind `base.a(...).b(...)` into (root, [call_a, call_b]) in source order."""
    calls: list[ast.Call] = []
    cur = node
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        calls.append(cur)
        cur = cur.func.value
    calls.reverse()
    return cur, calls


def _is_copy_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "copy":
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def find_violations(source: str) -> list[str]:
    """Return a message per image chain that builds on top of mount layers."""
    tree = ast.parse(source)
    # variable name → does the image it holds carry un-copied mount layers?
    has_mounts: dict[str, bool] = {}
    violations: list[str] = []

    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue

        root, calls = _chain(node.value)
        if not calls:
            continue

        # Inherit mount state when deriving from a previously assigned image.
        mounted = False
        if isinstance(root, ast.Name):
            if root.id not in has_mounts:
                continue  # not an image chain we track
            mounted = has_mounts[root.id]
        elif not (
            isinstance(root, ast.Attribute) or isinstance(root, ast.Call)
        ):
            continue

        tracked = isinstance(root, ast.Name) or "Image" in ast.dump(root)
        if not tracked:
            continue

        for call in calls:
            method = call.func.attr  # type: ignore[union-attr]
            if method in BUILD_METHODS and mounted:
                violations.append(
                    f"{target.id} (line {call.lineno}): .{method}() runs after "
                    f"add_local_* — Modal rejects this at deploy time. Branch "
                    f"from the pre-mount base image instead, or pass copy=True."
                )
            elif method.startswith("add_local_") and not _is_copy_true(call):
                mounted = True

        has_mounts[target.id] = mounted

    return violations


# ---------------------------------------------------------------------------
# The checker itself must catch the real bug — otherwise it is decoration.
# ---------------------------------------------------------------------------

def test_checker_flags_build_step_after_local_file_in_one_chain():
    src = (
        "img = modal.Image.debian_slim()"
        '.add_local_file("a.py", "/root/a.py")'
        '.run_commands("echo hi")\n'
    )
    violations = find_violations(src)
    assert len(violations) == 1
    assert "run_commands" in violations[0]


def test_checker_flags_deriving_from_an_image_that_ends_in_mounts():
    """The exact shape that broke the deploy: a second image branching off one
    whose chain ends with add_local_*."""
    src = (
        "base = modal.Image.debian_slim()"
        '.pip_install("x")'
        '.add_local_file("a.py", "/root/a.py")\n'
        'derived = base.run_commands("echo hi")'
        '.add_local_file("b.py", "/root/b.py")\n'
    )
    violations = find_violations(src)
    assert len(violations) == 1
    assert violations[0].startswith("derived")


def test_checker_accepts_branching_from_a_pre_mount_base():
    """The fix: both images branch from a base that has no mounts yet."""
    src = (
        'base = modal.Image.debian_slim().pip_install("x")\n'
        'one = base.add_local_file("a.py", "/root/a.py")\n'
        'two = base.run_commands("echo hi")'
        '.add_local_file("a.py", "/root/a.py")\n'
    )
    assert find_violations(src) == []


def test_checker_allows_build_step_after_copy_true():
    src = (
        "img = modal.Image.debian_slim()"
        '.add_local_file("a.py", "/root/a.py", copy=True)'
        '.run_commands("echo hi")\n'
    )
    assert find_violations(src) == []


def test_checker_ignores_non_image_chains():
    src = 'x = some.thing().run_commands("echo")\n'
    assert find_violations(src) == []


# ---------------------------------------------------------------------------
# The real file
# ---------------------------------------------------------------------------

def test_app_images_have_no_build_step_after_local_files():
    violations = find_violations(APP_PATH.read_text())
    assert violations == [], "\n".join(violations)


def test_app_file_is_actually_being_checked():
    """A guard on the guard: if the chain-tracking ever silently stops matching
    (an import rename, a refactor to a helper), the real test above would pass
    vacuously. Assert we still recognize the known image variables."""
    tree = ast.parse(APP_PATH.read_text())
    assigned = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id.endswith("_image")
    }
    assert {"align_image", "lyric_image"} <= assigned, assigned
