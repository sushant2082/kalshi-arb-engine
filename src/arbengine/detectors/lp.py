"""
General linear-programming arbitrage detector.

A set of related contracts is arbitrage-free if and only if some probability
distribution over the outcome states is consistent with every quoted price.
When no such distribution exists, the prices contradict each other and a
risk-free portfolio exists. Detecting arbitrage is therefore detecting the
infeasibility of that price system, which is one LP.

Every specialized case (complement, partition, ladder) is a special case of this
same program, so the LP doubles as the validator for the fast detectors.
"""

import logging
from datetime import datetime

import numpy as np
from scipy.optimize import linprog

from arbengine.fees import ROUNDING_HEADROOM, buy_cost, order_fee, sell_proceeds
from arbengine.models import ArbOpportunity, ContractGroup, Leg

log = logging.getLogger(__name__)


def solve_lp(
    payoff: np.ndarray,
    asks: list[float | None],
    bids: list[float | None],
    ask_sizes: list[int],
    bid_sizes: list[int],
    fee_multiplier: float,
    tolerance: float = 1e-6,
) -> tuple[float, np.ndarray, np.ndarray] | None:
    """
    Maximize the guaranteed profit floor `t` over depth-bounded positions.

    Variables: [buy_1..buy_n, sell_1..sell_n, t].

        maximize   t
        subject to Pi(s) - C >= t     for every state s
                   0 <= buy_i  <= ask_size_i
                   0 <= sell_i <= bid_size_i
                   t free

    where Pi(s) = sum_i (buy_i - sell_i) * M[i][s] is the terminal value in
    state s and C = sum_i buy_cost_i * buy_i - sum_i sell_proceeds_i * sell_i is
    today's net cash outlay. Rearranged for scipy's A_ub x <= b_ub form, each
    state gives:

        sum_i buy_i * (buy_cost_i - M[i][s])
      + sum_i sell_i * (M[i][s] - sell_proceeds_i)
      + t  <=  0

    scipy minimizes, so the objective is -t.

    Returns (t*, buy_quantities, sell_quantities), or None if the solve fails.
    t* is always >= 0 because the zero portfolio with t = 0 is always feasible;
    an arbitrage exists only when t* exceeds `tolerance`.
    """
    n, n_states = payoff.shape
    if n == 0 or n_states == 0:
        return None

    # A missing quote means that direction is untradeable: pin its bound to 0
    # and use a placeholder price that can never be selected.
    buy_costs = np.zeros(n)
    sell_proceeds_arr = np.zeros(n)
    ub_buy = np.zeros(n)
    ub_sell = np.zeros(n)

    for i in range(n):
        if asks[i] is not None and ask_sizes[i] > 0:
            buy_costs[i] = buy_cost(asks[i], fee_multiplier)
            ub_buy[i] = float(ask_sizes[i])
        if bids[i] is not None and bid_sizes[i] > 0:
            sell_proceeds_arr[i] = sell_proceeds(bids[i], fee_multiplier)
            ub_sell[i] = float(bid_sizes[i])

    if ub_buy.sum() == 0 and ub_sell.sum() == 0:
        return None

    n_vars = 2 * n + 1

    # Objective: minimize -t
    c = np.zeros(n_vars)
    c[-1] = -1.0

    # One constraint row per state.
    a_ub = np.zeros((n_states, n_vars))
    for s in range(n_states):
        a_ub[s, :n] = buy_costs - payoff[:, s]
        a_ub[s, n : 2 * n] = payoff[:, s] - sell_proceeds_arr
        a_ub[s, -1] = 1.0
    b_ub = np.zeros(n_states)

    bounds = (
        [(0.0, ub_buy[i]) for i in range(n)]
        + [(0.0, ub_sell[i]) for i in range(n)]
        + [(None, None)]
    )

    res = linprog(c, A_ub=a_ub, b_ub=b_ub, bounds=bounds, method="highs")

    if not res.success:
        log.debug("LP solve failed: %s", res.message)
        return None

    t_star = float(-res.fun)
    buys = np.asarray(res.x[:n], dtype=float)
    sells = np.asarray(res.x[n : 2 * n], dtype=float)

    # Clean numerical dust so downstream integer rounding is not fighting 1e-13.
    buys[np.abs(buys) < tolerance] = 0.0
    sells[np.abs(sells) < tolerance] = 0.0

    return t_star, buys, sells


def state_prices(
    payoff: np.ndarray,
    asks: list[float | None],
    bids: list[float | None],
    fee_multiplier: float,
) -> np.ndarray | None:
    """
    Diagnostic for the no-arb case: recover state prices pi(s) >= 0 with
    sell_proceeds_i <= sum_s pi(s) * M[i][s] <= buy_cost_i for every contract.

    Their existence confirms the quotes are mutually coherent, and normalizing
    them gives the market-implied distribution over states. Not required for
    detection — useful when debugging why a group did or didn't fire.
    """
    n, n_states = payoff.shape

    # Feasibility problem: no objective, just find any pi satisfying the bounds.
    c = np.zeros(n_states)

    rows: list[np.ndarray] = []
    rhs: list[float] = []
    for i in range(n):
        if asks[i] is not None:
            #  sum_s pi(s) M[i][s] <= buy_cost_i
            rows.append(payoff[i].copy())
            rhs.append(buy_cost(asks[i], fee_multiplier))
        if bids[i] is not None:
            # -sum_s pi(s) M[i][s] <= -sell_proceeds_i
            rows.append(-payoff[i])
            rhs.append(-sell_proceeds(bids[i], fee_multiplier))

    if not rows:
        return None

    res = linprog(
        c,
        A_ub=np.array(rows),
        b_ub=np.array(rhs),
        bounds=[(0.0, None)] * n_states,
        method="highs",
    )
    if not res.success:
        return None
    return np.asarray(res.x, dtype=float)


def _integerize(
    buys: np.ndarray,
    sells: np.ndarray,
    payoff: np.ndarray,
    asks: list,
    bids: list,
    fee_multiplier: float,
) -> tuple[np.ndarray, np.ndarray, float]:
    """
    Kalshi trades whole contracts, so the LP's continuous solution has to be
    floored to integers before it means anything executable.

    Flooring can only reduce position size, but it can also break the hedge —
    the worst-case profit of the floored portfolio is not guaranteed to stay
    positive. So we recompute the true worst-case profit over all states for the
    integer portfolio and return that, rather than reporting the LP's optimum.
    Callers must threshold on the recomputed value.
    """
    int_buys = np.floor(buys + 1e-9)
    int_sells = np.floor(sells + 1e-9)

    net = int_buys - int_sells
    terminal = payoff.T @ net  # value in each state

    # Price the floored portfolio with EXACT per-order fees rather than the
    # linearized rate the LP optimized against. The LP has to be linear, but the
    # number reported to the caller should be what Kalshi would actually charge.
    cash_out = 0.0
    for i in range(len(int_buys)):
        qty = int(int_buys[i])
        if qty > 0 and asks[i] is not None:
            cash_out += asks[i] * qty + order_fee(asks[i], qty, fee_multiplier)
        qty = int(int_sells[i])
        if qty > 0 and bids[i] is not None:
            cash_out -= bids[i] * qty - order_fee(bids[i], qty, fee_multiplier)

    worst = float(np.min(terminal)) - cash_out if terminal.size else -cash_out

    return int_buys, int_sells, worst


def detect_lp(
    group: ContractGroup,
    fee_multiplier: float,
    now: datetime,
    tolerance: float = 1e-6,
    min_profit: float = 0.0,
) -> ArbOpportunity | None:
    """
    Run the general LP against a validated group and return the locking
    portfolio if one exists at whole-contract sizes.
    """
    contracts = group.contracts
    payoff = group.payoff

    asks = [c.ask for c in contracts]
    bids = [c.bid for c in contracts]
    ask_sizes = [c.ask_size for c in contracts]
    bid_sizes = [c.bid_size for c in contracts]

    solved = solve_lp(
        payoff, asks, bids, ask_sizes, bid_sizes, fee_multiplier, tolerance
    )
    if solved is None:
        return None

    t_star, buys, sells = solved
    if t_star <= tolerance:
        return None

    n = len(contracts)
    buy_costs = np.zeros(n)
    sell_proceeds_arr = np.zeros(n)
    for i, c in enumerate(contracts):
        if c.ask is not None and c.ask_size > 0:
            buy_costs[i] = buy_cost(c.ask, fee_multiplier)
        if c.bid is not None and c.bid_size > 0:
            sell_proceeds_arr[i] = sell_proceeds(c.bid, fee_multiplier)

    int_buys, int_sells, worst = _integerize(
        buys, sells, payoff, asks, bids, fee_multiplier
    )

    if worst <= max(min_profit, tolerance):
        log.debug(
            "Group %s: LP found t*=%.4f but integer portfolio worst-case is %.4f",
            group.group_id, t_star, worst,
        )
        return None

    legs: list[Leg] = []
    for i, c in enumerate(contracts):
        if int_buys[i] > 0:
            legs.append(
                Leg(
                    ticker=c.ticker, side="buy", qty=int(int_buys[i]),
                    price=c.ask,
                    fee=order_fee(c.ask, int(int_buys[i]), fee_multiplier),
                )
            )
        if int_sells[i] > 0:
            legs.append(
                Leg(
                    ticker=c.ticker, side="sell", qty=int(int_sells[i]),
                    price=c.bid,
                    fee=order_fee(c.bid, int(int_sells[i]), fee_multiplier),
                )
            )

    if not legs:
        return None

    total_fee = sum(leg.fee for leg in legs)
    total_cost = -sum(leg.cash_flow for leg in legs)

    # "Fillable sets" for an LP portfolio is not a clean multiple the way a
    # partition is — the solution is already depth-bounded, so it represents one
    # complete execution. Report 1 set and let min_leg_size carry the depth
    # information the ranker needs.
    return ArbOpportunity(
        group_id=group.group_id,
        type="lp",
        legs=legs,
        total_cost=total_cost,
        total_fee=total_fee,
        guaranteed_profit=worst,
        fillable_sets=1,
        min_leg_size=min(leg.qty for leg in legs),
        leg_count=len(legs),
        first_seen=now,
        last_seen=now,
    )
