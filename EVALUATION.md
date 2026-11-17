# Guide for evaluators (human or AI agent)

Rally is a Python prototype for NBAY 6170 (Team 3). It finds unsold padel courts and open match
seats, ranks players with fixed rules, waits for a manager's approval, reads player replies with
Claude into a strict schema, and counts a booking only when the (mock) booking provider verifies it.
`README.md` has the full design; this page is the short path to checking it.

## Verify in about 2 minutes, no API key needed

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                                        # 57 pass, 10 live tests skipped without a key
python scripts/run_demo_sequences.py          # the three PRD demo sequences, printed as a trace
python scripts/golden/run_golden.py           # the 20 golden cases, offline rules parser
```

Without `ANTHROPIC_API_KEY`, a keyword-rules parser and fixed templates stand in for Claude; every
output says which parser it used. With a key in `.env` (copy `.env.example`), add `--live` to the
last two commands, and `pytest -m live` runs the 10 live parser cases. `streamlit run app.py` opens
the manager dashboard used in the demo video.

## Where to look

| Question | Where |
|---|---|
| Overall flow | `rally/engine.py` (wires the steps), `rally/coordinator.py` (state machine, sends, verification) |
| PRD tool → code map | `README.md`, section "PRD tools → code" |
| Hard safety invariants and their tests | `README.md`, section "Hard invariants and where they're enforced"; `tests/test_workflows.py` |
| LLM use (parsing and drafting, structured outputs) | `rally/llm.py`, `rally/schemas.py`; cases in `tests/test_llm_cases.py` |
| Club rules (single source, versioned) | `data/club_policy.yaml`, loaded by `rally/policy.py` |
| Real input data | `data/raw/occupancy_untitled-2.xlsx` → `scripts/build_fixtures.py` → `data/*.csv` |
| Append-only audit log | `rally/storage.py` (SQLite triggers block updates and deletes) |

## Evaluation evidence already in the repo

- **20 golden end-to-end cases:** spec in `fixtures/golden_cases.csv`; runner `scripts/golden/run_golden.py`.
  Each case runs on a fresh seeded engine through Rally's real functions and checks the case's own
  assertions. Results and a full trace per case:
  `outputs/golden/live-claude-haiku-5-5/` (live Claude run) and `outputs/golden/offline/`.
  Live run: 13 pass, 5 partial, 2 fail, no forbidden action in any case.
- **Independent judge:** `scripts/golden/judge.py` scored the 10 language-heavy cases with a
  different model (Claude Sonnet 5.5) from Rally's (Haiku 5.5): `judge_results.csv` and
  `agreement.json` in the live run folder.
- **Report:** `outputs/golden/Rally_Golden_Evaluation_Report.docx` (and `.md`).
- **Model and cost comparison:** `scripts/tier_benchmark.py` (10 tasks × 3 runs × 4 models); prices
  in `data/model_pricing.yaml`.

## Known open issues

Fixed on 2026-11-17 (player-app build; offline golden run now 13 pass, 6 partial, 1 fail, no forbidden action):

1. ~~Empty-court outreach does not move on to approved backups (G16).~~ Fixed: an approval can set a first
   wave (e.g. 4 of 6). If the wave can't fill the court, Rally invites the approved backups in rank order,
   within max sends, keeping earlier yeses (`Coordinator._send_backups`, event `BACKUP_WAVE_SENT`). G16 passes.
2. ~~Live invite drafts are often cut off (G10, G14).~~ Fixed: the Claude output limit in `rally/llm.py`
   `_structured()` is now 2,000 tokens. A live check drafted complete invites.
3. ~~Empty-court template wording (G14).~~ Fixed: the template and the drafting prompt now say the game is
   on only once 4 players say yes and book. G14 is PARTIAL only for minor items (states the time range
   rather than "90 minutes"; states the $30 policy price).

Still open:

4. G04 (offline parser only): the keyword backup parser leaves "7" blank instead of reading 7:00 PM;
   live Claude reads it. Minor, all safe: a send requested before approval raises an error instead of
   being logged (G08); no retry limit while the provider is down (G12); no message IDs for deduplicating
   repeated replies (G18); invites state a $30 price from `club_policy.yaml` (G10, G14).
5. The live golden run (`--live`) and the cross-model judge have not been re-run since these fixes.

A conditional yes ("I'm in if Daniela is playing") was once parsed as a firm yes by the offline
parser. That is fixed and covered by a regression test and golden case G09.

## What is simulated

Players, the club, messages (SMS/WhatsApp), the booking provider and payments are all simulated. The
only real input is the club's occupancy spreadsheet. No real person is contacted.
