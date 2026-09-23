---
description: Rebuild the data layer and verify the cleaning still recovers ground truth
---
Run `python -m stockroom.data.pipeline` then `pytest -q tests/test_data_layer.py`. If anything fails, diagnose from core.sql. Do not "fix" a test by reading dirt_manifest.json inside core.sql (CLAUDE.md rule 2).
