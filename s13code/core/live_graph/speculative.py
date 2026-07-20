"""Evidence-justified cancellation for speculative strategy races.

A *speculative race* launches two independent strategies for one goal and lets
the first outcome decide the frontier.  The whole point of the live graph is
that an outcome earns the next piece of graph, so the interesting question is
not "which strategy finished first?" but "did this outcome actually justify
cancelling the other one?".

The subtle, expensive failure mode is cancelling on **arrival order** instead
of on **evidence**: a fast-but-empty strategy tears down a slower strategy that
was about to produce the only usable evidence, and the run answers from
nothing.  ``resolve_speculative_race`` keeps cancellation tied to evidence:

* a strategy may cancel its sibling only when *its own* outcome is usable and
  the sibling is still live (``pending``/``running``);
* an empty or failed outcome never cancels a live sibling -- the graph waits;
* once either strategy has already produced the answer, the race is over.

Cancellation of the losing sibling is then made durable by the store and its
late result is discarded by the executor (see ``LiveGraphExecutor.run``); this
module only decides *whether* the cancellation is justified.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Node states in which a sibling strategy can still be cancelled or awaited.
LIVE_STATES = ("pending", "running")


@dataclass(frozen=True)
class RaceDecision:
    """The outcome-justified decision for one strategy completing a race."""

    cancel_sibling: bool
    can_answer: bool
    reason: str


def resolve_speculative_race(
    *, outcome_usable: bool, sibling_state: str | None, answered: bool
) -> RaceDecision:
    """Decide how one strategy's completion should mutate a speculative race.

    Parameters
    ----------
    outcome_usable:
        Whether *this* strategy actually produced usable evidence.  This is the
        guard that separates a real winner from a strategy that merely finished
        first with nothing.
    sibling_state:
        Current state of the competing strategy (``None`` if it was never
        added).  Only ``pending``/``running`` siblings are still cancellable.
    answered:
        Whether an ``answer`` node already exists, i.e. the race is resolved.
    """
    if answered:
        return RaceDecision(False, False, "race already resolved by an earlier strategy")

    sibling_live = sibling_state in LIVE_STATES

    if outcome_usable and sibling_live:
        return RaceDecision(
            True, True, "strategy returned usable evidence; cancel the redundant live sibling"
        )
    if outcome_usable:
        return RaceDecision(
            False, True, "strategy returned usable evidence; sibling already reached a terminal state"
        )
    if sibling_live:
        # The honest fix: no evidence here, but a sibling is still working, so
        # wait for it instead of cancelling the only path to an answer.
        return RaceDecision(
            False, False, "strategy produced no evidence; await the still-live speculative sibling"
        )
    return RaceDecision(
        False, True, "both strategies reached a terminal state; answer from whatever evidence exists"
    )
