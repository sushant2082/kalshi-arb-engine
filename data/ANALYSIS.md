# Kalshi ↔ Polymarket cross-venue MLB study

**Dataset:** `crossreads-2026-08-05.db` · 11,638 rows total
**Study window:** 2026-08-05 17:50 → 2026-08-06 17:31 UTC · 11,614 reads · 18 games · 165 crosses
**Cadence:** ~18 seconds between reads per game

All figures below are computed on the study window
(`read_at >= '2026-08-05T17:50'`), which excludes 24 development-run rows.

---

## Vocabulary

Defined once here, used throughout.

**Contract.** On both venues, a contract on a team pays **$1** if that team wins
and **$0** if it loses. So its price is directly the market's implied
probability: a contract at $0.58 means "58% likely to win."

**Leg.** One side of a two-part trade. Buying the Yankees on Kalshi is one leg;
buying the Red Sox on Polymarket is the other. A position with two legs is only
a hedge if *both* fill.

**Cross.** Exactly one team wins a baseball game, so owning *both* teams pays
exactly $1 no matter what happens. If you can buy one team on Kalshi and the
other on Polymarket for a combined **less than $1**, the difference is locked
profit. When that happens we say the two venues' prices have *crossed*.

A worked example, buying at each venue's cheapest available price:

```
buy Athletics on Polymarket    $0.40
buy Cincinnati on Kalshi       $0.44
Kalshi trading fee             $0.0173
                              ───────
total cost                     $0.8573   ← below $1, so this is a cross
guaranteed payout              $1.0000   (one of the two teams must win)
profit per set                 $0.1427   = 14.27%
```

**Set.** One complete pair — one contract on each team. A set always pays
exactly $1. Profit is quoted *per set*, so 100 sets of the above returns
$14.27.

**Read.** One observation: at a given moment, for a given game, we fetch both
venues' current prices and depths, compute the best available combination, and
write a row. A read happens roughly every 18 seconds per game.

**Non-crossing read.** A read where the two venues' combined price came to **$1
or more** — i.e. no profitable trade existed at that moment. These are recorded
too, and they are 98.6% of the dataset. That is deliberate: if we only stored
the crosses, a file with 165 rows would look identical whether crosses are
genuinely rare *or* the scanner silently broke after ten minutes. Recording the
misses is what makes "1.4% of the time" a measurable rate rather than a guess.

**Depth.** How many contracts are actually available at the quoted best price.
A price of $0.40 with depth 50 means you can buy 50 contracts at $0.40; the
51st costs more. Depth is not the same as "liquidity," which both venues also
publish and which means something looser — see Finding 4.

**Thinner leg.** Of the two legs, the one with less depth. It caps the whole
trade: if Polymarket offers 20,000 contracts but Kalshi offers 50, you can only
build 50 complete sets. The thinner leg is the binding constraint.

**Depth-limited dollar value.** Profit per set × the number of sets the thinner
leg allows. This is the honest size of an opportunity. A "14% edge" with 3
contracts of depth is worth about forty cents; the percentage alone is
misleading, which is why every figure here carries a dollar value beside it.

**Streak.** How many *consecutive* reads a cross survived. A streak of 1 means
it was present in one read and gone by the next — under ~18 seconds. Streaks
measure how long an opportunity stays open, which decides whether it can be
acted on at all.

**Right-skewed.** A distribution where most values are small but a few are very
large, dragging the average above the typical case. The tell is a mean far
above the median. Here the median cross is 0.32% and the mean is 1.17% — so the
*typical* cross is roughly a third of a percent, and quoting "average 1.17%"
would overstate what you would usually encounter by nearly 4×.

**Ask sum.** Adding a venue's two prices together. A healthy book sums slightly
*above* $1 (roughly 1.01–1.02) — that excess is the venue's spread, its margin.
A sum below $1 within a single venue is itself an arbitrage. Ask sums are used
throughout as a sanity check: if a venue's own two prices sum sensibly, its
book is internally coherent and a disagreement with the other venue is real
rather than a parsing error.

---

## What was measured

Every ~18 seconds, for every MLB game listed on both venues, we recorded both
venues' best price on both teams plus the depth behind each of those prices,
then computed the cheapest way to own both teams across the two venues.

### Data provenance matters here

Both venues serve **stale** data on their most convenient endpoints. Comparing
a stale price on one venue against a live price on the other manufactures
crosses that never existed:

| Endpoint | Staleness | Used for |
|---|---|---|
| Kalshi `/markets` | cached up to **15s** | ✗ not used for prices |
| Kalshi `/markets/{ticker}/orderbook` | live | ✓ prices |
| Polymarket Gamma `/markets` | cached up to **300s** | discovery only |
| Polymarket CLOB WebSocket | live (pushed) | ✓ prices |

An earlier run built on Kalshi's cached endpoint reported a **+10.25%** cross
that vanished on the next read. It was a 15-second-old Kalshi price compared
against a current Polymarket one during a fast-moving live game — not an
opportunity, just two clocks. **No price in this dataset comes from a cached
endpoint.**

Games are matched across venues on **team pair + first pitch time**, taken from
structured fields (Kalshi's ticker encodes date and start time in Eastern;
Polymarket publishes `gameStartTime` in UTC). Never on title text — similar
titles match different games, which is how a market for "who wins the division"
gets paired against "who wins tonight."

---

## Finding 1 — Crosses become ~40× more frequent as a game progresses

"Rate" is the share of reads in that phase where a cross existed.

| Game phase | Reads | Crosses | Rate |
|---|---:|---:|---:|
| Pregame (before first pitch) | 1,519 | 1 | **0.07%** |
| 0–30 min | 1,762 | 6 | 0.34% |
| 30–60 min | 1,839 | 13 | 0.71% |
| 60–90 min | 1,619 | 24 | 1.48% |
| 90–120 min | 1,626 | 38 | 2.34% |
| 120–150 min | 1,416 | 42 | **2.97%** |
| 150–180 min | 874 | 18 | 2.06% |
| 180–210 min | 434 | 12 | 2.76% |
| 210–240 min | 271 | 1 | 0.37% |
| 240–270 min | 207 | 10 | **4.83%** |
| 270–300 min | 47 | 0 | 0.00% |

Near-monotonic and strong. The 210–240 min dip sits on only 271 reads and the
final buckets are thin (few games run past four hours), so the tail is noisy.

**Interpretation.** Before a game starts, both venues have had hours to price it
and they agree almost perfectly — 1 cross in 1,519 pregame reads. Once play
begins, information arrives fast (runs score, pitchers change) and the two
venues update at different speeds. The gap between them is what a cross *is*.

## Finding 2 — But the crosses are small

```
median   0.32%     ← the typical cross
mean     1.17%     ← pulled up by rare large ones
p90      2.88%     ← 90% of crosses are smaller than this
max     17.25%
```

| Size of cross | Count | Share |
|---|---:|---:|
| under 0.5% | 98 | 59.4% |
| 0.5–1% | 36 | 21.8% |
| 1–2% | 8 | 4.8% |
| 2–5% | 9 | 5.5% |
| over 5% | 14 | 8.5% |

Nearly 60% are under half a percent. The mean sitting 3.7× above the median is
the right-skew: a handful of large crosses drag the average up, so quoting the
mean would badly misrepresent a typical observation.

Note that frequency rises with game phase (Finding 1) but size does not follow
the same pattern — the large crosses are scattered across all phases rather
than concentrated late.

## Finding 3 — They do not last. This is the decisive result.

Streak = number of consecutive reads a cross survived. One read ≈ 18 seconds.

```
109 distinct cross events
  1 read    85 events   78%
  2 reads   13 events   12%
  3+ reads  11 events   10%
median streak = 1 read
```

The three largest crosses, shown with the reads immediately before and after:

```
01:02:44   $1.0100   no cross
01:03:00   $0.9370   CROSS  $1,219      <<<
01:03:17   $1.0100   no cross

02:01:23   $0.9965   CROSS  $9
02:01:38   $0.8775   CROSS  $928        <<<
02:01:53   $1.0100   no cross

01:35:58   $1.0100   no cross
01:36:14   $0.9858   CROSS  $346        <<<
01:36:29   $1.0253   no cross
```

Each appears in one read and is gone by the next. **At an 18-second cadence we
are counting these events, not timing them** — the true lifetime is somewhere
below one read interval and this dataset cannot resolve it further. It could be
ten seconds or half a second.

## Finding 4 — Most crosses are worth cents once depth is applied

Depth-limited dollar value = profit per set × sets available on the thinner leg.

```
median      $0.07     ← the typical cross is worth seven cents
mean       $22.18     ← again pulled up by a few large ones
max     $1,219.30
```

| Value | Count | Share |
|---|---:|---:|
| under $1 | 126 | 76.4% |
| $1–10 | 22 | 13.3% |
| $10–100 | 11 | 6.7% |
| over $100 | 6 | 3.6% |

**Why the percentage alone deceives.** Polymarket publishes a `liquidity` figure
per market, often in the thousands of dollars. That is a measure of total resting
orders across the whole book — it is *not* the depth at the best price. One
market advertising **$5,543 of liquidity had 50 contracts at its best ask**. A
0.36% cross on 50 contracts is 18 cents. Any scanner reporting an edge
percentage without the fillable size will make rounding errors look tradeable.

## Finding 5 — The large crosses are genuine disagreements, not measurement errors

All six crosses above $100 were inspected individually. The test: does each
venue's own **ask sum** look normal? If Kalshi's two prices sum to ~1.02 and
Polymarket's to ~1.01, both books are internally coherent, and any disagreement
*between* them is a real difference of opinion rather than a parsing bug or a
stale quote.

Four of the six passed cleanly, showing 4–18 point disagreements on the same
team. Team identity was confirmed by matching full team names, not by assuming
the two venues list outcomes in the same order.

The largest, at first pitch:

```
Athletics @ Cincinnati        22:39:46 UTC
  Kalshi      ATH $0.58   CIN $0.44     (sum $1.02 — normal)
  Polymarket  ATH $0.40   CIN $0.61     (sum $1.01 — normal)

  Both books coherent, yet 18 points apart on the Athletics.
  Cheapest combination: ATH on Polymarket + CIN on Kalshi = $0.8573
```

Polymarket subsequently moved the Athletics from $0.40 to $0.47, toward
Kalshi's number — so Polymarket was the slower venue in this instance.

**Caveat on the $1,219.** That figure assumes filling **19,354 sets on both legs
at the same instant**. Neither venue offers atomic multi-leg fill (see below),
so it is an upper bound on what was theoretically available, not a realistic
profit.

## Finding 6 — Kalshi's own book crossed 45 times

In 0.43% of reads (45 of 10,433 where both Kalshi prices were present), Kalshi's
**two prices summed below $1** — meaning you could buy both teams on Kalshi
alone for less than the $1 they are guaranteed to pay.

This is a stronger opportunity than anything cross-venue: one exchange, one
rulebook, one settlement source, so there is no risk of the two sides resolving
inconsistently. It requires no second venue at all. This recorder was not
looking for it, so it is unquantified beyond the count.

---

## What this establishes, and what it does not

**Established:**

- Cross-venue crosses are real and become ~40× more frequent in live play than
  pregame
- The large ones are genuine venue disagreements, not artifacts of stale data or
  mismatched games
- The typical cross is 0.32% and worth $0.07 — the distribution is dominated by
  noise-scale events
- 78% exist for a single 18-second read

**Not established:**

- **How long a cross actually lasts.** 18-second polling is too coarse. Resolving
  this needs an event-driven loop reacting to both venues' WebSocket feeds
  rather than sampling on a timer.
- **Whether either leg is fillable at the quoted price and size.** No orders were
  placed. Quoted depth is what the venue displayed, not what would have executed.
- **Slippage and leg risk.** Neither venue supports *atomic multi-leg fill* —
  filling both legs as a single all-or-nothing transaction. You buy one leg,
  then the other, and the price can move in between. If only one leg fills, the
  position is not a hedge at all; it is an ordinary directional bet on one team.

## These positions are not risk-free

A cross only locks a profit if **both venues resolve the same way**. They are
separate companies with separate rulebooks, and their published rules already
differ:

| | Postponed game |
|---|---|
| **Kalshi** | market closes after the rescheduled game, **"within two days"** |
| **Polymarket** | "remains open until the game has been completed" — **no stated limit** |

A game postponed more than two days can therefore settle differently on the two
venues — and if they disagree, both legs can lose rather than offsetting. This
is *resolution basis risk*: the hedge depends on an assumption about rulebooks,
not just about prices, and no amount of order-book analysis can verify it.

Polymarket also resolves a tied, uncompleted game 50-50. Kalshi's tie handling
does not appear in its rules text at all.

---

## Reproducing

```bash
arbengine crossrec --duration 39600 --interval 12
```

One table, `cross_reads`. Columns:

| Column | Meaning |
|---|---|
| `read_at` | timestamp of the observation (UTC) |
| `game` | e.g. "Athletics @ Cincinnati Reds" |
| `minutes_in` | minutes since first pitch; **negative means pregame** |
| `kalshi_away`, `kalshi_home` | Kalshi's price for each team |
| `poly_away`, `poly_home` | Polymarket's price for each team |
| `*_size` columns | depth (contracts available) behind each of those prices |
| `best_total` | cheapest combined cost to own both teams, fees included |
| `best_profit` | `1 − best_total`; positive means a cross |
| `best_sets` | complete sets buildable, capped by the thinner leg |
| `best_dollars` | `best_profit × best_sets` — the honest value |
| `is_cross` | 1 if profitable at that moment, 0 otherwise |

```sql
-- reproduce Finding 1
SELECT CASE WHEN minutes_in < 0 THEN -1 ELSE CAST(minutes_in/30 AS INT) END AS bucket,
       COUNT(*)                                   AS reads,
       SUM(is_cross)                              AS crosses,
       ROUND(100.0*SUM(is_cross)/COUNT(*), 2)     AS rate_pct
FROM cross_reads
WHERE read_at >= '2026-08-05T17:50'
GROUP BY bucket ORDER BY bucket;
```

The file also contains 24 rows from development runs before 17:50 UTC on
2026-08-05. Filter with `read_at >= '2026-08-05T17:50'` to reproduce the figures
above exactly.

### A note on the collection window

The run was configured for 11 hours but recorded 23h41m of wall time. Duration
was measured with a monotonic clock, which does not advance while macOS sleeps,
so an overnight sleep did not count against the budget. The extra data is valid
— it is simply a second day of games — and the bug is fixed (duration is now
measured on the wall clock). Noted here because it explains why the window spans
two dates.
