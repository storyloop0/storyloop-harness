# Contributing

Use Python 3.12 or later. From the repository root:

```sh
python -m venv .venv
# Activate the environment for your shell.
python -m pip install -e '.[test]' build
python -m pytest tests -q
python examples/offline_turn.py
python -m build --wheel
```

Update current callers alongside API changes and preserve deterministic commit semantics.
Add regression tests for changed behavior. The import boundary tests prohibit
platform imports; product persistence, accounts, billing and HTTP routes belong
in the separate platform repository. Do not add credentials or local settings.

CI runs the source suite and tests an installed wheel from a directory outside
the checkout, including the deterministic offline example and runtime imports.

## Versions and releases

StoryLoop is under active incubation and does not promise backward compatibility.
APIs, configuration and persisted formats may change directly. Do not retain
compatibility adapters, aliases, fallback paths or deprecation periods solely for
older versions; migration support is not required. Update current callers,
examples, documentation and relevant tests together. Old saves may be recreated.
The package version lives in `pyproject.toml`; record changes in release notes.
Internal module paths are private.

Before a release, run CI, verify the wheel in a clean environment, and commit
the version and release notes. Create an immutable `vX.Y.Z` tag on that verified
commit; never move a published version tag. Git tags and wheels are the initial
distribution channels. Publication to a package index is a separate operation.
