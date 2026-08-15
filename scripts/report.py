#!/usr/bin/env python3
"""
Read the databases and print a plain-language report.

Run any time — during a session or long after. Reads whatever exists and says
so when something is missing rather than failing.

    .venv/bin/python scripts/report.py
"""

import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _rows(db: str, sql: str, params=()) -> list:
    path = ROOT / db
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _bar(frac: float, width: int = 24) -> str:
    filled = int(round(frac * width))
    return "█" * filled + "·" * (width - filled)


def cross_lifetimes() -> None:
    rows = _rows(
        "crossevents.db",
        "SELECT duration_sec, peak_profit, peak_dollars, peak_sets, game "
        "FROM cross_events",
    )
    print("── HOW LONG DOES AN OPPORTUNITY LAST? " + "─" * 26)
    if not rows:
        print("  No cross events recorded.")
        print("  Either no game was live, or the venues never crossed.")
        print("  Check the log for 'Tracking N games' — if N was 0, no games")
        print("  were in the window when it started.\n")
        return

    fillable = [r for r in rows if (r[3] or 0) >= 1]
    phantom = len(rows) - len(fillable)

    print(f"  {len(rows)} crosses detected, of which {len(fillable)} were")
    print(f"  actually tradeable ({phantom} had no depth behind the quote).")
    print()

    if not fillable:
        print("  None had depth. A quoted price with nothing offered at it is")
        print("  not a trade, so there is nothing to measure here.\n")
        return

    d = sorted(r[0] for r in fillable)
    print(f"  lifetime: median {statistics.median(d):.1f}s, "
          f"longest {max(d):.0f}s")
    print()
    buckets = [
        (0, 1, "under 1 second"), (1, 5, "1-5 seconds"),
        (5, 30, "5-30 seconds"), (30, 1e9, "over 30 seconds"),
    ]
    for lo, hi, label in buckets:
        k = sum(1 for v in d if lo <= v < hi)
        print(f"    {label:<16} {_bar(k / len(d))} {k:>3}  "
              f"{100 * k / len(d):>4.0f}%")
    print()
    print("  Anything under a few seconds cannot be captured by hand, and")
    print("  probably not by a two-leg bot placing orders sequentially.")
    print()

    top = sorted(fillable, key=lambda r: -(r[2] or 0))[:5]
    print("  Largest tradeable opportunities:")
    print(f"    {'value':>10}{'lasted':>10}{'edge':>8}  game")
    for dur, prof, dollars, sets, game in top:
        print(f"    ${dollars or 0:>9,.2f}{dur:>9.1f}s{(prof or 0) * 100:>7.2f}%"
              f"  {game[:30]}")
    print()


def cross_frequency() -> None:
    rows = _rows(
        "crossreads.db",
        """SELECT CASE WHEN minutes_in < 0 THEN -1
                       ELSE CAST(minutes_in/30 AS INT) END AS b,
                  COUNT(*), SUM(is_cross)
           FROM cross_reads WHERE minutes_in IS NOT NULL
           GROUP BY b ORDER BY b""",
    )
    if not rows:
        return
    print("── WHEN DO THEY HAPPEN? " + "─" * 40)
    print("  Share of observations where the venues crossed:")
    print()
    for bucket, reads, crosses in rows:
        label = "before game" if bucket == -1 else f"{bucket*30}-{bucket*30+30} min in"
        rate = (crosses or 0) / reads
        print(f"    {label:<16} {_bar(min(rate * 20, 1.0))} "
              f"{100 * rate:>5.2f}%  ({crosses or 0}/{reads})")
    print()
    print("  Crosses cluster in live play — before first pitch the two venues")
    print("  agree almost perfectly, then desynchronise as the game moves.")
    print()


def paper_pnl() -> None:
    rows = _rows(
        "crosspaper.db",
        """SELECT fill_status, status, COUNT(*), SUM(pnl), SUM(expected_profit)
           FROM paper_positions GROUP BY fill_status, status""",
    )
    print("── SIMULATED PROFIT AND LOSS " + "─" * 35)
    if not rows:
        print("  No positions taken. Paper trading runs during `crossrec`;")
        print("  the event recorder only measures, it does not trade.\n")
        return

    hedged = sum(r[3] or 0 for r in rows if r[0] == "hedged" and r[1] == "settled")
    broken = sum(r[3] or 0 for r in rows if r[0] == "broken" and r[1] == "settled")
    expected = sum(r[4] or 0 for r in rows if r[0] == "hedged")
    settled = sum(r[2] for r in rows if r[1] == "settled")
    open_n = sum(r[2] for r in rows if r[1] == "open")

    print(f"  positions: {settled} settled, {open_n} still open")
    print()
    print(f"  from hedged trades  ${hedged:>10,.2f}   (both legs filled)")
    print(f"    the crosses implied ${expected:>8,.2f}")
    print(f"  from broken trades  ${broken:>10,.2f}   (only one leg filled)")
    print()
    print("  Hedged is the real number. Broken means one leg failed, leaving")
    print("  an outright bet on one team — profit or loss there is luck, not")
    print("  edge, and should not be read as the strategy working.")
    if open_n:
        print()
        print(f"  {open_n} positions settle when their Kalshi market resolves.")
        print("  Re-run this report later to pick them up.")
    print()


def health() -> None:
    hb = _rows(
        "crossevents.db",
        "SELECT COUNT(*), MAX(evaluations), MAX(kalshi_updates), "
        "MAX(poly_updates) FROM heartbeats",
    )
    if not hb or not hb[0][0]:
        return
    n, evals, k, p = hb[0]
    print("── FEED HEALTH " + "─" * 49)
    print(f"  {n} heartbeats, {evals or 0:,} evaluations")
    print(f"  {k or 0:,} Kalshi updates, {p or 0:,} Polymarket updates")
    if not k or not p:
        print()
        print("  WARNING: one feed delivered nothing. A websocket that")
        print("  connects but stays silent looks identical to a quiet market,")
        print("  so treat a zero here as a broken run, not a calm one.")
    print()


def main() -> int:
    print()
    print("=" * 64)
    print(" CROSS-VENUE TEST REPORT")
    print("=" * 64)
    print()
    found = any((ROOT / f).exists()
                for f in ("crossevents.db", "crossreads.db", "crosspaper.db"))
    if not found:
        print("  No databases found. Run ./scripts/fullday.sh first.\n")
        return 1
    cross_lifetimes()
    cross_frequency()
    paper_pnl()
    health()
    print("=" * 64)
    print(" These are simulated results on real market data.")
    print(" No orders were placed. See RUNBOOK.md for what is NOT established.")
    print("=" * 64)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
