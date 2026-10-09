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

Changes must preserve the supported API and deterministic commit semantics.
Add regression tests for changed behavior. The import boundary tests prohibit
platform imports; product persistence, accounts, billing and HTTP routes belong
in the separate platform repository. Do not add credentials or local settings.

CI runs the source suite and tests an installed wheel from a directory outside
the checkout, including the deterministic offline example and runtime imports.

## Versions and releases

The package version lives in `pyproject.toml`. During the `0.1.x` series, keep the
documented public surfaces compatible; announce incompatible changes in a new
minor version and document migration steps. Internal module paths are private.

Before a release, run CI, verify the wheel in a clean environment, and commit
the version and release notes. Create an immutable `vX.Y.Z` tag on that verified
commit; never move a published version tag. Git tags and wheels are the initial
distribution channels. Publication to a package index is a separate operation.
