import csv
import logging
from pathlib import Path

from arbengine.models import ArbOpportunity, PaperPosition

log = logging.getLogger(__name__)

_CSV_FIELDS = [
    "first_seen",
    "last_seen",
    "persistence_sec",
    "group_id",
    "type",
    "guaranteed_profit",
    "profit_per_set",
    "total_cost",
    "total_fee",
    "fillable_sets",
    "min_leg_size",
    "leg_count",
    "elevated_risk",
    "legs",
]


def persistence_sec(opp: ArbOpportunity) -> float:
    return (opp.last_seen - opp.first_seen).total_seconds()


def format_opportunity(opp: ArbOpportunity, max_leg_count: int) -> str:
    ts = opp.last_seen.strftime("%H:%M:%S")
    risk = "  ⚠ELEVATED-LEG-RISK" if opp.leg_count > max_leg_count else ""
    legs = " ".join(
        f"{leg.side[0].upper()}{leg.qty}@{leg.price:.2f}:{leg.ticker}"
        for leg in opp.legs
    )
    return (
        f"[{ts}] {opp.type.upper():<14} {opp.group_id}"
        f"  profit=${opp.guaranteed_profit:.2f}"
        f"  per_set=${opp.profit_per_set:.4f}"
        f"  sets={opp.fillable_sets}"
        f"  legs={opp.leg_count}"
        f"  alive={persistence_sec(opp):.0f}s"
        f"{risk}\n    {legs}"
    )


def print_opportunity(opp: ArbOpportunity, max_leg_count: int) -> None:
    print(format_opportunity(opp, max_leg_count), flush=True)


def append_to_csv(opp: ArbOpportunity, path: Path, max_leg_count: int) -> None:
    is_new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "first_seen": opp.first_seen.isoformat(),
            "last_seen": opp.last_seen.isoformat(),
            "persistence_sec": f"{persistence_sec(opp):.1f}",
            "group_id": opp.group_id,
            "type": opp.type,
            "guaranteed_profit": f"{opp.guaranteed_profit:.4f}",
            "profit_per_set": f"{opp.profit_per_set:.4f}",
            "total_cost": f"{opp.total_cost:.4f}",
            "total_fee": f"{opp.total_fee:.4f}",
            "fillable_sets": opp.fillable_sets,
            "min_leg_size": opp.min_leg_size,
            "leg_count": opp.leg_count,
            "elevated_risk": opp.leg_count > max_leg_count,
            "legs": ";".join(
                f"{leg.ticker}|{leg.side}|{leg.qty}|{leg.price:.4f}|{leg.fee:.4f}"
                for leg in opp.legs
            ),
        })


def format_position(pos: PaperPosition) -> str:
    marker = {"complete": "✓", "partial": "~", "broken": "✗"}[pos.fill_status]
    return (
        f"    PAPER {marker} {pos.fill_status:<8} sets={pos.sets_filled}/{pos.sets_attempted}"
        f"  cash=${pos.net_cash:+.2f}"
        f"  expected=${pos.expected_profit:.2f}"
    )


def print_position(pos: PaperPosition) -> None:
    print(format_position(pos), flush=True)


def print_summary(summary: dict) -> None:
    print("\n── Paper trading summary " + "─" * 40, flush=True)
    print(
        f"  bankroll   ${summary['starting_bankroll']:.2f} → "
        f"${summary['ending_bankroll']:.2f}  "
        f"(P&L ${summary['total_pnl']:+.2f})",
        flush=True,
    )
    print(
        f"  locked     {summary['locked_count']:>4} positions  "
        f"P&L ${summary['locked_pnl']:+.2f}",
        flush=True,
    )
    print(
        f"  broken     {summary['broken_count']:>4} positions  "
        f"P&L ${summary['broken_pnl']:+.2f}   "
        f"← unhedged residuals, the number that decides if this is real",
        flush=True,
    )
    if summary["realization_ratio"] is not None:
        print(
            f"  realized/expected on locked: {summary['realization_ratio']:.3f}"
            f"   (well under 1.0 means the fee model or state space is wrong)",
            flush=True,
        )
    print("─" * 64 + "\n", flush=True)
