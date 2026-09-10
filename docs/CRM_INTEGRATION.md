# EngineerIP CRM Integration

This service is intentionally CRM-agnostic. The existing EngineerIP CRM should remain the source of truth for members, contacts, activity rows, outcomes and reporting. Telephony only supplies factual call lifecycle data.

## Recommended member mapping

Store a telephony extension against each CRM member:

```text
member_id 37 -> extension 103
member_id 38 -> extension 104
```

The CRM sends `member_id` + `extension` with outbound requests. Do not let a browser choose another member identity.

## Recommended call activity mapping

When `call.started` arrives, create or reserve one call activity keyed by `call_id`.

When `call.answered` arrives:

- mark reached/connected true
- set `answered_at`

When `call.completed` arrives:

- set `ended_at`
- store duration
- finalize the activity

When `call.disposition` arrives:

- store disposition and notes
- do not overwrite factual duration/answer state from the carrier

## Idempotency

A webhook can be retried. Use `call_id` as the stable telephony identifier and store the latest lifecycle state. If the CRM has a separate activity primary key, add a unique external identifier such as `telephony_call_id`.

## Reports compatibility

Map the finalized telephony record into the same activity/log rows consumed by `crm_service.crm_analytics`. Do not add an alternative reporting calculation in this service. This preserves the existing rule that dashboard, reports and CSV use one calculation engine.

## Click-to-call UI

The CRM contact page should call the server-side CRM endpoint, which then calls this service:

```text
Browser -> EngineerIP -> Telephony API -> Asterisk
```

Do not make:

```text
Browser -> Asterisk ARI
```

and do not ship the master `TELEPHONY_TOKEN` to a browser.

## Incoming calls

For inbound DID calls, Asterisk should provide the caller number and call identifier to EngineerIP. The CRM should normalize the number, look up a contact, and attach the inbound call activity to that contact when found. Unknown callers can be stored as unmatched activities and linked later.
