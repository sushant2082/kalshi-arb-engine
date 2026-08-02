"""
Read-only sharp-odds client (The Odds API).

Quota discipline matters here in a way it does not for the free venue APIs.
The Odds API bills one request PER REGION PER MARKET, so a single call for
`regions=eu,us&markets=h2h` costs two, and a careless poll loop burns a monthly
allowance in an afternoon. The free tier is 500 requests total.

Pinnacle is the anchor and lives in the EU region. The Odds API's own docs note
its odds are read from Pinnacle's public site and may lag — acceptable for
pregame, disqualifying for in-play.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from arbengine.value.implied import american_to_implied

log = logging.getLogger(__name__)


@dataclass
class QuotaState:
    """The Odds API reports usage on every response; worth surfacing."""

    used: int | None = None
    remaining: int | None = None

    def update(self, headers) -> None:
        for attr, key in (
            ("used", "x-requests-used"),
            ("remaining", "x-requests-remaining"),
        ):
            raw = headers.get(key)
            if raw is not None:
                try:
                    setattr(self, attr, int(raw))
                except ValueError:
                    pass


class OddsAPIClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.the-odds-api.com/v4",
        timeout: float = 25.0,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout)
        self.quota = QuotaState()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OddsAPIClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def sports(self) -> list[dict]:
        """Free — listing sports does not draw down the quota."""
        r = await self._http.get(f"{self._base}/sports", params={"apiKey": self._key})
        r.raise_for_status()
        self.quota.update(r.headers)
        return r.json()

    async def odds(
        self,
        sport_key: str,
        regions: str = "eu",
        markets: str = "h2h",
        bookmakers: str | None = None,
    ) -> list[dict]:
        """
        Fetch head-to-head odds for one sport. COSTS QUOTA.

        Prefer `bookmakers` over `regions` when a specific book is wanted:
        naming bookmakers directly is billed as one request regardless of how
        many regions they span, whereas `regions=eu,us` is billed twice.
        """
        params: dict[str, str] = {
            "apiKey": self._key,
            "markets": markets,
            "oddsFormat": "american",
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        else:
            params["regions"] = regions

        r = await self._http.get(f"{self._base}/sports/{sport_key}/odds", params=params)
        if r.status_code == 401:
            raise PermissionError("Odds API rejected the key (401)")
        if r.status_code == 422:
            log.warning("Odds API 422 for %s: %s", sport_key, r.text[:200])
            return []
        r.raise_for_status()
        self.quota.update(r.headers)
        return r.json()


def two_way_quotes(
    events: list[dict], book_key: str
) -> list[dict]:
    """
    Reduce raw Odds API events to two-way h2h lines from one book.

    Skips anything that is not a clean two-outcome market. Three-way soccer
    lines (home/draw/away) are dropped rather than folded into two outcomes:
    collapsing a draw silently changes what the contract means, and Kalshi's
    soccer game markets handle draws differently again.
    """
    out: list[dict] = []
    for ev in events:
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:
            continue

        book = next(
            (b for b in ev.get("bookmakers", []) if b.get("key") == book_key), None
        )
        if book is None:
            continue

        market = next(
            (m for m in book.get("markets", []) if m.get("key") == "h2h"), None
        )
        if market is None:
            continue

        outcomes = market.get("outcomes") or []
        if len(outcomes) != 2:
            # Three-way (with a draw) or malformed; not comparable to a binary
            # Kalshi contract without changing its meaning.
            continue

        by_name = {o.get("name"): o.get("price") for o in outcomes}
        if home not in by_name or away not in by_name:
            continue

        try:
            home_imp = american_to_implied(int(by_name[home]))
            away_imp = american_to_implied(int(by_name[away]))
        except (TypeError, ValueError):
            continue

        updated = book.get("last_update") or ev.get("commence_time")
        out.append({
            "event_id": ev.get("id"),
            "sport_key": ev.get("sport_key"),
            "home_team": home,
            "away_team": away,
            "commence_time": _parse(ev.get("commence_time")),
            "home_implied": home_imp,
            "away_implied": away_imp,
            "book": book_key,
            "last_update": _parse(updated),
        })
    return out


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
