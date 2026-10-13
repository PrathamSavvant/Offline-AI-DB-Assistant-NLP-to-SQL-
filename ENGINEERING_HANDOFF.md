# AI Database Assistant — Engineering Handoff

## Purpose

This is an offline, local-network-capable natural-language-to-SQL application. It lets a user ask questions about a MySQL or MariaDB database in plain English and returns a grounded answer, the generated SQL, and a tabular result set.

The system is deliberately designed so that the local language model can *propose* SQL but cannot directly access the database or execute anything. The Python backend remains the trusted control plane: it selects schema context, validates SQL, applies limits, executes approved read-only queries, and returns results.

The standard deployment is fully air-gapped: the UI, Python application, llama.cpp inference server, GGUF model, and database connection all run locally or on an offline LAN. It does not require a cloud LLM or internet connectivity.

## Major Components

| Component | Technology | Role |
|---|---|---|
| Web application/API | Python, Flask | Serves the browser UI, JSON APIs, streaming events, security checks, and application orchestration. |
| Production web server | Waitress | Hosts the Flask application with configurable worker threads. |
| Browser UI | Jinja-rendered HTML, CSS, vanilla JavaScript | Provides chat, schema, knowledge-management, diagnostics, and administration views. No Node.js build or SPA framework is required. |
| Local inference | llama.cpp with a GGUF model | Runs the local LLM behind an OpenAI-compatible HTTP API. |
| Database | MySQL 8+ or MariaDB 10.5+ | Source of business data and schema metadata. |
| Database driver | mysql-connector-python | Manages pooled Python-to-MySQL connections and result serialization. |
| SQL policy/parser | SQLGlot | Parses SQL structurally and enforces read-only safety controls. |
| Retrieval | Inverted index, RapidFuzz; optional Sentence Transformers + FAISS | Selects relevant database context and business knowledge before an LLM request. |

## Deployment Model

```text
┌────────────────────────────────────────────────────────────────┐
│                     Offline Windows workstation                  │
│                                                                │
│  Browser ── HTTP ──> Flask / Waitress :5000                    │
│                           │                                    │
│                           ├── HTTP ──> llama.cpp :8080          │
│                           │                 │                  │
│                           │                 └── local GGUF     │
│                           │                                    │
│                           └── MySQL protocol ──> MySQL/MariaDB │
│                                                (local or LAN)  │
└────────────────────────────────────────────────────────────────┘
```

The browser application is normally bound to `localhost`, so it is accessible only from the machine running it. MySQL may be installed on that same machine or on an approved offline-LAN host.

## System Requirements

### Target operating environment

- Windows 10/11, 64-bit (the distributed llama.cpp binary is Windows x64 CPU-targeted).
- No internet connection is required at runtime.
- MySQL 8.0+ or MariaDB 10.5+ must already be installed, running, and reachable.
- Use a dedicated read-only database account for routine application use.

### Bundled runtime contents

The deployment bundle includes:

- A Python runtime and required Python packages.
- llama.cpp Windows CPU binaries.
- A local Meta Llama 3.1 8B Instruct Q4_K_M GGUF model (approximately 4.9 GB on disk).
- Application code, templates, static assets, knowledge artifacts, launch scripts, and logs directory.

No Python installation, package download, Docker installation, Ollama installation, or Node.js installation is required for the packaged offline launch flow.

### Capacity guidance

- Allow at least **10 GB free disk space** for the bundle, model, logs, and generated knowledge artifacts; more is appropriate for database backups and growth.
- The 8B Q4 CPU model requires several GB of RAM in addition to Windows and MySQL. **16 GB RAM is a practical minimum; 32 GB is recommended** when MySQL and the model run on the same workstation.
- CPU-only inference is supported. Response speed depends mainly on CPU generation, RAM bandwidth, prompt size, and the selected model context.
- The default application listener is port **5000**; llama.cpp uses port **8080**; MySQL commonly uses port **3306**. Change ports only through configuration and ensure they do not conflict with existing services.

## Runtime Configuration

Configuration is supplied through environment variables (normally a local `.env` file). Important groups are:

| Group | Important settings |
|---|---|
| Database | `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, pool and timeout settings |
| Web server | `SERVER_HOST`, `SERVER_PORT`, Waitress threads and connection limits |
| LLM | `LLM_ENDPOINT`, `LLM_MODEL`, `LLM_TIMEOUT`, temperature, repeat penalty, maximum output tokens |
| Safety/context | SQL row limit, table/column context limits, prompt size limit, estimated scan/join limits |
| Maintenance | Background schema-sync enablement and interval |

Configuration must be set before startup. Do not include database passwords in this handoff document or distribute them with the general deployment bundle.

## Application Architecture

The design separates HTTP concerns, conversational state, schema intelligence, LLM composition, SQL safety, database execution, and background maintenance.

```text
                          ┌───────────────────────┐
                          │ Browser user interface │
                          └──────────┬────────────┘
                                     │ HTTP / JSON / SSE
                          ┌──────────▼────────────┐
                          │ API and request layer │
                          └──────────┬────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
                  │ Deterministic request router          │
                  └───────┬───────────────────────┬───────┘
                          │                       │
               simple cache request         analytical question
                          │                       │
              ┌───────────▼──────────┐  ┌────────▼───────────────────┐
              │ Schema/knowledge     │  │ Database intelligence       │
              │ read response        │  │ retrieval + query planning  │
              └──────────────────────┘  └────────┬───────────────────┘
                                                   │ bounded prompt
                                         ┌─────────▼──────────────┐
                                         │ llama.cpp local model   │
                                         └─────────┬──────────────┘
                                                   │ proposed JSON/SQL
                                         ┌─────────▼──────────────┐
                                         │ SQL validation/policy   │
                                         └─────────┬──────────────┘
                                                   │ safe SELECT only
                                         ┌─────────▼──────────────┐
                                         │ MySQL/MariaDB executor  │
                                         └────────────────────────┘
```

### Deterministic request router

Before an LLM is considered, the backend recognizes simple requests that have a known local answer. Examples include greetings, database table listings, full schema display, table-column information, foreign-key relationship lookups, and business-dictionary lookups.

These requests are answered from cached schema and knowledge data. This reduces latency, avoids unnecessary model use, and prevents a generative model from inventing metadata. An unclear request without a database subject is explicitly rejected with a request for more detail.

Questions that require analysis—such as counts, filters, aggregations, comparisons, and multi-table questions—continue to the SQL reasoning pipeline.

### Schema intelligence and knowledge

The system reads database metadata from MySQL `INFORMATION_SCHEMA` and maintains a local representation of:

- Tables, columns, data types, primary keys, indexes, and approximate row counts.
- Physical foreign-key relationships.
- Table profiles and business descriptions.
- User-defined aliases, abbreviations, display columns, terms, and reusable predicates.
- Semantic relationships where physical foreign keys are unavailable.

For an analytical question, retrieval selects a bounded set of relevant tables and columns. It combines deterministic term matching, fuzzy matching, optional embeddings, business definitions, and relationship information. Multi-table join paths are determined from the relationship graph, prioritizing physical foreign keys.

## Backend-to-llama.cpp Contract

llama.cpp is a separate local inference service, not a Python library embedded in the backend process. The backend communicates with it over loopback HTTP.

1. The launcher starts llama.cpp with the local GGUF model and an HTTP server on `http://localhost:8080`.
2. The backend checks `GET /health` during normal startup.
3. For a SQL question, Python builds a compact prompt containing the question, selected schema, allowed relationships, query plan, and output constraints.
4. Python sends `POST /v1/chat/completions` to llama.cpp using its OpenAI-compatible API.
5. llama.cpp returns a structured decision, preferably constrained by JSON Schema:

```json
{
  "sql": "SELECT ...",
  "answer": "Short user-facing summary",
  "confidence": 0.0,
  "needs_clarification": false
}
```

6. The backend parses and normalizes the model response. It can fall back to defensive extraction if a compatible local server/model does not fully honor structured output.
7. The backend validates the proposed SQL before it may reach MySQL.

The model has no database credentials, database socket, execution API, or authority to bypass backend policy.

## SQL Execution Lifecycle

```text
Natural-language question
  → classify request
  → select relevant schema/knowledge context
  → build deterministic query plan
  → request structured SQL from local LLM
  → parse and normalize response
  → validate SQL AST and policy
  → execute approved query through pooled MySQL connection
  → serialize rows and return answer, SQL, diagnostics, and follow-ups
```

### SQL safety policy

The intended execution model is read-only analytical access.

- SQL is parsed structurally rather than trusted as a text string.
- Only approved read-only query forms are accepted (`SELECT` and supported read-only common-table-expression forms).
- Data-changing and administrative statements are rejected: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, DDL, and similar operations.
- Referenced tables must belong to the discovered/approved schema.
- Result limits are injected or clamped to the configured maximum (default: 200 rows).
- Database reads have connection/read/query timeouts.
- Estimated scan and join thresholds can be configured to control expensive queries.
- Routine access should use a database account with read-only grants as a second independent layer of protection.

### Returned response

For an analytical request, the API response contains the user-facing answer, approved SQL where applicable, returned rows/columns, selected tables, execution timeline, reasoning/progress information, and possible follow-up prompts. The streaming endpoint uses Server-Sent Events to emit progress before the final response.

## Knowledge and Schema Synchronization

The application can run a background worker that periodically fingerprints the connected database structure. When it detects a schema change, it rebuilds knowledge artifacts in a staging area, validates the new output, then promotes it only after a successful build.

The existing active knowledge remains in use if the rebuild fails. This avoids replacing a working cache with partial or invalid generated artifacts.

Initial startup may perform an initial schema scan if cached knowledge is missing or invalid. For large databases, this can take several minutes.

## Operations

### Startup and shutdown

The packaged Windows launcher starts the local model server, prepares schema/knowledge state, starts the application, and opens the local browser interface when ready. A paired stop launcher terminates processes created by the bundle.

For a manually managed deployment, start the local llama.cpp server first, then start the production web server. The application UI is available at:

```text
http://localhost:5000
```

### Health endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness check: confirms that the web process is running. |
| `GET /readyz` | Readiness check: confirms database/cache readiness. |
| `GET /api/health` | Detailed application, database-pool, synchronization, and LLM reachability state. |

### Diagnostics

- Application and startup logs are written locally under the application log directory.
- The browser diagnostic page exposes recent operational logs and prompt/retrieval diagnostics when enabled.
- A readiness failure usually indicates missing/invalid schema knowledge, unreachable MySQL, or incomplete configuration.
- An LLM failure usually indicates that llama.cpp was not started, the model did not load, or the configured local endpoint/port is incorrect.

## Security and Network Posture

- Designed to work with no external network access.
- The web UI defaults to loopback access; do not bind to all network interfaces unless remote access is explicitly required and protected.
- Keep CORS restricted for the local same-origin deployment.
- Use a least-privilege, read-only MySQL account.
- Treat generated SQL, model output, and user-provided SQL as untrusted until backend validation succeeds.
- Keep the GGUF model, Python runtime, and bundled binaries under controlled change management; an air-gapped deployment should update them only through an approved removable-media process.

## Engineer Acceptance Checklist

- [ ] MySQL/MariaDB is running and reachable using the configured host, port, database, and read-only account.
- [ ] The configured database contains the expected tables.
- [ ] The local GGUF model and llama.cpp binary are present and compatible with the target Windows x64 host.
- [ ] llama.cpp responds successfully on the configured local endpoint.
- [ ] The backend reports success from `/healthz`, `/readyz`, and `/api/health`.
- [ ] A schema list request returns cached metadata without calling the LLM.
- [ ] A basic analytical question produces a read-only, limited SQL query and result set.
- [ ] Attempts to execute write or destructive SQL are rejected.
- [ ] Logs and startup milestones are accessible to the operator.
- [ ] The stop procedure terminates all processes launched for the application.
