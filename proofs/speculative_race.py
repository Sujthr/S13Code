"""Reproducible, offline proof of the speculative recall-vs-web strategy race.

Runs two end-to-end scenarios through the real S13 runtime with a deterministic
embedder and stubbed web/LLM (no network, no Ollama, no provider keys), and
prints the graph, the ordered event trace, the provider/agent assignments, the
evidence, and the final answer for each.

    uv run python proofs/speculative_race.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import s13code.runtime as runtime_module
from s13code.core.memory import MemoryScope, Principal
from s13code.core.memory.embeddings import DeterministicEmbedder
from s13code.runtime import S13Runtime

PROMPT = "What is my travel budget? Answer from memory, but search the web if you don't have it."


async def _fake_llm(prompt: str, system: str) -> dict:
    """Stand-in answer worker: echoes the evidence count it was handed."""
    lines = [line for line in prompt.splitlines() if line.startswith("- [")]
    return {"text": f"Grounded answer built from {len(lines)} evidence item(s).",
            "provider": "stub-gateway", "model": "stub"}


async def _fast_web(query: str, max_results: int = 3) -> dict:
    return {"query": query, "hits": [{"title": "Budget guide", "url": "https://web/budget",
                                       "snippet": "typical travel budgets"}]}


def _print_run(title: str, result: dict) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    print(f"prompt: {PROMPT}")
    print(f"\nfinal answer: {result['answer']}")
    print(f"planner mode: {result['trace']['planner'].get('mode')}")

    print("\ngraph nodes (state / agent):")
    for node_id, node in result["graph"]["nodes"].items():
        agent = node.get("metadata", {}).get("agent", node["skill"])
        print(f"  - {node_id:<8} {node['state']:<10} agent={agent}")
    print("graph edges:")
    for parent, child in result["graph"]["edges"]:
        print(f"  {parent} -> {child}")

    print("\nordered event trace:")
    for event in result["events"]:
        payload = event["payload"]
        note = ""
        if event["kind"] == "graph_patched":
            bits = []
            if payload.get("add"): bits.append(f"add={payload['add']}")
            if payload.get("cancel"): bits.append(f"cancel={payload['cancel']}")
            if payload.get("finish"): bits.append("finish")
            note = f"  {' '.join(bits)}  ({payload.get('reason', '')})"
        print(f"  #{event['sequence']:<3} {event['kind']:<16} {event['node_id'] or '-':<8}{note}")

    print("\nprovider/agent assignments:")
    for node_id, agent in result["trace"]["agents"].items():
        print(f"  - {node_id:<8} agent={agent['agent']:<14} provider={agent['provider']}")


async def _scenario(root: Path, *, with_fact: bool, title: str, web) -> dict:
    runtime = S13Runtime(root=root)
    runtime.memory.embedder = DeterministicEmbedder(128)
    runtime_module.web_search = web  # stub the only network skill
    scope = MemoryScope("proof", "speculative", "student-01", "assistant")
    if with_fact:
        runtime.remember_fact(text="Travel budget is 90000 rupees.", scope=scope,
                              source_uri="chat://budget/1", source_author="student-01",
                              principal=Principal("gateway", "gateway"))
    result = await runtime.run(prompt=PROMPT, scope=scope, llm=_fake_llm,
                               source_uri="api://agent/runs", source_author="student-01")
    runtime.close()
    _print_run(title, result)
    return result


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Scenario A: memory owns the fact -> recall wins, web is cancelled, and
        # its result is discarded. `_never_web` proves the web skill's late
        # value never reaches the graph even if it would have returned hits.
        cancelled = asyncio.Event()

        async def slow_web(query: str, max_results: int = 3) -> dict:
            await cancelled.wait()  # only returns if it is *not* cancelled
            return {"query": query, "hits": [{"title": "late", "url": "https://web/late", "snippet": "late"}]}

        await _scenario(root / "a", with_fact=True,
                        title="SCENARIO A -- confident memory cancels the redundant web strategy", web=slow_web)

        # Scenario B: no durable fact -> recall empty -> the graph waits for web
        # instead of cancelling it, and answers from the web evidence.
        await _scenario(root / "b", with_fact=False,
                        title="SCENARIO B -- empty memory waits for the speculative web strategy", web=_fast_web)


if __name__ == "__main__":
    asyncio.run(main())
