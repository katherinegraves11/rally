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

5. **"I'm free" (added 2026-11-17, Katie's call after the first review).** A solo player with no group
   ("I want to play at 6 tonight and have no one", Mia in the midpoint doc) can raise their hand
   instead of only waiting for an invite. They pick today or tomorrow and a time range.
   - **Right away:** they see open seats in club open matches that fit their level and time, and
     can join one (book and pay their share through the simulated club booking app). A game where
     Rally is already asking specific players is not shown, so Rally never races its own invites;
     organiser games are not shown, because the organiser chooses who joins.
   - **For games that come up:** while the window is open they move up the list for matching games
     (a visible "said they're free then" bonus from the club's rules file). The manager or
     organiser still approves every invite, and the weekly limit still applies.
   - **For empty courts:** free players at the same level and time push that court's group to the
     top, and the dashboard shows how many players said they're free for each opportunity, which
     is a demand signal the club doesn't have today.
   - Posting "I'm free" needs invites on (it is consent to be contacted in that window). It ends
     at the end of the window, or when they cancel it or join a game. Joining checks the booking
     system first and counts only once the booking system lists them as paid.
   This keeps Rally out of Playtomic's marketplace job (browsing every open match): it only shows
   games that fit this player now.

6. **"Ask about this game" (added 2026-11-17, Katie's call).** Under an "I'm free" window the player
   also sees *games around your time*: games that don't fit exactly, each with an honest note
   ("level 3.0, you're 2.2", "starts in 35 min", "ends 30 min after you're free"). They can ask
   about one, with a short note. **Requests go to whoever decides, never to other players**: no
   names or numbers are shared between players.
   - **Open match:** the request appears in *Player requests* on the club dashboard. *Let them in*
     sends the player a booking link (they book and pay; it counts only once the booking system
     lists them as paid). *Not this time* sends a polite set reply.
   - **Organiser's private game:** the request goes to the organiser in their player app, with the
     player's first name, last initial and level only.
   - **Empty court:** "I'm interested" is a demand signal on the dashboard ("2 players asked about
     this court"). The club can reply *We'll let you know* or *Not this time*; if the club then
     approves an outreach for that court, the player is invited the normal way.
   - One open request per player per game; a player can withdraw it. Requests close when the game
     starts or fills. A human approval here is the only way past the level and lead-time rules,
     and every decision is logged with who made it.

7. **Game page and team chat (added 2026-11-17, Katie's call: in-app chat instead of sharing phone
   numbers, because it is safer).** Once a player is booked, each of their games opens a game page:
   - **Where and when:** court, time, the club's address (fictional in the demo), "arrive 10 minutes
     early".
   - **Who's playing:** first name, last initial and level of everyone booked in that game. Names
     appear only after booking (at the invite stage group invites still show counts only).
   - **Game chat:** only the players booked in that game can read or post. It opens at booking and
     closes 3 hours after the game ends. Phone numbers are never shown.
   - **Safety:** any player can report a message; reported messages appear for the club on the
     dashboard, which can hide them for everyone or keep them. Rally never reads chat to make
     decisions (unlike invite replies, nothing said here changes a booking), and messages are shown
     as plain text.
   - The audit log records that a message was posted or reported, not what it said.

8. **Notifications (added 2026-11-17, Katie's call).** Players shouldn't have to open the app to find
   out something happened.
   - **Alerts tab** with an unread badge (also in the browser tab title). It lists, newest first:
     new invites and every club message about them (booking link, waitlist, game off), answers to
     "ask about this game", new game-chat messages from teammates, and for organisers: someone
     asked to join, Rally asked a player, a player said no, and "your game is full". Opening the
     tab marks them read. Each alert links to where you act.
   - **Phone / browser alerts:** the player can switch on browser notifications; while Rally is
     open (or in a background tab) a new alert pops up as a system notification. Real push to a
     closed phone app needs the club's app or Playtomic's, which is out of scope.
   - **Text alerts for chat (simulated):** invites already arrive by text or WhatsApp. For chat,
     a teammate's message also sends a short simulated text ("new message in your Court 2 game
     chat"), at most one per game every 30 minutes, never in the club's quiet hours, and never
     with the message itself. Players can switch chat texts off in Settings, or mute one game's
     chat from its page (muted chats don't alert at all).
   - Read state is per player. Alerts are built from what already happened (messages, chat,
     requests, the event log), so nothing new can drift out of sync.

9. **One place per conversation, and any player can ask for a 4th (added 2026-11-17, Katie's review).**
   - Home shows each booked game as a card with two clear buttons, **Details** and **Chat** (with a
     "new" count). Once booked, the invite card and its club texts leave "Earlier"; the club's texts
     sit on the game page under a collapsed **Messages from the club**, separate from **Game chat**.
   - **When a booked game has an open spot** (someone dropped out), every booked player sees it on
     the game page ("Open spot") and can ask for help:
     - club open match: **Ask the club to find a 4th** (goes to Player requests on the dashboard and
       to that game's page; the club approves the outreach as usual);
     - private game, not the organiser: **Ask the organiser to find a 4th** (alerts the organiser,
       who uses "Find someone for me"); the organiser sees their usual flow;
     - if Rally is already asking players: "The club is finding a 4th" (no names of who was asked).
   - Every booked player gets a "Your game is full" alert when the spot is filled.

10. **Panel fixes, second review (2026-11-17, Katie approved all seven).**
    1. **"I can't make it anymore"** on the game page for booked players (not the organiser). It frees the
       spot in the booking system, tells teammates (alert), and the open spot reaches the club or
       organiser through the usual detection. Too close to start for Rally: the page says to tell the
       front desk too.
       **Late-cancellation fee (Katie, 2026-11-17):** players who opted in and then drop out late pay a
       fee. Provisional settings in `cancellation` (policy 0.1.3): free until 24 hours before the game;
       after that a $30 fee (one player's share). The rule is shown on the game page before anyone
       needs it, and dropping out is a two-step confirm that states the fee first. Simulated payment,
       logged as `LATE_CANCEL_FEE`. The team should confirm the window and amount.
    2. **"Needs you" at the top of home:** everything waiting on the player (answer an invite, book and
       pay, an organiser decision) in one group; then games, then things in progress, then "I'm free".
    3. **One organiser card per game:** a teammate's "find a 4th" request shows inside the organiser's
       game card instead of as a second card; answering it also answers the request.
    4. **Small print said once:** the demo strip already says payments are simulated; repeated hint lines
       under cards are removed.
    5. **"Find a 4th" takes an optional note** ("my friend Sam might be free"); the dashboard request has
       an **Open this game** button that opens it under Players needed.
    6. **Answer-time target:** the club aims to answer requests within `club.request_reply_minutes`
       (provisional, 30 min; policy 0.1.3). Players see "the club aims to answer by 11:30 AM"; the
       dashboard shows how long each request has waited.
    7. **"I'm free" for the whole week** (today plus six days), and it remembers the player's last
       window (or starts from their usual playing times).

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
| `player_availability` table; active windows on `PlayerProfile.free_windows` | `storage.py`, `engine.profiles()` | Consent required to post |
| "Free then" ranking bonus (`ranking.free_bonus` in the policy) | `ranking.py`, `club_policy.yaml` | Transparent, rule-based ranking |
| Self-join: fresh booking-system check, then the booking system lists them as paid | `player_side.py` | No count without the booking system |
| `join_requests` table; club and organiser decisions; booking only after approval + payment | `storage.py`, `player_side.py`, `app.py` | A human approves; players never contact each other directly |
| `game_chat` table; access only for booked players of that game; reports reviewed by the club | `storage.py`, `player_side.py`, `app.py` | Players never see each other's contact details |
| Alerts feed derived from messages/chat/requests/events; `notification_seen`, `chat_mutes`, chat-text setting | `storage.py`, `player_side.py` | Quiet hours; no chat text content in texts or logs |

Organisers: `data/match_organizers.csv` (fictional) names the organiser of each private match.

## Privacy rules for the player side

- A player sees only their own messages, games and settings.
- Group invites show counts, never other players' names (golden case G15).
- An organiser sees candidates' first name + last initial, level and history with them, only
  after asking Rally to help, and only for their own match.

## Not in this build

Real sign-in (the demo picks a fictional player), real payments, real SMS/WhatsApp, sharing phone
numbers between players (chat is used instead), push to a closed phone app (browser alerts and simulated texts instead), browsing every
open match at the club (that is Playtomic's job; "I'm free" shows only games that fit), push notifications.
