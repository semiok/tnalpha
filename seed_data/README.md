# tnalpha Demo Seed Data

This package contains sanitized knowledge-base and topic-library demo content
exported from the dev environment.

Included:

- Brand / campaign knowledge-base records
- Brand docs / campaign docs metadata and generated digests
- Data-pool records
- Topic-library records
- Uploaded assets under `seed_data/assets/`

Excluded:

- `llmsetting` rows and API keys
- Runtime SQLite databases
- Local logs or generated temp files

Import:

```bash
python scripts/import_seed_data.py --db data/app.db --data-dir data --yes
```

The importer backs up the target DB before replacing knowledge/topic demo
tables. It copies assets into the target `DATA_DIR` and rewrites stored
`file_path` values for that machine.
