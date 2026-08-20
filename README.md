# Netflix MS SLA refresh

This repository runs an automated job that builds a single service level agreement (SLA)
tracking table for the Netflix Managed Services queue and writes it into a Google Sheet. The
job runs in GitHub Actions on a schedule, so it does not depend on any laptop being on. No
credentials are stored in the code. Every credential is provided at run time from an encrypted
GitHub Actions repository secret.

As of the last verified run on 2026-08-20, the job produced 180 rows: 146 from Slack, 32 from
Sprinklr, and 2 from other Asana tasks.

## What the job does, end to end

Each run joins three separate sources into one coherent table. The first source is the
Sprinklr request dashboard, read by a headless browser (Playwright) using a saved login
session. The second source is Asana, read through the Asana REST API. The third source is a
Slack intake Google Sheet that records, for every Slack request, the original message
timestamp and the person who reacted with the ticket emoji. The job matches these three
sources together, computes two SLA durations per request, sorts the rows newest first, and
writes the result to the target Google Sheet. It also writes a second tab that documents every
column in plain language.

The run is safe to repeat. If it cannot build any rows, it exits without touching the sheet,
so a bad run never blanks the existing data.

## The output sheet

The target sheet has two tabs, both rebuilt from scratch on every run.

The first tab, named `SLA`, holds one row per request with the eight columns described below.
The second tab, named `Definitions`, restates each column in simple language along with how it
is calculated, so a reader who has never seen the sheet can understand it without asking.

### Columns in the SLA tab

The table below lists each column, what it means, and how it is worked out. Durations are shown
as `Xh Ym`, meaning hours and minutes. Timestamps are shown in India Standard Time (IST) in the
format `yyyy-mm-dd HH:MM`.

| Column | Meaning | How it is calculated |
| --- | --- | --- |
| `source` | Where the request came from. | `Slack`, `Sprinklr`, or `Other`, decided from the task name. |
| `task_name` | Short title of the request. | Sprinklr: case number and subject. Slack: sender name and first line of the message. |
| `assignee` | Person handling the request. | Sprinklr: the assigned person. Slack: the Asana assignee, or if none, the person who added the ticket emoji. |
| `request_arrival_time` | When the request first came in. | Sprinklr: case created time. Slack: time of the first Slack message. |
| `Assigned/Emoji initiated` | When work was picked up. | Sprinklr: time the case was assigned to a person. Slack: time the ticket emoji was added, which creates the Asana task. |
| `SLA_Assignement` | Time from arrival to pick-up. | `Assigned/Emoji initiated` minus `request_arrival_time`. Blank if either time is missing. |
| `Request_fulfil_time` | When the request was finished. | Sprinklr: time the status became COMPLETED. Slack: time the Asana task was marked complete. Blank if still open. |
| `SLA_completion` | Time from pick-up to finish. | `Request_fulfil_time` minus `Assigned/Emoji initiated`. Blank if not finished, or if there is no pick-up time to measure from. |

A blank `Request_fulfil_time` or `SLA_completion` is not an error. It means the task is still
open, and it fills in automatically once the task is completed. This was verified on 2026-08-20:
every blank matched a task that was genuinely not yet completed in Asana or Sprinklr.

## How it runs

The workflow is defined in `.github/workflows/refresh.yml`. It runs on a cron schedule of
`0 */6 * * *`, which is every six hours in UTC, and it can also be started by hand from the
Actions tab using the Run workflow button. A concurrency group prevents two runs from
overlapping.

Each run performs these steps. It checks out the repository, sets up Python 3.12, installs the
pinned dependencies from `requirements.txt`, and installs the Chromium browser that Playwright
drives. It then writes the saved Sprinklr session from a secret to a file on the runner. Next
it runs `sla_refresh.py`, which does the scrape, the API reads, the join, and the sheet write.
Finally, if the run succeeded, it writes the refreshed Sprinklr session back into the secret so
the next run starts from a fresh session. That last step is described in detail below.

## Credentials (repository secrets)

All credentials live in GitHub as encrypted Actions secrets, set under Settings, then Secrets
and variables, then Actions. They are never committed to the repository. The `.gitignore`
blocks the local files that hold them.

| Secret | What it is |
| --- | --- |
| `ASANA_PAT` | Asana personal access token, used to read tasks from the Netflix Asana project. |
| `GOOGLE_SA_JSON` | Full JSON key of a Google service account that can edit the SLA sheet and read the intake sheet. |
| `SPRINKLR_SESSION_JSON` | Contents of a valid Sprinklr Playwright session (a saved login), used for the dashboard scrape. |
| `SLA_SHEET_ID` | The Google Sheet id of the target SLA sheet. |
| `INTAKE_SHEET_ID` | The Google Sheet id of the Slack intake log. |
| `GH_PAT` | A fine-grained GitHub token with Secrets read and write on this repository only. Lets the job update the `SPRINKLR_SESSION_JSON` secret with the refreshed session. Optional: without it the job still runs, but the Sprinklr session is not kept alive. |

## The Google service account

The job reads and writes Google Sheets as a service account, not as a person. This is what
makes it laptop independent, because a service account does not need an interactive Google
login. The service account email must be given access to both sheets: Editor on the SLA sheet
so the job can write it, and Viewer on the intake sheet so the job can read it. The service
account currently in use is `sla-writer@sla-refresh.iam.gserviceaccount.com`. If the key is ever
rotated, replace the `GOOGLE_SA_JSON` secret with the new JSON key and re-share both sheets with
the new email if the email changes.

## The Sprinklr session, and how it is kept alive

Sprinklr does not offer a usable API for this queue, so the only way to read the request list is
to load the dashboard in a browser and capture the data it fetches. The browser logs in using a
saved session rather than a password. That saved session is the `SPRINKLR_SESSION_JSON` secret.

The catch is that a Sprinklr session expires. Measured session lifetime is two to three days.
The login itself cannot be automated, because it goes through Netflix single sign-on and a
second factor, which a headless cloud runner cannot complete.

To reduce how often a human has to log in, the job keeps the session alive. On every successful
run the scrape saves a refreshed session to disk, and the final workflow step pushes that
refreshed session back into the `SPRINKLR_SESSION_JSON` secret. Because the job runs every six
hours, the session is refreshed long before it would go idle, so in normal operation it should
not reach the two to three day expiry. This write-back requires the `GH_PAT` secret. The step is
guarded, so if `GH_PAT` is missing or invalid it simply skips the write-back and logs that it
did so, rather than failing the run.

Two situations still force a manual login. The first is a hard logout on the Sprinklr side, for
example a forced session reset or a password change. The second is single session eviction: if a
person logs into the same Sprinklr account in their own browser, that can knock out the
automation session. Neither can be prevented from this job.

Whether the two to three day cap truly slides forward on activity, rather than being a fixed
ceiling, was not yet confirmed as of 2026-08-20. It becomes clear over time. If no run fails
past the current cap, the sliding behavior is confirmed.

### Refreshing the Sprinklr session by hand

When a run fails with a session or login error, produce a fresh session on a local machine by
running the project's Sprinklr login flow, which opens a browser and prompts for the second
factor. That writes the file `~/.netflix_sprinklr_session.json`. Then update the secret from
that file:

```
gh secret set SPRINKLR_SESSION_JSON < ~/.netflix_sprinklr_session.json
```

Then trigger a run to confirm it works:

```
gh workflow run "SLA refresh"
```

## Renewing the GH_PAT token

The `GH_PAT` token has an expiration. When it expires, the session write-back silently stops and
Sprinklr goes back to needing a manual login every two to three days. The step by step renewal
process is in `RENEW_GH_PAT.md`. In short, create a new fine-grained token scoped to this
repository with Secrets read and write, store it with `gh secret set GH_PAT`, run the workflow
once to confirm the write-back logs its success line, and delete the old token. The token in use
was created on 2026-08-20 with a 90 day expiration, so it expires around 2026-11-18, and a
reminder is scheduled for 2026-11-11.

## Running it locally

The job is designed for the cloud, but it can be run on a machine for debugging. Create a Python
virtual environment, install the dependencies, install Chromium, and export the same environment
variables that the workflow provides from secrets. The required variables are `ASANA_PAT`,
`GOOGLE_SA_JSON` holding the service account JSON, `SLA_SHEET_ID`, and `INTAKE_SHEET_ID`, plus a
valid `~/.netflix_sprinklr_session.json` on disk. Then run `python sla_refresh.py`. The script
prints how many rows it wrote and the breakdown by source.

## Files in this repository

The table below lists each tracked file and its purpose.

| File | Purpose |
| --- | --- |
| `sla_refresh.py` | The main job. Scrapes Sprinklr, reads Asana and the intake sheet, builds the table, writes both tabs, and formats them. |
| `sprinklr_to_asana.py` | The Sprinklr dashboard scrape, imported by the main job. Also saves the refreshed session on success. |
| `health.py` | Heartbeat and notification helpers used by the scrape. |
| `requirements.txt` | Pinned Python dependencies. Playwright is pinned because it ships a matching browser build. |
| `.github/workflows/refresh.yml` | The scheduled GitHub Actions workflow. |
| `.gitignore` | Blocks local secret, session, and service account files from being committed. |
| `RENEW_GH_PAT.md` | Step by step guide to renew the `GH_PAT` token before it expires. |
| `README.md` | This document. |

## Troubleshooting

If a run fails at the scrape step with a session or login error, the Sprinklr session has
expired and needs the manual refresh described above. If a run fails with a Google permissions
error, confirm the service account still has Editor on the SLA sheet and Viewer on the intake
sheet. If the SLA tab fills but the Sprinklr rows are stale while Slack and Asana rows are
current, the Sprinklr session is likely the problem even if the run reported success, so refresh
it. If the write-back step logs that it skipped because `GH_PAT` is missing, the token is unset
or expired and should be renewed.
