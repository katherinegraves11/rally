"""Booking provider boundary (PRD decision D04).

Rally only reads from a provider: it never writes bookings, prices or payments. `MockBookingProvider`
stands in for Playtomic (or a club spreadsheet) and keeps its own SQLite file so the boundary stays
visible. The `simulate_*` methods are demo fixtures a manager clicks to play the provider's part.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Protocol

from .schemas import Court, Match, RosterPlayer, Snapshot


class ProviderUnavailable(RuntimeError):
    """The provider could not be reached or returned stale data. Callers must stop or escalate."""


class BookingProvider(Protocol):
    name: str

    def health(self) -> str: ...

    def get_snapshot(self, club_id: str, start: datetime, end: datetime, now: datetime) -> Snapshot: ...

    def get_match(self, match_id: str) -> Optional[Match]: ...

    def bookings_at(self, court_id: str, start: datetime, end: datetime) -> list[Match]: ...


PROVIDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS courts (court_id TEXT PRIMARY KEY, name TEXT NOT NULL, court_type TEXT);
CREATE TABLE IF NOT EXISTS roster (player_id TEXT PRIMARY KEY, name TEXT, first_name TEXT, level REAL);
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY, court_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, kind TEXT NOT NULL,
    level REAL, player_ids TEXT NOT NULL, players_known INTEGER NOT NULL, capacity INTEGER NOT NULL,
    status TEXT NOT NULL, payment_status TEXT NOT NULL, dropout_player_ids TEXT NOT NULL, created_by TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _split(value: str | None) -> list[str]:
    return [v for v in (value or "").split(";") if v]


class MockBookingProvider:
    """A read-only provider backed by data/*.csv, plus demo-only fixture controls."""

    name = "mock_provider"

    def __init__(self, db_path: str | Path, data_dir: str | Path):
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.db_path.exists()
        with self._conn() as conn:
            conn.executescript(PROVIDER_SCHEMA)
            self._needs_backfill = False
            self._migrate(conn)
        if fresh:
            self._seed()
        elif self._needs_backfill:
            self._backfill_playtomic_fields()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add Playtomic's owner_id / booking_type to an older demo database, and backfill them."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)").fetchall()}
        if "owner_id" in cols:
            return
        conn.execute("ALTER TABLE matches ADD COLUMN owner_id TEXT")
        conn.execute("ALTER TABLE matches ADD COLUMN booking_type TEXT NOT NULL DEFAULT 'REGULAR_BOOKING'")
        self._needs_backfill = True

    def _fixture_organisers(self) -> dict[str, str]:
        path = self.data_dir / "match_organizers.csv"
        if not path.exists():
            return {}
        with open(path, newline="", encoding="utf-8") as f:
            return {r["match_id"]: r["organizer_id"] for r in csv.DictReader(f)}

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _seed(self) -> None:
        with self._conn() as conn:
            with open(self.data_dir / "courts.csv", newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    conn.execute("INSERT INTO courts VALUES (?,?,?)", (r["court_id"], r["name"], r["court_type"]))
            with open(self.data_dir / "players.csv", newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    conn.execute("INSERT INTO roster VALUES (?,?,?,?)",
                                 (r["player_id"], r["name"], r["first_name"], float(r["level"])))
            with open(self.data_dir / "matches.csv", newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    conn.execute(
                        "INSERT INTO matches (match_id, court_id, start, end, kind, level, player_ids, players_known, capacity, status, payment_status, dropout_player_ids, created_by, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["match_id"], r["court_id"], r["start"], r["end"], r["kind"],
                         float(r["level"]) if r["level"] else None, json.dumps(_split(r["player_ids"])),
                         1 if r["players_known"] == "True" else 0, int(r["capacity"]), r["status"],
                         r["payment_status"], json.dumps(_split(r["dropout_player_ids"])), r["created_by"], r["note"]))
            conn.execute("INSERT OR REPLACE INTO flags VALUES ('outage', '0')")
        self._backfill_playtomic_fields()   # owner_id / booking_type the way Playtomic reports them

    def _backfill_playtomic_fields(self) -> None:
        organisers = self._fixture_organisers()
        with self._conn() as conn:
            for r in conn.execute("SELECT match_id, kind, player_ids, players_known, created_by FROM matches").fetchall():
                players = json.loads(r["player_ids"])
                owner = organisers.get(r["match_id"]) or (players[0] if r["kind"] == "private" and r["players_known"]
                                                          and players and r["created_by"] == "seed" else None)
                conn.execute("UPDATE matches SET owner_id = ?, booking_type = ? WHERE match_id = ?",
                             (owner, "OPEN_MATCH" if r["kind"] == "club_open" else "REGULAR_BOOKING", r["match_id"]))

    @staticmethod
    def _match(row: sqlite3.Row) -> Match:
        return Match(
            match_id=row["match_id"], court_id=row["court_id"], start=datetime.fromisoformat(row["start"]),
            end=datetime.fromisoformat(row["end"]), kind=row["kind"], level=row["level"],
            player_ids=json.loads(row["player_ids"]), players_known=bool(row["players_known"]),
            capacity=row["capacity"], status=row["status"], payment_status=row["payment_status"],
            dropout_player_ids=json.loads(row["dropout_player_ids"]), created_by=row["created_by"],
            note=row["note"] or "", owner_id=row["owner_id"], booking_type=row["booking_type"] or "REGULAR_BOOKING")

    def _check_up(self) -> None:
        if self.health() != "ok":
            raise ProviderUnavailable("mock provider is in a simulated outage")

    # ---- read API (what a real Playtomic adapter would implement) -----------------------------
    def health(self) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM flags WHERE key = 'outage'").fetchone()
        return "unavailable" if row and row["value"] == "1" else "ok"

    def get_snapshot(self, club_id: str, start: datetime, end: datetime, now: datetime) -> Snapshot:
        self._check_up()
        with self._conn() as conn:
            courts = [Court(**dict(r)) for r in conn.execute("SELECT * FROM courts ORDER BY court_id")]
            roster = [RosterPlayer(**dict(r)) for r in conn.execute("SELECT * FROM roster ORDER BY player_id")]
            rows = conn.execute("SELECT * FROM matches WHERE status = 'confirmed' AND start < ? AND end > ?",
                                (end.isoformat(), start.isoformat())).fetchall()
        return Snapshot(club_id=club_id, taken_at=now, window_start=start, window_end=end, courts=courts,
                        matches=[self._match(r) for r in rows], roster=roster)

    def get_match(self, match_id: str) -> Optional[Match]:
        self._check_up()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        return self._match(row) if row else None

    def bookings_at(self, court_id: str, start: datetime, end: datetime) -> list[Match]:
        self._check_up()
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM matches WHERE court_id = ? AND status = 'confirmed' AND start < ? "
                                "AND end > ?", (court_id, end.isoformat(), start.isoformat())).fetchall()
        return [self._match(r) for r in rows]

    def all_matches(self) -> list[Match]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM matches WHERE status = 'confirmed' ORDER BY start, court_id").fetchall()
        return [self._match(r) for r in rows]

    # ---- demo fixtures: the manager plays the provider's part (all clearly SIMULATED) ---------
    def set_outage(self, on: bool) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO flags VALUES ('outage', ?)", ("1" if on else "0",))

    def simulate_seat_filled(self, match_id: str, player_id: str, payment_status: str = "paid") -> None:
        """The invited player completed the booking link, so the provider now lists them in the match."""
        with self._conn() as conn:
            row = conn.execute("SELECT player_ids, capacity FROM matches WHERE match_id = ?", (match_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown match {match_id}")
            players = json.loads(row["player_ids"])
            if player_id not in players:
                if len(players) >= row["capacity"]:
                    raise ValueError("match is already full")
                players.append(player_id)
            conn.execute("UPDATE matches SET player_ids = ?, payment_status = ? WHERE match_id = ?",
                         (json.dumps(players), payment_status, match_id))

    def create_player_booking(self, match_id: str, court_id: str, start: datetime, end: datetime,
                              player_ids: list[str], level: float, note: str) -> None:
        """A player booked and paid for a court in the club's booking app (simulated)."""
        with self._conn() as conn:
            conn.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (match_id, court_id, start.isoformat(), end.isoformat(), "private", level,
                          json.dumps(player_ids), 1, 4, "confirmed", "paid", "[]", "player_app", note,
                          player_ids[0], "REGULAR_BOOKING"))

    def simulate_convert_to_public(self, match_id: str) -> None:
        """The owner tapped 'Convert to Public Match' in Playtomic: the booking becomes an OPEN_MATCH."""
        with self._conn() as conn:
            conn.execute("UPDATE matches SET booking_type = 'OPEN_MATCH' WHERE match_id = ?", (match_id,))

    def link_booking(self, match_id: str, player_ids: list[str], note: str, level: float | None = None) -> None:
        """The club confirmed whose booking this is: it now lists the owner (and their guests) as players."""
        with self._conn() as conn:
            conn.execute("UPDATE matches SET player_ids = ?, players_known = 1, note = ?, level = COALESCE(level, ?) "
                         "WHERE match_id = ?", (json.dumps(player_ids), note, level, match_id))

    def simulate_player_dropout(self, match_id: str, player_id: str, note: str) -> None:
        """A booked player cancelled: the provider frees their seat and records them as dropped out."""
        with self._conn() as conn:
            row = conn.execute("SELECT player_ids, dropout_player_ids FROM matches WHERE match_id = ?",
                               (match_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown match {match_id}")
            players = [p for p in json.loads(row["player_ids"]) if p != player_id]
            dropouts = json.loads(row["dropout_player_ids"]) + [player_id]
            conn.execute("UPDATE matches SET player_ids = ?, dropout_player_ids = ?, note = ? WHERE match_id = ?",
                         (json.dumps(players), json.dumps(dropouts), note, match_id))

    def simulate_seat_taken_elsewhere(self, match_id: str, outsider_id: str = "EXT-WALKIN") -> None:
        """Someone outside Rally (e.g. at the front desk) filled the open seat."""
        self.simulate_seat_filled(match_id, outsider_id, payment_status="paid")

    def simulate_group_booking(self, court_id: str, start: datetime, end: datetime, player_ids: list[str],
                               payment_status: str = "paid") -> str:
        """The group used the booking link: the provider now shows a booking for the court."""
        booking_id = f"B-RALLY-{start:%Y%m%d-%H%M}-{court_id}"
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (booking_id, court_id, start.isoformat(), end.isoformat(), "private", None,
                          json.dumps(player_ids), 1, 4, "confirmed", payment_status, "[]", "rally_handoff",
                          "Open match the club created in Playtomic Manager for a Rally group (simulated)",
                          None, "OPEN_MATCH"))
        return booking_id

    def simulate_external_booking(self, court_id: str, start: datetime, end: datetime) -> str:
        """Another customer booked the court directly, so Rally's view is now stale."""
        booking_id = f"B-EXT-{start:%Y%m%d-%H%M}-{court_id}"
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO matches (match_id, court_id, start, end, kind, level, player_ids, players_known, capacity, status, payment_status, dropout_player_ids, created_by, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (booking_id, court_id, start.isoformat(), end.isoformat(), "private", None, "[]", 0, 4,
                          "confirmed", "paid", "[]", "external", "Booked directly by another customer (simulated)"))
        return booking_id


# The real, read-only Playtomic adapter lives in rally/playtomic.py (same read interface as the mock).
