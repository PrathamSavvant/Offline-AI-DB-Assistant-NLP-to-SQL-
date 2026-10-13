# Large database deployment

The backend is designed to serve requests from cached schema knowledge. User requests do not query `INFORMATION_SCHEMA` or rebuild knowledge.

Recommended production settings for a large database:

```dotenv
DB_POOL_SIZE=10
DB_CONNECT_TIMEOUT=10
DB_READ_TIMEOUT=30
DB_QUERY_TIMEOUT_MS=30000
SQL_ROW_LIMIT=200
MAX_CONTEXT_TABLES=4
MAX_CONTEXT_COLUMNS_PER_TABLE=12
SEMANTIC_CONTEXT_MAX_CHARS=480
MAX_PROMPT_CHARS=18000
MAX_PRECOMPUTED_JOIN_TABLES=100
ENABLE_EMBEDDINGS=false
SYNC_INTERVAL_MINUTES=60
```

`SQL_ROW_LIMIT` is enforced before execution. `DB_QUERY_TIMEOUT_MS` limits supported MySQL SELECT statements server-side. Keep `MAX_CONTEXT_TABLES` small; approved business meanings and join-plan keys are prioritized within the column budget. More columns are not automatically more accurate on a 4096-token local model. Validate larger limits on the target 8 GB machine before increasing them.

Embeddings are optional. For a fully offline installation, preload `BAAI/bge-small-en-v1.5` during deployment, then enable `ENABLE_EMBEDDINGS=true`. The application never downloads it on a user request.

Background synchronization performs the expensive metadata scan. It stages a new knowledge build and promotes it only after validation, while requests continue using the prior in-memory knowledge. Pairwise join paths are not precomputed above `MAX_PRECOMPUTED_JOIN_TABLES`; requests still compute a graph path for their small selected table set.
