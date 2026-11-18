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

from .eligibility import filter_eligible
from .engine import RallyEngine, short_name
from .fmt import day_label, duration_label, level_label, range_label, relative_day, time_label
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
    fill_requests: list[dict] = field(default_factory=list)


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
            "needs_cards": [c for c in cards if c.status in ("invited", "pay", "with_club")],
            "open_cards": [c for c in cards if c.status in ("invited", "pay", "waiting_group", "paid_waiting",
                                                             "checking", "with_club")],
            "progress_cards": [c for c in cards if c.status in ("waiting_group", "paid_waiting", "checking")],
            "past_cards": [c for c in cards if c.status not in ("invited", "pay", "waiting_group", "paid_waiting",
                                                                 "checking", "with_club", "booked")],
            "organiser_cards": self.organiser_cards(pid),
            "games": self.games(pid),
            "provider_ok": self.e.provider_status == "ok",
            "free": self.free_status(pid),
            "org_requests": self.pending_requests(organiser_id=pid) if self.e.organized_by(pid) else [],
        }

    def version(self, pid: str) -> str:
        """Changes whenever anything this player can see changes (the page polls it to refresh itself)."""
        msgs = self.e.storage.messages(player_id=pid)
        camps = [(c["campaign_id"], c["state"], c["updated_at"]) for c in self.e.storage.campaigns()]
        auth = sorted(self.e.storage.organizer_authorizations().items())
        free = [(a["avail_id"], a["status"]) for a in self.e.storage.availability(self.e.now(), pid)]
        opps = sorted(r["opportunity_id"] for r in self.e.storage.opportunities() if r["active"])
        reqs = [(r["request_id"], r["status"]) for r in self.e.storage.requests()]
        reqs.append(("chat", len(self.e.storage.chat()), sum(1 for c in self.e.storage.chat() if c["status"] != "visible")))
        raw = (f"{len(msgs)}|{camps}|{auth}|{self.e.storage.get_meta('sim_now')}|{self.e.provider_status}|"
               f"{free}|{opps}|{reqs}")
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
        unread_chat: dict[str, int] = {}
        for n in self.notifications(pid):
            if n["unread"] and n["key"].startswith("c:"):
                mid = n["link"].split("/game/")[1].split("#")[0]
                unread_chat[mid] = unread_chat.get(mid, 0) + 1
        joined = {e["detail"].get("match_id") for e in self.e.storage.events()
                  if e["event_type"] in ("PLAYER_SELF_JOINED", "PLAYER_REQUEST_BOOKED")
                  and e["detail"].get("player_id") == pid}
        out = []
        for m in self.e.provider.all_matches():
            if pid in m.player_ids and m.end > now:
                out.append({"match_id": m.match_id, "when": f"{relative_day(m.start, now)} · "
                                                            f"{range_label(m.start, m.end)}",
                            "court": f"Court {m.court_id[1:]}", "players": f"{len(m.player_ids)}/{m.capacity}",
                            "organiser": self.e.organizer_of(m.match_id) == pid,
                            "via_rally": m.created_by == "rally_handoff" or m.match_id in joined,
                            "unread_chat": unread_chat.get(m.match_id, 0),
                            "time": range_label(m.start, m.end), "day": relative_day(m.start, now)})
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
            card.fill_requests = [{"request_id": r["request_id"], "name": short_name(self._profile(r["player_id"]).name),
                                   "note": r["note"] or ""}
                                  for r in self.e.storage.requests(target_id=match_id)
                                  if r["kind"] == "fill_organiser" and r["status"] == "pending"]
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
        for r in self.e.storage.requests(target_id=match_id):   # the organiser's answer also answers teammates
            if r["kind"] == "fill_organiser" and r["status"] == "pending":
                self.decide_request(r["request_id"], "acknowledge", f"organiser:{pid}", organiser_id=pid)
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


    # ---- "I'm free" (solo player looking for a game) ------------------------------------------------
    FREE_HOURS = list(range(7, 24))   # club is open 07:00-24:00 (occupancy spreadsheet)

    BAND_HOURS = {"weekday_early": (7, 10), "weekday_midday": (10, 14), "weekday_afternoon": (14, 17),
                  "weekday_evening": (17, 21), "weekday_late": (21, 24), "weekend_morning": (8, 12),
                  "weekend_afternoon": (12, 17), "weekend_evening": (17, 21)}

    def free_options(self, pid: str | None = None) -> dict:
        """Day choices for the next week, and the window to pre-fill: the player's last window, else their usual
        playing time, else the next three hours."""
        now = self.e.now()
        days = [{"value": str(i), "label": "Today" if i == 0 else "Tomorrow" if i == 1
                 else (now + timedelta(days=i)).strftime("%a %b %-d")} for i in range(7)]
        start, end = min(now.hour + 1, 23), min(now.hour + 4, 24)
        if pid:
            last = (self.e.storage.get_pref(pid, "free_start", ""), self.e.storage.get_pref(pid, "free_end", ""))
            if all(last):
                start, end = int(last[0]), int(last[1])
            else:
                bands = [b for b in self._profile(pid).preferred_bands if b in self.BAND_HOURS]
                if bands:
                    start, end = self.BAND_HOURS[bands[0]]
        day = "0" if end > now.hour + 1 else "1"
        return {"days": days, "all_hours": self.FREE_HOURS, "default_start": start, "default_end": end,
                "default_day": day, "now_hour": now.hour}

    def free_status(self, pid: str) -> dict:
        now = self.e.now()
        windows = [{"avail_id": a["avail_id"], "label": self._window_label(a["start"], a["end"])}
                   for a in self.e.storage.availability(now, pid)]
        return {"windows": windows, "games": self.games_that_fit(pid) if windows else [],
                "around": self.games_around(pid) if windows else [], "requests": self.my_requests(pid),
                **self.free_options(pid)}

    def _window_label(self, start: datetime, end: datetime) -> str:
        return f"{relative_day(start, self.e.now())} · {time_label(start)}–{time_label(end)}"

    def post_free(self, pid: str, day: str, start_hour: int, end_hour: int) -> None:
        p = self._profile(pid)
        if p.opt_in_status != "opted_in":
            raise PlayerError("Turn invites on first: saying you're free lets the club message you about games then.")
        day = {"today": "0", "tomorrow": "1"}.get(str(day), str(day))
        if day not in {str(i) for i in range(7)}:
            raise PlayerError("Pick a day in the next week.")
        if not (start_hour in self.FREE_HOURS and 7 < end_hour <= 24 and end_hour - start_hour >= 2):
            raise PlayerError("Pick a window of at least 2 hours while the club is open (7 AM to midnight).")
        now = self.e.now()
        base = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=int(day))
        start, end = base + timedelta(hours=start_hour), base + timedelta(hours=end_hour)
        if end <= now + timedelta(minutes=self.e.policy().timing.min_lead_time_minutes):
            raise PlayerError("That time has already passed, or is too soon to set up a game.")
        start = max(start, now)
        if any(a["start"] < end and start < a["end"] for a in self.e.storage.availability(now, pid)):
            raise PlayerError("You've already said you're free for part of that time.")
        with self.e.storage.connect() as conn:
            avail_id = self.e.storage.add_availability(conn, pid, start, end, now)
            self.e.storage.set_pref(conn, pid, "free_start", str(start_hour))   # remembered for next time
            self.e.storage.set_pref(conn, pid, "free_end", str(end_hour))
        self._log("PLAYER_AVAILABLE", pid, {"avail_id": avail_id, "start": start.isoformat(), "end": end.isoformat(),
                                            "effect": "moves up the list for matching games in this window; "
                                                      "invites still need approval"})
        self.e.refresh()

    def cancel_free(self, pid: str, avail_id: int) -> None:
        mine = [a for a in self.e.storage.availability(self.e.now(), pid) if a["avail_id"] == avail_id]
        if not mine:
            raise PlayerError("That's no longer active.")
        with self.e.storage.connect() as conn:
            self.e.storage.set_availability_status(conn, avail_id, "cancelled")
        self._log("PLAYER_AVAILABILITY_CANCELLED", pid, {"avail_id": avail_id})

    def games_that_fit(self, pid: str) -> list[dict]:
        """Open seats this player could take right now: club open matches only, inside one of their "I'm free"
        windows, at their level, that Rally isn't already asking specific players about."""
        now, policy = self.e.now(), self.e.policy()
        p = self._profile(pid)
        snap = self.e.snapshot
        if snap is None or self.e.provider_status != "ok" or not p.free_windows:
            return []
        live = {c["opportunity_id"] for c in self.e.storage.campaigns() if State(c["state"]) not in TERMINAL_STATES}
        out = []
        for row in self.e.storage.opportunities():
            if not row["active"] or row["type"] != OpportunityType.MISSING_PLAYER.value:
                continue
            opp = Opportunity.model_validate(row["payload"])
            if opp.organizer_name or opp.opportunity_id in live or not p.is_free(opp.start, opp.end):
                continue
            if opp.start - now < timedelta(minutes=policy.timing.min_lead_time_minutes):
                continue
            if self._join_block(opp, p, snap, policy):
                continue
            out.append({"opportunity_id": opp.opportunity_id, "court": opp.court_name,
                        "when": f"{relative_day(opp.start, now)} · {range_label(opp.start, opp.end)}",
                        "level": level_label(opp.level_target), "players": f"{len(opp.present_player_ids)} of 4",
                        "price": price_per_player(policy)})
        return sorted(out, key=lambda g: g["when"])

    @staticmethod
    def _join_block(opp: Opportunity, p, snap, policy) -> str | None:
        """Why this player can't join, using the same rules as invites. The weekly invite limit and an
        unanswered invite elsewhere don't apply: the player is asking, not being messaged."""
        res = filter_eligible(opp, [p], snap, policy)
        blocking = [x for x in res.exclusions if x.reason_code not in ("CONTACT_CAP", "PENDING_INVITE")]
        return blocking[0].detail if blocking else None

    def self_join(self, pid: str, opp_id: str) -> str:
        p = self._profile(pid)
        if not any(g["opportunity_id"] == opp_id for g in self.games_that_fit(pid)):
            raise PlayerError("That game isn't open to you any more. Here's what's left.")
        opp = self.e.opportunity(opp_id)
        if self.e.provider.health() != "ok":
            raise PlayerError("The booking system isn't answering right now. You have not been charged.")
        match = self.e.provider.get_match(opp.match_id)   # fresh check, not the cached snapshot
        if match is None or match.open_seats == 0:
            self.e.refresh()
            raise PlayerError("Someone just took that spot. You have not been charged.")
        self.e.provider.simulate_seat_filled(opp.match_id, pid, "paid")
        after = self.e.provider.get_match(opp.match_id)
        verified = after is not None and pid in after.player_ids and after.payment_status == \
            self.e.policy().booking.required_payment_status
        window = next((a for a in self.e.storage.availability(self.e.now(), pid)
                       if a["start"] <= opp.start and opp.end <= a["end"]), None)
        with self.e.storage.connect() as conn:
            if window:
                self.e.storage.set_availability_status(conn, window["avail_id"], "joined")
        self._log("PLAYER_SELF_JOINED", pid, {"match_id": opp.match_id, "verified": verified, "simulated": True,
                                              "amount_usd": price_per_player(self.e.policy()),
                                              "note": "player joined from 'I'm free'; booking system checked"},
                  opportunity_id=opp_id)
        self.e.refresh()
        if not verified:
            return "Your booking hasn't shown up as paid yet. The club will check it."
        return f"You're in: {opp.court_name}, {range_label(opp.start, opp.end)}. Booked and paid."


    # ---- "Ask about this game" (near-fits; requests go to the club or the organiser) ----------------
    OPEN_REQUEST = ("pending", "approved", "acknowledged")
    ORGANISER_KINDS = ("organiser", "fill_organiser")   # answered by the game's organiser, not the club
    FILL_KINDS = ("fill_club", "fill_organiser")        # a booked player asking for a 4th
    NEAR_LEVEL = 0.8   # near-fits shown within this many levels of the player (exact fits use the club's ±0.3)
    REQUEST_NOTE_MAX = 200

    def _request_status(self, req: dict | None, kind: str) -> dict | None:
        if req is None:
            return None
        who = "the organiser" if kind == "organiser" else "the club"
        labels = {"pending": f"Asked · waiting for {who}", "approved": f"{who.capitalize()} said yes",
                  "declined": "Not this time", "acknowledged": "The club will let you know",
                  "booked": "You're booked", "withdrawn": "You withdrew this"}
        return {"request_id": req["request_id"], "status": req["status"], "label": labels.get(req["status"], "")}

    def games_around(self, pid: str) -> list[dict]:
        """Games overlapping the player's "I'm free" windows that are not exact fits, with honest notes on why.
        The player can ask about them; the club or organiser decides."""
        now, policy = self.e.now(), self.e.policy()
        p = self._profile(pid)
        if not p.free_windows or self.e.provider_status != "ok":
            return []
        tol = policy.eligibility.skill_tolerance
        min_lead = timedelta(minutes=policy.timing.min_lead_time_minutes)
        exact = {self.e.opportunity(g["opportunity_id"]).match_id for g in self.games_that_fit(pid)}
        mine = {r["target_id"]: r for r in self.e.storage.requests(player_id=pid)}
        matches = self.e.provider.all_matches()
        busy = [(m.start, m.end) for m in matches if pid in m.player_ids]

        def overlap_notes(start, end):
            win = next(((ws, we) for ws, we in p.free_windows if start < we and ws < end), None)
            if win is None:
                return None
            ws, we = win
            notes = []
            if start < ws:
                notes.append(f"starts before you're free ({time_label(start)})")
            if end > we:
                notes.append(f"ends {duration_label(end - we)} after you're free")
            return notes

        out = []
        for m in matches:
            organiser = self.e.organizer_of(m.match_id) if m.kind == "private" else None
            if m.open_seats == 0 or (m.kind != "club_open" and not organiser) or m.match_id in exact:
                continue
            if pid in m.player_ids or pid in m.dropout_player_ids or organiser == pid:
                continue
            if m.start <= now + timedelta(minutes=10) or any(bs < m.end and m.start < be for bs, be in busy):
                continue
            notes = overlap_notes(m.start, m.end)
            if notes is None:
                continue
            if m.level is not None and abs(round(p.level - m.level, 2)) > self.NEAR_LEVEL + 1e-9:
                continue
            if m.level is not None and abs(round(p.level - m.level, 2)) > tol + 1e-9:
                notes.insert(0, f"level {level_label(m.level)}, you're {level_label(p.level)}")
            if m.start - now < min_lead:
                notes.append(f"starts in {duration_label(m.start - now)}")
            kind = "organiser" if organiser else "open_match"
            if not notes and kind == "open_match":
                notes.append("the club is already asking players about this one")
            if kind == "organiser":
                notes.append(f"{short_name(self._profile(organiser).name)}'s private game")
            req = mine.get(m.match_id)
            req = req if req and req["status"] != "withdrawn" else None
            out.append({"target_id": m.match_id, "kind": kind, "court": f"Court {m.court_id[1:]}",
                        "when": f"{relative_day(m.start, now)} · {range_label(m.start, m.end)}",
                        "level": level_label(m.level) if m.level is not None else "any",
                        "players": f"{len(m.player_ids)} of {m.capacity} in", "notes": notes, "start": m.start,
                        "request": self._request_status(req, kind), "action": "Ask about this game"})
        for row in self.e.storage.opportunities():
            if not row["active"] or row["type"] != OpportunityType.EMPTY_COURT.value:
                continue
            opp = Opportunity.model_validate(row["payload"])
            if opp.start <= now + timedelta(minutes=10) or any(bs < opp.end and opp.start < be for bs, be in busy):
                continue
            notes = overlap_notes(opp.start, opp.end)
            if notes is None:
                continue
            notes.insert(0, "not a game yet: it needs 4 players")
            req = mine.get(opp.opportunity_id)
            req = req if req and req["status"] != "withdrawn" else None
            out.append({"target_id": opp.opportunity_id, "kind": "empty_court", "court": opp.court_name,
                        "when": f"{relative_day(opp.start, now)} · {range_label(opp.start, opp.end)}",
                        "level": "open", "players": "empty court", "notes": notes, "start": opp.start,
                        "request": self._request_status(req, "empty_court"), "action": "I'm interested"})
        return sorted(out, key=lambda g: g["start"])

    def ask_about(self, pid: str, target_id: str, note: str = "") -> None:
        p = self._profile(pid)
        if p.opt_in_status != "opted_in":
            raise PlayerError("Turn invites on first, so the club can reply to you.")
        note = (note or "").strip()
        if len(note) > self.REQUEST_NOTE_MAX:
            raise PlayerError(f"Please keep the note under {self.REQUEST_NOTE_MAX} characters.")
        game = next((g for g in self.games_around(pid) if g["target_id"] == target_id), None)
        if game is None:
            raise PlayerError("That game isn't available to ask about any more.")
        if game["request"] and game["request"]["status"] in self.OPEN_REQUEST:
            raise PlayerError("You've already asked about this game.")
        now = self.e.now()
        with self.e.storage.connect() as conn:
            rid = self.e.storage.add_request(conn, pid, target_id, game["kind"], note, now)
        self._log("PLAYER_JOIN_REQUEST", pid, {"request_id": rid, "target_id": target_id, "kind": game["kind"],
                                               "notes": game["notes"], "note": note,
                                               "goes_to": "organiser" if game["kind"] == "organiser" else "club"},
                  opportunity_id=target_id if game["kind"] == "empty_court" else f"MP-{target_id}")

    def withdraw_request(self, pid: str, request_id: int) -> None:
        req = self.e.storage.get_request(request_id)
        if req is None or req["player_id"] != pid or req["status"] not in ("pending", "acknowledged", "approved"):
            raise PlayerError("There's nothing to withdraw.")
        with self.e.storage.connect() as conn:
            self.e.storage.set_request(conn, request_id, "withdrawn", self.e.now())
        self._log("PLAYER_JOIN_REQUEST_WITHDRAWN", pid, {"request_id": request_id})

    def my_requests(self, pid: str) -> list[dict]:
        """Requests the player made that are still worth showing (open, or decided in the last day)."""
        out = []
        for r in self.e.storage.requests(player_id=pid):
            if r["status"] == "withdrawn" or r["kind"] in self.FILL_KINDS:
                continue
            info = self._target_info(r)
            if info is None or info["start"] <= self.e.now():
                continue
            answer_by = None
            if r["status"] == "pending" and r["kind"] not in self.ORGANISER_KINDS:
                answer_by = time_label(datetime.fromisoformat(r["created_at"])
                                       + timedelta(minutes=self.e.policy().club.request_reply_minutes))
            out.append({**info, **self._request_status(r, r["kind"]), "kind": r["kind"], "note": r["note"] or "",
                        "answer_by": answer_by,
                        "can_pay": r["status"] == "approved" and r["kind"] != "empty_court",
                        "price": price_per_player(self.e.policy())})
        return out

    def _target_info(self, r: dict) -> dict | None:
        now = self.e.now()
        if r["kind"] == "empty_court":
            row = self.e.storage.get_opportunity(r["target_id"])
            if row is None:
                return None
            o = Opportunity.model_validate(row["payload"])
            return {"court": o.court_name, "when": f"{relative_day(o.start, now)} · {range_label(o.start, o.end)}",
                    "start": o.start, "level": "open", "players": "empty court"}
        m = self.e.provider.get_match(r["target_id"]) if self.e.provider_status == "ok" else None
        if m is None:
            return None
        return {"court": f"Court {m.court_id[1:]}", "when": f"{relative_day(m.start, now)} · {range_label(m.start, m.end)}",
                "start": m.start, "level": level_label(m.level) if m.level is not None else "any",
                "players": f"{len(m.player_ids)} of {m.capacity} in"}

    def pending_requests(self, organiser_id: str | None = None) -> list[dict]:
        """Open requests for the club (organiser_id None: open matches and empty courts) or one organiser."""
        profiles = self.e.profiles()
        out = []
        for r in self.e.storage.requests():
            if r["status"] not in ("pending", "acknowledged"):
                continue
            if organiser_id is None and r["kind"] in self.ORGANISER_KINDS:
                continue
            if organiser_id is not None and (r["kind"] != "organiser"
                                             or self.e.organizer_of(r["target_id"]) != organiser_id):
                continue   # fill requests show inside the organiser's own game card instead
            info = self._target_info(r)
            if info is None or info["start"] <= self.e.now():
                continue
            p = profiles[r["player_id"]]
            waited = int((self.e.now() - datetime.fromisoformat(r["created_at"])).total_seconds() // 60)
            out.append({**info, "request_id": r["request_id"], "kind": r["kind"], "status": r["status"],
                        "waited_min": waited, "target_min": self.e.policy().club.request_reply_minutes,
                        "note": r["note"] or "", "player_id": p.player_id,
                        "name": p.name if organiser_id is None else short_name(p.name),
                        "player_level": level_label(p.level), "asked_at": time_label(datetime.fromisoformat(r["created_at"]))})
        return out

    def decide_request(self, request_id: int, decision: str, decided_by: str, organiser_id: str | None = None) -> str:
        """decision: approve | decline | acknowledge. The club decides open matches and empty courts; only the
        game's organiser decides requests for their private game."""
        r = self.e.storage.get_request(request_id)
        if r is None or r["status"] not in ("pending", "acknowledged"):
            raise PlayerError("That request has already been answered.")
        if r["kind"] in self.ORGANISER_KINDS:
            if organiser_id is None or self.e.organizer_of(r["target_id"]) != organiser_id:
                raise PlayerError("Only the game's organiser can answer this request.")
        elif organiser_id is not None:
            raise PlayerError("The club answers this request.")
        allowed = {"empty_court": ("acknowledge", "decline"), "fill_club": ("acknowledge", "decline"),
                   "fill_organiser": ("acknowledge", "decline")}.get(r["kind"], ("approve", "decline"))
        if decision not in allowed:
            raise PlayerError("That choice isn't available for this request.")
        now, policy = self.e.now(), self.e.policy()
        p = self._profile(r["player_id"])
        info = self._target_info(r)
        if info is None or info["start"] <= now:
            raise PlayerError("That game has already started or gone.")
        if decision == "approve":
            m = self.e.provider.get_match(r["target_id"])
            if self.e.provider.health() != "ok" or m is None or m.open_seats == 0:
                raise PlayerError("The spot isn't open any more, so there's nothing to approve.")
        status = {"approve": "approved", "decline": "declined", "acknowledge": "acknowledged"}[decision]
        when = f"{info['court']} {info['when'].split(' · ')[-1]}"
        fill = r["kind"] in self.FILL_KINDS
        decider = "the organiser" if r["kind"] == "fill_organiser" else "the club"
        body = {"approved": (f"Good news, {p.first_name}: you can join the game on {when}. Book and pay your "
                             f"${price_per_player(policy)} share in Rally; your spot is confirmed once it shows as paid."),
                "declined": (f"Thanks for asking about {when}, {p.first_name}. It's not a fit this time, but you'll "
                             "hear about other games at your level."),
                "acknowledged": (f"Thanks {p.first_name}! We've noted you're interested in {when}. If a group comes "
                                 "together, you'll get an invite.")}[status]
        if fill:
            body = (f"Thanks {p.first_name}: {decider} is finding a 4th for {when}. You'll get an alert when the "
                    "spot is filled." if status == "acknowledged" else
                    f"Thanks for flagging {when}, {p.first_name}. {decider.capitalize()} will sort the open spot "
                    "another way (for example at the front desk).")
        with self.e.storage.connect() as conn:
            self.e.storage.set_request(conn, request_id, status, now, decided_by=decided_by)
            self.e.messaging.send(conn, campaign_id=None, invite_id=None, player_id=p.player_id,
                                  channel=p.opt_in_channel, kind=f"request_{status}", body=body, now=now)
        self.e._log("JOIN_REQUEST_" + status.upper(), actor=decided_by, tool="player_requests",
                    detail={"request_id": request_id, "player_id": p.player_id, "target_id": r["target_id"],
                            "kind": r["kind"], "simulated_message": True})
        if fill:
            return f"{p.first_name} has been told." if status == "declined" else \
                f"{p.first_name} knows you're on it."
        return {"approved": f"{p.first_name} can now book and pay.", "declined": f"{p.first_name} has been told.",
                "acknowledged": f"{p.first_name} knows you'll be in touch."}[status]

    def pay_request(self, pid: str, request_id: int) -> str:
        r = self.e.storage.get_request(request_id)
        if r is None or r["player_id"] != pid or r["status"] != "approved" or r["kind"] == "empty_court":
            raise PlayerError("There's nothing to pay for here.")
        if self.e.provider.health() != "ok":
            raise PlayerError("The booking system isn't answering right now. You have not been charged.")
        m = self.e.provider.get_match(r["target_id"])
        if m is None or m.start <= self.e.now():
            raise PlayerError("That game has already started.")
        if m.open_seats == 0 and pid not in m.player_ids:
            raise PlayerError("Sorry, the spot was just taken. You have not been charged.")
        self.e.provider.simulate_seat_filled(m.match_id, pid, "paid")
        after = self.e.provider.get_match(m.match_id)
        verified = after is not None and pid in after.player_ids and \
            after.payment_status == self.e.policy().booking.required_payment_status
        now = self.e.now()
        window = next((a for a in self.e.storage.availability(now, pid)
                       if a["start"] < m.end and m.start < a["end"]), None)
        with self.e.storage.connect() as conn:
            if verified:
                self.e.storage.set_request(conn, request_id, "booked", now)
            if window:
                self.e.storage.set_availability_status(conn, window["avail_id"], "joined")
        self._log("PLAYER_REQUEST_BOOKED", pid, {"request_id": request_id, "match_id": m.match_id,
                                                 "verified": verified, "simulated": True})
        self.e.refresh()
        return "You're in: booked and paid." if verified else "Your booking hasn't shown as paid yet."


    # ---- game page and team chat (booked players only) ----------------------------------------------
    CHAT_MAX = 500

    def _booked_match(self, pid: str, match_id: str):
        """The match, if this player is booked and paid in it; otherwise PlayerError. Chat and teammates are
        only for players actually booked in the game."""
        if self.e.provider_status != "ok":
            raise PlayerError("The booking system isn't answering right now, so we can't open this game.")
        m = self.e.provider.get_match(match_id)
        required = self.e.policy().booking.required_payment_status
        if m is None or pid not in m.player_ids or m.payment_status != required:
            raise PlayerError("This game page is only for players booked in the game.")
        return m

    def chat_open(self, m) -> bool:
        hours = self.e.policy().club.chat_open_hours_after
        return self.e.now() <= m.end + timedelta(hours=hours)

    def game_page(self, pid: str, match_id: str) -> dict:
        m = self._booked_match(pid, match_id)
        policy, now = self.e.policy(), self.e.now()
        profiles = self.e.profiles()
        team = [{"player_id": x, "name": "You" if x == pid else short_name(profiles[x].name),
                 "level": level_label(profiles[x].level), "organiser": self.e.organizer_of(match_id) == x}
                for x in m.player_ids if x in profiles]
        names = {x: ("You" if x == pid else short_name(profiles[x].name)) for x in m.player_ids if x in profiles}
        msgs = []
        for c in self.e.storage.chat(match_id):
            hidden = c["status"] == "hidden"
            msgs.append({"msg_id": c["msg_id"], "mine": c["player_id"] == pid,
                         "name": names.get(c["player_id"], "A former player"),
                         "body": "This message was hidden by the club." if hidden else c["body"],
                         "hidden": hidden, "ts": time_label(datetime.fromisoformat(c["sim_ts"])),
                         "reported_by_me": c["reported_by"] == pid and c["status"] == "reported",
                         "can_report": c["player_id"] != pid and c["status"] == "visible"})
        open_ = self.chat_open(m)
        club_msgs = []
        for c in self.e.storage.campaigns():
            if self.e.storage.get_invite(f"{c['campaign_id']}:{pid}") is None:
                continue
            o = self.e.opportunity(c["opportunity_id"])
            if o.match_id == match_id or (o.type == OpportunityType.EMPTY_COURT and o.court_id == m.court_id
                                          and o.start == m.start):
                club_msgs += [{"dir": x["direction"], "body": x["body"],
                               "ts": time_label(datetime.fromisoformat(x["sim_ts"]))}
                              for x in self.e.storage.messages(c["campaign_id"], pid)]
        return {"club_messages": club_msgs, "fill": self.fill_status(pid, m), "cancel": self.cancel_terms(pid, m),
                "can_drop": m.start > now and self.e.organizer_of(match_id) != pid,
                "open_spots": max(0, m.capacity - len(m.player_ids)),
                "match_id": match_id, "court": f"Court {m.court_id[1:]}", "club": policy.club.name,
                "address": policy.club.address, "when": f"{relative_day(m.start, now)} · {range_label(m.start, m.end)}",
                "day": day_label(m.start), "level": level_label(m.level) if m.level is not None else "mixed",
                "team": team, "spots": f"{len(m.player_ids)} of {m.capacity}", "messages": msgs,
                "chat_open": open_, "chat_closes": time_label(m.end + timedelta(hours=policy.club.chat_open_hours_after)),
                "started": m.start <= now, "price": price_per_player(policy),
                "muted": match_id in self.e.storage.muted(pid)}

    def post_chat(self, pid: str, match_id: str, body: str) -> None:
        m = self._booked_match(pid, match_id)
        if not self.chat_open(m):
            raise PlayerError("This game's chat has closed.")
        body = (body or "").strip()
        if not body:
            raise PlayerError("Write a message first.")
        if len(body) > self.CHAT_MAX:
            raise PlayerError(f"Please keep messages under {self.CHAT_MAX} characters.")
        with self.e.storage.connect() as conn:
            msg_id = self.e.storage.add_chat(conn, match_id, pid, body, self.e.now())
        # the log records that a message was posted, never what it said
        self._log("GAME_CHAT_POSTED", pid, {"match_id": match_id, "msg_id": msg_id, "length": len(body)})
        self._chat_texts(pid, m)

    CHAT_TEXT_GAP = timedelta(minutes=30)

    def _chat_texts(self, author: str, m) -> None:
        """Simulated text to teammates about a new chat message: never the message itself, at most one per game
        every 30 minutes, not in the club's quiet hours, not if muted or switched off, only if invites are on."""
        from .coordinator import Coordinator
        now, policy = self.e.now(), self.e.policy()
        if Coordinator._in_quiet_hours(policy, now):
            return
        profiles = self.e.profiles()
        who = short_name(profiles[author].name) if author in profiles else "A teammate"
        court = f"Court {m.court_id[1:]}"
        tag = f"chat:{m.match_id}:"
        latest = self.e.storage.chat(m.match_id)[-1]["msg_id"]
        for t in m.player_ids:
            p = profiles.get(t)
            if t == author or p is None or p.opt_in_status != "opted_in" or not p.opt_in_channel:
                continue
            if self.e.storage.get_pref(t, "chat_texts", "on") != "on" or m.match_id in self.e.storage.muted(t):
                continue
            recent = [x for x in self.e.storage.messages(player_id=t) if x["kind"] == "chat_alert"
                      and (x["invite_id"] or "").startswith(tag)
                      and now - datetime.fromisoformat(x["sim_ts"]) < self.CHAT_TEXT_GAP]
            if recent:
                continue
            with self.e.storage.connect() as conn:
                self.e.messaging.send(conn, campaign_id=None, invite_id=f"{tag}{latest}", player_id=t,
                                      channel=p.opt_in_channel,
                                      kind="chat_alert", now=now,
                                      body=f"Rally: new message from {who} in your {court} game chat. "
                                           "Open the app to read it.")

    def report_chat(self, pid: str, match_id: str, msg_id: int) -> None:
        self._booked_match(pid, match_id)
        c = self.e.storage.get_chat(msg_id)
        if c is None or c["match_id"] != match_id or c["player_id"] == pid or c["status"] != "visible":
            raise PlayerError("That message can't be reported.")
        with self.e.storage.connect() as conn:
            self.e.storage.report_chat(conn, msg_id, pid, self.e.now())
        self._log("GAME_CHAT_REPORTED", pid, {"match_id": match_id, "msg_id": msg_id, "author": c["player_id"]})

    def reported_chat(self) -> list[dict]:
        """For the club: reported messages waiting for review, with the game and both players."""
        profiles = self.e.profiles()
        out = []
        for c in self.e.storage.chat(status="reported"):
            m = self.e.provider.get_match(c["match_id"]) if self.e.provider_status == "ok" else None
            out.append({"msg_id": c["msg_id"], "body": c["body"], "match_id": c["match_id"],
                        "game": (f"Court {m.court_id[1:]}, {range_label(m.start, m.end)}" if m else c["match_id"]),
                        "author": profiles[c["player_id"]].name if c["player_id"] in profiles else c["player_id"],
                        "reporter": profiles[c["reported_by"]].name if c["reported_by"] in profiles else c["reported_by"],
                        "ts": time_label(datetime.fromisoformat(c["sim_ts"]))})
        return out

    def review_chat(self, msg_id: int, decision: str, reviewer: str) -> str:
        c = self.e.storage.get_chat(msg_id)
        if c is None or c["status"] != "reported":
            raise PlayerError("That message has already been reviewed.")
        if decision not in ("hide", "keep"):
            raise PlayerError("Choose hide or keep.")
        status = "hidden" if decision == "hide" else "kept"
        with self.e.storage.connect() as conn:
            self.e.storage.review_chat(conn, msg_id, status, reviewer)
        self.e._log("GAME_CHAT_REVIEWED", actor=reviewer, tool="game_chat",
                    detail={"msg_id": msg_id, "match_id": c["match_id"], "decision": status})
        return "Hidden for everyone in the game." if status == "hidden" else "Kept; the report is closed."


    # ---- notifications ------------------------------------------------------------------------------
    MESSAGE_TITLES = {
        "invite": "New game invite", "booking_link": "You're in: book and pay your share",
        "booking_link_group": "Your group is set: book and pay", "waitlist": "You're on the waitlist",
        "seat_gone": "That spot was filled", "court_gone": "That game is off", "opt_out_ack": "Invites turned off",
        "request_approved": "Good news: you can join", "request_declined": "Your request was answered",
        "request_acknowledged": "The club noted your interest", "dropout_ack": "You've left the game",
    }

    def notifications(self, pid: str) -> list[dict]:
        """Everything this player should hear about, newest first. Built from what already happened (messages,
        chat, requests, the event log), so it can't drift out of sync."""
        self._profile(pid)
        now = self.e.now()
        profiles = self.e.profiles()
        seen = self.e.storage.seen_keys(pid)
        items: list[dict] = []

        def add(key, ts, icon, title, body, link, texted=False):
            items.append({"key": key, "ts": ts, "icon": icon, "title": title, "body": body, "link": link,
                          "texted": texted, "unread": key not in seen,
                          "when": time_label(datetime.fromisoformat(ts))})

        texted_chats = {x["invite_id"] for x in self.e.storage.messages(player_id=pid) if x["kind"] == "chat_alert"}
        for msg in self.e.storage.messages(player_id=pid):
            if msg["direction"] != "out" or msg["kind"] == "chat_alert":
                continue
            title = self.MESSAGE_TITLES.get(msg["kind"], "Message from the club")
            body = msg["body"] if len(msg["body"]) <= 160 else msg["body"][:157] + "…"
            add(f"m:{msg['message_id']}", msg["sim_ts"], "📨", title, body, f"/p/{pid}", texted=True)

        # chat from teammates in games this player is booked in (not muted, not hidden)
        if self.e.provider_status == "ok":
            muted = self.e.storage.muted(pid)
            for m in self.e.provider.all_matches():
                if pid not in m.player_ids or m.match_id in muted:
                    continue
                for c in self.e.storage.chat(m.match_id):
                    if c["player_id"] == pid or c["status"] == "hidden":
                        continue
                    who = short_name(profiles[c["player_id"]].name) if c["player_id"] in profiles else "A teammate"
                    body = c["body"] if len(c["body"]) <= 120 else c["body"][:117] + "…"
                    add(f"c:{c['msg_id']}", c["sim_ts"], "💬", f"{who} · Court {m.court_id[1:]} chat", body,
                        f"/p/{pid}/game/{m.match_id}#chat",
                        texted=f"chat:{m.match_id}:{c['msg_id']}" in texted_chats)

        # organiser: requests to join and Rally's progress on their game
        mine = set(self.e.organized_by(pid))
        if mine:
            for r in self.e.storage.requests():
                if r["kind"] == "organiser" and r["target_id"] in mine and r["player_id"] in profiles:
                    p = profiles[r["player_id"]]
                    add(f"r:{r['request_id']}", r["created_at"], "🙋", f"{short_name(p.name)} asked to join your game",
                        f"Level {level_label(p.level)}" + (f" · “{r['note']}”" if r["note"] else ""), f"/p/{pid}")
                elif r["kind"] == "fill_organiser" and r["target_id"] in mine and r["player_id"] in profiles:
                    add(f"r:{r['request_id']}", r["created_at"], "🆘",
                        f"{short_name(profiles[r['player_id']].name)} asked you to find a 4th",
                        "Your game has an open spot. Tap to choose who Rally can ask.", f"/p/{pid}")
            camps = {c["campaign_id"]: c for c in self.e.storage.campaigns()
                     if c["opportunity_id"] in {f"MP-{x}" for x in mine}}
            for ev in self.e.storage.events():
                if ev["campaign_id"] not in camps:
                    continue
                d = ev["detail"] or {}
                who = short_name(profiles[d["player_id"]].name) if d.get("player_id") in profiles else "A player"
                if ev["event_type"] == "INVITE_SENT":
                    add(f"e:{ev['event_id']}", ev["sim_ts"], "📣", f"Rally asked {who}", "Waiting for their answer.",
                        f"/p/{pid}")
                elif ev["event_type"] == "REPLY_DECLINED":
                    add(f"e:{ev['event_id']}", ev["sim_ts"], "↩️", f"{who} can't make it",
                        "Rally is asking the next player you chose, if there is one.", f"/p/{pid}")
                elif ev["event_type"] == "STATE_CHANGED" and ev["to_state"] == State.CONFIRMED.value:
                    add(f"e:{ev['event_id']}", ev["sim_ts"], "🎾", "Your game is full",
                        "The new player booked and paid.", f"/p/{pid}")
        # teammates (and the organiser) hear when someone in their game drops out
        if self.e.provider_status == "ok":
            my_games = {m.match_id: m for m in self.e.provider.all_matches() if pid in m.player_ids or m.match_id in mine}
            for ev in self.e.storage.events():
                d = ev["detail"] or {}
                if ev["event_type"] == "PLAYER_DROPPED_OUT" and d.get("match_id") in my_games and d.get("player_id") != pid:
                    who = short_name(profiles[d["player_id"]].name) if d.get("player_id") in profiles else "A player"
                    g = my_games[d["match_id"]]
                    add(f"e:{ev['event_id']}", ev["sim_ts"], "🚪", f"{who} can't make it anymore",
                        f"Court {g.court_id[1:]}, {range_label(g.start, g.end)} has an open spot. Tap to find a 4th.",
                        f"/p/{pid}/game/{d['match_id']}" if pid in g.player_ids else f"/p/{pid}")
        # every booked player (not just the organiser) hears when an open spot in their game is filled
        if self.e.provider_status == "ok":
            booked_in = {m.match_id for m in self.e.provider.all_matches() if pid in m.player_ids} - mine
            camps = {c["campaign_id"]: c for c in self.e.storage.campaigns()
                     if c["opportunity_id"] in {f"MP-{x}" for x in booked_in}}
            for ev in self.e.storage.events():
                if (ev["campaign_id"] in camps and ev["event_type"] == "STATE_CHANGED"
                        and ev["to_state"] == State.CONFIRMED.value):
                    newcomer = [i["player_id"] for i in self.e.storage.invites(ev["campaign_id"])
                                if i["status"] in (IS.ACCEPTED.value, IS.BOOKED.value)]
                    if pid not in newcomer:
                        add(f"e:{ev['event_id']}", ev["sim_ts"], "🎾", "Your game is full",
                            "The open spot was filled. See who's playing.",
                            f"/p/{pid}/game/{camps[ev['campaign_id']]['opportunity_id'][3:]}")
        return sorted(items, key=lambda x: (x["ts"], x["key"]), reverse=True)

    def unread_count(self, pid: str) -> int:
        return sum(1 for n in self.notifications(pid) if n["unread"])

    def latest_unread(self, pid: str) -> dict | None:
        return next((n for n in self.notifications(pid) if n["unread"]), None)

    def mark_all_read(self, pid: str) -> None:
        keys = [n["key"] for n in self.notifications(pid) if n["unread"]]
        if keys:
            with self.e.storage.connect() as conn:
                self.e.storage.mark_seen(conn, pid, keys)

    def set_chat_texts(self, pid: str, on: bool) -> None:
        self._profile(pid)
        with self.e.storage.connect() as conn:
            self.e.storage.set_pref(conn, pid, "chat_texts", "on" if on else "off")
        self._log("PLAYER_SETTINGS_UPDATED", pid, {"chat_texts": "on" if on else "off"})

    def chat_texts_on(self, pid: str) -> bool:
        return self.e.storage.get_pref(pid, "chat_texts", "on") == "on"

    def set_mute(self, pid: str, match_id: str, on: bool) -> None:
        self._booked_match(pid, match_id)
        with self.e.storage.connect() as conn:
            self.e.storage.set_mute(conn, pid, match_id, on)
        self._log("GAME_CHAT_MUTED" if on else "GAME_CHAT_UNMUTED", pid, {"match_id": match_id})


    # ---- a booked player asks for a 4th (someone dropped out) ----------------------------------------
    def fill_status(self, pid: str, m) -> dict | None:
        """What a booked player can do about an open spot in their game, or None if the game is full."""
        now, policy = self.e.now(), self.e.policy()
        if m.open_seats == 0 or m.start <= now:
            return None
        organiser = self.e.organizer_of(m.match_id) if m.kind == "private" else None
        org_name = short_name(self._profile(organiser).name) if organiser else None
        live = [c for c in self.e.storage.campaigns(f"MP-{m.match_id}") if State(c["state"]) not in TERMINAL_STATES]
        if live:
            asked = sum(1 for i in self.e.storage.invites(live[-1]["campaign_id"]) if i["sent_at"])
            who = "Rally" if not organiser else f"{org_name} and Rally"
            return {"state": "finding", "title": f"{who} {'are' if organiser else 'is'} finding a 4th",
                    "detail": f"{asked} player{'s' if asked != 1 else ''} asked so far. You'll get an alert when the "
                              "spot is filled."}
        if organiser == pid:
            return {"state": "organiser", "title": "Your game has an open spot",
                    "detail": "Go to Games and tap Find someone for me, or find someone yourself."}
        kind = "fill_club" if m.kind == "club_open" else ("fill_organiser" if organiser else None)
        if kind is None:
            return {"state": "none", "title": "This game has an open spot",
                    "detail": "Ask whoever booked the court; Rally can only help with club games and organised games."}
        mine = [r for r in self.e.storage.requests(player_id=pid, target_id=m.match_id)
                if r["kind"] == kind and r["status"] in ("pending", "acknowledged")]
        decider = org_name if organiser else "the club"
        if mine:
            r = mine[-1]
            by = time_label(datetime.fromisoformat(r["created_at"])
                            + timedelta(minutes=policy.club.request_reply_minutes))
            return {"state": "asked", "title": f"You asked {decider} to find a 4th",
                    "detail": ("They're on it. You'll get an alert when the spot is filled." if r["status"] == "acknowledged"
                               else (f"The club aims to answer by {by}." if not organiser
                                     else f"{org_name} has been alerted."))}
        if m.start - now < timedelta(minutes=policy.timing.min_lead_time_minutes):
            return {"state": "too_soon", "title": "This game has an open spot",
                    "detail": f"It's too close to start for Rally to find someone. Ask at the front desk."}
        return {"state": "can_ask", "kind": kind, "title": "This game has an open spot",
                "detail": f"Someone dropped out. {decider.capitalize()} can find a 4th at your level.",
                "action": f"Ask {decider} to find a 4th"}

    def request_fill(self, pid: str, match_id: str, note: str = "") -> None:
        m = self._booked_match(pid, match_id)
        status = self.fill_status(pid, m)
        if not status or status["state"] != "can_ask":
            raise PlayerError(status["detail"] if status else "Your game is already full.")
        note = (note or "").strip()[: self.REQUEST_NOTE_MAX]
        with self.e.storage.connect() as conn:
            rid = self.e.storage.add_request(conn, pid, match_id, status["kind"], note, self.e.now())
        self._log("PLAYER_FILL_REQUEST", pid, {"request_id": rid, "match_id": match_id, "kind": status["kind"],
                                               "goes_to": "organiser" if status["kind"] == "fill_organiser" else "club"},
                  opportunity_id=f"MP-{match_id}")
        self.e.refresh()


    # ---- "I can't make it anymore" (with the club's late-cancellation fee) ----------------------------
    def cancel_terms(self, pid: str, m) -> dict:
        c = self.e.policy().cancellation
        free_until = m.start - timedelta(hours=c.free_until_hours_before)
        late = self.e.now() > free_until
        return {"free_until": f"{day_label(free_until)} {time_label(free_until)}", "late": late,
                "fee": c.late_fee_usd if late else 0.0, "fee_label": f"${c.late_fee_usd:.0f}",
                "rule": (f"Free to cancel until {c.free_until_hours_before:.0f} hours before the game; after that "
                         f"there's a ${c.late_fee_usd:.0f} late-cancellation fee.")}

    def drop_out(self, pid: str, match_id: str) -> str:
        m = self._booked_match(pid, match_id)
        now = self.e.now()
        if m.start <= now:
            raise PlayerError("This game has already started.")
        if self.e.organizer_of(match_id) == pid:
            raise PlayerError("You organise this game. Please ask the club to change or cancel it.")
        terms = self.cancel_terms(pid, m)
        p = self._profile(pid)
        self.e.provider.simulate_player_dropout(match_id, pid, f"{p.name} dropped out at {time_label(now)}")
        with self.e.storage.connect() as conn:
            body = (f"You've left the game on Court {m.court_id[1:]}, {range_label(m.start, m.end)}. "
                    + (f"A {terms['fee_label']} late-cancellation fee applies (simulated)." if terms["late"]
                       else "No fee: you cancelled in time."))
            self.e.messaging.send(conn, campaign_id=None, invite_id=None, player_id=pid, channel=p.opt_in_channel,
                                  kind="dropout_ack", body=body, now=now)
        self._log("PLAYER_DROPPED_OUT", pid, {"match_id": match_id, "late": terms["late"],
                                              "effect": "seat freed in the booking system; teammates alerted"},
                  opportunity_id=f"MP-{match_id}")
        if terms["late"]:
            self._log("LATE_CANCEL_FEE", pid, {"match_id": match_id, "amount_usd": terms["fee"], "simulated": True,
                                               "rule": terms["rule"]}, opportunity_id=f"MP-{match_id}")
        self.e.refresh()
        return body
