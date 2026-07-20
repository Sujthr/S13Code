"""Proof suite for the speculative recall-vs-web strategy race.

Parts of the assignment this file covers:

* Part 2 (extend one boundary): an outcome-driven behaviour the old planner
  could not express -- two strategies launched together, the first *usable*
  outcome earns the answer, and the redundant strategy is cancelled with the
  losing worker's late result discarded.
* Part 3 (attack your own claim): the same attack -- a fast-but-empty strategy
  racing a slow-but-useful one -- run against a *naive* arrival-order planner
  (fails: evidence is lost) and against the shipped evidence-justified decision
  (passes: evidence survives).

The pure decision lives in :mod:`s13code.core.live_graph.speculative`; these
tests attack that exact function and its wiring, not a re-implementation.
"""
from __future__ import annotations

import asyncio

import pytest

import s13code.routes as agent_route
import s13code.runtime as runtime_module
from s13code.core.live_graph import (
    GraphPatch,
    GraphStore,
    LiveGraphExecutor,
    TaskSpec,
    resolve_speculative_race,
)
from s13code.core.memory.embeddings import DeterministicEmbedder

# --------------------------------------------------------------------------- #
# Part 3, unit level: the evidence-justified decision itself.
# --------------------------------------------------------------------------- #


def test_usable_outcome_cancels_a_live_sibling():
    decision = resolve_speculative_race(outcome_usable=True, sibling_state="running", answered=False)
    assert decision.cancel_sibling and decision.can_answer


def test_empty_outcome_never_cancels_a_live_sibling():
    # The core of the attack: an empty-but-fast strategy must NOT tear down a
    # sibling that is still working and may own the only evidence.
    decision = resolve_speculative_race(outcome_usable=False, sibling_state="running", answered=False)
    assert not decision.cancel_sibling and not decision.can_answer


def test_usable_outcome_with_terminal_sibling_answers_without_cancelling():
    decision = resolve_speculative_race(outcome_usable=True, sibling_state="succeeded", answered=False)
    assert not decision.cancel_sibling and decision.can_answer


def test_both_terminal_answers_from_whatever_exists():
    decision = resolve_speculative_race(outcome_usable=False, sibling_state="failed", answered=False)
    assert not decision.cancel_sibling and decision.can_answer


def test_already_answered_is_a_noop():
    decision = resolve_speculative_race(outcome_usable=True, sibling_state="running", answered=True)
    assert not decision.cancel_sibling and not decision.can_answer


# --------------------------------------------------------------------------- #
# Part 3, executor level: same attack, naive vs shipped decision.
# --------------------------------------------------------------------------- #


def _speculative_plan(*, naive: bool):
    """Build a planner that races ``recall`` and ``web``.

    ``naive=True`` cancels the sibling whenever a strategy *arrives* first.
    ``naive=False`` uses the shipped evidence-justified decision.
    """

    def plan(graph, event):
        if event.kind == "run_started":
            return GraphPatch(add=(TaskSpec("recall", "recall"), TaskSpec("web", "web")),
                              reason="launch both strategies speculatively")
        if event.node_id in ("recall", "web"):
            sibling = "web" if event.node_id == "recall" else "recall"
            sibling_state = graph.nodes.get(sibling, {}).get("state")
            usable = bool(event.payload.get("hits"))
            if naive:
                cancel_sibling = sibling_state in ("pending", "running")
                can_answer = True
            else:
                decision = resolve_speculative_race(outcome_usable=usable, sibling_state=sibling_state,
                                                    answered="answer" in graph.nodes)
                cancel_sibling, can_answer = decision.cancel_sibling, decision.can_answer
            if cancel_sibling:
                winners = tuple(n for n, node in graph.nodes.items()
                                if n != "answer" and node["state"] == "succeeded")
                return GraphPatch(add=(TaskSpec("answer", "answer"),),
                                  connect=tuple((w, "answer") for w in winners), cancel=(sibling,))
            if can_answer and "answer" not in graph.nodes:
                winners = tuple(n for n, node in graph.nodes.items()
                                if n != "answer" and node["state"] == "succeeded")
                return GraphPatch(add=(TaskSpec("answer", "answer"),),
                                  connect=tuple((w, "answer") for w in winners))
            return GraphPatch(reason="await the live sibling")
        if event.node_id == "answer":
            return GraphPatch(finish=True)
        return GraphPatch()

    return plan


class _RaceWorkers:
    """recall is fast and empty; web is slow and carries the only evidence."""

    def __init__(self):
        self.recall_done = asyncio.Event()
        self.release_web = asyncio.Event()
        self.evidence_seen_by_answer = None

    async def worker(self, task):
        if task.id == "recall":
            self.recall_done.set()
            return {"hits": []}  # fast, empty
        if task.id == "web":
            await self.release_web.wait()  # slow
            return {"hits": [{"url": "https://source/1", "snippet": "the only evidence"}]}
        # answer records how much sibling evidence actually survived to it.
        return {"answer": "done"}


async def _run_race(tmp_path, *, naive: bool):
    workers = _RaceWorkers()
    store = GraphStore(tmp_path / f"race-{naive}.db")

    async def answer_worker(task):
        snapshot = store.snapshot(f"race-{naive}")
        hits = 0
        for node in snapshot.nodes.values():
            if node["skill"] == "web":
                hits += len((node.get("result") or {}).get("hits", []))
        workers.evidence_seen_by_answer = hits
        return {"answer": "done", "web_hits": hits}

    skills = {"recall": workers.worker, "web": workers.worker, "answer": answer_worker}
    runner = LiveGraphExecutor(store, _ScriptedPlanner(_speculative_plan(naive=naive)), skills, max_workers=2)
    running = asyncio.create_task(runner.run(f"race-{naive}"))
    await asyncio.wait_for(workers.recall_done.wait(), timeout=1.0)
    await asyncio.sleep(0.02)  # let the empty recall be planned before web is released
    workers.release_web.set()
    await asyncio.wait_for(running, timeout=1.0)
    return store, workers


class _ScriptedPlanner:
    def __init__(self, plan):
        self.plan_fn = plan

    async def plan(self, graph, event):
        return self.plan_fn(graph, event)


@pytest.mark.asyncio
async def test_late_result_after_cancellation_is_discarded(tmp_path):
    """Both strategies finish in one tick; the winner cancels the loser, whose
    already-produced result is dropped instead of patching the graph."""
    loser_planned = False

    def plan(graph, event):
        nonlocal loser_planned
        if event.kind == "run_started":
            return GraphPatch(add=(TaskSpec("a_winner", "w"), TaskSpec("b_loser", "w")))
        if event.node_id == "a_winner":
            return GraphPatch(cancel=("b_loser",), finish=True, reason="winner made the loser redundant")
        if event.node_id == "b_loser":
            loser_planned = True  # would only happen if the late result leaked
        return GraphPatch()

    async def worker(task):
        return {"leaked": task.id == "b_loser"}  # both return immediately, same tick

    store = GraphStore(tmp_path / "late.db")
    report = await LiveGraphExecutor(store, _ScriptedPlanner(plan), {"w": worker}, max_workers=2).run("late")
    assert report.finished
    assert store.node_state("late", "b_loser") == "cancelled"
    assert not loser_planned, "the loser's late result leaked into the planner"
    assert not any(e.kind == "task_succeeded" and e.node_id == "b_loser" for e in store.events("late"))


@pytest.mark.asyncio
async def test_naive_arrival_order_planner_loses_the_only_evidence(tmp_path):
    """BEFORE the fix: cancelling on arrival throws away the slow web evidence."""
    store, workers = await _run_race(tmp_path, naive=True)
    assert store.node_state("race-True", "web") == "cancelled"
    # The answer ran with zero web evidence -- the attack succeeded.
    assert workers.evidence_seen_by_answer == 0


@pytest.mark.asyncio
async def test_evidence_justified_planner_waits_and_keeps_the_evidence(tmp_path):
    """AFTER the fix: empty recall waits, slow web survives and is used."""
    store, workers = await _run_race(tmp_path, naive=False)
    assert store.node_state("race-False", "web") == "succeeded"
    assert workers.evidence_seen_by_answer == 1


# --------------------------------------------------------------------------- #
# Part 2 + deterministic cancellation, end-to-end through the real runtime.
# --------------------------------------------------------------------------- #

_SPECULATIVE_PROMPT = ("What is my travel budget? Answer from memory, "
                       "but search the web if you don't have it.")


def _fake_answer(monkeypatch):
    async def answer(_app, prompt, _system):
        return {"text": "grounded answer", "provider": "fake", "model": "fake"}
    monkeypatch.setattr(agent_route, "gateway_text_llm", answer)


def test_speculative_prompt_launches_both_strategies_together(app_client, monkeypatch):
    app_client.app.state.s13_runtime.memory.embedder = DeterministicEmbedder(128)
    _fake_answer(monkeypatch)

    async def search(query, max_results=3):
        return {"query": query, "hits": [{"title": "t", "url": "https://web/1", "snippet": "web evidence"}]}
    monkeypatch.setattr(runtime_module, "web_search", search)

    body = app_client.post("/v1/agent/runs", json={"tenant_id": "t", "project_id": "spec",
        "prompt": _SPECULATIVE_PROMPT}).json()

    # The very first patch adds BOTH strategies, and both start before either
    # outcome exists -- a future node the old single-strategy planner could not.
    assert sorted(body["events"][1]["payload"]["add"]) == ["recall", "web"]
    starts = [e["node_id"] for e in body["events"] if e["kind"] == "task_started"]
    assert set(starts[:2]) == {"recall", "web"}
    # The answer node does not exist until an outcome justified it.
    answer_added_at = next(i for i, e in enumerate(body["events"])
                           if e["kind"] == "graph_patched" and "answer" in e["payload"]["add"])
    first_outcome_at = next(i for i, e in enumerate(body["events"]) if e["kind"] == "task_succeeded")
    assert answer_added_at > first_outcome_at


def test_confident_memory_cancels_web_and_discards_its_late_result(app_client, monkeypatch):
    """Memory owns the fact, so the slow web strategy is cancelled; its late
    result must never record an outcome or leak into the answer's evidence."""
    app_client.app.state.s13_runtime.memory.embedder = DeterministicEmbedder(128)
    _fake_answer(monkeypatch)

    release_web = asyncio.Event()
    web_hits = {"count": 0}

    async def slow_search(query, max_results=3):
        await release_web.wait()
        web_hits["count"] += 1
        return {"query": query, "hits": [{"title": "t", "url": "https://web/late", "snippet": "late web evidence"}]}
    monkeypatch.setattr(runtime_module, "web_search", slow_search)

    scope = {"tenant_id": "t", "project_id": "spec2"}
    # A durable fact makes recall the usable winner.
    app_client.post("/v1/agent/facts", json={**scope,
        "text": "Travel budget is 90000 rupees.", "source_uri": "chat://budget/1"})

    # recall (local) wins the race; web is still awaiting release when cancelled.
    body = app_client.post("/v1/agent/runs", json={**scope, "prompt": _SPECULATIVE_PROMPT}).json()
    release_web.set()  # web finishes *after* the graph already cancelled it

    nodes = body["graph"]["nodes"]
    assert nodes["recall"]["state"] == "succeeded"
    assert nodes["web"]["state"] == "cancelled"
    assert nodes["web"]["result"] is None  # the late result was discarded, not recorded
    assert nodes["answer"]["state"] == "succeeded"
    # answer depends only on the winning strategy.
    assert sorted(body["graph"]["edges"]) == [["recall", "answer"]]
    assert any(e["kind"] == "task_cancelled" and e["node_id"] == "web" for e in body["events"])


def test_empty_memory_waits_for_web_and_answers_from_it(app_client, monkeypatch):
    app_client.app.state.s13_runtime.memory.embedder = DeterministicEmbedder(128)
    _fake_answer(monkeypatch)

    async def search(query, max_results=3):
        return {"query": query, "hits": [{"title": "t", "url": "https://web/2", "snippet": "web evidence"}]}
    monkeypatch.setattr(runtime_module, "web_search", search)

    # No durable fact: recall comes back empty, so web must survive.
    body = app_client.post("/v1/agent/runs", json={"tenant_id": "t", "project_id": "spec3",
        "prompt": _SPECULATIVE_PROMPT}).json()

    nodes = body["graph"]["nodes"]
    assert nodes["web"]["state"] == "succeeded"  # never cancelled
    assert nodes["answer"]["state"] == "succeeded"
    assert ["recall", "answer"] in [list(edge) for edge in body["graph"]["edges"]]
    assert ["web", "answer"] in [list(edge) for edge in body["graph"]["edges"]]
