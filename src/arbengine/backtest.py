"""
Synthetic dislocation backtest.

Nothing has fired against live prices yet, which means the detection →
paper-fill → settlement path has never executed end to end on real market
geometry. That is a dangerous state to sit in: the first genuine opportunity
would be the first time this code runs, and a bug there costs the opportunity.

So this module takes a real group with real quotes and perturbs one leg until
coherence breaks, then checks that:

  1. a detector fires,
  2. the reported portfolio is genuinely riskless across EVERY state,
  3. the paper broker fills it,
  4. settlement in every state returns the promised profit.

Step 2 and 4 are the substance. A detector that fires is easy; a detector whose
portfolio actually pays in the worst state is the claim being tested. Settling
in only one state would let a directional bet pass as a lock.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from arbengine.config import Settings
from arbengine.models import ArbOpportunity, ContractGroup
from arbengine.paper import PaperBroker
from arbengine.scanner import scan_group

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    group_id: str
    shape: str
    legs: int
    scenario: str
    detected: bool = False
    detector: str = ""
    expected_profit: float = 0.0
    # Worst-case realized P&L across every settlement state.
    worst_pnl: float | None = None
    best_pnl: float | None = None
    states_tested: int = 0
    riskless: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.detected and self.riskless

    def __str__(self) -> str:
        if not self.detected:
            return f"  {self.scenario:<28} NO FIRE   ({self.group_id})"
        mark = "PASS" if self.passed else "FAIL"
        return (
            f"  {self.scenario:<28} {mark}  {self.detector:<11} "
            f"expected=${self.expected_profit:.4f} "
            f"worst=${self.worst_pnl:+.4f} best=${self.best_pnl:+.4f} "
            f"states={self.states_tested}"
        )


def _cheapen_all_asks(group: ContractGroup, factor: float) -> ContractGroup:
    """
    Scale every ask down so a bracket partition costs less than $1.

    This is the partition dislocation: the legs still tile the outcome space,
    they are just collectively underpriced.
    """
    updated = []
    for c in group.contracts:
        if c.ask is None:
            updated.append(c)
            continue
        new_ask = max(round(c.ask * factor, 4), 0.001)
        updated.append(c.model_copy(update={
            "ask": new_ask,
            "ask_size": max(c.ask_size, 10),
        }))
    return group.model_copy(update={"contracts": updated})


def _invert_ladder_pair(group: ContractGroup, margin: float) -> ContractGroup | None:
    """
    Force a monotonic inversion: find contracts A, B where A implies B, then
    quote the strict subset A richer than its superset B.

    Coherence requires price(A) <= price(B). Breaking that by `margin` should
    produce a 2-leg lock worth roughly `margin` per set.
    """
    payoff = group.payoff
    ceiling = 0.99

    # The pair has to have room for the full margin below the price ceiling.
    # Picking the first implying pair is wrong: if the superset already trades
    # near $0.99, clamping the sell price silently injects a 1-cent inversion
    # instead of the requested one, fees eat it, and a correct refusal by the
    # detector looks like a miss. Prefer mid-priced legs, which also keeps the
    # scenario realistic.
    best: tuple[float, int, int] | None = None
    for i, a in enumerate(group.contracts):
        for j, b in enumerate(group.contracts):
            if i == j or b.ask is None:
                continue
            implies = bool(np.all(payoff[j] >= payoff[i])) and bool(
                np.any(payoff[j] > payoff[i])
            )
            if not implies:
                continue
            if b.ask + margin > ceiling:
                continue
            # Distance from the middle of the price range; smaller is better.
            score = abs(b.ask - 0.5)
            if best is None or score < best[0]:
                best = (score, i, j)

    if best is None:
        return None

    _, i, j = best
    a, b = group.contracts[i], group.contracts[j]
    buy_price = max(round(b.ask, 4), 0.01)
    sell_price = round(buy_price + margin, 4)

    contracts = list(group.contracts)
    contracts[i] = a.model_copy(update={
        "bid": sell_price, "bid_size": max(a.bid_size, 10),
    })
    contracts[j] = b.model_copy(update={
        "ask": buy_price, "ask_size": max(b.ask_size, 10),
    })
    return group.model_copy(update={"contracts": contracts})


def verify_riskless(
    opp: ArbOpportunity, group: ContractGroup, settings: Settings
) -> tuple[float, float, int, list[str]]:
    """
    Settle the reported portfolio in EVERY state and return (worst, best, n,
    notes).

    This is the real test. An arbitrage claim is a claim about the worst case,
    so checking one settlement proves nothing — the losing state is exactly the
    one a broken detector would omit.
    """
    notes: list[str] = []
    pnls: list[float] = []
    now = datetime.now(tz=None).replace(tzinfo=None)

    for state in range(group.state_space.n):
        broker = PaperBroker(
            bankroll=1_000_000.0,
            max_sets_per_opp=settings.paper_max_sets_per_opp,
            leg_fill_prob=1.0,
            slippage_cents=0.0,
            fee_multiplier=settings.fee_multiplier * group.fee_scale,
        )
        pos = broker.attempt(opp, opp.first_seen)
        if pos is None:
            notes.append(f"state {state}: paper broker refused the fill")
            continue
        settled = broker.settle(
            pos, group, state, opp.first_seen + timedelta(hours=1)
        )
        pnls.append(settled.pnl)

    if not pnls:
        return 0.0, 0.0, 0, notes + ["no state settled"]
    return min(pnls), max(pnls), len(pnls), notes


def run_scenario(
    group: ContractGroup, settings: Settings, scenario: str, perturbed: ContractGroup
) -> BacktestResult:
    result = BacktestResult(
        group_id=group.group_id,
        shape=group.shape,
        legs=len(group.contracts),
        scenario=scenario,
    )

    now = perturbed.contracts[0].fetched_at
    found = scan_group(perturbed, settings, now)
    if not found:
        return result

    opp = max(found, key=lambda o: o.guaranteed_profit)
    result.detected = True
    result.detector = opp.type
    result.expected_profit = opp.guaranteed_profit

    worst, best, n, notes = verify_riskless(opp, perturbed, settings)
    result.worst_pnl = worst
    result.best_pnl = best
    result.states_tested = n
    result.notes = notes
    # Riskless means profitable in the WORST state, not on average.
    result.riskless = n > 0 and worst > 0

    if n > 0 and worst <= 0:
        result.notes.append(
            f"portfolio loses ${-worst:.4f} in its worst state — NOT a lock"
        )
    return result


def backtest_group(group: ContractGroup, settings: Settings) -> list[BacktestResult]:
    """Run every applicable synthetic dislocation against one real group."""
    results: list[BacktestResult] = []

    if group.shape == "bracket":
        quoted = all(c.ask is not None for c in group.contracts)
        mece = bool(np.all(group.payoff.sum(axis=0) == 1))
        if quoted and mece:
            total = sum(c.ask for c in group.contracts)
            if total > 0:
                # Scale the whole partition to cost ~$0.95 all-in.
                for target in (0.95, 0.90):
                    factor = target / total
                    results.append(run_scenario(
                        group, settings, f"partition @ ${target:.2f}",
                        _cheapen_all_asks(group, factor),
                    ))

    for margin in (0.05, 0.02):
        perturbed = _invert_ladder_pair(group, margin)
        if perturbed is not None:
            results.append(run_scenario(
                group, settings, f"ladder inversion {margin:.0%}", perturbed
            ))

    return results


def summarize(results: list[BacktestResult]) -> dict:
    fired = [r for r in results if r.detected]
    passed = [r for r in results if r.passed]
    failed = [r for r in fired if not r.passed]
    return {
        "scenarios": len(results),
        "fired": len(fired),
        "passed": len(passed),
        "failed": len(failed),
        "missed": len(results) - len(fired),
        "failures": failed,
    }
