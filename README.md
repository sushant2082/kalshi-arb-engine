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

## What the live data actually looks like

Verified against the live API on 2026-07-28 (181 bracketed series open):

**Almost everything wide is a ladder, not a partition.** `KXBTCD`, `KXNATGASD`,
`KXCPIYOY`, `KXWTI` and friends publish nested `greater` ("above K") contracts —
75 to 188 legs on one outcome variable. So the monotonic detector, not the
partition detector, is where the edge lives.

**Wide bracket partitions can't fire, structurally.** `KXBTC` publishes 188 true
`between` brackets, but the asks sum to over $3 because far-out-of-the-money
brackets are quoted at the $0.01 minimum tick when they're worth ~$0. Add a
minimum $0.01 fee per leg and a 188-leg partition carries $1.88 of fees alone.
No partition arbitrage is possible at that width — don't wait on one.

**The ladders sit 2–5 cents from inverting.** That is the real finding. It is
close enough that violations plausibly occur intermittently, and far enough that
they weren't present in any pass so far. This is a question only sustained
monitoring answers, which is what the persistence table is for.

`scan` prints the three closest groups on every pass that finds nothing —
"no violations" on its own can't be distinguished from a detector that is
silently inert.

## Rate limiting and the CDN cache

Two things about Kalshi's API shape the whole design, and neither is obvious
from the docs.

**Market data is CDN-cached for 15 seconds.** `/markets` comes back through
CloudFront with `Cache-Control: max-age=15`; the `Age` header climbs 3 → 13 and
resets on a miss. So a REST response can describe a book from 15 seconds ago,
and polling faster than that returns byte-identical bytes. `KALSHI_POLL_SEC`
defaults to 15 for that reason — below it you pay tokens to learn nothing.

That is also a correctness issue, not just efficiency. The staleness guard
measures when quotes were *true*, so both REST paths back-date their timestamps
by the response's `Age`. Without it a cached response looks fresh, and the guard
happily compares a live leg against a 14-second-old one — which manufactures
arbitrage that never existed.

**The WebSocket is the only real-time path.** It is not cached. If you care
about violations that live less than 15 seconds — and the fee arithmetic says
those are the only ones that survive fees — use `stream`, not `scan`.

**Uncached requests cost ~5x the documented rate.** `GET
/account/endpoint_costs` reports 10 tokens for `/markets`, and cache hits do
behave that cheaply. A cache *miss* bills far more. Measured by forcing misses
with distinct series tickers: clean at 3 misses/s, rate limited at 4.4, implying
**~46 tokens per uncached call**. A 200/s Basic Read budget therefore sustains
about 4 uncached requests per second, not 20.

The average rate matters less than the burst. A scanner averaging 2 req/s still
429s constantly if it fires each pass as a burst priced at 10 tokens per
request. Calibrating `KALSHI_REQUEST_COST` to 50 took a representative run from
191 rate-limit responses to zero.

Per-second Read budgets by tier: basic 200, advanced 300, expert 600,
premier 1000, paragon 2000, prime 4000, prestige 6000. Set
`KALSHI_READ_BUDGET` to match yours.

## Known limitations

**Fee model is conservative.** The full per-contract fee is charged at the
quoted price so the LP stays linear. Kalshi rounds once per order, so this
slightly *understates* profit — the safe direction for an arbitrage check, but
it means marginal opportunities get filtered out. The per-series scaling factor
is read live from `/series`; `FEE_MULTIPLIER` is only the base rate it scales.

**Sub-tick bracket gaps are closed automatically.** Kalshi's `between` brackets
are inclusive on both ends, so consecutive brackets read as `[55700, 55799.99]`
and `[55800, 55899.99]`, leaving a one-cent sliver when converted to half-open
intervals. Those slivers are unreachable (the variable is quantized) and are
snapped shut. The tolerance is 5% of the median bracket width, so a genuinely
missing bracket still fails validation — but a series with wildly uneven bracket
widths could in principle have a real gap absorbed.

**Detection uses top of book only.** Quotes come from `/markets`, which carries
level one and its size. That keeps every leg in an event on one timestamp and
avoids a request storm, but it means fillable size is capped at the best level;
deeper liquidity is invisible to the LP's depth bounds.

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
