# Sprinklr Support Tickets tab: methodology and architecture

## Aim

The Sprinklr Support Tickets tab on the Netflix Managed Services dashboard shows every support case the Premium Blend team has raised with Sprinklr's vendor support desk (care@prod.sprinklrsupport.com), each with an accurate status, the Premium Blend owner who opened it, timing, and a link to the authoritative case in Sprinklr's portal. The team can then see at a glance which cases sit with them, which sit with Sprinklr, which are resolved, and how long cases take to close. The data is derived from email because the Premium Blend team has no login to Sprinklr's support community portal and no support API is available to Premium Blend.

## The failure this rebuild corrects

The first implementation grew reactively and violated the working rules in CLAUDE.md. The classification logic was copied into four separate places (classify_status.py, merge_status.py, reviewed_status.json, and the scheduled cloud routine's prompt), so the copies drifted. Field names drifted between the builder and the web page, and a blank "Days open" column shipped to production because the page read a field named age_days while the routine wrote a field named days_open. Status corrections had no authoritative, shared path, so a person who saw a wrong status could not fix it for the team. There was no schema check and no automated test, so defects were discovered in production instead of before deployment, and production became the development loop. This methodology replaces that with one pipeline, one data contract, one test gate, and one shared override store.

## Data contract

Every row in web/tickets.json conforms to one fixed schema, defined once in tickets_pipeline/schema.py: case_id (string), title (string), topic (string), priority (Urgent, High, or Normal), status (Awaiting us, Awaiting Sprinklr, or Resolved), owner (string), agent (string), opened (date), last_update (date), days_open (integer), days_idle (integer), n_msgs (integer), portal_link (string), gmail_link (string), evidence (string), needs_review (boolean), source (Auto or Override). The build validates every row against this schema and refuses to write or deploy if any row violates it. The web page reads only these field names. Because the schema is the single definition both sides use, a drift such as days_open versus age_days cannot recur, because a missing or misnamed field fails the build rather than shipping blank.

## One pipeline

A single module, tickets_pipeline, is the only code that produces web/tickets.json. It runs five stages in order. Stage one, pull, searches Gmail for care@prod.sprinklrsupport.com threads over the requested window. Stage two, parse, groups messages by the seven digit Sprinklr Ticket Number, because one case can span several email threads. Stage three, classify, derives status, owner, dates, topic, and priority from the message bodies using the rules stated below. Stage four, override, applies human corrections from overrides.json, which win over the classifier. Stage five, validate and write, asserts the schema and the locked truth set and then emits web/tickets.json. Both the local run and the scheduled cloud routine call this same module. The routine's prompt no longer restates the rules; it runs the module. This removes every duplicated copy of the logic and is the root fix for the drift defects.

## Classification rules, stated once

Status is read from the latest substantive message in a case. Sprinklr's automated "we are waiting to hear back from you" or "we usually assume the case is solved" follow-up, appearing as the latest message, means the case is auto-closing and is therefore Resolved. A Sprinklr message that closes or answers the case (any closure wording, or an explanation that resolves it) is Resolved. A Sprinklr message that asks Premium Blend for information is Awaiting us. A Sprinklr message that says the team is still investigating, is with engineering, or will follow up is Awaiting Sprinklr. A Premium Blend message that thanks or asks to close is Resolved, and any other Premium Blend message is Awaiting Sprinklr. Owner is the Premium Blend person who initiated the case, taken as the earliest address ending in premium-blend.com found anywhere in the thread body including quoted history, excluding the group alias netflix@premium-blend.com; addresses ending in netflix.com are the client and are never the owner. The opened date is the earliest timestamp seen anywhere in the thread, so a case first raised before the pull window still shows its true age. days_open is the count of days from opened to today, and days_idle is the count of days from the last message to today.

## Human overrides, shared and authoritative

Status inferred from email cannot be perfect, so any team member can correct a case's status from a dropdown in the tab (Awaiting us, Awaiting Sprinklr, Resolved, or Auto to release the correction). The dropdown posts the correction to a serverless route at web/api/override, which stores it in overrides.json in the repository. The pipeline applies overrides last, so a human correction wins over the classifier, holds for every viewer, and is also recorded in status_truth.json so the classifier is measured against it and never reverts it. A row corrected by a human carries source set to Override so the origin of its status is visible.

## Tests, the deploy gate

A labeled set of cases with known correct statuses, built from Harsh's reviews on 2026-09-28, is stored in status_truth.json. The build asserts its output matches every labeled case; a mismatch fails the build and blocks the deploy. Every future correction is added to this set, so a case that has been fixed once can never silently break again.

## Deploy discipline

The pipeline runs and validates before anything is published. Only a build that passes both the schema and the truth set is committed and deployed. Production is not used as the development loop.
