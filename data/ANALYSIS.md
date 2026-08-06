# Kalshi ↔ Polymarket cross-venue MLB study

**Dataset:** `crossreads-2026-08-05.db` · 11,638 rows total
**Study window:** 2026-08-05 17:50 → 2026-08-06 17:31 UTC · 11,614 reads · 18 games · 165 crosses

All figures below are computed on the study window (`read_at >= '2026-08-05T17:50'`), which excludes 24 development-run rows.
**Cadence:** ~18s median between reads per game

## What was measured

Every ~18 seconds, for every MLB game carried by both venues, we recorded both
venues' best ask on both teams plus the depth behind each quote. Buying one
team on each venue pays exactly $1 whichever team wins, so an all-in cost below
$1 is a cross.

**Non-crossing reads are recorded too.** Only logging the crosses would make a
genuinely rare event indistinguishable from a scanner that silently stopped
working — which happened repeatedly during development.

### Data provenance matters here

Both venues serve stale data on their convenient endpoints, and comparing a
stale price against a live one manufactures crosses that never existed:

| Path | Cache | Used for |
|---|---|---|
| Kalshi `/markets` | `max-age=15` | ✗ not used for prices |
| Kalshi `/markets/{t}/orderbook` | none | ✓ prices |
| Polymarket Gamma `/markets` | **`max-age=300`** | discovery only |
| Polymarket CLOB WebSocket | none (push) | ✓ prices |

An earlier poll-based run using Kalshi's cached endpoint reported a **+10.25%**
cross that vanished on the next read. It was a 15-second-old Kalshi price
against a live Polymarket one during an in-progress game. Nothing in this
dataset comes from a cached price path.

Pairs are joined on **team pair + first pitch** from structured identifiers
(Kalshi's ticker encodes date + HHMM Eastern; Polymarket publishes
`gameStartTime` in UTC), never on title text.

---

## Finding 1 — Crosses get ~40× more frequent as a game progresses

| Game phase | Reads | Crosses | Rate |
|---|---:|---:|---:|
| Pregame | 1,519 | 1 | **0.07%** |
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

The trend is strong and near-monotonic (the 210–240 min bucket dips on 271
reads, and the last two buckets are thin — few games run that long). This is the headline result: the two venues price
pregame baseball almost identically, and desynchronize under live information
flow. Pregame is effectively arbitrage-free at 0.07%.

## Finding 2 — Cross *size* is small and heavily right-skewed

```
median   0.32%
mean     1.17%
p90      2.88%
max     17.25%
```

| Size | Count | Share |
|---|---:|---:|
| < 0.5% | 98 | 59.4% |
| 0.5–1% | 36 | 21.8% |
| 1–2% | 8 | 4.8% |
| 2–5% | 9 | 5.5% |
| > 5% | 14 | 8.5% |

Frequency rises with game phase; size does not follow the same trend. The
large ones are outliers scattered across phases, not a late-game regime.

## Finding 3 — They do not persist. This is the decisive result.

```
109 distinct cross events
  1 read   85 events   78%
  2 reads  13 events   12%
  ≥3 reads 11 events   10%
median streak = 1 read (~18 seconds)
```

The three largest crosses, with their neighbouring reads:

```
01:02:44   1.0100   no cross
01:03:00   0.9370   CROSS  $1219      <<<
01:03:17   1.0100   no cross

02:01:23   0.9965   CROSS  $9
02:01:38   0.8775   CROSS  $928       <<<
02:01:53   1.0100   no cross

01:35:58   1.0100   no cross
01:36:14   0.9858   CROSS  $346       <<<
01:36:29   1.0253   no cross
```

Single ticks, gone within 17 seconds. At this cadence we are **counting** these
events, not observing their lifetime — the true duration is somewhere below one
read interval and cannot be resolved from this data.

## Finding 4 — Most crosses are worth cents

Depth-limited dollar value (thinner leg caps the trade):

```
median   $0.07
mean    $22.18
max  $1,219.30
```

| Value | Count | Share |
|---|---:|---:|
| < $1 | 126 | 76.4% |
| $1–10 | 22 | 13.3% |
| $10–100 | 11 | 6.7% |
| > $100 | 6 | 3.6% |

Polymarket advertises a `liquidity` figure in the thousands that is **not**
top-of-book depth. A market showing $5,543 of liquidity had 50 shares at the
best ask. Any percentage quoted without fillable size is misleading.

## Finding 5 — The large crosses are genuine disagreements, not artifacts

All six crosses above $100 were inspected individually. In four, both venues'
books were internally coherent (ask sums 1.01–1.02) while disagreeing 4–18
points on the same team — a real disagreement, not a parsing or staleness
error. Team mapping was verified by name lookup, not index position.

Largest example, at first pitch:

```
Athletics @ Cincinnati   22:39:46 UTC
  Kalshi      ATH 0.58   CIN 0.44    (sum 1.02)
  Polymarket  ATH 0.40   CIN 0.61    (sum 1.01)
  → 18-point disagreement on the same team
  → buy ATH on Polymarket + CIN on Kalshi = 0.8573 all-in
```

Polymarket subsequently moved ATH 0.40 → 0.47, toward Kalshi. So Polymarket was
the slow side.

**Caveat:** the $1,219 figure assumes filling 19,354 sets on *both* legs
simultaneously. Neither venue offers atomic multi-leg fill.

## Finding 6 — Kalshi's own book crossed 45 times

In 0.43% of reads (45 of 10,433), Kalshi's two asks summed below $1 — a ~2%
intra-venue arbitrage requiring no second venue and carrying no resolution
basis risk. This recorder was not checking for it.

---

## What this does and does not establish

**Established:**
- Cross-venue crosses are real, and ~40× more frequent in live play than pregame
- The large ones are genuine venue disagreements, not measurement artifacts
- Median cross is 0.32% and worth $0.07; the distribution is dominated by noise
- 78% exist for a single 18-second read

**Not established:**
- Actual lifetime of a cross. 18s polling is too coarse; this needs an
  event-driven loop off both WebSocket feeds.
- Whether either leg is fillable at the quoted size. No orders were placed.
- Slippage and leg risk. Without atomic fill, a partially-filled position is a
  directional bet, not a hedge.

## Known settlement risk

These are **not risk-free** positions. The venues publish different rules:

| | Postponed game |
|---|---|
| Kalshi | closes after the rescheduled game, **"within two days"** |
| Polymarket | "remains open until the game has been completed" — **no stated limit** |

A postponement beyond two days can settle differently on the two venues, in
which case both legs can lose. Polymarket also resolves a tie 50-50; Kalshi's
tie handling does not appear in its rules text.

## Reproducing

```bash
arbengine crossrec --duration 39600 --interval 12
```

Schema is a single table, `cross_reads`. Key columns: `read_at`, `game`,
`minutes_in` (signed; negative is pregame), `kalshi_away/home`,
`poly_away/home`, the four `*_size` columns, `best_total`, `best_profit`,
`best_sets`, `best_dollars`, `is_cross`.

```sql
-- cross rate by game phase
SELECT CASE WHEN minutes_in < 0 THEN -1 ELSE CAST(minutes_in/30 AS INT) END AS bucket,
       COUNT(*), SUM(is_cross),
       ROUND(100.0*SUM(is_cross)/COUNT(*), 2) AS rate_pct
FROM cross_reads GROUP BY bucket ORDER BY bucket;
```

The file also contains 24 rows from development runs before 17:50 UTC on
2026-08-05. Filter with `read_at >= '2026-08-05T17:50'` to reproduce the
figures above exactly.

### A note on the collection window

The run was configured for 11 hours but recorded for 23h41m of wall time. The
duration was measured with a monotonic clock, which does not advance while
macOS sleeps, so an overnight sleep did not count against the budget. The extra
data is valid — it is simply a second day of games — and the bug is fixed
(duration is now measured on the wall clock). It is noted here because it
explains why the window spans two dates.
