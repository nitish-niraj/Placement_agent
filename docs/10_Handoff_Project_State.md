# 10 — Project Handoff (state + the one open problem)

| | |
|---|---|
| Date | 2026-09-09 |
| Purpose | Complete context to continue in a fresh session |
| Repo | `E:\agent\placement-intelligence` · GitHub: `nitish-niraj/Placement_agent` (public) · CI green on every push |
| Stack | Python 3.11 venv `.venv/Scripts/python` (Git Bash), Docker Compose: postgres(pgvector)/redis/minio/api/worker/evolution-api v2.3.7, all `restart: unless-stopped`, daily 09:30 DB backup (Task Scheduler "PIA daily backup") |

## 1. Done & verified (P0–P12 + extras)

- **Pipeline**: WhatsApp ingestion (idempotent, 4-layer dedup: exact-7d-rule/visual dHash/near token-Jaccard ≥0.72 **company-gated**/semantic) → hybrid classification (rules>LLM) → document intelligence (XLSX/PDF/image → 2,471 real rows) → **eligibility engine** (identifier-dominance ladder; Nitish/12515641 found in 5/6 real lists; Singh/12528230 correctly NOT_FOUND) → **company memory** (resolver, aliases, watch-state auto-activation, lifecycle machine) → **events/deadlines** (deterministic 12-type rules + IST date parser + drive fields: designation/salary_package/job_location/eligibility_note from `*Designation :-*` template) → **notifications** (Telegram §11.1 template, CRITICAL/HIGH immediate, digest 20:30, escalations T-24/6/1h, delivery history, dedup anchors) → **dashboard** (React+TS 10 screens, served by API container at :8000, bearer `DASHBOARD_TOKEN`) → **Ask PIA** (P12 hybrid: pgvector `nemotron-3-embed-1b` 2048d + keyword + structured facts; schema-validated LLM answer with citation filtering; deterministic evidence fallback)
- **KYC Type-1 listener** (`apps/worker/src/pia_worker/teams/`) — DEC-008 amended: informational company-arrival sessions may be auto-attended with the owner's own login; post-selection KYC is ALWAYS manual.
- Gates: ruff ✓ mypy ✓ 292 tests ✓; gitleaks in CI; `.env`/`teams_session.json`/`transcripts/`/`backups/` gitignored.
- **Security debt**: the university password was shared in chat → **rotate it** (and update `TEAMS_PASSWORD` in `.env`).

## 2. What the listener already achieves (proven live)

1. `login_save` (run on host, headed): programmatic Microsoft login → `infrastructure/teams_session.json` (550 KB, validated authenticated) ✅
2. `listen(url)`: resolves tinyurl/shorteners → **decodes the launcher to the direct meeting URL** (avoids the `ms-teams:` protocol dialog) → warms the app shell at `https://teams.cloud.microsoft/` (15 s, **realistic Edge UA required** — default UA makes the SPA hang) → opens the meeting → guest name entry ("Nitish Kumar") → **JOINS** ✅ (several live tests)
3. Post-join: opens chat, tries captions, watches the chat for Google-Forms links → **instant Telegram relay** (armed; fires only if a link is posted while the bot is inside — the earlier CAPP401 link was from a previous session so 0 relays was correct behavior), transcript file, 2-min linger after form, auto-Leave ✅
4. After leaving: `MeetingSummary` via NIM (host runs need `.env` sourced for `NVIDIA_API_KEY`; NIM 500-flaky → honest raw-transcript fallback — **delivered to Telegram in tests**) ✅
5. Captions: transcript capture **works when captions are ON** (real speech captured: "Hello this is Mike testing 1-2 three"); the AUTO-ENABLE is the open issue below.

## 3. THE open problem (exactly two sub-issues)

**A. Authenticated join — "Sign in" on the pre-join screen isn't reliably driven.**
Without it the participant shows as **"Nitish Kumar (Unverified)"** (guest identity) instead of the verified account (`TEAMS_EMAIL` in `.env`). Owner wants verified identity.
Observed behavior (from ~8 experiments):
- The pre-join has exactly **one** "Sign in" text control (a 52×17 px link near the bottom). Clicking it does NOT reliably produce a new popup (`context.expect_page` timed out at 20 s twice), and same-tab navigation either stalls 30 s+ at the same URL or — in ONE success — completed: the later join_failed diagnostic captured the page at `teams.microsoft.com/v2/` showing **the authenticated identity + "Join now"** (pre-join had become signed-in, join simply not clicked in time).
- Conclusion: the sign-in sometimes opens a popup window (must register `context.on("page")` BEFORE clicking and drive email→password→"Stay signed in: Yes" inside it — creds now in `TEAMS_EMAIL`/`TEAMS_PASSWORD` in `.env`), and sometimes needs longer patience. Current code polls all pages but times out too early and re-fills the name field after the sign-in hop (the authenticated pre-join has NO name field — that's normal).

**B. Media-off not guaranteed.** The join loop's `get_by_label("camera"/"mic")` toggles miss on this UI (user screenshot proved camera ON at join; in-meeting `_mute_if_live` fallback also missed the meeting-bar buttons). Need: pre-join — click the camera/mic icon **buttons** (aria-labels observed in the menu dump: "Turn camera off"/"Mute mic" appear only when ON; the pre-join buttons have labels like "Camera"/"Microphone" with state); in-meeting — the meeting-bar buttons expose aria-label "Mute"/"Unmute" (check `data-tid` or `aria-pressed` states).

## 4. How to fix (concrete plan for next session)

1. Register `context.on("page", …)` handler BEFORE the pre-join sign-in click; keep a `login_pages` list; in the join loop, for each login page: fill `input[type=email]` (submit via `#idSIButton9` / `button[type=submit]`), then `input[type=password]`, then "Yes"/"Next"; poll up to 60 s. If after clicking "Sign in" the SAME page navigates (URL contains login.microsoftonline), drive the same page.
2. Only fill the name if a `text` input is VISIBLE (`is_visible()`, not just count) and skip after the first successful fill; treat "Join now" absence while a login page exists as "wait", not "fail".
3. Media off: pre-join — click by aria-label "Camera"/"Mic"/"Microphone" **buttons** (get_by_role("button", name=…)); if camera tile shows video, press `space` after focusing. In-meeting — use `aria-label*='Mute'` / `'Turn camera off'` (these labels only render when live) on meeting-bar buttons.
4. Verify captions toggle in the meeting bar "More(…)" flyout: the menu dump shows a 'Captions' entry; click `get_by_text('Captions')` inside the flyout, then a submenu may need "Turn on live captions" — dump the submenu too.
5. Keep the `join_failed_*` screenshot+body dumps (transcripts/) — they solved every mystery so far.
6. Re-test loop: create a personal test meeting, run `listen(url, max_minutes=4)`, watch: participant list identity, mic/cam icons, captions bar, then paste a forms link in chat → expect Telegram relay within ~10 s.

## 5. Commands & locations

```bash
# stack
cd E:/agent/placement-intelligence/infrastructure && docker compose up -d
# listener CLI (host, .env sourced for the LLM key):
set -a; . ./infrastructure/.env; set +a
.venv/Scripts/python -m pia_worker.teams.listener "<meeting-url>" --minutes 4
# re-save session if expired:
.venv/Scripts/python -m pia_worker.teams.login_save
# gates
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy apps/api/src apps/worker/src packages/shared/src && .venv/Scripts/python -m pytest -q
```
Files: `apps/worker/src/pia_worker/teams/{listener.py,login_save.py,__init__.py (is_teams_url)}`, tests `tests/unit/test_ask.py` etc., diagnostics in `transcripts/`, env keys `TEAMS_EMAIL/TEAMS_PASSWORD/EMBEDDING_MODEL/NOTIFY_*`. Docs: 00–09 + this handoff; thresholds: docs/07, docs/08.
