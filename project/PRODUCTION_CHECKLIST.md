# Offline production acceptance checklist

This application is local-first, but a successful startup is **not** proof of
correct answers. Complete this checklist on the actual workshop database and
the intended 8 GB CPU-only computer before release.

## 1. Database identity and visibility

- Use a dedicated MySQL account with `SELECT` (and `SHOW VIEW` if views are
  needed). Do not deploy with `root` or a write-capable account.
- Ask the DBA for the minimum expected visible table count. Set
  `DB_EXPECTED_MIN_TABLES` to that number in `project/.env`. The readiness
  endpoint will fail if the account sees fewer tables.
- Open `/api/health` and verify `knowledge_readiness.schema_ready`,
  `tables_cached`, `profiles_cached`, `verified_fk_relationships`, and
  `last_live_schema_check`. A low approved-meaning count is a *semantic*
  coverage warning, not a schema failure.
- If `/readyz` reports a cache/registry mismatch, run schema synchronization
  from the dashboard. Do not answer operational questions until it succeeds.

## 2. Approve opaque-name knowledge

Document table meanings, column meanings, status values, event/history grain,
and current-record rules with the DBA. Do not guess them from abbreviations.
The existing Defined Knowledge screen supports individual edits. For reviewed
bulk input, use `import_knowledge.py`; it validates every table/column and the
database identity before writing anything. Dry-run is the default.

```powershell
cd project
python import_knowledge.py --file C:\private\workshop-knowledge.json
python import_knowledge.py --file C:\private\workshop-knowledge.json --apply
```

Restart the application after an external bulk import so all in-memory
retrieval indices and prompt context are refreshed. The importer creates a
timestamped backup of the prior `defined_knowledge.json` before replacement.
Keep independent encrypted backups of the whole per-database knowledge folder
and perform a restore drill. Do not copy one database's knowledge into another.

Knowledge package shape (replace all identifiers and meanings with reviewed
values):

```json
{
  "database_id": "value from /api/database/registry",
  "schema_fingerprint": "value from /api/database/registry",
  "tables": {
    "opaque_table_name": {
      "definition": "Approved meaning of one row",
      "record_role": "event_history",
      "record_grain": "One event for one item",
      "current_record": {"column": "current_flag", "value": "Y"},
      "aliases": ["approved business phrase"],
      "columns": {"status_code": {"business_meaning": "Current workflow state"}},
      "abbreviations": {"EQPT": {"meaning": "equipment", "column": "item_code"}},
      "predicate_mappings": [{"phrase": "waiting for collection", "column": "status_code", "value": "READY"}]
    }
  }
}
```

Use only fields relevant to each table. The example is a format illustration,
not a recommendation to add a current-record rule to every history table.

### Relationships without declared MySQL foreign keys

The application prefers real MySQL foreign keys. If an operational join exists
only by business convention, have the DBA review its exact columns, direction,
cardinality, and whether it represents current or historical records. Propose
it through `POST /api/semantic-relationships`; every new API proposal remains
`pending`, even if the request says `approved`. After a separate human review,
approve its returned `relationship_id` with
`PUT /api/semantic-relationships/<relationship_id>` and body
`{"status":"approved"}`. The approval is effective immediately after the
runtime refresh. A later endpoint edit returns the relationship to `pending`.

Example proposal body (illustrative identifiers only):

```json
{
  "from_table": "opaque_work_table",
  "from_columns": ["unit_code", "shop_code"],
  "to_table": "opaque_group_table",
  "to_columns": ["unit_id", "shop_id"],
  "description": "DBA-confirmed work-to-group key; many work rows can belong to one group"
}
```

Both sides must name existing columns with compatible data types, and composite
keys must have the same number of columns. Schema changes that invalidate a
join mark it `needs_review`. Equal plausible approved paths are treated as
ambiguous instead of silently choosing one. Approval records a human decision;
it does **not** prove that the data has the claimed cardinality. Validate that
claim with representative DB queries before approving.

## 3. Golden-question acceptance

Create 30–50 real, DBA-reviewed questions covering opaque names, status
changes, current-versus-history semantics, joins, ambiguous terms, and no-match
cases. Keep the suite private and bound to one database identity/fingerprint.

```json
{
  "database_id": "value from /api/database/registry",
  "schema_fingerprint": "value from /api/database/registry",
  "cases": [
    {
      "id": "query-001",
      "question": "A reviewed operational question",
      "expected_tables": ["opaque_table_name"],
      "expected_predicates": [
        {"table": "opaque_table_name", "column": "status_code", "value": "READY"}
      ],
      "expected_status": "ready"
    }
  ]
}
```

```powershell
cd project
python evaluate_queries.py --cases C:\private\golden-questions.json --output C:\private\deterministic-report.json
python evaluate_queries.py --cases C:\private\golden-questions.json --full-pipeline --output C:\private\live-report.json
```

The default run does not call Llama or execute generated SQL. The optional
`--full-pipeline` run **does** call the local model and run validated read-only
queries. Run that mode on a stable test snapshot where exact row counts can be
checked; live daily data will make exact expected counts stale. For stable
cases, add `expected_scalar` or `expected_rows` to verify the actual answer,
not merely the SQL shape. `expected_rows` ignores row order unless
`require_row_order` is true. For ambiguous cases, use `expect_no_sql: true`.
Keep expected values only in the private case file. Reports omit question
text, SQL, returned rows, and predicate values. Record pass rate and
latency on the actual target hardware. Do not treat passing unit tests as a
substitute for this acceptance suite.
`require_approved_joins` defaults to true in each case; set
`require_verified_joins` to true as well for cases that must use only declared
MySQL foreign keys. The report distinguishes FK-verified joins from approved
non-FK joins. A query needing more tables than `MAX_FINAL_TABLES` now fails
with a clear reason instead of silently dropping a requested table.

## 4. Runtime limits and privacy

- Generated SQL now passes MySQL `EXPLAIN` before execution. The defaults reject
  scans estimated above 2 million rows and join work above 5 million rows.
  These are rough optimizer estimates, not exact costs. Tune
  `DB_MAX_ESTIMATED_SCAN_ROWS` and `DB_MAX_ESTIMATED_JOIN_ROWS` with the DBA
  against representative queries; the existing `DB_QUERY_TIMEOUT_MS` remains
  a backstop. If EXPLAIN is unavailable, the query fails closed.
- The offline launcher binds the application and Llama to `127.0.0.1`.
  Do not expose either port to the network. The application does not provide
  multi-user authentication; deploy only on a single-user controlled PC or
  add an approved access-control layer first.
- Sensitive prompt and log APIs are disabled by default. Set
  `ENABLE_SENSITIVE_DEBUG=true` only for a controlled diagnostic session,
  then turn it off. Existing historical log files may still contain earlier
  raw data; review their retention and access permissions under local policy.
- Keep the model, database credentials, knowledge files, chat sessions, and
  reports under OS access controls appropriate for the data. Verify backup and
  restore, startup from the desktop icon, and behavior after a model or DB
  outage.
- Before redistributing the bundled Meta Llama 3.1 model into a military
  environment, have the deploying organization review the applicable model
  licence and acceptable-use policy and include required notices. This is a
  release decision, not something the application can verify automatically.
