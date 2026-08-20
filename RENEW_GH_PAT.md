# Renew GH_PAT (Sprinklr session write-back token)

The Netflix SLA refresh uses a GitHub fine-grained personal access token stored as the
repository secret `GH_PAT`. The GitHub Actions job uses this token to write the refreshed
Sprinklr session back into the `SPRINKLR_SESSION_JSON` secret after every successful run,
which keeps the Sprinklr login alive and removes the routine 2-to-3-day manual re-login.

The token created on 2026-08-20 was set with a 90-day expiration, so it expires around
2026-11-18. When it expires, the write-back step stops working silently. The step is
guarded, so nothing breaks or errors: it simply skips the write-back, and from that point
the Sprinklr session goes back to expiring every 2 to 3 days and needs manual re-login.
A one-time reminder is scheduled for 2026-11-11 to renew before that happens.

## Steps to renew

1. Create a new fine-grained token at https://github.com/settings/personal-access-tokens/new
   - Token name: `sla-session-writeback`
   - Resource owner: `HarshKPB`
   - Repository access: Only select repositories, then pick `netflix-ms-sla-refresh`
   - Permissions: Repository permissions, then Secrets, set Access to Read and write. Leave every other permission at No access.
   - Expiration: 90 days
2. Generate the token and copy it. It starts with `github_pat_`.
3. Store it as the secret. Run the command below and paste the token at the prompt. Do not paste the token into any chat and do not put it in a command argument, so it stays out of shell history.

   ```
   cd ~/Desktop/MISC/netflix-ms-sla-refresh && gh secret set GH_PAT
   ```

4. Verify the write-back works. Trigger a run:

   ```
   cd ~/Desktop/MISC/netflix-ms-sla-refresh && gh workflow run "SLA refresh"
   ```

   Then open the run in the Actions tab and check the step named "Persist refreshed Sprinklr
   session". It should log: `Refreshed Sprinklr session written back to secret.`

5. Delete the old expired token from https://github.com/settings/personal-access-tokens so
   there is no stale credential lying around.

## How to check the current GH_PAT set date

```
cd ~/Desktop/MISC/netflix-ms-sla-refresh && gh secret list
```

The `GH_PAT` row shows when it was last set. The set date plus your chosen expiration is the
expiry. GitHub does not expose a fine-grained token's expiry date to the CLI, so track it
from the creation date.
