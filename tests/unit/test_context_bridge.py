# CMN-C2-285 - Unit tests: the caller-context bridge (src/graph/context_bridge.py).
#
# GraphNode.execute() invokes the inner graph without forwarding the outer
# state's input_context, so an inner-node read would always see {}. The bridge
# carries the VALIDATED caller contract across that boundary. End-to-end proof
# that a caller value actually reaches the inner graph lives in the boundary
# suite; these tests pin the hand-off itself.

import asyncio

import pytest

from src.graph.context_bridge import get_caller_input_context, set_caller_input_context


@pytest.fixture(autouse=True)
def _clear_bridge():
    """Leave no value behind. In the running agent the graph node sets the
    context immediately before every inner invoke, so a stale value is never
    readable; a test that left one would still pollute its neighbours."""
    set_caller_input_context(None)
    yield
    set_caller_input_context(None)


def test_unset_context_reads_as_empty():
    set_caller_input_context(None)
    assert get_caller_input_context() == {}


def test_value_round_trips():
    set_caller_input_context({"store_hint": "s-01"})
    assert get_caller_input_context() == {"store_hint": "s-01"}


def test_stashed_value_is_copied_not_aliased():
    """A later mutation of the caller's mapping must not change what the inner
    graph is handed."""
    original = {"store_hint": "s-01"}
    set_caller_input_context(original)
    original["store_hint"] = "tampered"
    assert get_caller_input_context() == {"store_hint": "s-01"}


def test_concurrent_invocations_do_not_see_each_other():
    """The hand-off is per task, so two invocations in one process cannot read
    each other's caller contract."""
    set_caller_input_context({"store_hint": "outer"})

    async def _inner():
        async def _task(value):
            set_caller_input_context({"store_hint": value})
            await asyncio.sleep(0)
            return get_caller_input_context()["store_hint"]

        return await asyncio.gather(_task("a"), _task("b"))

    assert asyncio.run(_inner()) == ["a", "b"]
    assert get_caller_input_context() == {"store_hint": "outer"}
