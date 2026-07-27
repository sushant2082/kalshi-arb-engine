from datetime import datetime
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

DetectorType = Literal["complement", "partition", "monotonic", "time_monotonic", "lp"]
Side = Literal["buy", "sell"]

# A half-open interval [lo, hi) over the outcome variable. -inf / +inf allowed
# for the open tails of a ladder.
Interval = tuple[float, float]


class Contract(BaseModel):
    """One tradeable Kalshi YES contract with its top-of-book quote."""

    ticker: str
    # Best quotes in dollars, [0, 1]. None when that side of the book is empty.
    bid: float | None = None
    ask: float | None = None
    bid_size: int = 0
    ask_size: int = 0
    fetched_at: datetime

    # Outcome definition: YES settles true iff the outcome variable lands in
    # this interval. Populated by groups.py from market metadata.
    interval: Interval | None = None
    # Raw metadata retained for auditing how the interval was derived.
    strike_type: str | None = None
    subtitle: str = ""

    @field_validator("bid", "ask")
    @classmethod
    def _check_price(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"price must be in [0, 1] dollars, got {v}")
        return v

    @property
    def tradeable(self) -> bool:
        """True if there is depth on at least one side worth feeding to a detector."""
        return (self.ask is not None and self.ask_size > 0) or (
            self.bid is not None and self.bid_size > 0
        )


class StateSpace(BaseModel):
    """
    A mutually exclusive, collectively exhaustive partition of one outcome
    variable. `states[s]` is the half-open interval defining state s.
    """

    states: list[Interval]
    # Human-readable label per state, for alerts and debugging.
    labels: list[str] = Field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.states)


class GroupValidation(BaseModel):
    """Why a group was accepted or rejected as a valid arbitrage universe."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


class ContractGroup(BaseModel):
    """
    A set of contracts sharing one outcome variable, plus the state space they
    partition and the payoff matrix mapping contracts to states.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    group_id: str
    series_ticker: str
    event_ticker: str
    contracts: list[Contract]
    state_space: StateSpace
    # M[i][s] = 1 if contract i's YES settles true in state s, else 0.
    payoff: np.ndarray
    validation: GroupValidation
    # "bracket" (disjoint ranges) or "ladder" (nested "at least K" thresholds).
    shape: Literal["bracket", "ladder", "binary"] = "bracket"

    @property
    def tickers(self) -> list[str]:
        return [c.ticker for c in self.contracts]


class Leg(BaseModel):
    """One leg of a locking portfolio."""

    ticker: str
    side: Side
    qty: int
    price: float  # ask if buying, bid if selling
    fee: float    # per-contract fee at `price`

    @property
    def cash_flow(self) -> float:
        """Negative when buying (cash out), positive when selling (cash in)."""
        if self.side == "buy":
            return -(self.price + self.fee) * self.qty
        return (self.price - self.fee) * self.qty


class ArbOpportunity(BaseModel):
    """A detected coherence violation and the portfolio that locks it."""

    group_id: str
    type: DetectorType
    legs: list[Leg]

    total_cost: float          # net cash out today, dollars
    total_fee: float           # summed fees across all legs, dollars
    guaranteed_profit: float   # t*, worst-case profit across all states, dollars

    fillable_sets: int         # complete locking sets executable at quoted depth
    min_leg_size: int          # smallest per-leg quoted size
    leg_count: int

    first_seen: datetime
    last_seen: datetime

    @property
    def elevated_execution_risk(self) -> bool:
        # Set by the scanner against MAX_LEG_COUNT_ALERT; recomputed here as a
        # convenience default of >2 legs (anything past a 2-leg lock).
        return self.leg_count > 2

    @property
    def profit_per_set(self) -> float:
        if self.fillable_sets <= 0:
            return 0.0
        return self.guaranteed_profit / self.fillable_sets


def opportunity_key(opp: ArbOpportunity) -> str:
    """
    Stable identity for an opportunity across scans, so persistence tracking
    measures how long one violation lasted rather than counting each poll tick
    as a fresh event. Keyed on group + type + the signed leg structure, not on
    price or size (those move while the same violation persists).
    """
    structure = ",".join(sorted(f"{leg.ticker}:{leg.side}" for leg in opp.legs))
    return f"{opp.group_id}|{opp.type}|{structure}"


# ── Paper trading ─────────────────────────────────────────────────────────────
# Simulation only. Nothing in this section calls a Kalshi write endpoint.

FillStatus = Literal["complete", "partial", "broken"]
PositionStatus = Literal["open", "settled"]


class PaperFill(BaseModel):
    """A simulated fill for one leg."""

    ticker: str
    side: Side
    requested_qty: int
    filled_qty: int
    price: float  # effective price after simulated slippage
    fee: float

    @property
    def cash_flow(self) -> float:
        if self.side == "buy":
            return -(self.price + self.fee) * self.filled_qty
        return (self.price - self.fee) * self.filled_qty


class PaperPosition(BaseModel):
    """
    A simulated attempt to execute a detected lock.

    `fill_status` is the honest part: "complete" means every leg filled at the
    quoted size, "partial" means every leg filled but at a reduced common set
    count (still hedged), and "broken" means the legs filled unevenly and the
    residual is a live directional exposure, not a lock.
    """

    id: int | None = None
    opportunity_key: str
    group_id: str
    type: DetectorType

    entered_at: datetime
    fills: list[PaperFill]
    fill_status: FillStatus

    sets_attempted: int
    sets_filled: int          # complete hedged sets actually achieved
    net_cash: float           # sum of fill cash flows; negative = paid out
    total_fee: float
    expected_profit: float    # what the detector promised for sets_filled

    bankroll_at_entry: float

    status: PositionStatus = "open"
    realized_payout: float | None = None  # settlement payout, dollars
    pnl: float | None = None
    settled_at: datetime | None = None
    settlement_state: str | None = None   # which state the outcome landed in
