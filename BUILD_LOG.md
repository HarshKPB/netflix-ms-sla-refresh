# Build log

How the Netflix MS SLA dashboard is built and operated, so a cold reader can run,
refresh, and extend it. Reconstructed on 2026-10-30 from git history and the session
decision logs. For the why behind each choice see DECISIONS.md; for the dated feature
list see CHANGELOG.md.

## What it is

A password-gated static dashboard on Vercel (netflix-sla.vercel.app, repo
HarshKPB/netflix-ms-sla-refresh) with four tabs: SLA Queue, Sprinklr Support Tickets,
Queue SLA (reporting), and Sprinklr SLA. It reports Premium Blend's responsiveness on
the Netflix Managed Services request queue and on care@ escalation tickets.

## Data flow

1. `sla_refresh.py` (the pipeline, run by `.github/workflows/refresh.yml` every 6 hours)
   builds `web/data.json` (gitignored, generated in-job, deployed to Vercel) from three
   sources:
   - Slack queue rows: Asana tasks read directly from the Netflix MS project
     (`fetch_asana`, `GET /tasks`, project 1214792401514445, ASANA_PAT).
   - Enrichment: the intake Google Sheet "Requests" tab (`load_intake`, service
     account), joined by Asana gid, adds arrival ts, first-reply ts, requester
     region/country, requester email.
   - Sprinklr rows: the Sprinklr dashboard scrape (`sprinklr_to_asana.fetch_cases_from_dashboard`,
     Playwright with a stored SSO session).
2. Two curated maps are merged onto the rows at build time:
   - `external_map.json`: external/internal per request (keys: Slack message-ts,
     thread-ts, Asana gid).
   - `sprinklr_external.json`: external/internal per Sprinklr form case (key:
     intakeResponseId).
3. `user_type` (FTE / Contractor / Agency) is computed in `user_type_of` from the
   requester email: the intake-sheet email for Slack rows, the "Form Requesters" tab
   (`load_form_requesters`, by intake_response_id) for Sprinklr rows, the task-name
   email for Gmail-inbound `[Reactivation]` tasks.
4. care@ tickets (the Sprinklr Support Tickets tab) are built separately by
   `tickets_pipeline` into `web/tickets.json`, with `origin_map.json` merged for the
   escalation lane.

## Run it

- Full refresh (writes the sheet + data.json + deploys), normally the Action:
  `gh workflow run "SLA refresh"` (or the scheduled 6-hour run).
- Verify a run and read the proof lines:
  `gh run view <id> --log | grep -iE "external coverage|user type:|wrote [0-9]+ rows:"`
  A healthy run prints `unknown 0` for both sources.
- Re-mint the Sprinklr session when the banner shows it is down (needs Harsh for 2FA):
  `./remint_sprinklr.sh` then `gh secret set SPRINKLR_SESSION_JSON` and
  `gh workflow run "SLA refresh"`. See RENEW_GH_PAT.md and METHODOLOGY.md.

## Keeping the classification current

- The external/internal map auto-refreshes weekly via the cloud routine
  `trig_01PkeqLQ7odJruQ2wR8epopb` (reads new Slack threads + reactivation emails, opens
  a PR). The rule it follows is `docs/EXTERNAL_INVOLVEMENT_RULE.md`.
- The Sprinklr requester emails ("Form Requesters" tab) are refreshed every 30 minutes
  by the request-mix pipeline, so `user_type` for Sprinklr stays current.
- New requests that arrive between refreshes show as Unclassified (external) or Unknown
  (user type); neither is ever counted as internal/FTE. They resolve on the next map
  refresh.

## Verified coverage (2026-10-30)

- External: Slack 50 external / 257 internal / 0 unknown of 307; Sprinklr 10 / 24 / 0
  of 34.
- User type: FTE 247, Agency 51, Contractor 43, 0 Unknown, of 341 rows.

## Notes and gotchas

- `web/data.json` is gitignored and generated in-job, so there is no web-only deploy;
  the full refresh workflow is the deploy path.
- The dashboard is password-gated (web/middleware.js), so `data.json` cannot be fetched
  for local inspection; the Action log is the verification channel.
- `git push` and `gh pr create` can be blocked by the harness auto-mode guard; the repo
  carries `.claude/settings.local.json` allowing them, which loads in a fresh session.
- Pipeline edits that must not change the Google Sheet add keys only to the data.json
  row comprehension, never to HEADERS.
