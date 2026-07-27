# kalshi-arb-engine

Static-arbitrage detection for Kalshi bracket and ladder markets. It makes no
prediction about outcomes. It checks whether quoted prices are mutually
coherent, and when they are not, it identifies the exact locking portfolio, its
maximum fillable size, and how long the violation persisted.

## Safety boundaries

**This never places a real order.** The Kalshi client issues `GET` requests and
WebSocket subscriptions only; there is no order, cancel, deposit, or withdrawal
path anywhere in the codebase. Credentials are a read-only API key loaded from
the environment.

Paper trading is simulation against quoted depth. It moves a number in SQLite,
never money.

## The idea

A set of related contracts is arbitrage-free **if and only if** some probability
distribution over the outcome states is consistent with every quoted price. When
no such distribution exists, the prices contradict each other and a risk-free
portfolio exists. That is the finite-state fundamental theorem of asset pricing,
and detecting arbitrage is detecting the infeasibility of that price system —
one linear program.

Every common case is a special case of that LP:

| Case | Coherence condition | Violation |
|---|---|---|
| Complement | `ask_yes + ask_no + fees >= 1` | buy both, guaranteed $1 payout |
| Partition | `sum(ask_i + fee_i) >= 1` | buy one of each, guaranteed $1 payout |
| Monotonic ladder | A implies B ⟹ `price_A <= price_B` | buy the cheap superset, sell the rich subset |
| Time monotonic | earlier implies later ⟹ non-decreasing in horizon | same inversion |

So the fast O(n) checks run first (they localize clean 2-leg locks that are
easier to fill), and the general LP runs as both fallback and cross-check. The
LP must never find *less* than a specialized detector does — that invariant is
asserted in the tests and logged as a warning at runtime.

**Where the edge actually lives:** multi-outcome bracket and ladder markets —
economic-indicator ranges, crypto price buckets, temperature bands — where many
thin related contracts and uncoordinated order flow break coherence. Two-sided
moneyline games almost never violate it. Point `TARGET_SERIES` at bracketed
series, not game markets.

## Architecture

```
Kalshi REST/WS (read-only)
        │
        ├─> groups.py      parse strikes → validated StateSpace + payoff matrix
        │                  (rejects gaps/overlaps rather than guessing)
        ↓
   ContractGroup
        │
        ├─> detectors/specialized.py   complement · partition · monotonic   (O(n), 2-leg locks)
        ├─> detectors/lp.py            general LP                           (fallback + validator)
        ↓
   scanner.py    dedupe · rank by executability · track persistence
        │
        ├─> storage.py   SQLite: order_book_snapshots, arb_opportunities, paper_positions
        ├─> alerts.py    console + CSV
        └─> paper.py     simulated fills, leg-risk modelling, settlement
```

## Setup

```bash
cp .env.example .env          # fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest    # 87 tests

arbengine discover            # list series ranked by bracket density
arbengine scan --once         # single pass
arbengine scan                # continuous loop
```

`discover` is the first thing to run. Kalshi's active series rotate, so
`TARGET_SERIES` in `.env.example` is a starting guess, not a verified list —
`discover` ranks what is actually open right now by how many bracketed markets
it has.

## What it records

`arb_opportunities` is keyed uniquely on `(group, type, leg structure)`, so a
violation that persists across many scans extends one row rather than spawning a
new one per tick. `last_seen - first_seen` is therefore the true persistence
duration.

**That persistence distribution is the actual output of this tool.** It answers
whether an opportunity was ever capturable at REST latency before any capital is
committed. A 40-cent lock that lives for 200ms is a statistic, not a trade.

## Paper trading

Enabled by `PAPER_ENABLED`. Every detected lock is simulated against quoted
depth and settled against the realized outcome state.

The thing that makes this honest: **Kalshi has no atomic multi-leg fill.** A
simulator that assumes every leg fills at the quoted price will report a
flawless win rate for a strategy that in reality gets picked off leg by leg. So
legs fill independently here, and when they fill unevenly the position is marked
`broken` and the unhedged residual is settled as the directional exposure it
actually is.

The summary reports locked and broken P&L **separately** on purpose. Blending
them hides the only number that decides whether this is tradeable for real.

Two knobs for stress-testing:

- `PAPER_LEG_FILL_PROB` — per-leg fill probability. `1.0` is the optimistic
  bound and *will* overstate real performance. Lower it to see how much edge
  survives leg risk.
- `PAPER_SLIPPAGE_CENTS` — adverse price movement per leg on entry.

The summary also prints `realized/expected` on locked positions. If that ratio
sits well under 1.0, the fee model or the state space is wrong — it is a
tripwire, not a performance metric.

## Config

Env-driven via `pydantic-settings`; see `src/arbengine/config.py`.

| Var | Default | Meaning |
|---|---|---|
| `FEE_MULTIPLIER` | `0.07` | `fee(P) = ceil(m·P·(1−P)·100)/100` — **verify per market category** |
| `MIN_GUARANTEED_PROFIT` | `0.01` | minimum `t*` in dollars to flag |
| `MIN_FILLABLE_SETS` | `2` | ignore 1-lot locks |
| `MAX_LEG_COUNT_ALERT` | `4` | above this, mark elevated execution risk |
| `LP_TOLERANCE` | `1e-6` | numeric tolerance on `t*` |
| `TARGET_SERIES` | — | comma-separated series tickers to scan |
| `KALSHI_POLL_SEC` / `MAX_QUOTE_AGE_SEC` | `10` / `30` | poll interval and staleness guard |

## Known limitations

**Fee model is conservative.** The full per-contract fee is charged at the
quoted price so the LP stays linear. Kalshi rounds once per order, so this
slightly *understates* profit — the safe direction for an arbitrage check, but
it means marginal opportunities get filtered out.

**Inclusive vs exclusive strike bounds are collapsed.** `greater` (x > K) and
`greater_or_equal` (x >= K) both normalize to `(K, ∞)`. Kalshi's numeric strikes
are quantized, so this is usually harmless, but a ladder mixing both types is
wrong at exactly one point.

**Subtitle parsing is a fallback, not a source of truth.** When structured
strike fields are absent, `groups.py` parses ranges out of subtitle text. That
cannot distinguish inclusive from exclusive bounds and Kalshi's phrasing is not
stable across series. Structured strikes always win when both are present.

**Integer flooring can break a hedge.** The LP solves in continuous quantities;
flooring to whole contracts can leave the portfolio unbalanced. The engine
recomputes the true worst case over all states for the *integer* portfolio and
reports that, never the LP optimum — but it means the reported profit is
sometimes materially below `t*`.

**REST polling understates opportunity count.** Arbitrage windows are short. The
WS path (`stream_books`) exists for this reason but is not yet wired into the
main loop; the REST loop measures persistence honestly but will miss fast ones.

## Verify against current docs before trusting

- Kalshi fee multiplier per market category.
- How bracket ranges and thresholds are encoded in market metadata for the
  series you target (`strike_type` / `floor_strike` / `cap_strike` vs subtitle).
- Order-book endpoint and WS channel names.
- Which bracketed series are currently active and liquid enough to matter.

## Testing

```bash
.venv/bin/python -m pytest
```

Covers the fee model at known prices, all four specialized detectors (including
the cases where a realistic fee correctly erases a 1-cent gap), the LP and its
state-price diagnostic, the cross-check that the LP dominates every specialized
result, state-space validation on gaps and overlaps, the YES-ask-from-NO-bid
book inversion, the staleness guard, persistence tracking, and paper-trade
settlement in every outcome state.
