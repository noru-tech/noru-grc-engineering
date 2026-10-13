# `needs_review`

**Rule:** a manifest entry still carrying `needs_review: true` — something the collector could not
decide and a person has not resolved — cannot pass repository enforcement.

| | |
|---|---|
| Emitted by | the `enforce` action and `enforce.py` (repository enforcement). CI mode reports the same validator error as [`invalid`](./invalid.md); enforcement names it from a path ending in `needs_review` |
| Gates by default | yes — always, under repository enforcement: a per-piece `fail_on` list cannot remove it |
| Accepted in a ratchet baseline | yes |

## Why it matters

Collectors mark what they cannot settle — a field they cannot classify, a privacy declaration whose
purpose the code cannot reveal — and the validators refuse to let such a manifest be pushed. A
`needs_review` flag in a merged manifest means a judgement nobody has made yet is sitting in the
record.

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example

The examples run `plugins/repo-enforcement/scripts/enforce.py` against the throwaway repository
`scripts/test_repo_enforcement.py` sets up with `/repo-enforcement:setup` (ratchet mode; pieces
`ai-inventory`, `iac-scan` and `privacy-datamap` required), checked as of 2026-09-04. The `enforce`
action prints the same violations as GitHub annotations. The repository's freshly scanned `privacy-datamap` manifest still has an unresolved
privacy declaration:

```text
$ python3 plugins/repo-enforcement/scripts/enforce.py validate --repo=. --as-of=2026-09-04 --output=text
Repository enforcement: FAIL
...
  FAIL [privacy-datamap/missing_interpretation] system[0].privacy_declarations[0]: missing required `interpretation` — name the person who decided this, when, until when, and why
  FAIL [privacy-datamap/needs_review] system[0].privacy_declarations[0].needs_review: still true — the collector cannot know what a system uses data for. Name the purpose, the data use and the subjects, then remove the flag
```

## Passing example

The same declaration with a name, `data_use`, `data_subjects` and an interpretation block, and the
flag removed — as the `privacy-datamap` green fixture in `scripts/test_ci_mode.py` does:

```yaml
privacy_declarations:
  - name: Operate the fixture service
    data_use: essential.service
    data_subjects: [customer]
    data_categories: [user.contact.email, user.authorization.password]
    interpretation:
      owner: Fixture Owner
      decided_at: 2026-08-20
      expires_at: 2027-02-19
      rationale: Reviewed the synthetic fixture schema and its application semantics.
```

## How to fix

Resolve the entry the message names, then delete `needs_review`. `/repo-enforcement:work
<fingerprint>` routes the item to the owning piece.

## Recording a disposition

Under ratchet adoption the exact violation can be accepted in `.noru/enforcement-baseline.json`
with a named person, a rationale, a decision date and an expiry within the policy's
`exceptions.maximum_days` ([repository-enforcement.md](../repository-enforcement.md#install-and-adoption)).
`enforce.py baseline propose` writes a candidate; it is not an approval until those fields are filled
in and reviewed.
