from __future__ import annotations

import pytest

from workplane.runner import RunnerError, prepare


async def test_runs_refuse_to_start_without_an_approval_mode():
    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def call(self, method: str, path: str, **kw):
            self.calls.append((method, path))
            if path == "/api/runner-config":
                return {
                    "approval_mode": None,
                    "repo_roots": ["~/src"],
                    "worktree_root": "~/worktrees",
                    "state_dir": "~/state",
                }
            raise AssertionError(f"unexpected call {method} {path}")

    api = Api()
    with pytest.raises(RunnerError, match="approval_mode"):
        await prepare(api, 1)
    assert api.calls == [("GET", "/api/runner-config")]  # nothing was created or registered
