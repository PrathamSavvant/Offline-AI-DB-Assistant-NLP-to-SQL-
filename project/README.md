# AI Database Assistant

A local-first conversational SQL application for exploring MySQL databases in
plain English. It combines deterministic schema retrieval, approved business
knowledge, relationship-aware planning, and a locally hosted language model to
produce grounded SQL and readable results.

The application is designed so the model does not need to understand an entire
database from memory. The backend selects a small, relevant, verified context
before asking the model to reason about SQL.

## Main capabilities

- Ask database questions in natural language.
- Inspect tables, columns, relationships, and schema metadata.
- Open the dedicated Schema Map page from the shared sidebar. The map includes all tables,
  approved abbreviations, foreign keys, and approved business relationships.
  Search, filter by domain, zoom, pan, or select a table to inspect its columns
  and connections. Use Refresh after approving knowledge or rescanning schema.
- Use consistent, collapsible navigation across chat, knowledge, table details,
  schema exploration, and system pages. Connection settings are available from every page.
- Retrieve relevant tables from large schemas using bounded hybrid retrieval.
- Resolve business phrases and abbreviations from approved knowledge.
- Select verified foreign-key and semantic join paths.
- Generate SQL through a local llama.cpp-compatible model endpoint.
- Validate and post-process generated SQL before execution.
- Display SQL, result tables, errors, and suggested follow-up questions.
- Preserve the active chat while navigating between application pages.
- Review schema changes and inspect query/knowledge diagnostics.

## Two-agent architecture

The backend has two explicit logical agents and one independent guard. Only the
second agent uses a language model.

- **Agent 1 — Database Intelligence Agent:** deterministic normalization,
  approved-knowledge resolution, retrieval, reranking, predicate resolution,
  verified-FK planning, and Query Plan construction.
- **Agent 2 — Llama SQL Agent:** local, bounded SQL composition and concise
  explanation from the completed Query Plan. It does not discover schema or
  relationships.
- **Guard — SQL Validator:** independently parses and verifies generated SQL
  before execution, including read-only safety, tables, columns, planned
  predicates, and complete verified FK constraints.

```text
User question
    |
    v
Request router
    |-- simple metadata request --------> deterministic response
    |
    `-- analytical SQL request
            |
            v
      Query normalization
            |
            v
      Hybrid table retrieval
      - exact and token matching
      - inverted index
      - fuzzy matching
      - optional embeddings
      - approved knowledge signals
            |
            v
      Candidate reranking and context selection
            |
            v
      Predicate and business-value resolution
            |
            v
      Verified relationship / join-path selection
            |
            v
      Structured Query Plan (ready / ambiguous / failed)
            |
            v
      Llama SQL Agent
      - bounded selected-schema prompt
      - structured SQL decision
            |
            v
      SQL validation and post-processing
            |
            v
      MySQL execution and result presentation
```

### Design principle

The database schema remains the source of structural truth. Approved knowledge
adds business meaning, but it does not replace real tables, columns, or verified
relationships. Pending knowledge never affects retrieval or prompts until it is
explicitly approved.

## Knowledge model

The user-facing knowledge layer is intentionally small.

### Defined Knowledge

Approved knowledge that may influence retrieval and the final LLM prompt:

- **Table context:** what a table represents in the business.
- **Abbreviations:** organization-specific shorthand and its meaning.

### Pending Knowledge

Unapproved discoveries waiting for review:

- Missing table context.
- Detected abbreviations.

Pending entries are ordered by the number of unidentified tokens, highest
first. They remain isolated from query generation until accepted.

## Technology

- Python and Flask
- MySQL
- llama.cpp OpenAI-compatible HTTP API
- sqlglot for SQL parsing and validation
- RapidFuzz for fuzzy retrieval
- Optional Sentence Transformers and FAISS semantic retrieval
- Waitress for production serving
- Server-rendered HTML, CSS, and JavaScript

## Project structure

```text
project/
|-- agent/          Conversation orchestration and thread memory
|-- config/         Environment-backed application settings
|-- core/           Retrieval, knowledge, schema, joins, and query planning
|-- knowledge/      Database-specific generated and approved knowledge files
|-- routes/         Flask page and API routes
|-- services/       Metadata, schema synchronization, and SQL reasoning
|-- templates/      Application UI
|-- tests/          Unit and regression tests
|-- app.py          Flask application factory and development entry point
|-- production.py   Waitress production entry point
`-- requirements.txt
```

## Requirements

- Python 3.12 or a compatible recent Python 3 version
- MySQL server and credentials for a readable database
- A llama.cpp server exposing `/health` and OpenAI-compatible chat endpoints
- A GGUF instruct model suitable for code or SQL generation

## Installation

From the `project` directory, create and activate a virtual environment.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Linux or macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The embedding packages are only used when semantic embeddings are enabled.

## Configuration

Create a `.env` file inside `project/`:

```dotenv
DB_HOST=localhost
DB_PORT=3306
DB_USER=your_mysql_user
DB_PASSWORD=your_mysql_password
DB_NAME=your_database

LLM_ENDPOINT=http://localhost:8080
LLM_MODEL=local-model
LLM_TIMEOUT=180
LLM_TEMPERATURE=0.1
LLM_REPEAT_PENALTY=1.15
LLM_MAX_TOKENS=384

MAX_CONTEXT_TABLES=4
MAX_CONTEXT_COLUMNS_PER_TABLE=12
MAX_PROMPT_CHARS=18000
MAX_RETRIEVAL_CANDIDATES=30
SQL_ROW_LIMIT=200

ENABLE_EMBEDDINGS=false
ENABLE_BACKGROUND_SYNC=true
SYNC_INTERVAL_MINUTES=15
```

Do not commit real database credentials or private model configuration.

## Model recommendation

The offline bundle currently includes **Meta Llama 3.1 8B Instruct, Q4_K_M**.
On an 8 GB CPU-only PC, avoid assuming a larger or less-quantized model will
fit or improve correctness. Benchmark any model change against the same private
golden-question suite on the target computer.

Use an instruct model, keep temperature low, and preserve the structured-output
contract. Model quality helps with SQL composition, but the deterministic
retrieval, knowledge, join, and validation layers are responsible for grounding
and safety.

Start the llama.cpp server on the endpoint configured by `LLM_ENDPOINT`. The
application expects the model server to be running separately.

## Running the application

For deployment on an offline, sensitive, large workshop database, follow the
[production acceptance checklist](PRODUCTION_CHECKLIST.md) before release.

### Development

```powershell
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

### Production

```powershell
python production.py
```

Waitress binds to `127.0.0.1:5000` by default. Network exposure requires
explicit configuration and an approved access-control design.

## First-time workflow

1. Start MySQL and the local llama.cpp server.
2. Configure `.env` with the database and model endpoint.
3. Start the Flask application.
4. Open **Connection & Knowledge** and verify the connection.
5. Synchronize the schema knowledge cache.
6. Add table context and approve useful abbreviations where needed.
7. Ask a natural-language database question in **AI Chat**.
8. Inspect the generated SQL before relying on important results.

## Chat sessions

The current conversation is preserved in browser-tab session storage. Moving
between AI Chat, Defined Knowledge, Pending Knowledge, and diagnostic pages does
not create a new thread or discard visible results. Selecting **New Session** is
the explicit action that clears the active conversation and creates a fresh
backend thread ID.

Chat persistence survives navigation and refresh in the same browser tab. It is
not intended as permanent cross-device storage, and restarting the backend
clears its in-memory conversational context.

## Useful endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Basic process health |
| `GET /readyz` | Application readiness |
| `GET /api/health` | Database, schema, and model status |
| `POST /api/agent/chat` | Main conversational query endpoint |
| `POST /api/execute` | Execute validated SQL from the UI |
| `GET /api/tables` | Read indexed table metadata |
| `GET /api/knowledge` | Read defined knowledge summaries |
| `GET /api/knowledge/pending` | Read pending knowledge |
| `POST /api/knowledge/impact` | Compare query behavior with knowledge enabled or disabled |
| `GET /api/debug/prompt` | Inspect prompt diagnostics |

## Testing

Run the complete suite from `project/`:

```powershell
python -m pytest tests -q
```

The tests cover database identity and switching, schema synchronization, hybrid
retrieval, candidate preservation, reranking, predicates, join paths, knowledge
behavior, SQL reasoning, output validation, and chat-session persistence.

## Operational notes

- Database identity is included in cached knowledge to prevent cross-database
  contamination.
- Retrieval and prompt sizes are bounded for large schemas.
- Embeddings are optional; deterministic retrieval remains available without
  them.
- Generated SQL is treated as untrusted until it passes validation.
- Schema synchronization detects changes and rebuilds affected knowledge.
- Logs and the Prompt Debugger should be used when table selection, predicates,
  joins, or model output are unexpected.

## Troubleshooting

### The application cannot reach the model

Confirm that the llama.cpp server is running and that `LLM_ENDPOINT` matches its
address. Check `/api/health` for the active endpoint and model status.

### The wrong tables are selected

Add concise table context or approve the relevant abbreviation, then inspect
the knowledge-impact and prompt diagnostics. Avoid adding schema facts that are
already represented by columns or foreign keys.

### A join is missing or incorrect

Confirm that the foreign key exists in MySQL or that an approved semantic
relationship has been defined. The system should not invent unsupported join
paths.

### Knowledge appears to be ignored

Only approved Defined Knowledge is allowed into retrieval and prompt context.
Pending tokens are intentionally inactive.

### Chat disappears after restarting the server

The browser restores visible messages, but backend conversational memory is
currently process-local. A server restart therefore starts fresh reasoning
context even if prior messages are still visible in the tab.

## Security

This application connects directly to a database and can execute generated SQL.
Use a dedicated MySQL account with the minimum required permissions, keep the
service on a trusted network, review generated SQL for sensitive workloads, and
never expose development mode publicly.
