"""Player app rules (docs/PLAYER_APP_ARCHITECTURE.md) and the G16 backup-wave fix. Offline, no API key."""

from datetime import timedelta

import pytest

from conftest import EC_MAIN, MP_MAIN, approve, get_view, invites, state
from rally.player_side import PlayerError, PlayerSide

PRIV = "M-PRIV-1900-C5"   # private 7:00 PM game on Court 5, 3/4, organiser P015 (Elif)
ORG = "P015"


@pytest.fixture
def side(engine):
    return PlayerSide(engine)


def _opp_ids(engine):
    return {row["opportunity_id"] for row in engine.storage.opportunities() if row["active"]}


def test_private_match_needs_the_organisers_ask(engine, side):
    assert f"MP-{PRIV}" not in _opp_ids(engine)
    assert any(n.ref == PRIV and n.reason_code == "PRIVATE_MATCH_EXCLUDED" for n in engine.not_actionable)
    side.organiser_decide(ORG, PRIV, "self")              # "I'll find someone": Rally stays out
    assert f"MP-{PRIV}" not in _opp_ids(engine)
    side.organiser_decide(ORG, PRIV, "rally")             # "Find someone for me"
    assert f"MP-{PRIV}" in _opp_ids(engine)
    assert engine.opportunity(f"MP-{PRIV}").organizer_name == "Elif Y."


def test_only_the_organiser_can_authorise_or_approve(engine, side):
    with pytest.raises(PlayerError):
        side.organiser_decide("P014", PRIV, "rally")      # a player in the game, but not its organiser
    side.organiser_decide(ORG, PRIV, "rally")
    with pytest.raises(PlayerError):
        side.organiser_approve("P014", PRIV, ["P038"])


def test_organiser_approval_is_the_campaign_scope(engine, side):
    side.organiser_decide(ORG, PRIV, "rally")
    cands = side.organiser_candidates(ORG, PRIV)
    picked = [cands[0]["player_id"]]
    cid = side.organiser_approve(ORG, PRIV, picked)
    camp = engine.storage.get_campaign(cid)
    assert camp["approval"]["approved_by"].startswith("organiser ")
    outsider = next(c["player_id"] for c in cands[1:])
    res = engine.coordinator.dispatch_invite(cid, outsider, engine.now(), actor="test")
    assert not res.sent and "approved candidate" in res.reason
    body = engine.storage.messages(cid, picked[0])[0]["body"]
    assert "Elif Y." in body and "Yilmaz" not in body    # organiser shown as first name + initial only


def test_player_cannot_touch_someone_elses_invite(engine, side):
    cid = approve(engine, MP_MAIN, n=2)
    first, second = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"]
    with pytest.raises(PlayerError):
        side.reply(second, f"{cid}:{first}", "yes")
    with pytest.raises(PlayerError):
        side.reply(second, f"{cid}:{second}", "yes")      # not sent to them yet (sequential)


def test_pay_only_after_a_clear_yes_and_confirm_only_via_provider(engine, side):
    cid = approve(engine, MP_MAIN, n=1)
    pid = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"][0]
    with pytest.raises(PlayerError):
        side.pay(pid, f"{cid}:{pid}")                     # no yes, no booking link yet
    side.reply(pid, f"{cid}:{pid}", "Yes, I'm in!")
    assert state(engine, cid) == "AWAITING_BOOKING_VERIFICATION"
    side.pay(pid, f"{cid}:{pid}")
    assert state(engine, cid) == "CONFIRMED"
    assert any(ev["event_type"] == "BOOKING_CHECKED" for ev in engine.storage.events(cid))
    with pytest.raises(PlayerError):
        side.pay(pid, f"{cid}:{pid}")


def test_pay_is_blocked_while_the_provider_is_down(engine, side):
    cid = approve(engine, MP_MAIN, n=1)
    pid = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"][0]
    side.reply(pid, f"{cid}:{pid}", "yes")
    engine.set_provider_outage(True)
    with pytest.raises(PlayerError):
        side.pay(pid, f"{cid}:{pid}")
    assert state(engine, cid) != "CONFIRMED"


def test_group_court_is_booked_only_when_all_four_have_paid(engine, side):
    cid = approve(engine, EC_MAIN, n=4)
    ids = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"]
    for pid in ids:
        side.reply(pid, f"{cid}:{pid}", "yes")
    assert state(engine, cid) == "AWAITING_BOOKING_VERIFICATION"
    for pid in ids[:3]:
        side.pay(pid, f"{cid}:{pid}")
        assert state(engine, cid) == "AWAITING_BOOKING_VERIFICATION"
    side.pay(ids[3], f"{cid}:{ids[3]}")
    assert state(engine, cid) == "CONFIRMED"


def test_group_card_shows_counts_not_names(engine, side):
    cid = approve(engine, EC_MAIN, n=4)
    ids = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"]
    side.reply(ids[0], f"{cid}:{ids[0]}", "yes")
    card = side.invite_cards(ids[1])[0]
    assert card.seats_held == 1
    others = [engine.profiles()[p].first_name for p in ids if p != ids[1]]
    text = " ".join([card.title, card.headline, card.detail] + [m["body"] for m in card.thread])
    assert not any(n in text for n in others)


def test_personal_weekly_limit_is_respected(engine, side):
    view = get_view(engine, MP_MAIN)
    pid = view.shortlist.candidates[0].player_id
    assert engine.profiles()[pid].invites_last_7d >= 0
    side.save_settings(pid, ["weekday_evening"], "simulated_sms", 1)
    if engine.profiles()[pid].invites_last_7d >= 1:
        _, sl = engine.evaluate(view.opp)
        assert pid not in [c.player_id for c in sl.candidates + sl.reserve]
    with pytest.raises(PlayerError):
        side.save_settings(pid, [], "simulated_sms", engine.policy().contact.max_invites_per_player_per_7_days + 1)


def test_personal_limit_blocks_at_send_time(engine, side):
    cid = approve(engine, MP_MAIN, n=2)
    first, second = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"]
    with engine.storage.connect() as conn:   # simulate an earlier invite this week, then a limit of 1
        conn.execute("INSERT INTO prior_invites VALUES (?, ?)", (second, (engine.now() - timedelta(days=1)).isoformat()))
    side.save_settings(second, [], "simulated_sms", 1)
    side.reply(first, f"{cid}:{first}", "no")
    assert not invites(engine, cid)[second]["sent_at"]


def test_opt_out_in_the_app_stops_invites(engine, side):
    view = get_view(engine, MP_MAIN)
    pid = view.shortlist.candidates[0].player_id
    side.set_opt_in(pid, False)
    assert engine.profiles()[pid].opt_in_status == "opted_out"
    _, sl = engine.evaluate(view.opp)
    assert pid not in [c.player_id for c in sl.candidates + sl.reserve]
    side.set_opt_in(pid, True)
    assert engine.profiles()[pid].opt_in_status == "opted_in"
    kinds = [ev["event_type"] for ev in engine.storage.events()]
    assert "CONSENT_REVOKED" in kinds and "CONSENT_GRANTED" in kinds


def test_g16_two_yes_two_no_invites_the_approved_backups(engine):
    view = get_view(engine, EC_MAIN)
    ids = [c.player_id for c in view.shortlist.candidates[:6]]
    template, source, _ = engine.draft(EC_MAIN)
    cid = engine.approve(EC_MAIN, ids, 6, view.opp.start - timedelta(minutes=45),
                         engine.policy().consent.allowed_channels, template, source, first_wave_size=4)
    sent = [p for p, i in invites(engine, cid).items() if i["sent_at"]]
    assert sorted(sent) == sorted(ids[:4])
    for pid, ans in zip(ids[:4], ["yes", "yes", "no", "no"]):
        engine.reply(f"{cid}:{pid}", ans)
    inv = invites(engine, cid)
    assert sum(1 for i in inv.values() if i["status"] == "ACCEPTED") == 2      # prior interest kept
    assert all(inv[p]["sent_at"] for p in ids[4:])                              # backups invited
    assert state(engine, cid) != "ESCALATED"
    outsiders = {m["player_id"] for m in engine.storage.messages(cid)} - set(ids)
    assert not outsiders


def test_g16_backups_never_exceed_max_sends(engine):
    view = get_view(engine, EC_MAIN)
    ids = [c.player_id for c in view.shortlist.candidates[:6]]
    template, source, _ = engine.draft(EC_MAIN)
    cid = engine.approve(EC_MAIN, ids, 5, view.opp.start - timedelta(minutes=45),
                         engine.policy().consent.allowed_channels, template, source, first_wave_size=4)
    for pid, ans in zip(ids[:4], ["yes", "no", "no", "no"]):
        engine.reply(f"{cid}:{pid}", ans)
    assert sum(1 for i in invites(engine, cid).values() if i["sent_at"]) == 5


# ---- "I'm free" (solo player looking for a game) -----------------------------------------------------
MP_C6 = "MP-M-OPEN-2030-C6"   # open match, 3/4, level 2.5, Tue 20:30
AMA = "P029"                  # level 2.2, opted in


def test_im_free_shows_open_seats_that_fit(engine, side):
    assert side.free_status(AMA)["games"] == []
    side.post_free(AMA, "today", 18, 23)
    games = side.free_status(AMA)["games"]
    assert [g["opportunity_id"] for g in games] == [MP_C6]


def test_im_free_needs_invites_on_and_a_sensible_window(engine, side):
    side.set_opt_in(AMA, False)
    with pytest.raises(PlayerError):
        side.post_free(AMA, "today", 18, 23)
    side.set_opt_in(AMA, True)
    with pytest.raises(PlayerError):
        side.post_free(AMA, "today", 18, 19)          # under 2 hours
    with pytest.raises(PlayerError):
        side.post_free(AMA, "today", 7, 9)            # already over (demo clock is 10:50)
    side.post_free(AMA, "today", 18, 23)
    with pytest.raises(PlayerError):
        side.post_free(AMA, "today", 20, 23)          # overlaps the first window


def test_games_rally_is_already_asking_about_are_not_shown(engine, side):
    approve(engine, MP_C6, n=2)
    side.post_free(AMA, "today", 18, 23)
    assert all(g["opportunity_id"] != MP_C6 for g in side.free_status(AMA)["games"])


def test_wrong_level_player_sees_no_game(engine, side):
    side.post_free("P001", "today", 18, 23)           # level 3.6; Court 6 is level 2.5
    assert all(g["opportunity_id"] != MP_C6 for g in side.free_status("P001")["games"])


def test_free_bonus_moves_a_player_up_and_says_why(engine, side):
    def ama():
        v = get_view(engine, MP_C6)
        return next(c for c in v.shortlist.candidates + v.shortlist.reserve if c.player_id == AMA)
    before = ama()
    side.post_free(AMA, "today", 18, 23)
    after = ama()
    # time_fit can also rise to 1.0, so the gain is at least the bonus
    assert after.total >= before.total + engine.policy().ranking.free_bonus - 0.1
    assert after.rank < before.rank
    assert "said they're free then" in after.explanation


def test_self_join_books_through_the_booking_system(engine, side):
    side.post_free(AMA, "today", 18, 23)
    msg = side.self_join(AMA, MP_C6)
    assert "Booked and paid" in msg
    match = engine.provider.get_match("M-OPEN-2030-C6")
    assert AMA in match.player_ids and match.payment_status == "paid"
    assert MP_C6 not in _opp_ids(engine)                              # the open seat is gone
    assert side.free_status(AMA)["windows"] == []                     # window marked joined
    assert any(ev["event_type"] == "PLAYER_SELF_JOINED" for ev in engine.storage.events())
    with pytest.raises(PlayerError):
        side.self_join(AMA, MP_C6)


def test_self_join_blocked_when_booking_system_is_down(engine, side):
    side.post_free(AMA, "today", 18, 23)
    engine.provider.set_outage(True)
    with pytest.raises(PlayerError):
        side.self_join(AMA, MP_C6)
    engine.provider.set_outage(False)
    assert AMA not in engine.provider.get_match("M-OPEN-2030-C6").player_ids


# ---- "Ask about this game" (near-fits; the club or organiser decides) ---------------------------------
C4 = "M-OPEN-1130-C4"         # open match 11:30, level 3.0, 3/4 (too soon for invites, level off for Ama)
EC_1300 = "EC-20261110-1300-C1"


def test_games_around_shows_near_fits_with_honest_notes(engine, side):
    side.post_free(AMA, "today", 11, 14)
    around = {g["target_id"]: g for g in side.games_around(AMA)}
    assert C4 in around and EC_1300 in around
    assert any("level 3.0" in n for n in around[C4]["notes"])
    assert any("starts in" in n for n in around[C4]["notes"])
    assert around[EC_1300]["action"] == "I'm interested"


def test_club_lets_a_player_in_then_payment_books_it(engine, side):
    side.post_free(AMA, "today", 11, 14)
    side.ask_about(AMA, C4, "Happy to play up a level")
    with pytest.raises(PlayerError):
        side.ask_about(AMA, C4)                                    # one open request per game
    rid = side.my_requests(AMA)[0]["request_id"]
    with pytest.raises(PlayerError):
        side.pay_request(AMA, rid)                                 # not approved yet
    side.decide_request(rid, "approve", "manager:test")
    assert AMA not in engine.provider.get_match(C4).player_ids     # approval alone books nothing
    side.pay_request(AMA, rid)
    m = engine.provider.get_match(C4)
    assert AMA in m.player_ids and m.payment_status == "paid"
    assert engine.storage.get_request(rid)["status"] == "booked"


def test_declined_request_tells_the_player_and_books_nothing(engine, side):
    side.post_free(AMA, "today", 11, 14)
    side.ask_about(AMA, C4)
    rid = side.my_requests(AMA)[0]["request_id"]
    side.decide_request(rid, "decline", "manager:test")
    assert side.my_requests(AMA)[0]["label"] == "Not this time"
    assert any(m["kind"] == "request_declined" for m in engine.storage.messages(player_id=AMA))
    with pytest.raises(PlayerError):
        side.pay_request(AMA, rid)


def test_only_the_organiser_answers_requests_for_their_game(engine, side):
    side.post_free("P013", "today", 18, 22)                        # Aisha 3.5; Elif's game is level 4.0
    side.ask_about("P013", PRIV, "I can play 7pm")
    rid = side.pending_requests(organiser_id=ORG)[0]["request_id"]
    assert not side.pending_requests()                             # not in the club's queue
    assert side.pending_requests(organiser_id=ORG)[0]["name"] == "Aisha D."   # first name + initial only
    with pytest.raises(PlayerError):
        side.decide_request(rid, "approve", "manager:test")
    with pytest.raises(PlayerError):
        side.decide_request(rid, "approve", "organiser:P014", organiser_id="P014")
    side.decide_request(rid, "approve", f"organiser:{ORG}", organiser_id=ORG)
    assert side.my_requests("P013")[0]["can_pay"]


def test_empty_court_interest_is_a_signal_not_a_booking(engine, side):
    side.post_free(AMA, "today", 11, 14)
    side.ask_about(AMA, EC_1300)
    rid = side.my_requests(AMA)[0]["request_id"]
    with pytest.raises(PlayerError):
        side.decide_request(rid, "approve", "manager:test")        # courts: acknowledge or decline only
    side.decide_request(rid, "acknowledge", "manager:test")
    r = side.my_requests(AMA)[0]
    assert r["label"] == "The club will let you know" and not r["can_pay"]


def test_far_off_levels_are_not_shown(engine, side):
    side.post_free("P013", "today", 18, 23)                        # Aisha 3.5; Court 6 is level 2.5
    assert all(g["target_id"] != "M-OPEN-2030-C6" for g in side.games_around("P013"))


# ---- game page and team chat ---------------------------------------------------------------------------
OPEN_C2 = "M-OPEN-1730-C2"      # Daniela (P001), Kwame (P002), Sofia (P003) booked and paid


def test_game_page_shows_address_and_teammates_only_to_booked_players(engine, side):
    page = side.game_page("P001", OPEN_C2)
    assert page["address"] and "fictional" in page["address"]
    names = [p["name"] for p in page["team"]]
    assert "You" in names and "Kwame A." in names                  # first name + initial, never a number
    assert all("+1" not in str(p) for p in page["team"])
    with pytest.raises(PlayerError):
        side.game_page("P005", OPEN_C2)                            # not in the game
    with pytest.raises(PlayerError):
        side.game_page("P004", OPEN_C2)                            # Ethan dropped out


def test_only_booked_players_can_chat_and_the_log_keeps_no_text(engine, side):
    side.post_chat("P001", OPEN_C2, "Who's bringing balls?")
    assert side.game_page("P002", OPEN_C2)["messages"][0]["body"] == "Who's bringing balls?"
    with pytest.raises(PlayerError):
        side.post_chat("P005", OPEN_C2, "hi")                      # outsider
    with pytest.raises(PlayerError):
        side.post_chat("P001", OPEN_C2, "x" * 501)
    logged = [ev for ev in engine.storage.events() if ev["event_type"] == "GAME_CHAT_POSTED"]
    assert logged and "balls" not in str(logged[0]["detail"])


def test_a_new_booking_joins_the_chat(engine, side):
    cid = approve(engine, MP_MAIN, n=1)
    pid = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"][0]
    with pytest.raises(PlayerError):
        side.game_page(pid, OPEN_C2)                               # invited is not booked
    side.reply(pid, f"{cid}:{pid}", "yes")
    with pytest.raises(PlayerError):
        side.game_page(pid, OPEN_C2)                               # said yes, not paid yet
    side.pay(pid, f"{cid}:{pid}")
    assert any(p["name"] == "You" for p in side.game_page(pid, OPEN_C2)["team"])


def test_report_goes_to_the_club_and_hide_applies_to_everyone(engine, side):
    side.post_chat("P002", OPEN_C2, "something rude")
    msg_id = side.game_page("P001", OPEN_C2)["messages"][0]["msg_id"]
    with pytest.raises(PlayerError):
        side.report_chat("P002", OPEN_C2, msg_id)                  # can't report your own message
    side.report_chat("P001", OPEN_C2, msg_id)
    assert [r["msg_id"] for r in side.reported_chat()] == [msg_id]
    side.review_chat(msg_id, "hide", "manager:test")
    for pid in ("P001", "P002", "P003"):
        m = side.game_page(pid, OPEN_C2)["messages"][0]
        assert m["hidden"] and "rude" not in m["body"]
    assert side.reported_chat() == []


def test_chat_closes_after_the_game(engine, side):
    engine.advance(int((engine.provider.get_match(OPEN_C2).end - engine.now()).total_seconds() // 60) + 3 * 60 + 5)
    assert not side.game_page("P001", OPEN_C2)["chat_open"]
    with pytest.raises(PlayerError):
        side.post_chat("P001", OPEN_C2, "gg")


# ---- notifications ---------------------------------------------------------------------------------------
def test_invite_creates_an_unread_alert_and_opening_alerts_marks_read(engine, side):
    cid = approve(engine, MP_MAIN, n=1)
    pid = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"][0]
    alerts = side.notifications(pid)
    assert alerts[0]["title"] == "New game invite" and alerts[0]["unread"]
    assert side.unread_count(pid) == 1
    side.mark_all_read(pid)
    assert side.unread_count(pid) == 0
    side.reply(pid, f"{cid}:{pid}", "yes")
    assert side.latest_unread(pid)["title"].startswith("You're in")


def test_chat_alerts_teammates_not_the_author(engine, side):
    side.post_chat("P001", OPEN_C2, "Who's bringing balls?")
    assert side.unread_count("P001") == 0
    a = side.latest_unread("P002")
    assert a["title"] == "Daniela M. · Court 2 chat" and a["link"].endswith("#chat")


def test_chat_text_never_contains_the_message_and_is_rate_limited(engine, side):
    side.post_chat("P001", OPEN_C2, "secret plan")
    side.post_chat("P001", OPEN_C2, "another one")
    texts = [m for m in engine.storage.messages(player_id="P002") if m["kind"] == "chat_alert"]
    assert len(texts) == 1 and "secret" not in texts[0]["body"]
    engine.advance(31)
    side.post_chat("P001", OPEN_C2, "third")
    assert len([m for m in engine.storage.messages(player_id="P002") if m["kind"] == "chat_alert"]) == 2


def test_no_chat_texts_in_quiet_hours_or_when_switched_off(engine, side):
    side.set_chat_texts("P002", False)
    side.post_chat("P001", OPEN_C2, "hi")
    assert not [m for m in engine.storage.messages(player_id="P002") if m["kind"] == "chat_alert"]
    assert side.unread_count("P002") == 1                         # still alerted in the app
    engine.advance(int((engine.now().replace(hour=21, minute=30) - engine.now()).total_seconds() // 60))
    side.post_chat("P001", OPEN_C2, "late")
    assert not [m for m in engine.storage.messages(player_id="P003") if m["kind"] == "chat_alert"
                and m["sim_ts"] >= engine.now().replace(hour=21).isoformat()]


def test_muted_chat_sends_no_alert_at_all(engine, side):
    side.set_mute("P002", OPEN_C2, True)
    side.post_chat("P001", OPEN_C2, "hi")
    assert side.unread_count("P002") == 0
    assert not [m for m in engine.storage.messages(player_id="P002") if m["kind"] == "chat_alert"]


def test_organiser_hears_about_requests_and_progress(engine, side):
    side.post_free("P013", "today", 18, 22)
    side.ask_about("P013", PRIV, "I can play 7pm")
    assert side.latest_unread(ORG)["title"] == "Aisha D. asked to join your game"
    side.mark_all_read(ORG)
    side.organiser_decide(ORG, PRIV, "rally")
    cands = side.organiser_candidates(ORG, PRIV)
    side.organiser_approve(ORG, PRIV, [cands[0]["player_id"]])
    assert side.latest_unread(ORG)["title"].startswith("Rally asked ")


def test_only_the_texted_chat_message_is_marked_as_texted(engine, side):
    side.post_chat("P001", OPEN_C2, "first")
    side.post_chat("P001", OPEN_C2, "second")
    chat = [a for a in side.notifications("P002") if a["key"].startswith("c:")]
    assert [a["texted"] for a in chat] == [False, True]          # newest first: only the first was texted


# ---- one place per conversation; any booked player can ask for a 4th ----------------------------------
def test_booked_invite_leaves_earlier_and_club_messages_move_to_the_game(engine, side):
    cid = approve(engine, MP_MAIN, n=1)
    pid = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"][0]
    side.reply(pid, f"{cid}:{pid}", "yes")
    side.pay(pid, f"{cid}:{pid}")
    home = side.home(pid)
    assert not [c for c in home["past_cards"] if c.status == "booked"]
    page = side.game_page(pid, OPEN_C2)
    assert page["club_messages"] and any("Reply" in m["body"] for m in page["club_messages"])


def test_booked_player_asks_the_club_for_a_4th(engine, side):
    page = side.game_page("P001", OPEN_C2)                        # 3/4: Ethan dropped out
    assert page["open_spots"] == 1 and page["fill"]["state"] == "can_ask"
    side.request_fill("P001", OPEN_C2)
    assert side.game_page("P001", OPEN_C2)["fill"]["state"] == "asked"
    reqs = side.pending_requests()
    assert reqs and reqs[0]["kind"] == "fill_club"
    with pytest.raises(PlayerError):
        side.decide_request(reqs[0]["request_id"], "approve", "manager:test")   # acknowledge or decline only
    side.decide_request(reqs[0]["request_id"], "acknowledge", "manager:test")
    assert side.latest_unread("P001")["body"].startswith("Thanks Daniela: the club is finding a 4th")


def test_once_rally_is_asking_players_see_progress_not_names(engine, side):
    approve(engine, MP_MAIN, n=2)
    fill = side.game_page("P001", OPEN_C2)["fill"]
    assert fill["state"] == "finding" and "1 player asked" in fill["detail"]
    with pytest.raises(PlayerError):
        side.request_fill("P001", OPEN_C2)


def test_everyone_in_the_game_hears_it_is_full(engine, side):
    cid = approve(engine, MP_MAIN, n=1)
    pid = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"][0]
    side.reply(pid, f"{cid}:{pid}", "yes")
    side.pay(pid, f"{cid}:{pid}")
    for teammate in ("P001", "P002", "P003"):
        assert any(a["title"] == "Your game is full" for a in side.notifications(teammate))
    assert side.game_page("P001", OPEN_C2)["fill"] is None


def test_non_organiser_in_a_private_game_asks_the_organiser(engine, side):
    page = side.game_page("P014", PRIV)                           # Ben is in Elif's game, not the organiser
    assert page["fill"]["state"] == "can_ask" and "Elif Y." in page["fill"]["action"]
    side.request_fill("P014", PRIV)
    assert not side.pending_requests()                            # not the club's call
    card = side.organiser_cards(ORG)[0]                           # shown inside Elif's own game card
    assert card.fill_requests and card.fill_requests[0]["name"] == "Ben T."
    assert not side.pending_requests(organiser_id=ORG)            # no second, duplicate card
    assert side.latest_unread(ORG)["title"] == "Ben T. asked you to find a 4th"
    assert side.game_page(ORG, PRIV)["fill"]["state"] == "organiser"



# ---- second panel review: drop out + fee, merged organiser card, answer times, week-long I'm free ------
def _book_into_c2(engine, side):
    cid = approve(engine, MP_MAIN, n=1)
    pid = engine.storage.get_campaign(cid)["approval"]["approved_player_ids"][0]
    side.reply(pid, f"{cid}:{pid}", "yes")
    side.pay(pid, f"{cid}:{pid}")
    return pid


def test_drop_out_frees_the_spot_charges_the_late_fee_and_tells_teammates(engine, side):
    _book_into_c2(engine, side)
    page = side.game_page("P001", OPEN_C2)
    assert page["can_drop"] and page["cancel"]["late"]              # the demo game is today: inside 24 h
    msg = side.drop_out("P001", OPEN_C2)
    assert "$30 late-cancellation fee" in msg
    m = engine.provider.get_match(OPEN_C2)
    assert "P001" not in m.player_ids and "P001" in m.dropout_player_ids
    assert any(ev["event_type"] == "LATE_CANCEL_FEE" for ev in engine.storage.events())
    assert side.latest_unread("P002")["title"] == "Daniela M. can't make it anymore"
    with pytest.raises(PlayerError):
        side.game_page("P001", OPEN_C2)                            # no longer in the game or its chat
    assert f"MP-{OPEN_C2}" in _opp_ids(engine)                     # the club sees the open spot again


def test_no_fee_when_cancelling_in_time(engine, side):
    from types import SimpleNamespace
    later = SimpleNamespace(start=engine.now() + timedelta(hours=48))
    terms = side.cancel_terms("P001", later)
    assert not terms["late"] and terms["fee"] == 0


def test_the_organiser_cannot_drop_out_of_their_own_game(engine, side):
    with pytest.raises(PlayerError):
        side.drop_out(ORG, PRIV)


def test_organiser_answering_the_card_answers_the_teammates_request(engine, side):
    side.request_fill("P014", PRIV, "my friend might be free")
    side.organiser_decide(ORG, PRIV, "rally")
    r = [x for x in engine.storage.requests(target_id=PRIV) if x["kind"] == "fill_organiser"][0]
    assert r["status"] == "acknowledged"
    assert side.organiser_cards(ORG)[0].fill_requests == []
    assert side.game_page("P014", PRIV)["fill"]["state"] in ("asked", "finding")


def test_requests_show_when_the_club_aims_to_answer(engine, side):
    side.post_free(AMA, "today", 11, 14)
    side.ask_about(AMA, C4)
    r = side.my_requests(AMA)[0]
    assert r["answer_by"] == "11:20 AM"                            # asked 10:50 + 30 min target
    assert side.pending_requests()[0]["target_min"] == 30


def test_im_free_covers_the_week_and_remembers_the_window(engine, side):
    opts = side.free_options(AMA)
    assert len(opts["days"]) == 7
    side.post_free(AMA, "3", 18, 21)                               # three days from now
    w = engine.storage.availability(engine.now(), AMA)[0]
    assert (w["start"].date() - engine.now().date()).days == 3
    again = side.free_options(AMA)
    assert (again["default_start"], again["default_end"]) == (18, 21)


# ---- Start a game + report a dropout --------------------------------------------------------------------
def test_start_a_game_books_the_court_and_hands_over_to_find_someone(engine, side):
    opts = side.start_options(AMA)
    assert opts["slots"] and opts["slots"][0]["quiet"]                     # quiet hours first
    match_id = side.start_game(AMA, opts["slots"][0]["value"], 2, 2.2)
    m = engine.provider.get_match(match_id)
    assert m.player_ids[0] == AMA and len(m.player_ids) == 3 and m.payment_status == "paid"
    assert engine.organizer_of(match_id) == AMA
    assert any(ev["event_type"] == "PLAYER_BOOKED_COURT" for ev in engine.storage.events())
    card = side.organiser_cards(AMA)[0]
    assert card.stage == "ask"                                             # "Find someone for me" is next
    side.organiser_decide(AMA, match_id, "rally")
    assert side.organiser_candidates(AMA, match_id)                        # Rally can rank players for it
    names = [x["name"] for x in side.game_page(AMA, match_id)["team"]]
    assert names.count("Guest of yours") == 2


def test_start_a_game_rejects_a_court_that_was_just_taken(engine, side):
    slot = side.start_options(AMA)["slots"][0]["value"]
    side.start_game(AMA, slot, 2, 2.2)
    with pytest.raises(PlayerError):
        side.start_game("P026", slot, 2, 2.3)


def test_organiser_reports_a_friend_dropped_out_and_it_applies_at_once(engine, side):
    msg = side.report_dropout(ORG, PRIV, "P014")                           # Elif: Ben texted her he can't come
    assert "off the game" in msg
    m = engine.provider.get_match(PRIV)
    assert "P014" not in m.player_ids and "P014" in m.dropout_player_ids
    assert side.latest_unread("P039")["title"] == "Ben T. can't make it anymore"
    assert any(n["title"] == "You've left the game" for n in side.notifications("P014"))
    assert not [e for e in engine.storage.events() if e["event_type"] == "LATE_CANCEL_FEE"]


def test_in_a_club_match_the_club_confirms_a_reported_dropout(engine, side):
    msg = side.report_dropout("P001", OPEN_C2, "P002")
    assert "club will confirm" in msg
    assert "P002" in engine.provider.get_match(OPEN_C2).player_ids        # nothing changes yet
    with pytest.raises(PlayerError):
        side.report_dropout("P003", OPEN_C2, "P002")                       # one report at a time
    rid = side.pending_requests()[0]["request_id"]
    side.decide_dropout_report(rid, "confirm", "manager:test")
    assert "P002" not in engine.provider.get_match(OPEN_C2).player_ids


def test_a_non_organiser_cannot_change_a_private_booking(engine, side):
    with pytest.raises(PlayerError):
        side.report_dropout("P014", PRIV, "P039")


def test_new_player_share_is_credited_back_to_the_organiser(engine, side):
    match_id = side.start_game(AMA, side.start_options(AMA)["slots"][0]["value"], 2, 2.2)
    side.organiser_decide(AMA, match_id, "rally")
    first = side.organiser_candidates(AMA, match_id)[0]["player_id"]
    cid = side.organiser_approve(AMA, match_id, [first])
    side.reply(first, f"{cid}:{first}", "yes")
    side.pay(first, f"{cid}:{first}")
    credits = [e for e in engine.storage.events() if e["event_type"] == "ORGANISER_CREDITED"]
    assert len(credits) == 1 and credits[0]["detail"]["amount_usd"] == 30
    assert any(n["title"] == "A new player paid their share" for n in side.notifications(AMA))
