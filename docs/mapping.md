# ERP → Platform field mapping

The ERP speaks Dutch and uses NL locale conventions. The platform speaks English and expects ISO-canonical values. Every row that reaches the platform has gone through this mapping. Every row that fails this mapping gets written to `POST /platform/v1/integration-errors` with the exact rule it tripped, so support can see at a glance what to fix.

## Table

| ERP field (`/erp/v1/huurders`) | Platform field (`POST /platform/v1/tenants`) | Transform | Validation | Example in → out |
|---|---|---|---|---|
| `huurder_id` | `external_id` | copy as-is | non-empty string | `"H-0001"` → `"H-0001"` |
| `naam` | `name` | trim whitespace | non-empty string | `" Fietspad Koffie "` → `"Fietspad Koffie"` |
| `kvk_nummer` | `chamber_of_commerce_id` | strip non-digits | must be exactly 8 digits after stripping; otherwise reject | `"12 345 678"` → `"12345678"`; `"1234567"` → reject (`invalid_kvk`); `"abc"` → reject (`invalid_kvk`) |
| `unit_code` | `unit` | copy as-is | non-empty string | `"U-021"` → `"U-021"` |
| `contract_start` | `lease_start` | parse `DD-MM-YYYY` → ISO `YYYY-MM-DD` | real calendar date; day 1–31, month 1–12 | `"15-02-2025"` → `"2025-02-15"`; `"32-13-2024"` → reject (`invalid_date`) |
| `maandhuur` | `monthly_rent_eur` | strip currency, drop thousand `.`, treat `,` as decimal | finite non-negative number | `"1.250,00"` → `1250.00`; `"duizend euro"` → reject (`invalid_amount`) |
| `status` | `status` | lowercase then look up: `actief→active`, `beeindigd→ended`, `beëindigd→ended` | result must be one of `{active, ended}`; otherwise reject | `"Actief"` → `"active"`; `"misschien"` → reject (`unknown_status`) |

## Guarantees

- **Idempotent on `external_id`.** Re-running on the same ERP data changes nothing because the platform's `POST /platform/v1/tenants` upserts by `external_id`.
- **Two tenants sharing a KvK number is legitimate** (one company can lease several units) and must not be flagged. The 8-digit KvK check deliberately does not require uniqueness.
- **Monotonic watermark.** WF1 remembers the greatest `gewijzigd_op` it has successfully fetched (in workflow static data) and asks the ERP for everything strictly after that on the next run, so a crash does not drop records.
- **The 409 case is not a validation error.** It only happens when the ERP has valid data for a tenant but the platform already holds a different `chamber_of_commerce_id` for the same `external_id`. That is a human-escalation case ("someone changed the registration number"), not an auto-retry case.

## Rejection codes (what WF1 writes to `integration-errors`)

| Code | Reason | Where it's caught |
|---|---|---|
| `invalid_kvk` | KvK number is not 8 digits after padding, or is not numeric. | Code node, before POST |
| `invalid_date` | `contract_start` is not a parseable `DD-MM-YYYY`. | Code node |
| `invalid_amount` | `maandhuur` is not a non-negative number after locale normalization. | Code node |
| `unknown_status` | `status` is neither `actief` nor `beeindigd` (or their variants). | Code node |
| `missing_name` | `naam` is `null`, empty, or whitespace only. | Code node |
| `external_id_conflict` | `409` from the platform. Different KvK for same external_id. | HTTP node error branch |
| `platform_422` | Validation failure the platform reports that we did not catch locally. | HTTP node error branch |
| `platform_5xx` | Transient platform failure after N retries (should be rare). | HTTP node error branch |

## What is explicitly out of scope here

- Translating other free-text fields (notes, descriptions) — the ERP does not carry them.
- Timezone math on `gewijzigd_op`. Both sides treat timestamps as UTC; sync windows have a few seconds of overlap by design.
- Address normalization. Address lives on a different flow (not built here).
