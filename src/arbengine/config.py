from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── Credentials (read-only key) ────────────────────────────────────────────
    kalshi_api_key_id: str = Field(default="")
    kalshi_private_key_path: Path = Field(default=Path("kalshi_key.pem"))

    # ── Endpoints ──────────────────────────────────────────────────────────────
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    # ── Fee model ──────────────────────────────────────────────────────────────
    # fee(P) = ceil(multiplier * P * (1-P) * 100) / 100
    # VERIFY this multiplier against current Kalshi docs per market category.
    fee_multiplier: float = 0.07

    # ── Detection thresholds ───────────────────────────────────────────────────
    # Minimum guaranteed profit t* (dollars, whole portfolio) required to flag.
    min_guaranteed_profit: float = 0.01
    # Ignore locks that cannot be executed at least this many complete times.
    min_fillable_sets: int = 2
    # Above this leg count, mark the opportunity as elevated execution risk.
    max_leg_count_alert: int = 4
    # Numeric tolerance on t*; below this, treat as arbitrage-free.
    lp_tolerance: float = 1e-6

    # ── Scan targets ───────────────────────────────────────────────────────────
    # Bracketed/laddered series only. Single moneyline games almost never
    # violate coherence and are a waste of rate limit.
    target_series: list[str] = ["KXBTCD", "KXETHD", "KXHIGHNY", "KXCPIYOY"]

    # ── Polling ────────────────────────────────────────────────────────────────
    kalshi_poll_sec: int = 10
    max_quote_age_sec: int = 30

    # ── Storage / output ───────────────────────────────────────────────────────
    db_path: Path = Path("arbengine.db")
    csv_output_path: Path = Path("opportunities.csv")

    # ── Paper trading (simulation only — never places real orders) ────────────
    paper_enabled: bool = True
    paper_bankroll: float = 10_000.0
    paper_max_sets_per_opp: int = 50
    # Probability an individual leg fills at the quoted price. 1.0 is the
    # optimistic bound and will overstate real performance, because Kalshi has
    # no atomic multi-leg fill. Lower it to stress-test leg risk.
    paper_leg_fill_prob: float = 1.0
    # Extra adverse price movement per leg, in cents, applied on entry.
    paper_slippage_cents: float = 0.0

    @field_validator("target_series", mode="before")
    @classmethod
    def _split_series(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("fee_multiplier")
    @classmethod
    def _check_multiplier(cls, v: float) -> float:
        if v < 0:
            raise ValueError("fee_multiplier must be non-negative")
        return v

    @field_validator("paper_leg_fill_prob")
    @classmethod
    def _check_fill_prob(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"paper_leg_fill_prob must be in [0, 1], got {v}")
        return v
