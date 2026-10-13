# Contributing

Contributions should preserve the project's deterministic safety boundaries:

- pending knowledge must never affect retrieval or prompts;
- table selection must be justified by schema or approved business evidence;
- joins must use physical foreign keys or explicit approved relationships;
- model output must remain a proposal until independent validation succeeds;
- tests must use synthetic data and must not include private schemas or credentials.

## Development setup

```powershell
cd project
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Run the deterministic suite before submitting a change:

```powershell
python -m pytest tests -q
```

Add focused regression coverage for retrieval, planning, relationship, SQL
validation, or lifecycle changes. Tests that require a real MySQL or llama.cpp
instance must use the `live` marker.

Keep pull requests focused and describe the user-visible behavior, trust-boundary
impact, verification performed, and any deployment migration required.
