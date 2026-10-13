# EXPLAIN

Ten questions a reviewer or interviewer is likely to ask about this repo, with short plain-English answers and pointers to the exact file, function, or n8n node that implements each.

## 1. What makes the tenant sync idempotent, and why upsert by `external_id`?

The platform's `POST /platform/v1/tenants` writes with `INSERT ... ON CONFLICT(external_id) DO UPDATE` (see `mock_apis/main.py:upsert_tenant`). `external_id` is the ERP's primary key (`huurder_id`), which is the only identifier guaranteed stable across both systems. Names change, units are reassigned, KvK numbers get corrected. Re-running WF1 on the same ERP data produces zero changes, which is the test `tests/test_auth_and_errors.py::test_upsert_is_idempotent_on_same_payload` covers.

## 2. How does the workflow react differently to 401, 403, 422, 429 and 503, and why?

In WF1's **Classify response** Code node (`workflows/01-erp-to-platform-tenant-sync.json`), the HTTP node is set to `neverError=true` so the response always flows through and we decide the branch:

- **401** (bad or missing key) → the whole run is broken; the error is recorded, the next scheduled run will try again. Humans fix the credential.
- **403** (read-only key on a write) → same shape as 401 for the error log, but suggested fix is different.
- **409** (external_id conflict) → stop retrying this row. A different KvK on the platform means a human has to decide which one is correct; retrying would only hide the collision.
- **422** (validation) → stop retrying, record which `codes` tripped, link to the mapping rule.
- **429** → the HTTP node's built-in retry respects `Retry-After` and retries up to 3 times before falling into the error branch. Only logs after we really can't catch up.
- **503** → same as 429 but with exponential-ish `waitBetweenTries`. Transient by assumption.

## 3. Why does `Retry-After` matter, and where is it handled?

Honouring `Retry-After` is the difference between backing off and hammering someone's server. The mock sets a numeric `Retry-After` header on both `429` and `503` (`mock_apis/main.py:_rate_limit` and `_maybe_fault`). The n8n `httpRequest` node (`typeVersion` 4.2) consumes `Retry-After` automatically when retries are enabled on the node, which is set in **Fetch ERP tenants** and **Upsert tenant** via `retryOnFail: true`, `maxTries: 3`, `waitBetweenTries: 1500`.

## 4. How does the mapping turn `"1.250,00"` and `DD-MM-YYYY` into canonical values? What breaks if the locale assumption is wrong?

The **Map & validate** Code node in WF1 strips currency, drops `.` as the thousands separator, treats `,` as the decimal, and parses dates with `DD-MM-YYYY` → `YYYY-MM-DD`. Rules:

- Amount: `"1.250,00"` → `"1250.00"` → `1250.00`; `"duizend euro"` → `invalid_amount`.
- Date: `"15-02-2025"` → `"2025-02-15"`; a `"32-13-2024"` round-trip check kicks it out as `invalid_date`.

If the ERP's locale shifts (e.g. a feed starts emitting `"1,250.00"` US-style), the amount parser silently returns `1.25`, which is a nasty bug class. The fix is a canary row per source centre whose expected magnitude is known; the sync fails if the parsed value is off by three orders of magnitude.

## 5. What makes the migration safe to re-run? What is the Idempotency-Key made of, and why those fields?

Two layers:

- The platform's `UNIQUE(external_id, centre, period)` constraint catches duplicates at the row level inside the batch.
- The migration (`scripts/migrate_turnover.py:post_batches`) sends an `Idempotency-Key` header per batch = `sha256("BATCH|{centre}|{external_id}|{period}|{len}|{offset}")`. The platform stores the response body against that key (`idempotency_keys` table). Replaying the exact batch returns the exact first response, without a second insert attempt.

The in-row key uses `(centre, external_id, period, amount)` because those four identify "one centre-tenant-month-amount tuple". `amount` is included because a late correction row that overwrites March 2025 with a different number is a legitimate new row.

## 6. How do the reconciliation numbers prove nothing was lost?

The migration counts four buckets: `read`, `imported`, `duplicates` (local + server), `rejected` (local + server). The report checks `imported + duplicates + rejected == read` and prints `OK` or `MISMATCH` in `reports/migration_report.md`. If that assertion ever fails, something was dropped silently and the invariant surfaces it. The test for this is `tests/test_migrate.py::test_end_to_end_dry_run_reconciles`.

## 7. What does the Error Trigger workflow catch that the IF branch does not?

The WF1 IF branch catches **expected** failures: the HTTP step completed, we got back a 4xx/5xx, we recorded it. The Error Trigger (`workflows/03-catch-wf-failures.json`) catches **unexpected** failures: a Code node threw, the n8n worker killed the execution, a credential disappeared. It wraps those as an integration-error row too, so one view (`GET /platform/v1/integration-errors?status=new`) covers both.

## 8. How does the Claude triage step work, why structured output, and what are the eval's limits?

The **AI Agent** node in WF2 is wired to **Anthropic Chat Model** + a **Structured Output Parser** that forces the response to a fixed JSON shape (`category`, `likely_cause`, `suggested_fix`, `client_message`, `confidence`). Structured output matters because the next node is a plain `PATCH /platform/v1/integration-errors/{id}`. If the LLM free-formed its answer, the PATCH would fail validation.

Eval limits (why 95% is honest, not generous):
- Cases are hand-labelled by a human (me), not scraped. 20 cases, ≥2 per category.
- Cases include one `mapping-02` where the error body has no `codes[]` list, just a Pydantic-style `fields` array. The rule classifier labels it `validation` where a careful human (and Claude) would label it `mapping`. That case is kept intentionally; the eval is not tuned to the model.
- The eval doesn't measure the quality of `client_message` or `suggested_fix`; those need human review.

## 9. What would break first at 100x the data volume, and what would you change (pagination, batching, rate limits, queue mode)?

At 100x, three things break roughly in this order:

1. **Fetch**: the ERP endpoint returns everything in one JSON array. Fix: add `page_token` + `size` to `/erp/v1/huurders` and loop in WF1 with a Split-In-Batches node around the HTTP step.
2. **Rate limit**: the global 5 rps starves parallel runs. Fix: lift to a per-API-key quota on the platform side; add jittered backoff in n8n (set `waitBetweenTries` with a small random offset) to avoid thundering-herd retries.
3. **Single n8n worker**: a 15-min schedule running tens of thousands of rows in one tick spikes memory. Fix: switch n8n to **queue mode** (Redis + worker pool) and move the per-row Code work into one HTTP step with `send multiple items` to the platform's batch upsert.

The migration script is already batched; the only change there would be raising the batch size to the platform's cap and tracking batch-level `Idempotency-Key` in a sidecar table so a resumed run is cheap.

## 10. A client says "tenant X is missing on the platform". Walk through the debugging steps using this system.

1. Hit `GET /platform/v1/tenants/{external_id}`. If it's there, the complaint is about freshness, not loss. Check WF1's last run time and the row's `updated_at`.
2. Not there → `GET /platform/v1/integration-errors?status=new` and search by payload. If the row is in that list, read the attached `triage` block for category and client-friendly message.
3. Still nothing → check that the ERP actually emits it: `GET /erp/v1/huurders?updated_since=1970-01-01` and grep for the `huurder_id`.
4. If the ERP has it but the sync didn't pick it up, open n8n → executions for `erp-to-platform-tenant-sync` → filter by date around the ERP `gewijzigd_op`. Nothing there means the watermark is wrong; set the webhook payload `{"updated_since":"..."}` to force a resync of that window.
5. If the sync picked it up but silently dropped it (shouldn't happen; the Error Trigger would have caught it), reach for the Error Trigger logs via `source LIKE 'n8n:%'` in `integration_errors`.

That's six lookups max, every one a plain HTTP call. The reason that works is point 7 above: expected failures and unexpected failures land in the same table.
