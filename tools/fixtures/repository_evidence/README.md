# Repository Evidence Fixtures

These fixtures exercise the ORG-083 repository evidence validator without
collecting live repository measurements or enabling enforcement.

- `pantheon-config-org083-pilot.input.yaml` is the only normalized input fixture
  and uses `org083.normalized-input.v1`.
- `pantheon-config-org083-pilot.artifact.json` is fixture evidence content used
  by inline artifact records.
- `pantheon-config-org083-pilot.canonical.json` is the deterministic canonical
  form produced by `tools/repository_evidence.py canonicalize`.

The pilot is non-authoritative, non-enforcing, and intentionally derives
`inventory_only`.
