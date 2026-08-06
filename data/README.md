# Datasets

## `crossreads-2026-08-05.db`

Kalshi ↔ Polymarket cross-venue MLB price observations. See
[ANALYSIS.md](ANALYSIS.md) for the study and findings.

SQLite, one table `cross_reads`, 11,638 rows. Every read is recorded, including
the ~98.5% where nothing crossed — a dataset of only the crosses cannot
distinguish a rare event from a scanner that stopped working.

Contains public market observations only: quoted prices and depths from two
public exchanges. No credentials, account identifiers, or order data.

```sql
-- reproduce the headline result
SELECT CASE WHEN minutes_in < 0 THEN -1 ELSE CAST(minutes_in/30 AS INT) END AS bucket,
       COUNT(*) AS reads,
       SUM(is_cross) AS crosses,
       ROUND(100.0*SUM(is_cross)/COUNT(*), 2) AS rate_pct
FROM cross_reads
WHERE read_at >= '2026-08-05T17:50'
GROUP BY bucket ORDER BY bucket;
```

`minutes_in` is signed — negative is pregame. `best_dollars` is the
depth-limited value of the trade, capped by the thinner leg; `best_profit`
alone is misleading without it.
