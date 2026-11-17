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
