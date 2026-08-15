# Runbook — testing the cross-venue algorithm

Everything here is **read-only and simulated**. No order is placed on any
venue, no funds move. The engine has no write path to either exchange.

## What you need

| | |
|---|---|
| A Kalshi account | free; **US-only** — the API geo-blocks elsewhere |
| Kalshi API key + RSA private key | from Kalshi's API settings |
| Python 3.11+ | |
| Polymarket | nothing — its read APIs need no auth |

Both venues' market data is public. You need Kalshi credentials only because
Kalshi requires a signed key for *any* request, including reads.

## Setup

```bash
git clone git@github.com:sushant2082/kalshi-arb-engine.git
cd kalshi-arb-engine
git checkout nikhil-testing

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 279 tests, all should pass
```

Get Kalshi credentials: **kalshi.com → Account → API Keys → Create**. You get a
key ID and download an RSA private key once. Save the key file into the repo
directory (it is gitignored).

```bash
cp .env.example .env
```

Edit `.env`:

```
KALSHI_API_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=kalshi_key.pem
```

Verify it works:

```bash
.venv/bin/arbengine discover
```

Series and market counts means you are connected. A 401 means the key ID or key
file path is wrong.

---

## The main thing to run

```bash
.venv/bin/arbengine crossrec --duration 21600 --interval 12
```

Six hours, covering an evening MLB slate. It will:

1. Find every MLB game listed on **both** Kalshi and Polymarket
2. Poll both venues' live (uncached) prices about every 12 seconds
3. Log **every** read to `crossreads.db`, crossing or not
4. Open a simulated position whenever the venues cross
5. Settle each position against the **real game result** once Kalshi resolves it
6. Print a P&L summary

Start it ~15 minutes before first pitch. US evening games typically start
between 6:00 and 8:00 PM ET.

### What the output means

```
── PAPER P&L ────────────────────────────────────────────────────
  bankroll     $10,000.00 -> $10,043.18   (realized $+43.18)
  from hedged  $+51.02   (the cross implied $+48.90)
  from broken  $-7.84   <- one leg only; variance, not edge
  settled      14 positions, 11 profitable
```

- **from hedged** — positions where *both* legs filled. This is the real
  number. Compare it to what the cross implied: if realized falls well short,
  the fee model or the matching is wrong.
- **from broken** — only one leg filled, so it is an outright bet on one team,
  not a hedge. Over a small sample this is noise in either direction. **Do not
  read a positive broken P&L as the strategy working**; it means a coin landed
  your way.
- Positions stay `open` until their Kalshi market resolves. Re-run later to
  sweep them, or query directly.

---

## Other commands

```bash
# 5-minute look at what is happening right now, no paper trading
.venv/bin/arbengine crossmlb --duration 300 --interval 30

# Live monitor on uncached feeds, in-progress games only
.venv/bin/arbengine crosslive --duration 600 --interval 5

# Event-driven: measures how LONG each cross survives (50ms resolution)
.venv/bin/arbengine crossevent --duration 21600

# Intra-Kalshi arbitrage only, no second venue, no settlement risk
.venv/bin/arbengine scan --once
```

`crossevent` is the open research question — see "What we don't know" below.

---

## Reading the databases

| File | Contents |
|---|---|
| `crossreads.db` | every price observation, crossing or not |
| `crosspaper.db` | simulated positions and their settled P&L |
| `crossevents.db` | cross open/close times from `crossevent` |

```sql
-- P&L by fill status. Hedged is the real number.
SELECT fill_status, status, COUNT(*), ROUND(SUM(pnl),2)
FROM paper_positions GROUP BY fill_status, status;

-- every position, most recent first
SELECT opened_at, game, away_venue||'/'||home_venue AS route,
       sets_wanted, ROUND(cost,2), fill_status, winner, ROUND(pnl,2)
FROM paper_positions ORDER BY opened_at DESC;

-- cross rate by game phase (minutes_in is negative pregame)
SELECT CASE WHEN minutes_in < 0 THEN -1 ELSE CAST(minutes_in/30 AS INT) END AS bucket,
       COUNT(*), SUM(is_cross),
       ROUND(100.0*SUM(is_cross)/COUNT(*), 2) AS rate_pct
FROM cross_reads GROUP BY bucket ORDER BY bucket;
```

---

## Tuning

In `.env`:

```
PAPER_BANKROLL=10000            # starting simulated bankroll
PAPER_MAX_SETS_PER_OPP=200      # cap per position
PAPER_LEG_FILL_PROB=1.0         # see below
FEE_MULTIPLIER=0.07             # verified against Kalshi's fee schedule
```

**`PAPER_LEG_FILL_PROB` is the most important knob.** At `1.0` both legs always
fill at the quoted price, which is the optimistic bound and *will* overstate
results — neither venue offers atomic multi-leg fill, so in reality you place
one leg, then the other, and the price can move between. Set it to `0.8` or
`0.7` to see how much edge survives leg risk. Broken positions will appear, and
that is the point.

---

## What we already know

From an 11,614-read study (see `data/ANALYSIS.md`):

- Crosses are **~40× more frequent in live play than pregame** (0.07% → 4.83%)
- The typical cross is **0.32% and worth $0.07** once capped by the thinner leg
- **78% exist for a single 18-second read**
- Six of 165 crosses exceeded $100; four of those six were genuine venue
  disagreements with both books internally coherent

## What we don't know

- **How long a cross actually lasts.** 18-second polling only proves "shorter
  than 18 seconds". `crossevent` measures this at 50ms — that data does not
  exist yet, and it decides whether any of this is actionable.
- **Whether the quoted depth would actually fill.** No orders have been placed.
- **Whether a hedge holds through settlement.** See below.

## These are not risk-free positions

A cross only locks a profit if **both venues resolve the game the same way**.
They are separate companies with separate rulebooks, and they already differ:

| | Postponed game |
|---|---|
| **Kalshi** | closes after the rescheduled game, "within two days" |
| **Polymarket** | "remains open until the game has been completed", no limit |

A postponement beyond two days can settle differently on the two venues, and if
they disagree **both legs can lose**. Polymarket resolves a tied uncompleted
game 50-50; Kalshi's tie handling does not appear in its rules text.

This is *resolution basis risk*. No amount of order-book analysis detects it,
which is why the paper trader settles against the real Kalshi result rather
than assuming a hedge pays $1.

## If something looks too good

Every large edge in this project's history turned out to be a bug before it
turned out to be real. In order of how often they bit:

1. **Stale data.** Kalshi's `/markets` is cached 15s, Polymarket's Gamma API
   300s. Comparing either against a live feed manufactures double-digit crosses.
   The code only uses live endpoints — but if you add a data source, check it.
2. **Mismatched games.** A baseball series plays the same teams on consecutive
   days; joining on date alone maps one line onto several different games,
   including already-decided ones.
3. **In-progress vs pregame.** A live price against a pregame line looks like a
   20-point edge and is just the game having happened.
4. **Percentage without depth.** A market advertising $5,543 of "liquidity" had
   50 contracts at its best ask.

If you see a double-digit edge, assume one of these before assuming it is real.
Check that each venue's own two prices sum to roughly 1.01–1.02; if one sums to
something odd, that book is the problem.
