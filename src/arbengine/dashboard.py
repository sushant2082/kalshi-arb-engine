"""
Live terminal dashboard for the scan and paper-trading loop.

Design intent: make the *absence* of opportunities as legible as their
presence. A dashboard that only lights up on a fill would spend most of its
life looking identical to a crashed process, and the near-miss margins are the
actual signal most of the time — they say whether the engine is close to firing
or nowhere near it.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from arbengine.models import ArbOpportunity, PaperPosition


def _fmt_money(v: float, width: int = 0) -> Text:
    colour = "green" if v > 0 else "red" if v < 0 else "dim"
    return Text(f"${v:+,.2f}".rjust(width), style=colour)


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class DashboardState:
    """
    Everything the dashboard renders. Kept separate from the rendering so the
    scan loop never has to know about the terminal, and so it can be tested.
    """

    def __init__(self, starting_bankroll: float, mode: str = "stream") -> None:
        self.mode = mode
        self.starting_bankroll = starting_bankroll
        self.bankroll = starting_bankroll

        self.started_at = datetime.now(timezone.utc)
        self.groups = 0
        self.markets = 0
        self.updates = 0
        self.scans = 0
        self.rate_limited = 0

        self.opportunities: deque[ArbOpportunity] = deque(maxlen=200)
        self.positions: list[PaperPosition] = []
        self.near_misses: list[dict] = []
        self.events: deque[tuple[datetime, str, str]] = deque(maxlen=12)

    # ── Mutations ─────────────────────────────────────────────────────────────

    def log(self, message: str, style: str = "") -> None:
        self.events.append((datetime.now(timezone.utc), message, style))

    def record_opportunity(self, opp: ArbOpportunity) -> None:
        self.opportunities.append(opp)
        self.log(
            f"{opp.type} on {opp.group_id} — ${opp.guaranteed_profit:.2f} "
            f"across {opp.leg_count} legs",
            "bold green",
        )

    def record_position(self, pos: PaperPosition) -> None:
        self.positions.append(pos)
        marker = {"complete": "filled", "partial": "partial fill", "broken": "BROKEN"}
        style = "red" if pos.fill_status == "broken" else "green"
        self.log(
            f"paper {marker[pos.fill_status]}: {pos.sets_filled} sets on "
            f"{pos.group_id}",
            style,
        )
        self.bankroll += pos.net_cash

    def settle_position(self, pos: PaperPosition) -> None:
        for i, existing in enumerate(self.positions):
            if existing.id == pos.id:
                self.positions[i] = pos
                break
        if pos.realized_payout:
            self.bankroll += pos.realized_payout
        self.log(
            f"settled {pos.group_id}: {'+' if (pos.pnl or 0) >= 0 else ''}"
            f"${pos.pnl:.2f}",
            "green" if (pos.pnl or 0) >= 0 else "red",
        )

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def uptime_sec(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    @property
    def settled(self) -> list[PaperPosition]:
        return [p for p in self.positions if p.status == "settled"]

    @property
    def realized_pnl(self) -> float:
        return sum(p.pnl or 0.0 for p in self.settled)

    @property
    def locked_positions(self) -> list[PaperPosition]:
        return [p for p in self.positions if p.fill_status in ("complete", "partial")]

    @property
    def broken_positions(self) -> list[PaperPosition]:
        return [p for p in self.positions if p.fill_status == "broken"]


# ── Rendering ─────────────────────────────────────────────────────────────────

def _header(state: DashboardState) -> Panel:
    left = Text()
    left.append("kalshi-arb-engine  ", style="bold cyan")
    left.append(f"{state.mode}", style="bold")
    left.append("   PAPER TRADING — no real orders", style="dim yellow")

    right = Text()
    right.append(f"up {_fmt_age(state.uptime_sec)}   ", style="dim")
    right.append(f"{state.groups} groups / {state.markets} markets", style="dim")

    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="right")
    grid.add_row(left, right)
    return Panel(grid, style="cyan", padding=(0, 1))


def _pnl_panel(state: DashboardState) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column(justify="left")

    equity = state.starting_bankroll + state.realized_pnl
    t.add_row("bankroll", Text(f"${equity:,.2f}", style="bold"))
    t.add_row("realized", _fmt_money(state.realized_pnl))

    locked = sum(p.pnl or 0.0 for p in state.settled if p.fill_status != "broken")
    broken = sum(p.pnl or 0.0 for p in state.settled if p.fill_status == "broken")
    t.add_row("  from locks", _fmt_money(locked))
    # Broken legs are the number that decides whether this is real, so it is
    # never folded into the headline.
    t.add_row("  from broken", _fmt_money(broken))

    t.add_row("", "")
    t.add_row("positions", Text(str(len(state.positions))))
    t.add_row("  settled", Text(str(len(state.settled)), style="dim"))
    t.add_row(
        "  broken",
        Text(
            str(len(state.broken_positions)),
            style="red" if state.broken_positions else "dim",
        ),
    )
    return Panel(t, title="paper P&L", border_style="green", padding=(0, 1))


def _activity_panel(state: DashboardState) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column(justify="left")

    rate = state.updates / state.uptime_sec if state.uptime_sec > 0 else 0.0
    t.add_row("book updates", Text(f"{state.updates:,}"))
    t.add_row("  per sec", Text(f"{rate:,.1f}", style="dim"))
    t.add_row("re-scans", Text(f"{state.scans:,}"))
    t.add_row("opportunities", Text(
        str(len(state.opportunities)),
        style="bold green" if state.opportunities else "dim",
    ))
    t.add_row(
        "rate limited",
        Text(str(state.rate_limited), style="yellow" if state.rate_limited else "dim"),
    )
    return Panel(t, title="feed", border_style="blue", padding=(0, 1))


def _near_miss_panel(state: DashboardState) -> Panel:
    """
    The closest groups to violating. This is the main readout when nothing is
    firing — without it, a working engine and a broken one look the same.
    """
    t = Table(expand=True, box=None, pad_edge=False)
    t.add_column("group", style="cyan", no_wrap=True, ratio=3)
    t.add_column("shape", style="dim", no_wrap=True, ratio=1)
    t.add_column("legs", justify="right", style="dim", no_wrap=True, ratio=1)
    t.add_column("from firing", justify="right", no_wrap=True, ratio=2)

    rows = [m for m in state.near_misses if m.get("monotonic_margin") is not None]
    rows.sort(key=lambda m: -m["monotonic_margin"])

    if not rows:
        return Panel(
            Align.center(Text("waiting for quotes…", style="dim"), vertical="middle"),
            title="closest to arbitrage", border_style="magenta",
        )

    for m in rows[:8]:
        gap = -m["monotonic_margin"]
        style = "bold green" if gap <= 0 else "yellow" if gap < 0.01 else "dim"
        t.add_row(
            m["group_id"], m["shape"], str(m["legs"]),
            Text(f"{gap * 100:.2f}¢", style=style),
        )
    return Panel(t, title="closest to arbitrage", border_style="magenta")


def _positions_panel(state: DashboardState) -> Panel:
    t = Table(expand=True, box=None, pad_edge=False)
    t.add_column("group", style="cyan", no_wrap=True, ratio=3)
    t.add_column("type", style="dim", no_wrap=True, ratio=2)
    t.add_column("fill", no_wrap=True, ratio=2)
    t.add_column("sets", justify="right", no_wrap=True, ratio=1)
    t.add_column("P&L", justify="right", no_wrap=True, ratio=2)

    if not state.positions:
        return Panel(
            Align.center(
                Text(
                    "no paper trades yet — nothing has crossed",
                    style="dim",
                ),
                vertical="middle",
            ),
            title="paper positions", border_style="green",
        )

    for p in reversed(state.positions[-8:]):
        fill_style = {
            "complete": "green", "partial": "yellow", "broken": "bold red",
        }[p.fill_status]
        pnl = (
            _fmt_money(p.pnl) if p.pnl is not None
            else Text("open", style="dim")
        )
        t.add_row(
            p.group_id, p.type,
            Text(p.fill_status, style=fill_style),
            f"{p.sets_filled}/{p.sets_attempted}", pnl,
        )
    return Panel(t, title="paper positions", border_style="green")


def _log_panel(state: DashboardState) -> Panel:
    lines = []
    for ts, msg, style in reversed(state.events):
        line = Text()
        line.append(ts.strftime("%H:%M:%S "), style="dim")
        line.append(msg, style=style or "")
        lines.append(line)
    if not lines:
        lines = [Text("starting up…", style="dim")]
    return Panel(Group(*lines), title="activity", border_style="dim")


def render(state: DashboardState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(_header(state), size=3, name="header"),
        Layout(name="body"),
        Layout(_log_panel(state), size=8, name="log"),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=2),
    )
    layout["left"].split_column(
        Layout(_pnl_panel(state), name="pnl"),
        Layout(_activity_panel(state), name="feed"),
    )
    layout["right"].split_column(
        Layout(_near_miss_panel(state), name="near"),
        Layout(_positions_panel(state), name="positions"),
    )
    return layout
