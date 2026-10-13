# AI Database Assistant

> A private, offline-first natural-language interface for MySQL and MariaDB.
> It turns business questions into grounded, read-only SQL using deterministic
> intent planning, approved business knowledge, verified relationships, and a
> locally hosted LLM.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL%20%7C%20MariaDB-8%2B-4479A1?logo=mysql&logoColor=white)
![llama.cpp](https://img.shields.io/badge/LLM-llama.cpp-6C63FF)
![Offline](https://img.shields.io/badge/runtime-fully%20offline-22C55E)
![Tests](https://img.shields.io/badge/tests-376%20passing-14B8A6)

AI Database Assistant is designed for databases where physical names do not
describe their real business meaning. It scans the live schema, keeps approved
terminology and relationship knowledge separate from unverified suggestions,
builds a deterministic query plan, and gives a local language model only the
small verified context needed to compose SQL.

The model never receives database credentials and never executes SQL directly.

## Why this project exists

Large operational databases are difficult to explore conversationally:

- table and column names may be abbreviated or opaque;
- one business entity may span current, historical, lookup, and event tables;
- foreign keys prove physical connectivity but not always business intent;
- generated SQL must be treated as untrusted before it reaches production data;
- sensitive deployments may not permit cloud LLMs or external network access.

This project addresses those constraints with a local, inspectable pipeline
instead of asking a language model to infer the entire database by itself.

## Core capabilities

- Ask analytical questions in natural language.
- Explore tables, columns, domains, foreign keys, and approved relationships.
- Visualize the complete schema as an interactive relationship map.
- Resolve opaque names through approved aliases, abbreviations, business
  entities, column meanings, phrases, and predicate mappings.
- Keep pending knowledge isolated until a user explicitly approves it.
- Select a primary business entity before join discovery.
- Use physical foreign keys and human-approved non-FK relationships without
  inventing joins from similar column names.
- Generate SQL through a local llama.cpp-compatible endpoint.
- Validate generated SQL structurally with SQLGlot before execution.
- Enforce read-only queries, allowed tables, approved join keys, row limits,
  and configurable query-cost thresholds.
- Detect schema changes and rebuild cached intelligence without blocking chat.
- Run as a normal Python application or a self-contained offline Windows bundle.

## Architecture

```text
Natural-language question
          |
          v
  Deterministic request router
          |
          +---- metadata/schema request ----> cached direct response
          |
          v
  Business-intent resolver
  - primary entity
  - requested attributes
  - filters and grouping concepts
  - ambiguity detection
          |
          v
  Bounded hybrid retrieval
  - exact/token index
  - approved knowledge
  - fuzzy matching
  - optional local embeddings
          |
          v
  Verified query planning
  - physical FK paths
  - approved business relationships
  - predicate/value resolution
  - current/history record rules
          |
          v
  Local Llama SQL composer
  - selected schema only
  - structured decision output
          |
          v
  Independent SQL guard
  - AST parse and read-only policy
  - table/column/join verification
  - row and cost limits
          |
          v
  MySQL / MariaDB execution
```

### Trust boundaries

| Layer | Responsibility | Authority |
| --- | --- | --- |
| Schema scanner | Reads tables, columns, keys, indexes, and row estimates | Structural metadata |
| Defined Knowledge | Stores user-approved business meanings | Semantic interpretation |
| Business-intent resolver | Selects entities and support roles before joins | Deterministic planning |
| Relationship planner | Connects already-selected entities | Verified FK or approved edge only |
| Local LLM | Composes SQL from the completed plan | Proposal only |
| SQL validator | Parses and enforces execution policy | Final execution gate |

Pending knowledge, vector similarity, generated tags, and model output are
never sufficient authority for a join or executable query by themselves.

## Application workspace

| Area | Purpose |
| --- | --- |
| **AI Chat** | Ask questions, inspect generated SQL, and view tabular results |
| **Schema Map** | Search and navigate the complete table relationship graph |
| **Defined Knowledge** | Manage approved table meanings and business terms |
| **Pending Review** | Accept or reject detected abbreviations and unknown tokens |
| **Dashboard** | Inspect schema, knowledge, and runtime health |
| **Schema Changes** | Review structural changes discovered during synchronization |
| **Prompt Debugger** | Audit retrieval, intent, relationships, predicates, and prompt context |

## Repository layout

```text
.
|-- project/
|   |-- agent/          # Deterministic agent facade and conversation memory
|   |-- config/         # Environment-backed settings
|   |-- core/           # Retrieval, intent, knowledge, joins, and safety logic
|   |-- routes/         # Flask pages and JSON/SSE APIs
|   |-- services/       # SQL reasoning, metadata, sync, and post-processing
|   |-- static/         # Browser JavaScript and CSS
|   |-- templates/      # Server-rendered application UI
|   |-- tests/          # Unit and regression suite
|   |-- app.py          # Flask application factory
|   |-- production.py   # Waitress production entry point
|   `-- requirements.txt
|-- Build Offline Package.ps1
|-- DEPLOYMENT_GUIDE.md
|-- ENGINEERING_HANDOFF.md
`-- Dockerfile
```

Local models, Python runtimes, generated database knowledge, `.env`, logs, and
offline deployment bundles are intentionally excluded from this repository.

## Requirements

- Python 3.10 or newer
- MySQL 8+ or MariaDB 10.5+
- A dedicated read-only database account
- A local llama.cpp server exposing an OpenAI-compatible chat endpoint
- A compatible instruct GGUF model

Embeddings are optional. Deterministic retrieval remains available when
Sentence Transformers and FAISS are disabled.

## Quick start

### 1. Clone and enter the application

```powershell
git clone https://github.com/PrathamSavvant/AI-Database-Assistant.git
cd AI-Database-Assistant\project
```

### 2. Create the Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Configure local services

```powershell
Copy-Item .env.example .env
```

Edit `.env` with the MySQL connection and local model endpoint:

```dotenv
DB_HOST=localhost
DB_PORT=3306
DB_USER=readonly_user
DB_PASSWORD=change_me
DB_NAME=your_database

LLM_ENDPOINT=http://localhost:8080
LLM_MODEL=local-model
```

Never commit the completed `.env` file.

### 4. Start llama.cpp

The model and llama.cpp binaries are not stored in Git. Start your local server
with a compatible GGUF instruct model, for example:

```powershell
llama-server.exe -m C:\models\model.gguf --host 127.0.0.1 --port 8080 -c 4096
```

### 5. Validate and run

```powershell
python preflight.py
python main.py --force
python production.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

## Configuration highlights

| Setting | Purpose | Recommended starting point |
| --- | --- | --- |
| `DB_USER` | Database identity used by the app | Dedicated read-only account |
| `LLM_ENDPOINT` | Local OpenAI-compatible model endpoint | `http://localhost:8080` |
| `MAX_CONTEXT_TABLES` | Maximum retrieved business endpoints | `4` |
| `MAX_CONTEXT_COLUMNS_PER_TABLE` | Per-table prompt budget | `12` |
| `MAX_PROMPT_CHARS` | Hard prompt-size boundary | `18000` |
| `SQL_ROW_LIMIT` | Maximum returned rows | `200` |
| `ENABLE_EMBEDDINGS` | Optional semantic retrieval | `false` on low-memory systems |
| `SYNC_INTERVAL_MINUTES` | Schema refresh cadence | `15` |

See [`project/.env.example`](project/.env.example) for complete configuration.

## Knowledge workflow

1. The scanner creates database-scoped schema metadata.
2. Unknown abbreviations are placed in **Pending Review**.
3. A reviewer supplies a meaning and optionally maps the term to a column.
4. Accepted terms move into **Defined Knowledge**.
5. Only approved knowledge can influence normalization, retrieval, predicates,
   relationship selection, or the LLM prompt.
6. If one approved term maps equally to multiple tables, the planner requests
   clarification instead of choosing arbitrarily.

For databases with opaque names, define the business entity, common aliases,
record grain, important columns, predicate phrases, and any DBA-approved
non-FK relationship. Do not duplicate facts already present in the physical schema.

## API overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Process liveness |
| `GET` | `/readyz` | Database and cache readiness |
| `GET` | `/api/health` | Detailed runtime health |
| `POST` | `/api/agent/chat` | Conversational query execution |
| `POST` | `/api/agent/stream` | Streaming reasoning and result events |
| `GET` | `/api/tables` | Indexed schema metadata |
| `GET` | `/api/schema-map` | Schema graph data |
| `GET` | `/api/knowledge` | Approved knowledge summaries |
| `GET` | `/api/knowledge/pending` | Pending review entries |
| `POST` | `/api/knowledge/impact` | Compare planning with and without knowledge |
| `GET` | `/api/debug/prompt` | Last prompt and planning diagnostics |

## Testing

From `project/`:

```powershell
python -m pytest tests -q
```

The regression suite covers business-intent resolution, large-schema retrieval,
candidate preservation, approved knowledge isolation, predicates, physical and
semantic joins, ambiguity handling, SQL AST policy, database switching, schema
synchronization, offline launch behavior, and chat sessions.

Live tests that require a configured MySQL database and llama.cpp server are
marked `live` and excluded from the normal test run.

## Offline Windows deployment

The source repository does not contain the multi-gigabyte runtime bundle. On a
prepared Windows build machine, run:

```powershell
.\Build Offline Package.ps1
```

The resulting package includes the selected local model, llama.cpp, Python
runtime, dependencies, launch scripts, and application source. Follow
[`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) and complete
[`project/PRODUCTION_CHECKLIST.md`](project/PRODUCTION_CHECKLIST.md) before release.

## Security

- Use a dedicated least-privilege, read-only database account.
- Keep the default loopback listener unless authenticated network access has
  been deliberately designed.
- Treat all generated SQL as untrusted until it passes the independent validator.
- Keep `.env`, database knowledge, logs, models, and deployment artifacts out of Git.
- Review [`SECURITY.md`](SECURITY.md) before reporting a vulnerability.

This project reduces risk but does not replace database permissions, backups,
access control, auditing, or production acceptance testing.

## Project status

The application is under active development. The deterministic test suite is
green, but deployment against a new database still requires a representative
golden-question set and DBA review of business meanings and non-FK relationships.
