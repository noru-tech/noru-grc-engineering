---
name: doctor
description: Check that this machine and repository can run a last-mile piece — toolchain, git provenance, gitignore hygiene, credential presence.
argument-hint: "[path to repository, defaults to the current directory]"
---

# /noru:doctor

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.mjs" --repo=<repo> --output=json
```

Checks, and why each one matters:

| Check | Why |
|---|---|
| `node` ≥ 18 | Collectors and the REST upload use global `fetch`, `FormData` and `Blob` |
| `python3` | Validators are stdlib-only Python — nothing to install, but it must exist |
| `git` | Provenance (`slug`, `commit_sha`, `branch`) comes from git; without it a push carries none |
| inside a git work tree | Manifests are meant to be committed and reviewed in a pull request |
| `.noru/` | Created by a piece's `:scan` on first run |
| `.noru/.cache/` gitignored | The cache holds machine state **and snapshots of the organization's compliance data**. Commit `.noru/<piece>.yml`; do not commit the cache |
| `NORU_API_KEY` presence | Only the `evidence-push` upload needs it. The script reports set/not set and never reads the value |

If `.noru/.cache/` is not ignored, offer to add it:

```gitignore
.noru/.cache/
```

Then run the hub's context command to show the provenance a push would carry, and warn the user if
the working tree is dirty — a push from a dirty tree records a commit sha that does not describe
what was actually scanned.

Report the failures with their hints, and stop there. This command changes nothing.
