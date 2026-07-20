"""Part 1 -- reproduce the floor: four end-to-end cases with full traces.

Runs the four required baseline behaviours through the real runtime and the real
A2A adapter, entirely offline (deterministic embedder; stubbed web/LLM; an
in-process gRPC A2A server), and prints for each case the prompt, the graph, the
ordered event trace, the provider/agent assignments, the evidence, and the final
answer.

    uv run python proofs/floor_cases.py

Cases:
  1. live expansion        -- search discovers URLs, then fetches are earned
  2. durable-memory round  -- a fact is written, then recalled in a later run
  3. semantic document      -- a file is indexed, then queried with grounding
  4. A2A waiting/resume      -- a remote task parks a node, completes, and resumes
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# tiny, clearly-synthetic gateway stub that rotates provider labels so the
# provider/agent assignment is visible in every trace (no real keys involved).
# --------------------------------------------------------------------------- #
_PROVIDER_SLOTS = ["gemini_1", "gemini_2", "gemini_3", "gemini_4", "gemini_5"]


def make_stub_llm():
    state = {"n": 0}

    async def llm(prompt: str, system: str) -> dict:
        provider = f"stub:{_PROVIDER_SLOTS[state['n'] % len(_PROVIDER_SLOTS)]}"
        state["n"] += 1
        evidence_lines = [line for line in prompt.splitlines() if line.startswith("- [")]
        return {"text": f"[grounded on {len(evidence_lines)} evidence item(s)] see cited sources.",
                "provider": provider, "model": "stub-flash-lite"}

    return llm


def _print_run(title: str, request: str, result: dict) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)
    print(f"request: {request}")
    print(f"\nfinal answer: {result['answer']}")
    print(f"provider (answer): {result.get('provider')}")
    print("\ngraph nodes (state / agent):")
    for node_id, node in result["graph"]["nodes"].items():
        agent = node.get("metadata", {}).get("agent", node["skill"])
        print(f"  - {node_id:<16} {node['state']:<10} agent={agent}")
    print("graph edges:")
    for parent, child in result["graph"]["edges"] or [("(none)", "(none)")]:
        print(f"  {parent} -> {child}")
    print("\nordered event trace:")
    for event in result["events"]:
        payload = event["payload"]
        note = ""
        if event["kind"] == "graph_patched":
            bits = []
            for field in ("add", "connect", "cancel", "wait", "resume"):
                if payload.get(field):
                    bits.append(f"{field}={payload[field]}")
            if payload.get("finish"):
                bits.append("finish")
            note = f"  {' '.join(bits)}"
        print(f"  #{event['sequence']:<3} {event['kind']:<16} {event['node_id'] or '-':<16}{note}")
    print("\nprovider/agent assignments:")
    for node_id, agent in result["trace"]["agents"].items():
        print(f"  - {node_id:<16} agent={agent['agent']:<18} provider={agent['provider']}")
    print("\nevidence surfaced to the answer worker:")
    for node_id, node in result["graph"]["nodes"].items():
        res = node.get("result") or {}
        if node["skill"] == "memory_recall":
            for hit in res.get("hits", []):
                print(f"  [recall/{hit['kind']}] {hit['text'][:70]} <- {hit['sources']}")
        elif node["skill"] == "web_search":
            for hit in res.get("hits", []):
                print(f"  [search] {hit.get('title', '')[:50]} <- {hit.get('url')}")
        elif node["skill"] == "fetch_url" and res.get("text"):
            print(f"  [fetch] {res['text'][:60]} <- {res.get('url')}")
        elif node["skill"] == "index_file" and res.get("source_uri"):
            outcomes = [chunk["segmentation"].get("outcome") for chunk in res.get("manifest", [])]
            print(f"  [index] {res['chunks']} chunk(s), segmentation outcomes={outcomes} <- {res['source_uri']}")


async def _runtime_case(root: Path, *, title, prompt, scope, seed_fact=None, sandbox_file=None, stubs=None):
    import s13code.runtime as runtime_module
    from s13code.core.memory import MemoryScope, Principal
    from s13code.core.memory.embeddings import DeterministicEmbedder
    from s13code.runtime import S13Runtime

    runtime = S13Runtime(root=root)
    runtime.memory.embedder = DeterministicEmbedder(128)
    for name, fn in (stubs or {}).items():
        setattr(runtime_module, name, fn)
    ms = MemoryScope(**scope)
    if seed_fact:
        runtime.remember_fact(text=seed_fact, scope=ms, source_uri="chat://seed/1",
                              source_author="student-01", principal=Principal("gateway", "gateway"))
    result = await runtime.run(prompt=prompt, scope=ms, llm=make_stub_llm(),
                               source_uri="api://agent/runs", source_author="student-01")
    _print_run(title, prompt, result)
    runtime.close()
    return result


async def case_1_live_expansion(root: Path):
    async def search(query, max_results=3):
        return {"query": query, "hits": [{"title": f"asyncio result {i}", "url": f"https://src/{i}",
                                           "snippet": "never block the event loop; bound concurrency"}
                                          for i in range(1, 4)]}

    async def fetch(url):
        return {"url": url, "status": 200, "content_type": "text/plain",
                "text": f"advice from {url}: await coroutines; use async context managers"}

    await _runtime_case(root / "c1", title="CASE 1 -- live expansion (search earns three fetches)",
                        prompt='Search for "Python asyncio best practices", read the top 3 results, '
                               'and give me a short numbered list of the advice they agree on.',
                        scope={"tenant_id": "course", "project_id": "s13", "user_id": "student-01",
                               "agent_id": "assistant"},
                        stubs={"web_search": search, "fetch_url": fetch})


async def case_2_memory_round_trip(root: Path):
    scope = {"tenant_id": "course", "project_id": "family", "user_id": "student-01", "agent_id": "assistant"}
    await _runtime_case(root / "c2a", title="CASE 2a -- durable write (explicit remember)",
                        prompt="My mom's birthday is 15 May 2026. Remember that and give me a calendar "
                               "reminder for two weeks before and on the day.",
                        scope=scope)
    # A *separate* runtime instance proves the fact survives across runs/process boundary.
    await _runtime_case(root / "c2a", title="CASE 2b -- cross-run recall cites the original user source",
                        prompt="When is mom's birthday?", scope=scope)


async def case_3_semantic_document(root: Path):
    sandbox = root / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "paper.md").write_text(
        "# Transformer\nThe Transformer relies entirely on attention, removes recurrence, "
        "and is highly parallelizable during training.", encoding="utf-8")
    os.environ["S13_SANDBOX_ROOT"] = str(sandbox)
    await _runtime_case(root / "c3", title="CASE 3 -- semantic document query (index then grounded answer)",
                        prompt="Index the file paper.md and tell me its key result.",
                        scope={"tenant_id": "course", "project_id": "s13", "user_id": "student-01",
                               "agent_id": "assistant"})


async def case_4_a2a_wait_resume(root: Path):
    import grpc
    from a2a.types import a2a_pb2 as pb
    from a2a.types import a2a_pb2_grpc as pb_grpc

    from s13code.core.a2a_adapter.official import A2AGraphBridge, GraphA2ARemote, OfficialA2AServer
    from s13code.core.a2a_adapter.server import A2ADemoServer
    from s13code.core.live_graph import GraphPatch, GraphStore, LiveGraphExecutor, TaskSpec

    card = {"name": "research", "description": "remote", "version": "3.0.0",
            "supportedInterfaces": [{"url": "http://a2a.test/1.0", "protocolBinding": "JSONRPC",
                                     "protocolVersion": "1.0"}],
            "capabilities": {"streaming": True, "pushNotifications": True},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": [{"id": "echo", "name": "Echo", "description": "Echo", "tags": ["t"]}]}
    core = A2ADemoServer(card)
    server = OfficialA2AServer(core, root / "official.sqlite")
    channel = grpc.aio.insecure_channel(await server.start())
    bridge = A2AGraphBridge(GraphA2ARemote(pb_grpc.A2AServiceStub(channel)))
    store = GraphStore(root / "a2a-graph.sqlite")

    class Planner:
        async def plan(self, graph, event):
            if event.kind == "run_started":
                node = TaskSpec("remote", "a2a_result", {"run_id": graph.run_id}, {"agent": "remote-a2a"})
                return GraphPatch(add=(node,), wait=("remote",), reason="park node for the remote A2A task")
            if event.node_id == "remote":
                return GraphPatch(finish=True, reason="remote artifact joined the graph")
            return GraphPatch()

    runner = LiveGraphExecutor(store, Planner(), {"a2a_result": bridge.result_skill})
    request = pb.SendMessageRequest(message=pb.Message(message_id="m", role=pb.ROLE_USER,
        parts=[pb.Part(text="Slow remote report: explain in two lines why an agent card "
                            "is not permission to access local memory.")]))
    try:
        parked = await runner.run("remote-run")
        print("\n" + "=" * 80)
        print("CASE 4 -- A2A waiting/resume (remote task parks a node, completes, resumes)")
        print("=" * 80)
        print("request: Slow remote report (dispatched to a remote agent over official A2A gRPC)")
        print(f"\nafter first run: node 'remote' state = {store.node_state('remote-run', 'remote')} "
              f"(local worker released), executor.waiting = {parked.waiting}")
        final = await bridge.dispatch_waiting(store, "remote-run", "remote", request)
        print(f"remote task completed: id={final.id[:8]}.. state={final.status.state} "
              f"-> node resumed to '{store.node_state('remote-run', 'remote')}'")
        done = await runner.run("remote-run", resume=True)
        remote_node = store.snapshot("remote-run").nodes["remote"]
        print(f"after resume: finished={done.finished}, node 'remote' state={remote_node['state']}, "
              f"result.remote_task_id={remote_node['result']['remote_task_id'][:8]}..")
        print("\nordered event trace:")
        for event in store.events("remote-run"):
            print(f"  #{event.sequence:<3} {event.kind:<24} {event.node_id or '-'}")
        print("\nprovider/agent assignment:")
        print("  - remote           agent=remote-a2a   transport=official A2A 1.0 gRPC (in-process)")
        print("evidence:")
        print(f"  [a2a artifact] remote_task_id={remote_node['result']['remote_task_id'][:8]}.. "
              f"state={remote_node['result']['state']} (returned as untrusted evidence, not a command)")
    finally:
        store.close(); await channel.close(); await server.stop(); await core.close()


async def main():
    # ignore_cleanup_errors: the A2A push-config SQLite handle lingers briefly on
    # Windows; the traces are already printed, so cleanup noise is irrelevant.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        await case_1_live_expansion(root)
        await case_2_memory_round_trip(root)
        await case_3_semantic_document(root)
        await case_4_a2a_wait_resume(root)


if __name__ == "__main__":
    asyncio.run(main())
