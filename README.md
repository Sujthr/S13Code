# S13Code

`S13Code` is the standalone Session 13 agent runtime. It implements a live task graph, scoped and provenance-bearing memory, Rohan's semantic chunking V2, and Agent2Agent interoperability. It asks `glc_v3` for model completions over HTTP and never owns provider credentials.

## What runs where

| Service | Default address | Responsibility |
|---|---|---|
| `glc_v3` | `http://127.0.0.1:8111` | Models, keys, routing and channels |
| `S13Code` HTTP | `http://127.0.0.1:8113` | Graph, memory, documents and JSON-RPC A2A |
| `S13Code` gRPC | `127.0.0.1:8114` | Official A2A gRPC service |
| Ollama | `http://127.0.0.1:11434` | Phi-4 segmentation and Nomic embeddings |

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A running `glc_v3`
- A running Ollama with `phi4` and `nomic-embed-text`

```bash
ollama pull phi4
ollama pull nomic-embed-text
ollama serve
```

## Install and run

Unzip `glc_v3`, `S13Code`, and `S13Proof` beside one another. Start `glc_v3` first. Then, from this directory:

```bash
uv sync

export GLC_BASE_URL=http://127.0.0.1:8111
export S13_GATEWAY_PROVIDER=gemini
export S13_SANDBOX_ROOT="$PWD/sandbox"
export S13_CHUNK_MODEL=phi4:latest
export S13_LIVE_SEMANTIC_CHUNKING=1

uv run s13code serve
```

State is written under `~/.s13code` by default. Set `S13_DATA_DIR` to use another directory.

Check both services:

```bash
curl http://127.0.0.1:8111/healthz
curl http://127.0.0.1:8113/healthz
curl http://127.0.0.1:8113/readyz
curl http://127.0.0.1:8113/.well-known/agent-card.json
```

## Run a prompt

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "s13",
    "user_id": "student-01",
    "agent_id": "assistant",
    "prompt": "Say hello."
  }'
```

The response contains the final answer, graph nodes and edges, ordered graph events, and provider/agent assignments. Inspect a persisted run with:

```bash
curl http://127.0.0.1:8113/v1/agent/runs/<run-id>
```

## Index the sample corpus

The five files under `sandbox/papers/` are fixed `.txt` fixtures for semantic chunking and retrieval proofs.

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "papers",
    "user_id": "student-01",
    "prompt": "Index every .txt file under papers/. Confirm how many chunks were indexed in total."
  }'
```

Document ingestion is versioned and atomic: source preparation, semantic boundaries, exact spans, Nomic embeddings, and visibility succeed together or roll back together.

## Architecture

- `s13code/core/live_graph/`: durable graph state, patches, event replay and bounded parallel execution
- `s13code/core/memory/`: scope checks, provenance, contradiction history, semantic chunking and FAISS retrieval
- `s13code/core/a2a_adapter/`: Agent Cards, JSON-RPC, SSE/push, official gRPC and trust checks
- `s13code/gateway.py`: the only `S13Code → glc_v3` seam
- `s13code/runtime.py`: joins graph, memory, tools and model calls into an inspectable run
- `tests/`: executable invariants and regression cases

## Test before opening a pull request

```bash
uv run ruff check .
uv run pytest -q

cd ../S13Proof
uv sync
uv run pytest -q
```

## Student contribution

Fork the official [`theschoolofai/S13Code`](https://github.com/theschoolofai/S13Code) repository linked from Axiom, create a branch, implement one meaningful extension, and open one pull request against that repository. Do not open the Session 13 pull request against [`theschoolofai/glc_v3`](https://github.com/theschoolofai/glc_v3).

Add one subsection to this README in the same pull request. It must contain:

1. the user-visible capability,
2. the exact prompt or API request,
3. the graph and ordered event trace,
4. the actual final result,
5. evidence and provider/agent assignments,
6. the adversarial failure and its fix,
7. commands that reproduce the result from a fresh checkout.

Do not commit `.env`, credentials, personal memory, generated databases, unrestricted local paths, benchmark output containing private data, or provider responses containing secrets. Use synthetic identities in every proof.

## Student contribution: Part 1 — floor reproduction

Four baseline behaviours reproduced end-to-end through the real runtime and the
real A2A adapter, offline (deterministic embedder; stubbed web/LLM; an in-process
A2A gRPC server). Full prompt, graph, ordered event trace, provider/agent
assignments, evidence, and answer for each are printed by:

```bash
uv run python proofs/floor_cases.py
```

| Case | Prompt / request | Graph (earned in order) | What the ordered trace proves |
|---|---|---|---|
| **Live expansion** | *Search for "Python asyncio best practices", read the top 3 results…* | `search → {fetch_1,fetch_2,fetch_3} → distill → answer` | `search` is the only node until it succeeds (#4); the three `fetch_*` nodes are added *after* (#5) and start together (#6–8). Future nodes never precede their inputs. |
| **Durable-memory round trip** | run A: *My mom's birthday is 15 May 2026. Remember that…* → run B: *When is mom's birthday?* | A: `remember → reminder → answer`; B: `recall → answer` | B (a *separate* runtime instance) recalls the `kind=fact` sourced to `api://agent/runs`, ranked above the model's own prior episode. The fact — not the earlier answer — is the citation. |
| **Semantic document query** | *Index the file paper.md and tell me its key result.* | `index_file → recall → distill → answer` | Indexing is durable *before* retrieval is added (#5); the answer is grounded in the retrieved `document_chunk`, provider assignments visible per node. |
| **A2A waiting/resume** | *Slow remote report: explain why an agent card is not permission to access local memory.* | `remote` (waiting → pending → succeeded) | Node parks in `waiting` and the local worker is released; `a2a_task_completed` (external) resumes exactly that node; the artifact returns as untrusted evidence. Trace: `run_started → wait → a2a_task_completed → run_resumed → task_started → task_succeeded → finish`. |

**Honest limitation the trace exposed.** In the semantic-document case the manifest
reports `segmentation outcomes=['below_semantic_floor']`: the sample document was
short enough that Rohan V2's suffix-rollover boundary detection **never actually
ran** — the file became a single chunk by the deliberate small-block floor, not by
topic analysis. So this case proves the *indexing/retrieval/grounding path* but does
**not** exercise semantic chunking; that requires a multi-topic document above the
floor. A second, subtler observation: cross-run recall also surfaces the model's own
prior answer as an `episode`; it is correctly ranked below the user fact here, but
that ranking — not a hard boundary — is all that stops a model-authored prior answer
from being treated as a user source.

## Student contribution: Part 2 & 3 — speculative recall-vs-web race with evidence-justified cancellation

**1. User-visible capability.** The old planner commits to exactly one strategy
per intent: a memory question runs `recall` only, and if durable memory is empty
the run answers from nothing. This extension adds an outcome-driven behaviour the
planner could not express before. When a user asks a memory question *and*
explicitly authorises a web fallback, the graph launches **both** strategies
(`recall` and `web`) at once and lets the first *usable* outcome earn the answer.
The redundant strategy is cancelled only when **evidence**, not arrival order,
justifies it: a fast-but-empty `recall` never tears down a slower `web` search
that still owns the only evidence. When memory *is* confident, the still-running
`web` worker is cancelled and its late result is discarded rather than recorded.

The decision is a single pure function,
[`resolve_speculative_race`](s13code/core/live_graph/speculative.py), wired into
the deterministic planner's new `speculative_recall` mode in
[`runtime.py`](s13code/runtime.py). It changes no existing intent and adds no
dependency.

**2. Exact request.**

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "What is my travel budget? Answer from memory, but search the web if you don'\''t have it.",
    "tenant_id": "proof", "project_id": "speculative",
    "user_id": "student-01", "agent_id": "assistant"
  }'
```

The reproducible offline proof (`proofs/speculative_race.py`) runs the same
prompt through the real runtime twice, with and without a durable fact.

**3 + 4 + 5. Graph, ordered event trace, final result, evidence & assignments.**

*Scenario A — a durable fact exists, so `recall` wins and `web` is cancelled:*

```
graph nodes:  recall succeeded (memory_recall) | web cancelled (web_fallback) | answer succeeded (answer_with_evidence)
graph edges:  recall -> answer            # answer depends only on the winning strategy

 #1  run_started
 #2  graph_patched      add=['recall','web']   (first frontier for speculative_recall)
 #3  task_started       recall
 #4  task_started       web                     # both strategies live together
 #5  task_succeeded     recall
 #6  task_cancelled     web                      # cancelled while running...
 #7  graph_patched      add=['answer'] cancel=['web']   (usable evidence; cancel the redundant live sibling)
 #8  task_started       answer
 #9  task_succeeded     answer
 #10 graph_patched      finish                   (grounded answer produced)

final answer: "Grounded answer built from 1 evidence item(s)."
evidence:     the durable FACT "Travel budget is 90000 rupees." [source: chat://budget/1]
              web's late result is NOT in the graph (node result = null)
providers:    answer -> stub-gateway   recall/web -> local (no provider)
```

The `answer` node does not exist until event #7 — *after* the first real outcome
(#5). Future nodes never precede their inputs.

*Scenario B — no durable fact, so empty `recall` waits and `web` supplies the answer:*

```
graph edges:  recall -> answer, web -> answer

 #5  task_succeeded     recall
 #6  graph_patched      (no evidence; await the still-live speculative sibling)   # NOT cancelled
 #7  task_succeeded     web
 #8  graph_patched      add=['answer']            (sibling terminal; answer from web evidence)
 #11 graph_patched      finish

final answer: "Grounded answer built from 1 evidence item(s)."  (grounded in the web hit)
```

**6. Adversarial failure and fix.** The attack is the exact scenario a naive
speculative planner gets wrong: a **fast-but-empty `recall` racing a slow-but-useful
`web`**. A naive planner cancels the sibling on arrival order, so it tears down
`web` the moment empty `recall` returns and answers with zero evidence.
`tests/test_speculative_cancellation.py` runs this attack both ways against the
same workers:

- `test_naive_arrival_order_planner_loses_the_only_evidence` — **before**: the
  naive decision cancels `web`; the answer worker sees `0` evidence items.
- `test_evidence_justified_planner_waits_and_keeps_the_evidence` — **after**: the
  shipped `resolve_speculative_race` keeps `web` alive; the answer worker sees `1`.

A second attack, `test_late_result_after_cancellation_is_discarded`, forces both
strategies to complete in one event-loop tick: the winner cancels the loser, and
the loser's already-produced result is dropped instead of patching the graph (no
`task_succeeded` for the loser, planner never re-entered for it).

**7. Reproduce from a fresh checkout.**

```bash
git clone https://github.com/Sujthr/S13Code.git && cd S13Code
git checkout feat/speculative-strategy-race
uv sync --dev

# the extension's proof suite (11 tests: unit, executor-level attack, e2e runtime)
uv run pytest tests/test_speculative_cancellation.py -v

# the end-to-end trace shown above (offline: no network, no Ollama, no keys)
uv run python proofs/speculative_race.py

# floor + extension together
uv run ruff check .
uv run pytest -q
```

> Honest limitation exposed by the traces: `resolve_speculative_race` treats
> "usable" as "the outcome returned any hit". A high-similarity but *irrelevant*
> memory hit would still win the race and cancel a web search that might have been
> more on-point. Usefulness here means "produced candidate evidence", not
> "produced correct evidence" — ranking relevance stays with the downstream
> answer worker, which is instructed to treat similarity as a hint, not proof.

## License

MIT. See `LICENSE`.
