# Rally player app: architecture

Status: plan for the first build (2026-11-17). Owner: Katie. This is the one plan for the
player-facing side; update it before changing the code.

## What it is

Rally has two complementary sides on one engine:

| Side | Who | What they do | Code |
|---|---|---|---|
| Club side (exists) | Club GM / operations | See at-risk slots, approve exactly who is invited, watch outcomes | `app.py` (Streamlit) |
| Player side (new) | Opted-in players and game organisers | Get one relevant invite, answer it, pay their share, set what they want to hear about; organisers ask Rally to fill a dropped seat | `player_app/` (FastAPI, phone-first web) |

Both run on the same `RallyEngine` and the same SQLite files in `runtime/`. A GM approval appears as
an invite in the player's inbox; a player's tap appears on the GM dashboard and in the audit log.
No state lives in either front end.

Who the players are comes from the midpoint check-in: Segments 2 and 3 (women and men, Gen Z and
millennial). Journeys A (organiser dropout: Ludovica, Brian) and B (solo player: Mia, Robert).

## Journeys in the first build

1. **Solo player invite (B).** The inbox shows each approved invite as a card: court, day, time,
   level band, $ share, and how many seats are open. Buttons: *I'm in*, *Can't make it*,
   *Ask a question*, or a free-text reply. Every reply goes through the existing `parse_reply()`
   (Claude into the strict schema; Python decides). On a clear yes the booking-link message
   arrives with *Complete booking and pay my share* (simulated Playtomic). Only the provider
   check turns it into *Confirmed*.
2. **Organiser dropout (A).** An organiser whose private game is short a player sees
   "Your 7:00 PM game has 3 of 4. Find your own, or should we?".
   - *I'll find someone*: Rally records it and stays out.
   - *Find someone for me*: this is the organiser's authorisation (PRD section 8, private matches).
     The match becomes actionable, Rally ranks players with the same rules, and the organiser sees
     name, level and history ("played with you before", "plays weekday evenings", "played 4 days
     ago"), then approves who may be asked, in order. That approval is the campaign approval
     (`approved_by` = the organiser), with the same scope checks on every send.
3. **Opt-in and preferences.** Join or leave Rally invites, choose channel (SMS / WhatsApp,
   simulated), times you play (the time bands ranking already uses), and a personal weekly
   invite limit (never above the club's cap). *Pause* = opt out. STOP in any reply still works.
4. **Empty-court group invite.** The card says plainly that the game happens only once four
   players say yes and book, and shows "2 of 4 in" (counts only, never names). Each player pays
   their own share; the court counts as booked only when all four have paid and the provider
   shows the booking.

## Engine additions (`rally/`)

| Change | Where | Rule it keeps |
|---|---|---|
| `organizer_authorizations` table; private match actionable only when its organiser chose "find someone for me" | `storage.py`, `detection.py` | Private matches excluded by default |
| Organiser approval path (`approve(..., approver=...)`) | `engine.py` | Nothing is sent without an approval |
| `player_settings` table (time bands, channel, personal weekly limit) merged into `PlayerProfile` | `storage.py`, `engine.profiles()` | Consent and caps re-checked at send |
| Personal limit: `min(club cap, player limit)` in eligibility and at send | `eligibility.py`, `coordinator.py` | Contact cap |
| `player_payments` table; group booking created with the provider only when every held player has paid | `storage.py`, `engine.py` | No CONFIRMED without provider verification |
| Every player-side change is an audit event (`PLAYER_SETTINGS_UPDATED`, `CONSENT_GRANTED`, `ORGANISER_AUTHORISED`, `PLAYER_PAID`) | `engine.py` | Append-only log |
| G16 fix: approved backups invited when the first wave cannot fill a court | `coordinator.py` | Only approved players, within max sends |

Organisers: `data/match_organizers.csv` (fictional) names the organiser of each private match.

## Privacy rules for the player side

- A player sees only their own messages, games and settings.
- Group invites show counts, never other players' names (golden case G15).
- An organiser sees candidates' first name + last initial, level and history with them, only
  after asking Rally to help, and only for their own match.

## Not in this build

Real sign-in (the demo picks a fictional player), real payments, real SMS/WhatsApp, browsing all
open matches (Rally reaches out; it is not a marketplace), push notifications.
