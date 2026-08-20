# Netflix MS SLA refresh

Laptop independent. GitHub Actions runs every 6 hours, scrapes the Sprinklr dashboard,
reads Asana and the Slack intake sheet, and writes the coherent SLA table into the Google
sheet. No credentials live in this repo. They are provided as repository secrets.

## Columns written

source, task_name, assignee, request_arrival_time, Assigned/Emoji initiated, SLA_Assignement,
Request_fulfil_time, SLA_completion. Definitions are in the SLA project's DATA_DICTIONARY.

## Required repository secrets

Set these under Settings, Secrets and variables, Actions.

| Secret | What it is |
| --- | --- |
| `ASANA_PAT` | Asana personal access token |
| `GOOGLE_SA_JSON` | the full JSON key of a Google service account that can edit the SLA sheet and read the intake sheet |
| `SPRINKLR_SESSION_JSON` | the contents of a valid Sprinklr playwright session (storage_state) |
| `SLA_SHEET_ID` | the target sheet id |
| `INTAKE_SHEET_ID` | the Slack intake sheet id |

## One-time setup

1. Create a Google service account, download its JSON key, and put that JSON in
   `GOOGLE_SA_JSON`.
2. Share the SLA sheet (editor) and the intake sheet (viewer) with the service account email.
3. Add the other secrets above.

## The one recurring manual step

Sprinklr expires the login every few days and requires 2FA, which cannot be automated. When
the run fails with a session error, produce a fresh session locally and update the
`SPRINKLR_SESSION_JSON` secret. Between refreshes the job is fully hands off.

To produce a fresh session locally, run the project's login flow, then read the file
`~/.netflix_sprinklr_session.json` and paste its contents into the secret.

## Run it now

Actions tab, SLA refresh, Run workflow. The log prints how many rows were written.
