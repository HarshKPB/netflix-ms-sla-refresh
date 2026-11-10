# Changelog

Notable changes to the Netflix MS SLA dashboard and its pipeline. Dates are UTC.
Reconstructed on 2026-10-30 from git history and the per-session decision logs.
Routine "chore: auto-refresh Sprinklr tickets" data commits are omitted (the 6-hour
GitHub Action writing refreshed data, not code changes).

## 2026-10-30
### Added
- Queue SLA: User type dimension (FTE / Contractor / Agency / Unknown), as both a
  Break-down-by dimension and filter chips, so the four tiles isolate one requester
  type (PR #4, PR #5).
- Export CSV gains a `user_type` column.
- Sprinklr requester resolution: `sla_refresh.load_form_requesters()` reads the
  "Form Requesters" tab (maintained every 30 min by the request-mix pipeline, which
  resolves the Sprinklr form author to a SCIM email) and maps `intake_response_id`
  to email, so Sprinklr rows carry a user type instead of Unknown (PR #4, 6418b53).
### Changed
- `user_type` derived from the requester email domain (netflix.com = FTE,
  netflixcontractors.com = Contractor, any other domain = Agency). For Gmail-inbound
  `[Reactivation]` tasks with no intake-sheet row, the email is parsed from the task
  name. Live split on 341 rows: FTE 247, Agency 51, Contractor 43, 0 Unknown.

## 2026-10-29
### Added
- Queue SLA: External-involvement view. Overall SLA (all queue requests) plus an
  External subset (requests a Premium Blend person escalated to a team outside PB).
  Filter chips External / Internal / Unclassified feeding the tile math, plus a
  Break-down-by dimension (PR #3, 6739420).
- `external_map.json` (repo root): per-request external/internal classification,
  keyed three ways (Slack message-ts, thread-ts, Asana gid). Built from a manual read
  of all 286 Slack intake threads plus 10 Gmail-reactivation orphan tasks.
- `sprinklr_external.json`: 34 request-form cases (10 external), keyed by
  `intakeResponseId`.
- `docs/EXTERNAL_INVOLVEMENT_RULE.md`: the classification rule and the incremental
  refresh procedure (37c309c).
- Weekly cloud routine (`trig_01PkeqLQ7odJruQ2wR8epopb`, Mon 03:31 UTC) that re-reads
  new Slack threads and reactivation emails via claude.ai connectors and opens a PR,
  so `external_map` stays current with no manual work.
- Per-source coverage log line (external / internal / unknown counts) each run.
### Changed
- Slack branch resolves external involvement by sheet ts, falling back to the Asana
  task gid for rows with no intake-sheet row.
- Production verification: Slack 50 ext / 257 int / 0 unknown of 307; Sprinklr 10 / 24
  / 0 of 34.

## 2026-10-28
### Fixed
- care@ tickets: duplicate rows eliminated by grouping on the Sprinklr ConversationId
  (plus review-link number and a unique-subject fallback) and a dedupe pass that drops
  stale `thread:<hex>` stubs (80a7bca). 78 rows collapsed to 64 distinct.
### Added
- Origin mapping for care@ escalations (Slack / Sprinklr-form / direct-email lanes)
  via `origin_map.json`, surfaced as an Origin column and a per-lane SLA breakdown.
  33 of 64 traced to a logged intake request.
- `escalation-analysis/`: care@ KPIs, the MarTech on-call inventory
  (`!oncall martech-mardata-support`), and the Slack/form/email origin audit.

## 2026-10-23
### Changed
- Queue region now reads the requester region from the intake sheet (bot-resolved per
  requester), with the Asana task Region as fallback; added Requester Country and a
  Country breakdown (b33a4b3).
- Sprinklr ticket tab switched mean to median (mean shown underneath), matching the
  Queue tab (73a8a2b).

## 2026-10-22
### Changed
- Queue metadata (Region / Category / Data Type / Priority) sourced from Asana custom
  fields instead of text-guessing the request wording; added a Priority breakdown
  (6e9abde).

## 2026-10-21
### Added
- First-human-response SLA for the request queue, from the intake sheet
  "First PB Reply TS" column the Ticket Bot stamps (20f4664, then 6124104).
- Sprinklr session resilience: `sprinklr_ok` flag in data.json, an auto-opened GitHub
  issue on session expiry, a dashboard banner, and `remint_sprinklr.sh` for a
  one-command re-mint (ef42ccd).
### Changed
- Queue SLA cards decluttered from 6 to 4 (one per stage, median with mean underneath)
  (6a490e5).

## 2026-10-15
### Added
- Queue SLA reporting tab for the PB request queue (reads data.json): emoji-ack,
  first-response, and completion medians with drill-down (ba917be).
### Changed
- Leads with median, not mean (pickup is usually minutes; a few stale requests inflate
  the mean) (1ff84d3).
- PB team only: non-@premium-blend.com assignees dropped from the per-person breakdown
  (b926dca).
- Assignee identity normalized to one canonical email per person (c388194, af79902).

## 2026-10-14
### Added
- Sprinklr-ticket SLA reporting tab: initial and resolution SLA, drill-down by
  region / channel / type / priority / date (989fb22).
### Fixed
- Regression gate: live user overrides supersede the locked status_truth, so a
  dashboard status change no longer fails every later build (d1648c5).

## 2026-08 to 2026-10 (foundation)
### Added
- `tickets_pipeline` (schema + classify + pipeline): one build module for care@
  tickets with schema and regression gates, incremental merge, and shared user
  overrides via `web/api/override.js` writing `overrides.json`.
- Cloud-hosted Sprinklr-to-Asana sync in GitHub Actions (retired the local launchd
  job), filtered to `#NNN` requests to skip system entities.
- `setup_session.py` (interactive Sprinklr SSO login) and session write-back so the
  fast session eviction self-heals on each run.
