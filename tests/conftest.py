from datetime import datetime, timezone

import numpy as np
import pytest

from arbengine.models import (
    Contract,
    ContractGroup,
    GroupValidation,
    StateSpace,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return NOW


def make_contract(
    ticker: str,
    ask: float | None = None,
    bid: float | None = None,
    ask_size: int = 100,
    bid_size: int = 100,
    interval: tuple[float, float] | None = None,
) -> Contract:
    return Contract(
        ticker=ticker,
        ask=ask,
        bid=bid,
        ask_size=ask_size if ask is not None else 0,
        bid_size=bid_size if bid is not None else 0,
        fetched_at=NOW,
        interval=interval,
    )


def make_group(
    contracts: list[Contract],
    payoff: list[list[float]],
    labels: list[str] | None = None,
    shape: str = "bracket",
    group_id: str = "TEST",
) -> ContractGroup:
    """Build a group directly from a hand-written payoff matrix."""
    n_states = len(payoff[0])
    states = [(float(i), float(i + 1)) for i in range(n_states)]
    return ContractGroup(
        group_id=group_id,
        series_ticker="TEST",
        event_ticker=group_id,
        contracts=contracts,
        state_space=StateSpace(
            states=states,
            labels=labels or [f"s{i}" for i in range(n_states)],
        ),
        payoff=np.array(payoff, dtype=float),
        validation=GroupValidation(ok=True),
        shape=shape,
    )
