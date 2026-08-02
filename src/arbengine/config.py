from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── Credentials (read-only key) ────────────────────────────────────────────
    kalshi_api_key_id: str = Field(default="")
    kalshi_private_key_path: Path = Field(default=Path("kalshi_key.pem"))

    # ── Endpoints ──────────────────────────────────────────────────────────────
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    # ── Fee model ──────────────────────────────────────────────────────────────
    # VERIFIED against the Kalshi Fee Schedule effective 2026-07-07:
    #   taker: fees = round_up(M x 0.07 x C x P x (1-P))
    # M is a per-series multiplier read live from /series; series with M=0
    # (KXBTCY, KXETHY, ...) are genuinely fee-free.
    fee_multiplier: float = 0.07
    # Rounding granularity. The published formula says centicent; the published
    # fee TABLE matches cent exactly. Cent is the conservative reading and can
    # only overstate fees — see fees.FEE_ROUNDING.
    fee_rounding: float = 0.01

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
    #
    # NoDecode is required: without it pydantic-settings treats any list field
    # as complex and JSON-decodes the raw env value before field validators
    # run, so a plain comma-separated TARGET_SERIES raises a JSONDecodeError
    # instead of reaching _split_series below.
    target_series: Annotated[list[str], NoDecode] = [
        "KXBTCD", "KXETHD", "KXHIGHNY", "KXCPIYOY",
    ]

    # ── Polling ────────────────────────────────────────────────────────────────
    # Kalshi serves /markets through a CDN with Cache-Control: max-age=15.
    # Polling faster returns byte-identical cached responses while the misses
    # that do occur burn the token budget, so 15s is the floor at which REST
    # can tell you anything new. Use `stream` for anything faster — the
    # WebSocket is not cached and is the only real-time path.
    kalshi_poll_sec: int = 15
    # Must exceed the 15s cache TTL or every REST quote is rejected as stale
    # once back-dated by its cache age.
    max_quote_age_sec: int = 30

    # ── Stream scanning ───────────────────────────────────────────────────────
    # How often dirty groups are re-solved. This is a FLOOR on the lifetime of a
    # violation the engine can see: anything that appears and vanishes inside
    # one interval is missed.
    #
    # The right value depends entirely on the workload. A multi-leg intra-venue
    # LP on a 188-leg group takes ~58ms, so a 25ms cadence is not physically
    # possible there on one core. A cross-venue pair check is O(1) arithmetic on
    # two quotes and can run at 25ms comfortably — which is why vendors quoting
    # a 25ms debounce are describing pairwise checks, not a multi-leg solve.
    scan_interval_sec: float = 0.25
    # Threads used to solve dirty groups concurrently. scipy releases the GIL in
    # its compiled paths, so this scales with physical cores. Raise it on a
    # many-core host; the LP is CPU-bound and does not use a GPU.
    scan_workers: int = 4

    # ── Rate limiting ─────────────────────────────────────────────────────────
    # Kalshi meters by token cost against a continuously refilling budget.
    # Per-second Read budgets by tier: basic 200, advanced 300, expert 600,
    # premier 1000, paragon 2000, prime 4000, prestige 6000. Market-data
    # endpoints cost the default 10 tokens, so basic sustains ~20 req/s.
    # GET /account/endpoint_costs is authoritative on non-default costs.
    kalshi_read_budget: float = 200.0
    # MEASURED, not the documented default. GET /account/endpoint_costs reports
    # 10 tokens for /markets, and cache HITS do behave that cheaply — but Kalshi
    # fronts market data with CloudFront, and a cache MISS bills far more.
    # Calibration (distinct series tickers, forcing misses): clean at 3 misses/s,
    # rate limited at 4.4 misses/s, implying ~46 tokens per uncached call. So a
    # 200/s Read budget sustains roughly 4 uncached requests per second, not 20.
    #
    # This matters more than the average rate: a scanner averaging 2 req/s still
    # 429s constantly if it fires each pass as a burst priced at 10 tokens.
    kalshi_request_cost: float = 50.0
    # Burst capacity, in seconds of budget. Kalshi documents two seconds for
    # Basic/Advanced Read, but starting a run with a full two-second bucket
    # fires a 40-request burst that 429s whenever the server's own bucket is
    # not equally full — which it never is after discovery. Measured: an even
    # 20 req/s is clean indefinitely, so cap the burst at one second and let
    # the sustained rate do the work.
    kalshi_bucket_seconds: float = 1.0
    # Headroom against a shared key or clock skew.
    kalshi_rate_safety: float = 0.9
    # Concurrent in-flight requests. The token bucket is the real limiter, so
    # this only bounds socket usage.
    kalshi_concurrency: int = 16

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
        """
        Accept both a comma-separated string (from env) and a real list (from
        code or tests). Also tolerates a JSON array, since that is what the
        default pydantic-settings decoder would have expected.
        """
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                import json
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
            return [s.strip() for s in text.split(",") if s.strip()]
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
