"""Read-only adapter for the real Playtomic Third Party API (docs/PLAYER_APP_ARCHITECTURE.md, "The Playtomic
connection").

What it uses, as documented at third-party.playtomic.io (checked 2026-11-18):
- GET /api/v1/bookings?tenant_id=...&start_booking_date=...&end_booking_date=...&sport_id=PADEL&page=&size=
  fields: booking_id, resource_id, resource_name, booking_start_date, booking_end_date (UTC), booking_type,
  origin, price, payment_status, is_canceled, participant_info.owner_id, participant_info.participants[]
  (participant_id, name, participant_type CUSTOMER | GUEST | CONTACT).
- GET /api/v1/venues/{venue_id}/players?include=SPORTS&limit=&cursor_id=
  fields: player_id, name, email, phone, accepts_commercial_communications, sports[].sport_id / level_value.
- Bearer token in the Authorization header. Credentials come from the club (Playtomic Manager → Settings →
  Developer tools). The docs list no write endpoints, no webhooks and no rate limits, so Rally polls and
  never writes: every booking, join, payment, leave or conversion happens in the Playtomic app.

Not in the documented API, so worked out here: seat count (padel = 4), a game's level (the average of its
players' padel levels), and whether an open match is club-managed (origin MANAGER) or player-made.
Switch on with RALLY_PROVIDER=playtomic, PLAYTOMIC_TOKEN and PLAYTOMIC_VENUE_ID (optional PLAYTOMIC_BASE_URL).
"""

from __future__ import annotations

import json
import os
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .providers import ProviderUnavailable
from .schemas import Court, Match, RosterPlayer, Snapshot

BASE_URL = "https://thirdparty.playtomic.io"
PAYMENT_MAP = {"PAID": "paid", "PARTIAL_PAID": "pending", "PENDING": "pending", "UNPAID": "pending"}
PADEL_SEATS = 4


class PlaytomicReadOnly(RuntimeError):
    """Rally tried to change something in Playtomic. The API is read-only: the player or club does it in the app."""


Fetch = Callable[[str, dict], dict]


class PlaytomicProvider:
    """Implements the same read interface as MockBookingProvider, against the real API."""

    name = "playtomic"

    def __init__(self, token: str, venue_id: str, tz: str = "America/New_York", base_url: str = BASE_URL,
                 fetch: Optional[Fetch] = None, courts: Optional[list[Court]] = None):
        if not token or not venue_id:
            raise ValueError("PLAYTOMIC_TOKEN and PLAYTOMIC_VENUE_ID are needed for the Playtomic connection.")
        self.token, self.venue_id, self.tz, self.base_url = token, venue_id, ZoneInfo(tz), base_url.rstrip("/")
        self._fetch = fetch or self._http_get
        self._courts = courts
        self._levels: dict[str, float] = {}
        self._last_error: str | None = None

    @classmethod
    def from_env(cls, tz: str, courts: Optional[list[Court]] = None) -> "PlaytomicProvider":
        return cls(os.environ.get("PLAYTOMIC_TOKEN", ""), os.environ.get("PLAYTOMIC_VENUE_ID", ""), tz,
                   os.environ.get("PLAYTOMIC_BASE_URL", BASE_URL), courts=courts)

    # ---- HTTP ---------------------------------------------------------------------------------------
    def _http_get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # network, 401/403 (expired token), 5xx: Rally stops rather than guesses
            raise ProviderUnavailable(f"Playtomic API: {type(exc).__name__}: {exc}") from exc

    def _get(self, path: str, params: dict) -> dict | list:
        try:
            data = self._fetch(path, params)
            self._last_error = None
            return data
        except ProviderUnavailable as exc:
            self._last_error = str(exc)
            raise

    # ---- players (roster + padel levels) --------------------------------------------------------
    def players(self) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"include": "SPORTS", "limit": 100}
            if cursor:
                params["cursor_id"] = cursor
            page = self._get(f"/api/v1/venues/{self.venue_id}/players", params)
            rows = page.get("data", page if isinstance(page, list) else [])
            out.extend(rows)
            if not page.get("has_more") or not page.get("next_cursor_id"):
                break
            cursor = page["next_cursor_id"]
        for p in out:
            level = next((s.get("level_value") for s in p.get("sports") or [] if s.get("sport_id") == "PADEL"), None)
            if level is not None:
                self._levels[p["player_id"]] = float(level)
        return out

    # ---- bookings -> Rally matches ------------------------------------------------------------------
    def _bookings(self, start: datetime, end: datetime) -> list[dict]:
        out, page = [], 0
        while True:
            fmt = "%Y-%m-%dT%H:%M:%S"
            params = {"tenant_id": self.venue_id, "sport_id": "PADEL", "page": page, "size": 200,
                      "start_booking_date": start.astimezone(timezone.utc).strftime(fmt),
                      "end_booking_date": end.astimezone(timezone.utc).strftime(fmt)}
            rows = self._get("/api/v1/bookings", params)
            rows = rows.get("data", rows) if isinstance(rows, dict) else rows
            out.extend(rows)
            if len(rows) < 200:
                return out
            page += 1

    def _when(self, value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=timezone.utc).astimezone(self.tz)

    def to_match(self, b: dict) -> Match:
        info = b.get("participant_info") or {}
        ids, guests = [], 0
        for p in info.get("participants") or []:
            if p.get("participant_type") == "CUSTOMER" and p.get("participant_id"):
                ids.append(p["participant_id"])
            else:   # GUEST / CONTACT: someone without a Rally-visible account
                guests += 1
                ids.append(f"GUEST-{b['booking_id']}-{guests}")
        owner = info.get("owner_id")
        if owner and owner not in ids:
            ids.insert(0, owner)
        levels = [self._levels[i] for i in ids if i in self._levels]
        club_open = b.get("booking_type") == "OPEN_MATCH" and b.get("origin") == "MANAGER"
        return Match(
            match_id=b["booking_id"], court_id=b.get("resource_id", ""),
            start=self._when(b["booking_start_date"]), end=self._when(b["booking_end_date"]),
            kind="club_open" if club_open else "private",
            level=round(statistics.mean(levels), 1) if levels else None,
            player_ids=ids, players_known=True, capacity=PADEL_SEATS,
            status="cancelled" if b.get("is_canceled") else "confirmed",
            payment_status=PAYMENT_MAP.get(b.get("payment_status", ""), "unknown"),
            created_by="playtomic", note=b.get("resource_name", ""),
            owner_id=None if club_open else owner, booking_type=b.get("booking_type", "REGULAR_BOOKING"))

    # ---- the read interface Rally uses --------------------------------------------------------------
    def health(self) -> str:
        return "unavailable" if self._last_error else "ok"

    def get_snapshot(self, club_id: str, start: datetime, end: datetime, now: datetime) -> Snapshot:
        players = self.players()
        matches = [m for m in (self.to_match(b) for b in self._bookings(start, end)) if m.status == "confirmed"]
        courts = self._courts or sorted({Court(court_id=m.court_id, name=m.note or m.court_id, court_type="padel")
                                         for m in matches}, key=lambda c: c.court_id)
        roster = [RosterPlayer(player_id=p["player_id"], name=p.get("name", ""),
                               first_name=(p.get("name") or "").split(" ")[0],
                               level=self._levels.get(p["player_id"], 0.0)) for p in players]
        return Snapshot(club_id=club_id, taken_at=now, window_start=start, window_end=end, courts=courts,
                        matches=matches, roster=roster)

    def get_match(self, match_id: str) -> Optional[Match]:
        rows = self._get("/api/v1/bookings", {"tenant_id": self.venue_id, "booking_id": [match_id]})
        rows = rows.get("data", rows) if isinstance(rows, dict) else rows
        return self.to_match(rows[0]) if rows else None

    def bookings_at(self, court_id: str, start: datetime, end: datetime) -> list[Match]:
        return [m for m in (self.to_match(b) for b in self._bookings(start - timedelta(hours=3), end))
                if m.court_id == court_id and m.status == "confirmed" and m.start < end and start < m.end]

    def all_matches(self) -> list[Match]:
        now = datetime.now(self.tz)
        return [m for m in (self.to_match(b) for b in self._bookings(now - timedelta(days=1), now + timedelta(days=7)))
                if m.status == "confirmed"]

    # ---- writes: never. The player or club does these in Playtomic; Rally reads the result back. -----
    def _read_only(self, *_, **__):
        raise PlaytomicReadOnly("Playtomic's API is read-only: this happens in the Playtomic app, then Rally "
                                "reads it back.")

    set_outage = simulate_seat_filled = simulate_seat_taken_elsewhere = simulate_group_booking = _read_only
    simulate_external_booking = simulate_player_dropout = create_player_booking = _read_only
    simulate_convert_to_public = link_booking = _read_only
