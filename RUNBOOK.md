# Runbook — testing the cross-venue algorithm

Everything here is **read-only and simulated**. No order is placed on any
venue, no funds move. The engine has no write path to either exchange.

## Start here — the whole thing in six steps

### 1. Get Kalshi API credentials

Go to **kalshi.com → Account → Profile → API Keys → Create New Key**.

You get two things:
- a **Key ID** (a UUID, shown on screen — copy it)
- an **RSA private key file** (downloads once, cannot be re-downloaded)

You need a Kalshi account for this, but **no money in it**. Nothing here places
an order. Kalshi just requires a signed key for every request, including
read-only ones.

Polymarket needs nothing at all — its market data is fully public.

> **This must run from inside the US.** Kalshi's API is unreliable or blocked
> elsewhere. Testing from India, only about 1 request in 3 completed.

### 2. Clone and install

```bash
git clone git@github.com:sushant2082/kalshi-arb-engine.git
cd kalshi-arb-engine

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### 3. Check it built correctly

```bash
.venv/bin/python -m pytest -q
```

Expect **294 passed**. If anything fails, stop — do not run against live data
with a broken build.

### 4. Add your credentials

Put the downloaded key file in the repo directory (it is gitignored, so it
will not be committed):

```bash
mv ~/Downloads/kalshi-key.pem ./kalshi_key.pem
cp .env.example .env
```

Then edit `.env` and set exactly two lines:

```
KALSHI_API_KEY_ID=paste-your-key-id-here
KALSHI_PRIVATE_KEY_PATH=kalshi_key.pem
```

### 5. Confirm you are connected

```bash
.venv/bin/arbengine discover
```

A table of series and market counts means it works.

- **401 Unauthorized** → the Key ID or the key file path is wrong
- **connection errors** → network or region problem, see the note in step 1

### 6. Run the test

```bash
./scripts/fullday.sh
```

Start it before the games — early afternoon ET is fine. It waits quietly until
games begin, picks them up as they start, and runs for 10 hours by default.

Leave it alone. `Ctrl-C` stops it cleanly and still prints the report.

```bash
./scripts/fullday.sh 6                 # 6 hours instead of 10
.venv/bin/python scripts/report.py     # re-read results any time
```

**Re-run `report.py` the next day.** Positions settle only after their Kalshi
market resolves, so P&L is incomplete until the games have finished.

### What to send back

Either the printed report, or the three database files:

```
crossevents.db   how long each cross lasted
crossreads.db    every price observation
crosspaper.db    simulated positions and P&L
```

---

## What you need

| | |
|---|---|
| A Kalshi account | free; **US-only** — the API is unreliable elsewhere |
| Kalshi API key + RSA private key | from Kalshi's API settings |
| Python 3.11+ | |
| Money in the account | **no** — nothing places an order |
| Polymarket account | **no** — its read APIs need no auth |

## The main thing to run

```bash
./scripts/fullday.sh
```

One command. Runs the event-driven recorder across a whole slate, then prints
a plain-language report. Start it any time — it picks up games as they begin
and waits quietly when none are live. Ctrl-C stops it cleanly and still
reports.

```bash
./scripts/fullday.sh 8            # 8 hours instead of the default 10
.venv/bin/python scripts/report.py  # re-read the results any time
```

Re-run `report.py` a day later to pick up positions that have since settled.

### The alternative: polling with paper trading

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

**Frequency** — from an 11,614-read polling study (`data/ANALYSIS.md`):
crosses are **~40× more common in live play than pregame** (0.07% → 4.83%),
rising steadily as a game progresses.

**Lifetime** — from a 30-minute event-driven run over 5 live games, the first
measurement at 50ms resolution:

| | |
|---|---|
| crosses detected | 37 |
| **actually tradeable** | **19** (18 had no depth behind the quote) |
| median lifetime | **1.9s** |
| longest | 262s |
| over 30s | 3 of 19 |

Largest tradeable ones ranged $290–$1,539. The single biggest lasted **1.9
seconds**.

This sample is small — five games, half an hour. Confirming or overturning it
is exactly what a full-day run is for.

## What we don't know

- **Whether the quoted depth would actually fill.** No orders have been placed.
  Displayed size is not executed size.
- **Whether a two-leg order can land inside the window.** Median tradeable
  lifetime is under two seconds; placing sequential orders on two venues is
  unlikely to fit inside that, and nothing here has tested it.
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
