"""PlayerSide: the player app's only door into Rally (docs/PLAYER_APP_ARCHITECTURE.md).

Read models and actions for opted-in players and game organisers. Every action goes through the same
engine, coordinator and audit log as the club dashboard, so the two sides always agree:
- replies go through parse_reply() (Claude into a strict schema; Python decides),
- an organiser's "find someone for me" is the authorisation a private match needs, and their pick
  of players is the campaign approval (same scope checks on every send),
- paying only creates a booking in the (mock) provider; only verify_booking() can confirm it.
A player only ever sees their own invites, games and settings; group invites show counts, not names.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .engine import RallyEngine, short_name
from .fmt import day_label, level_label, range_label, relative_day, time_label
from .messaging import price_per_player
from .occupancy import BAND_LABELS, time_band
from .schemas import TERMINAL_STATES, InviteStatus as IS, Opportunity, OpportunityType, State

CHANNEL_LABELS = {"simulated_sms": "Text message (SMS)", "simulated_whatsapp": "WhatsApp"}
PLAYER_BANDS = ["weekday_early", "weekday_midday", "weekday_afternoon", "weekday_evening", "weekday_late",
                "weekend_morning", "weekend_afternoon", "weekend_evening"]

QUICK_REPLIES = {
    "yes": "Yes, I'm in!",
    "no": "Sorry, I can't make it this time.",
}


class PlayerError(ValueError):
    """Something the player cannot do right now; the message is shown to them as is."""


@dataclass
class InviteCard:
    invite_id: str
    campaign_id: str
    kind: str                 # "open_match" | "organiser_game" | "new_game"
    title: str
    court: str
    when: str
    day: str
    time: str
    level: str
    price: int
    status: str               # player-facing status key (see _status)
    headline: str
    detail: str
    can_reply: bool
    can_pay: bool
    paid: bool
    seats_held: int
    seats_total: int
    reply_by: str | None
    thread: list[dict] = field(default_factory=list)
    sort_key: tuple = ()


@dataclass
class OrganiserCard:
    match_id: str
    title: str
    when: str
    court: str
    level: str
    players_in: int
    capacity: int
    stage: str                # "ask" | "self" | "choose" | "running" | "done" | "not_possible"
    headline: str
    detail: str
    opportunity_id: str | None = None
    asked: list[dict] = field(default_factory=list)


class PlayerSide:
    def __init__(self, engine: RallyEngine):
        self.e = engine

    # ---- helpers -----------------------------------------------------------------------------------
    def _profile(self, pid: str):
        profiles = self.e.profiles()
        if pid not in profiles:
            raise PlayerError("Unknown player.")
        return profiles[pid]

    def _invite_of(self, pid: str, invite_id: str) -> dict:
        inv = self.e.storage.get_invite(invite_id)
        if inv is None or inv["player_id"] != pid or not inv["sent_at"]:
            raise PlayerError("That invite isn't yours.")  # a player can only act on their own invites
        return inv

    def _log(self, event_type: str, pid: str, detail: dict, campaign_id: str | None = None,
             opportunity_id: str | None = None) -> None:
        self.e._log(event_type, actor=f"player:{pid}", tool="player_app", detail={"player_id": pid, **detail},
                    campaign_id=campaign_id, opportunity_id=opportunity_id)

    # ---- who can I be (demo sign-in) ---------------------------------------------------------------
    def roster(self) -> list[dict]:
        profiles = self.e.profiles()
        active: dict[str, int] = {}
        for c in self.e.storage.campaigns():
            if State(c["state"]) in TERMINAL_STATES:
                continue
            for inv in self.e.storage.invites(c["campaign_id"]):
                if inv["sent_at"] and inv["status"] in (IS.SENT.value, IS.ACCEPTED.value, IS.NEEDS_MANAGER.value):
                    active[inv["player_id"]] = active.get(inv["player_id"], 0) + 1
        out = []
        for pid, p in sorted(profiles.items(), key=lambda kv: kv[1].name):
            out.append({"player_id": pid, "name": p.name, "first_name": p.first_name, "level": level_label(p.level),
                        "opt_in": p.opt_in_status, "active_invites": active.get(pid, 0),
                        "organiser": bool(self.e.organized_by(pid))})
        return out

    # ---- home --------------------------------------------------------------------------------------
    def home(self, pid: str) -> dict:
        self.e.refresh()
        p = self._profile(pid)
        cards = self.invite_cards(pid)
        return {
            "profile": p,
            "short_name": short_name(p.name),
            "level": level_label(p.level),
            "club": self.e.policy().club.name,
            "now": self.e.now(),
            "now_label": f"{day_label(self.e.now())} · {time_label(self.e.now())}",
            "open_cards": [c for c in cards if c.status in ("invited", "pay", "waiting_group", "paid_waiting",
                                                             "checking", "with_club")],
            "past_cards": [c for c in cards if c.status not in ("invited", "pay", "waiting_group", "paid_waiting",
                                                                 "checking", "with_club")],
            "organiser_cards": self.organiser_cards(pid),
            "games": self.games(pid),
            "provider_ok": self.e.provider_status == "ok",
        }

    def version(self, pid: str) -> str:
        """Changes whenever anything this player can see changes (the page polls it to refresh itself)."""
        msgs = self.e.storage.messages(player_id=pid)
        camps = [(c["campaign_id"], c["state"], c["updated_at"]) for c in self.e.storage.campaigns()]
        auth = sorted(self.e.storage.organizer_authorizations().items())
        raw = f"{len(msgs)}|{camps}|{auth}|{self.e.storage.get_meta('sim_now')}|{self.e.provider_status}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    # ---- invites -----------------------------------------------------------------------------------
    def invite_cards(self, pid: str) -> list[InviteCard]:
        policy = self.e.policy()
        now = self.e.now()
        cards = []
        for c in self.e.storage.campaigns():
            inv = self.e.storage.get_invite(f"{c['campaign_id']}:{pid}")
            if inv is None or not inv["sent_at"]:
                continue
            opp = self.e.opportunity(c["opportunity_id"])
            invites = self.e.storage.invites(c["campaign_id"])
            held = sum(1 for i in invites if i["status"] in (IS.ACCEPTED.value, IS.BOOKED.value))
            paid = pid in self.e.storage.payments(c["campaign_id"])
            thread = [{"dir": m["direction"], "body": m["body"], "kind": m["kind"],
                       "ts": time_label(datetime.fromisoformat(m["sim_ts"]))}
                      for m in self.e.storage.messages(c["campaign_id"], pid)]
            status, headline, detail = self._status(opp, c, inv, held, paid, now)
            kind = ("new_game" if opp.type == OpportunityType.EMPTY_COURT
                    else "organiser_game" if opp.organizer_name else "open_match")
            title = {"new_game": "New game for four", "open_match": "1 spot in an open match",
                     "organiser_game": f"{opp.organizer_name}'s game needs a 4th"}[kind]
            cards.append(InviteCard(
                invite_id=inv["invite_id"], campaign_id=c["campaign_id"], kind=kind, title=title,
                court=opp.court_name, when=f"{relative_day(opp.start, now)} · {range_label(opp.start, opp.end)}",
                day=day_label(opp.start), time=range_label(opp.start, opp.end),
                level=self._level_text(opp, c), price=price_per_player(policy), status=status, headline=headline,
                detail=detail, can_reply=status in ("invited", "with_club"), can_pay=status == "pay", paid=paid,
                seats_held=held, seats_total=policy.club.players_per_court,
                reply_by=time_label(datetime.fromisoformat(inv["reply_deadline"]))
                if inv["reply_deadline"] and status == "invited" else None,
                thread=thread, sort_key=(status not in ("invited", "pay"), opp.start)))
        return sorted(cards, key=lambda x: x.sort_key)

    def _level_text(self, opp: Opportunity, campaign: dict) -> str:
        if opp.type == OpportunityType.MISSING_PLAYER:
            return level_label(opp.level_target)
        tol = self.e.policy().eligibility.skill_tolerance
        levels = [self.e.profiles()[pid].level for pid in (campaign.get("approval") or {}).get("approved_player_ids", [])]
        if not levels:
            return "mixed"
        mid = sorted(levels)[len(levels) // 2]
        return f"{mid - tol:.1f}–{mid + tol:.1f}"

    def _status(self, opp: Opportunity, campaign: dict, inv: dict, held: int, paid: bool,
                now: datetime) -> tuple[str, str, str]:
        st = State(campaign["state"])
        s = IS(inv["status"])
        seats = self.e.policy().club.players_per_court
        group = opp.type == OpportunityType.EMPTY_COURT
        booking_link = any(m["kind"] == "booking_link" for m in self.e.storage.messages(campaign["campaign_id"],
                                                                                        inv["player_id"]))
        if s == IS.BOOKED or (st == State.CONFIRMED and s == IS.ACCEPTED):
            return "booked", "You're booked", "The club's booking system shows your spot as paid. See you on court."
        if s == IS.SENT and st not in TERMINAL_STATES:
            return "invited", "You're invited", ("This game only happens once 4 players say yes and book. "
                                                 "Nobody is charged until then." if group else
                                                 "Say yes and you'll get a link to book and pay your share.")
        if s == IS.NEEDS_MANAGER and st not in TERMINAL_STATES:
            return "with_club", "The club is reading your message", (
                "Your reply needed a person, so the club will get back to you. Nothing is booked yet. "
                "You can still answer yes or no below.")
        if s == IS.ACCEPTED and st not in TERMINAL_STATES:
            if st == State.ESCALATED:
                return "with_club", "The club is checking this game", (
                    "Something changed (for example the court was booked elsewhere). The club will let you "
                    "know. Nothing has been charged.")
            if booking_link and not paid:
                return "pay", "Book and pay your share", ("Your spot is held while you book. It's confirmed "
                                                          "once the booking system shows it paid.")
            if paid:
                if st == State.UNVERIFIED:
                    return "checking", "Checking your booking", (
                        "The booking system isn't answering right now, so we can't confirm yet. "
                        "We never mark a game booked without checking.")
                return "paid_waiting", "Paid, waiting for the others" if group else "Checking your booking", (
                    f"{held} of {seats} players have said yes. The court is booked once everyone has paid."
                    if group else "We're checking with the booking system.")
            return "waiting_group", f"You're in · {held} of {seats} so far", (
                "Your spot is held. Once 4 players have said yes, everyone gets a link to book and pay.")
        if s == IS.DECLINED:
            return "declined", "You passed", "No problem. You'll hear about other games that fit."
        if s == IS.OPTED_OUT:
            return "opted_out", "You've turned off invites", "Turn them back on any time under Settings."
        if s == IS.WAITLISTED:
            return "waitlisted", "You're on the waitlist", "All 4 spots filled first. We'll message you if one opens."
        if s == IS.TIMED_OUT:
            return "expired", "This invite timed out", "We moved on to the next player. You'll hear about other games."
        if st == State.CONFIRMED:
            return "filled", "This game is full", "Someone else took the spot. Thanks for being up for it."
        return "closed", "No longer available", "This game didn't go ahead through Rally. Nothing was charged."

    def reply(self, pid: str, invite_id: str, text: str):
        text = (text or "").strip()
        if not text:
            raise PlayerError("Write a reply first.")
        if len(text) > 500:
            raise PlayerError("Please keep replies under 500 characters.")
        inv = self._invite_of(pid, invite_id)
        campaign = self.e.storage.get_campaign(inv["campaign_id"])
        if State(campaign["state"]) in TERMINAL_STATES:
            raise PlayerError("This game is closed, so there's nothing to reply to.")
        return self.e.reply(invite_id, text)

    def pay(self, pid: str, invite_id: str) -> str:
        """The player completes the (simulated) Playtomic booking link and pays their share.

        Missing player: the provider lists them in the match. Empty court: each payment is recorded; when every
        held player has paid, the provider shows the group booking. Either way only the next verify_booking()
        check can mark the game CONFIRMED."""
        inv = self._invite_of(pid, invite_id)
        cid = inv["campaign_id"]
        campaign = self.e.storage.get_campaign(cid)
        if inv["status"] != IS.ACCEPTED.value or State(campaign["state"]) != State.AWAITING_BOOKING_VERIFICATION:
            raise PlayerError("There's nothing to pay for this game right now.")
        if not any(m["kind"] == "booking_link" for m in self.e.storage.messages(cid, pid)):
            raise PlayerError("Your booking link hasn't arrived yet.")
        payments = self.e.storage.payments(cid)
        if pid in payments:
            raise PlayerError("You've already paid your share.")
        if self.e.provider_status != "ok":
            raise PlayerError("The booking system isn't answering right now. Try again in a moment; you have "
                              "not been charged.")
        amount = price_per_player(self.e.policy())
        with self.e.storage.connect() as conn:
            self.e.storage.record_payment(conn, cid, pid, amount, self.e.now())
        self._log("PLAYER_PAID", pid, {"invite_id": invite_id, "amount_usd": amount, "simulated": True,
                                       "note": "simulated Playtomic payment; not a booking until the provider "
                                               "shows it and verify_booking() checks it"}, campaign_id=cid,
                  opportunity_id=campaign["opportunity_id"])
        opp = self.e.opportunity(campaign["opportunity_id"])
        held = [i["player_id"] for i in self.e.storage.invites(cid) if i["status"] == IS.ACCEPTED.value]
        paid = set(self.e.storage.payments(cid))
        if opp.type == OpportunityType.MISSING_PLAYER or set(held) <= paid:
            self.e.simulate_provider_confirmation(cid, payment_status="paid")
            return "Paid. The booking system now shows your booking; Rally checked it."
        self.e.refresh()
        return f"Paid. {len(paid)} of {len(held)} players have paid; the court is booked once everyone has."

    # ---- games already on the books ----------------------------------------------------------------
    def games(self, pid: str) -> list[dict]:
        now = self.e.now()
        out = []
        for m in self.e.provider.all_matches():
            if pid in m.player_ids and m.end > now:
                out.append({"match_id": m.match_id, "when": f"{relative_day(m.start, now)} · "
                                                            f"{range_label(m.start, m.end)}",
                            "court": f"Court {m.court_id[1:]}", "players": f"{len(m.player_ids)}/{m.capacity}",
                            "organiser": self.e.organizer_of(m.match_id) == pid,
                            "via_rally": m.created_by == "rally_handoff"})
        return out

    # ---- organiser journey (A) ---------------------------------------------------------------------
    def organiser_cards(self, pid: str) -> list[OrganiserCard]:
        now = self.e.now()
        policy = self.e.policy()
        auths = self.e.storage.organizer_authorizations()
        cards = []
        for match_id in self.e.organized_by(pid):
            m = self.e.provider.get_match(match_id) if self.e.provider_status == "ok" else None
            if m is None or m.end <= now:
                continue
            opp_id = f"MP-{match_id}"
            camps = self.e.storage.campaigns(opp_id)
            latest = camps[-1] if camps else None
            card = OrganiserCard(
                match_id=match_id, title=f"Your game · {relative_day(m.start, now)} {time_label(m.start)}",
                when=f"{relative_day(m.start, now)} · {range_label(m.start, m.end)}",
                court=f"Court {m.court_id[1:]}", level=level_label(m.level), players_in=len(m.player_ids),
                capacity=m.capacity, stage="ask", headline="", detail="", opportunity_id=opp_id)
            auth = auths.get(match_id)
            lead_ok = m.start - now >= timedelta(minutes=policy.timing.min_lead_time_minutes)
            if latest is not None:
                profiles = self.e.profiles()
                card.asked = [{"name": short_name(profiles[i["player_id"]].name),
                               "status": self._asked_label(i["status"], bool(i["sent_at"]))}
                              for i in self.e.storage.invites(latest["campaign_id"])]
                st = State(latest["state"])
                if st == State.CONFIRMED:
                    card.stage, card.headline = "done", "Your game is full"
                    card.detail = "The new player booked and paid, and the booking system confirmed it."
                elif st in TERMINAL_STATES:
                    card.stage, card.headline = "not_possible", "Rally couldn't fill it"
                    card.detail = "Nobody you approved could make it. Ask Rally again or find someone yourself."
                else:
                    card.stage, card.headline = "running", "Rally is asking for you"
                    card.detail = ("One player at a time, in the order you chose. Nothing is booked until "
                                   "they've paid and the booking system confirms it.")
            elif m.open_seats == 0:
                card.stage, card.headline, card.detail = "done", "Your game is full", "All 4 spots are taken."
            elif auth and auth["mode"] == "self":
                card.stage, card.headline = "self", "You're finding someone"
                card.detail = "Rally is staying out of it. Changed your mind? Ask Rally instead."
            elif auth and auth["mode"] == "rally":
                if self.e.storage.get_opportunity(opp_id) is None or not lead_ok:
                    card.stage, card.headline = "not_possible", "Too close to start time"
                    card.detail = (f"Rally needs at least {policy.timing.min_lead_time_minutes} minutes to find "
                                   "and book someone.")
                else:
                    card.stage, card.headline = "choose", "Choose who Rally can ask"
                    card.detail = "These players fit your game's level and time. Pick who may be asked, in order."
            else:
                card.stage = "ask" if lead_ok else "not_possible"
                card.headline = (f"{m.capacity - len(m.player_ids)} spot open in your game" if lead_ok
                                 else "Too close to start time")
                card.detail = ("Someone dropped out. Find your own, or should we?" if lead_ok else
                               f"Rally needs at least {policy.timing.min_lead_time_minutes} minutes to help.")
            cards.append(card)
        return cards

    @staticmethod
    def _asked_label(status: str, sent: bool) -> str:
        if not sent:
            return "next in line" if status == IS.QUEUED.value else "not asked"
        return {IS.SENT.value: "asked, waiting", IS.ACCEPTED.value: "said yes, booking",
                IS.BOOKED.value: "booked", IS.DECLINED.value: "can't make it", IS.TIMED_OUT.value: "no answer",
                IS.NEEDS_MANAGER.value: "replied, club is checking", IS.OPTED_OUT.value: "turned off invites",
                IS.CLOSED.value: "not needed"}.get(status, status.lower().replace("_", " "))

    def organiser_decide(self, pid: str, match_id: str, mode: str) -> None:
        if mode not in ("rally", "self"):
            raise PlayerError("Unknown choice.")
        if self.e.organizer_of(match_id) != pid:
            raise PlayerError("Only the game's organiser can decide this.")
        camps = self.e.storage.campaigns(f"MP-{match_id}")
        if camps and State(camps[-1]["state"]) not in TERMINAL_STATES:
            raise PlayerError("Rally is already asking players for this game.")
        with self.e.storage.connect() as conn:
            self.e.storage.set_organizer_decision(conn, match_id, pid, mode, self.e.now())
        self._log("ORGANISER_AUTHORISED" if mode == "rally" else "ORGANISER_DECLINED_HELP", pid,
                  {"match_id": match_id, "mode": mode,
                   "effect": "private match may be filled by Rally; organiser approves who is asked"
                   if mode == "rally" else "Rally stays out of this private match"},
                  opportunity_id=f"MP-{match_id}")
        self.e.refresh()

    def organiser_candidates(self, pid: str, match_id: str) -> list[dict]:
        if self.e.organizer_of(match_id) != pid:
            raise PlayerError("Only the game's organiser can see this.")
        opp_id = f"MP-{match_id}"
        if self.e.storage.get_opportunity(opp_id) is None:
            return []
        opp = self.e.opportunity(opp_id)
        _, shortlist = self.e.evaluate(opp)
        profiles = self.e.profiles()
        band = time_band(opp.start)
        out = []
        for c in shortlist.candidates + shortlist.reserve:
            p = profiles[c.player_id]
            history = []
            if pid in p.played_with:
                history.append("Played with you before")
            together = sorted(set(p.played_with) & set(opp.present_player_ids) - {pid})
            if together:
                history.append(f"Played with {', '.join(profiles[x].first_name for x in together)}")
            if band in p.preferred_bands:
                history.append(f"Usually plays {BAND_LABELS.get(band, band)}")
            if p.last_played_days_ago is not None:
                history.append(f"Last played {p.last_played_days_ago} day{'s' if p.last_played_days_ago != 1 else ''} ago")
            out.append({"player_id": c.player_id, "name": short_name(p.name), "level": level_label(p.level),
                        "rank": c.rank, "score": round(c.total), "history": history,
                        "suggested": c.rank <= self.e.policy().missing_player.default_approved_candidates})
        return out

    def organiser_approve(self, pid: str, match_id: str, ordered_ids: list[str]) -> str:
        if self.e.organizer_of(match_id) != pid:
            raise PlayerError("Only the game's organiser can approve this.")
        if not ordered_ids:
            raise PlayerError("Pick at least one player Rally may ask.")
        opp_id = f"MP-{match_id}"
        if self.e.storage.get_opportunity(opp_id) is None:
            raise PlayerError("This game can't be filled through Rally right now.")
        opp = self.e.opportunity(opp_id)
        policy = self.e.policy()
        template, source, _ = self.e.draft(opp_id)
        window_end = opp.start - timedelta(minutes=policy.timing.min_lead_time_minutes)
        organiser = self._profile(pid)
        return self.e.approve(opp_id, ordered_ids, len(ordered_ids), window_end, policy.consent.allowed_channels,
                              template, source, approver=f"organiser {organiser.name}")

    # ---- settings and consent ----------------------------------------------------------------------
    def settings(self, pid: str) -> dict:
        p = self._profile(pid)
        policy = self.e.policy()
        club_cap = policy.contact.max_invites_per_player_per_7_days
        return {"profile": p, "level": level_label(p.level),
                "bands": [{"key": b, "label": BAND_LABELS[b], "on": b in p.preferred_bands} for b in PLAYER_BANDS],
                "channels": [{"key": c, "label": CHANNEL_LABELS.get(c, c), "on": c == p.opt_in_channel}
                             for c in policy.consent.allowed_channels],
                "cap_options": list(range(1, club_cap + 1)), "club_cap": club_cap,
                "my_cap": p.invite_cap(club_cap), "invites_last_7d": p.invites_last_7d,
                "opted_in": p.opt_in_status == "opted_in", "status": p.opt_in_status}

    def save_settings(self, pid: str, bands: list[str], channel: str, max_per_week: int) -> None:
        policy = self.e.policy()
        p = self._profile(pid)
        bad = [b for b in bands if b not in PLAYER_BANDS]
        if bad:
            raise PlayerError("Unknown time band.")
        channel = channel or p.opt_in_channel or policy.consent.allowed_channels[0]
        if channel not in policy.consent.allowed_channels:
            raise PlayerError("That channel isn't available at this club.")
        club_cap = policy.contact.max_invites_per_player_per_7_days
        if not 1 <= max_per_week <= club_cap:
            raise PlayerError(f"Choose between 1 and {club_cap} invites a week (the club's limit).")
        before = {"bands": p.preferred_bands, "channel": p.opt_in_channel, "max_per_week": p.invite_cap(club_cap)}
        with self.e.storage.connect() as conn:
            self.e.storage.set_player_settings(conn, pid, self.e.now(), preferred_bands=";".join(bands),
                                               opt_in_channel=channel,
                                               max_invites_per_week=None if max_per_week == club_cap else max_per_week)
        self._log("PLAYER_SETTINGS_UPDATED", pid, {"before": before, "after": {"bands": bands, "channel": channel,
                                                                              "max_per_week": max_per_week}})

    def set_opt_in(self, pid: str, on: bool) -> None:
        p = self._profile(pid)
        policy = self.e.policy()
        status = "opted_in" if on else "opted_out"
        if p.opt_in_status == status:
            return
        with self.e.storage.connect() as conn:
            if on and not p.opt_in_channel:
                existing = self.e.storage.player_settings().get(pid, {})
                self.e.storage.set_player_settings(conn, pid, self.e.now(),
                                                   preferred_bands=existing.get("preferred_bands"),
                                                   opt_in_channel=policy.consent.allowed_channels[0],
                                                   max_invites_per_week=existing.get("max_invites_per_week"))
            self.e.storage.set_consent(conn, pid, status, self.e.now(),
                                       source="Player app opt-in (simulated)" if on else "Player app: invites off")
        self._log("CONSENT_GRANTED" if on else "CONSENT_REVOKED", pid,
                  {"from": p.opt_in_status, "to": status, "source": "player app",
                   "effect": "may be invited to games that fit" if on else "no further invites"})
        self.e.refresh()
