# Decisions

Architecture and approach decisions for the Netflix MS SLA dashboard, newest first.
Each block is the decision, why it was taken, and what was rejected. Reconstructed on
2026-10-30 from the per-session decision logs; the full dated line-by-line trail lives
in `~/.claude/session-logs/netflix-ms-sla-refresh/*/decisions.log`.

## User type from email domain, computed in one place
2026-10-30. `user_type` (FTE / Contractor / Agency) is derived from the requester
email domain in `sla_refresh.user_type_of`, applied to both Slack and Sprinklr rows.
why: the data already exists (the intake sheet carries Requester Email and a
bot-resolved Requester Org), so no manual classification is needed; keeping the rule
in one function stops the two sides from drifting.
rejected: having the request-mix pipeline emit a `user_type` column too (two
implementations could disagree). It emits the email only; the dashboard owns the rule.

## Sprinklr requester join on intake_response_id, via a sheet tab
2026-10-30. Sprinklr rows get their requester email from a "Form Requesters" tab the
request-mix pipeline writes, joined on `intake_response_id` (the bare #NNN).
why: the dashboard scrape carries no requester email, and a sheet tab is a clean
machine-readable handoff that `sla_refresh` already has access to.
rejected: an Asana custom field (the dashboard's Asana token is a guest, 402), parsing
Asana task comments (messy, slow across 92 tasks), and Asana notes (clobbered by the
6-hour sync). Also rejected joining on Asana gid, because the dashboard's Sprinklr rows
come from the scrape and carry no gid.

## External involvement read from Slack threads into a committed map
2026-10-29. Each queue request is labelled external/internal by reading its actual
Slack thread, and the result is committed to `external_map.json` for the pipeline to
merge.
why: the cloud pipeline has no Slack access and the intake sheet's Thread Summary is
about 21 percent populated (declining to 10 percent), so neither can produce the label
at build time; a curated map that the pipeline merges is durable and auto-refreshes.
rejected: an in-pipeline detector over the Thread Summary column (confirmed undercount,
it missed real escalations); a pure keyword detector (false positives on generic
closings, false negatives on reworded escalations), replaced by per-thread semantic
judgment from Premium Blend sender lines only.

## external_map keyed by ts and gid, with a gid fallback
2026-10-29. The map carries Slack message-ts, thread-ts, and Asana gid keys; the
Slack branch looks up by ts, then falls back to the task gid.
why: the pipeline keys rows by Message TS, but classification is per thread (Thread
TS), and 10 Gmail-inbound `[Reactivation]` tasks have no sheet row at all (no ts), only
a gid. Three-way keying resolves all 307 Slack rows with zero unknown.

## Weekly cloud routine for the auto-refresh, not local or detector-only
2026-10-29. A claude.ai cloud routine re-reads new Slack threads and reactivation
emails via connectors and opens a PR to keep `external_map` current.
why: the user wanted it fully automatic and machine-independent; the routine inherits
the account's Slack / Gmail / Sheets connectors, which a cloud agent can use.
rejected: a local launchd job (dies when the Mac sleeps) and a cloud detector that only
flags new requests for a human to classify (not fully automatic).

## care@ origin mapping by lane; dedupe by ConversationId
2026-10-28. care@ escalations are mapped to their originating lane (Slack, Sprinklr
form, or direct email) in `origin_map.json`, and duplicate ticket rows are collapsed by
Sprinklr ConversationId.
why: a naive sender-split mis-blamed Premium Blend for Sprinklr-internal stalls, and
ticket threads without a literal "Ticket Number" were spawning `thread:<hex>` stub
rows; ConversationId is the stable per-case id.
rejected: text-only ticket-number matching (missed reopen and scheduling fragments) and
treating low Slack linkage as the true intake rate (emails often do not cite the
upstream request, so it is a floor, not the rate).

## Queue metadata from Asana custom fields
2026-10-22. Region, Category, Data Type, and Priority for the queue come from Asana
custom fields, not from guessing the request wording.
why: a probe showed about 293 of 400 tasks carry these fields, giving one consistent
Netflix taxonomy; the text detector was 73 percent "Other".
rejected: keeping the `detect_*` text heuristics for the queue (dropped).

## First-response SLA from the intake sheet, not fetched in the pipeline
2026-10-21. First-human-response time is read from the intake sheet "First PB Reply TS"
column the Ticket Bot stamps, not fetched from Slack inside `sla_refresh`.
why: the bot already owns Slack and stamps the column hourly; in-pipeline
conversations.replies hit channel_not_found on the support channel (4 of 296). The bot
owns Slack, the sheet is the handoff, the pipeline reads the sheet, same pattern as
arrival and pickup.
rejected: a Slack bot token in the pipeline (dual-writer race, scope round-trips) and a
second cloud routine writing data.json (dual-writer race).

## Median-led Queue SLA, PB team only
2026-10-15. The Queue SLA tab leads with the median (mean shown underneath) and counts
only @premium-blend.com assignees (plus unassigned).
why: pickup is usually minutes, so a few multi-day outliers make the mean misleading;
the report measures Premium Blend responsiveness, so Sprinklr-agent and Netflix names
do not belong in the per-person view. Done in the tab JS so the raw data keeps every
row.

## Single tickets_pipeline architecture
2026-09-28. Replaced ad-hoc scripts with one `tickets_pipeline` (pull, parse, classify,
override, validate, write) called by both local runs and the cloud routine, gated by a
JSON schema and a labelled regression test.
why: reactive symptom-patching was causing field drift (for example days_open vs
age_days) and violated the plan-first and test-before-scale rules.
rejected: continuing to patch the separate build / classify / merge scripts.

## Sprinklr session resilience instead of elimination
2026-10-21. The browser session cannot be removed (no Sprinklr API credential for the
Netflix tenant; the dashboard needs Netflix SSO and 2FA), so failure is made loud and
recovery trivial: a `sprinklr_ok` flag, an auto-opened GitHub issue, a dashboard
banner, a one-command re-mint, and session write-back each run.
why: the root cause of the roughly 5-day session death is not visible from logs
(absolute TTL vs SSO policy vs login elsewhere), so detect-and-recover beats chasing it.
