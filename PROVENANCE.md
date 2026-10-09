# Repository provenance

This repository was extracted from `packages/harness` in
`https://github.com/lumman0/storyloop` on 2026-10-09. The source repository is
planned to move to `https://github.com/storyloop0/storyloop-platform` while
retaining the complete earlier history.

| Reference | Commit |
| --- | --- |
| Package-stage baseline, `package-stage-2026-10-09` | `a14b3513edf45dd6de4b592a39be764901ceeb12` |
| Source checkout HEAD when extracted | `a0592e0921819d2f449efd4861dfda07248b4fc8` |
| Extracted harness history tip | `00f306448f71e528f95ff52a87cffc66ea3f0b17` |
| `multi-agent-beta-archive-2026-10` (peeled) | `9b9e6a09a4694ceeccfb3c491759debd72d90ef6` |
| `multi-agent-beta-archive-2026-10-r1` (peeled) | `7151dc07e92a41db9a62de83704b3696726cf3ae` |
| `archive/multi-agent-beta-2026-10` | `7151dc07e92a41db9a62de83704b3696726cf3ae` |

Extraction command, run in the original repository:

```sh
git subtree split --prefix=packages/harness a14b3513edf45dd6de4b592a39be764901ceeb12
```

Only commits affecting the package directory are retained (four commits at the
split). Earlier implementation history remains in the original repository. The
split does not rewrite that repository. The package-stage and beta archive tags
must not be moved or deleted. Local Git tags are not server-enforced protection;
remote tag protection must be configured when publishing.
