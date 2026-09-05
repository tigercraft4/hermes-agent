"""Regression: ``_check_kanban_mode`` / ``_check_kanban_orchestrator_mode``
must never be served from the registry's TTL check_fn cache.

Root cause: the inner check_fn cache in ``tools/registry.py`` keys on
``(fn, profile_scope)`` only. Kanban availability additionally depends on
``HERMES_KANBAN_TASK`` (request/env-dependent, not part of the cache key), so
a probe made without the env var set — priming a cached ``False`` for this
profile — silently hides the entire Kanban lifecycle surface from a real
dispatcher-spawned worker in the same process (in-process/multiplex gateway
and cron paths), even though a direct, uncached call to the function would
correctly return ``True``. ``@no_cache_check_fn`` fixes this by bypassing the
cache entirely for these two functions, matching the same seam already used
by ``check_memory_requirements``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tools import kanban_tools
from tools.registry import _check_fn_cached, _check_fn_cache


@pytest.fixture(autouse=True)
def _clear_check_fn_cache():
    _check_fn_cache.clear()
    yield
    _check_fn_cache.clear()


class TestKanbanCheckFnsAreUncached:
    def test_registered_as_no_cache(self):
        """Both gates must be registered in the no-cache set (the behavior
        ``@no_cache_check_fn`` provides), not just "happen to return the
        right answer once" — this is what the transition test below exercises."""
        from tools.registry import _NO_CACHE_CHECK_FNS

        assert kanban_tools._check_kanban_mode in _NO_CACHE_CHECK_FNS
        assert kanban_tools._check_kanban_orchestrator_mode in _NO_CACHE_CHECK_FNS

    def test_env_flip_within_process_is_observed_without_cache_invalidation(
        self, monkeypatch
    ):
        """Reproduction (single process, no cache clear between calls):

        1. No HERMES_KANBAN_TASK -> cached probe is False.
        2. HERMES_KANBAN_TASK set -> a direct call already returns True.
        3. The CACHED probe (same cache the tool-schema builder uses) must
           immediately observe True too -- it must not keep serving the
           stale cached False for the TTL window.
        """
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        with (
            patch.object(kanban_tools, "_is_delegated_child_context", return_value=False),
            patch.object(kanban_tools, "_is_dispatcher_owned_worker", return_value=True),
            patch.object(kanban_tools, "_profile_has_kanban_toolset", return_value=False),
        ):
            # Step 1: prime the cache with a False result for this profile scope.
            assert _check_fn_cached(kanban_tools._check_kanban_mode) is False

            # Step 2: flip to a dispatcher-owned worker mid-process.
            monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test123")
            assert kanban_tools._check_kanban_mode() is True

            # Step 3: the cached probe must reflect the flip immediately,
            # not the stale primed value from step 1.
            assert _check_fn_cached(kanban_tools._check_kanban_mode) is True

    def test_orchestrator_gate_env_flip_within_process_is_observed(self, monkeypatch):
        """Same transition, inverted expectation: a dispatcher-owned worker
        must lose orchestrator-only board-routing tools the instant
        HERMES_KANBAN_TASK is set, without waiting out a stale cached True."""
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        with (
            patch.object(kanban_tools, "_is_delegated_child_context", return_value=False),
            patch.object(kanban_tools, "_is_dispatcher_owned_worker", return_value=True),
            patch.object(kanban_tools, "_profile_has_kanban_toolset", return_value=True),
        ):
            # Step 1: prime the cache with True (orchestrator profile, no task yet).
            assert _check_fn_cached(kanban_tools._check_kanban_orchestrator_mode) is True

            # Step 2: flip to a dispatcher-owned worker mid-process.
            monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test123")
            assert kanban_tools._check_kanban_orchestrator_mode() is False

            # Step 3: the cached probe must reflect the flip immediately.
            assert _check_fn_cached(kanban_tools._check_kanban_orchestrator_mode) is False
