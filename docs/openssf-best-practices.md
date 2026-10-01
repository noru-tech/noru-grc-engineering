# OpenSSF Best Practices: prepared answers

Answers for the [OpenSSF Best Practices badge](https://www.bestpractices.dev/) at the **passing**
level, ready to paste into the form. This is preparation, not a claim: the project holds no badge
until the form is submitted, and some answers below need a maintainer to confirm a fact the
repository cannot show.

Criteria are the passing-level (`'0'`) entries of
[`criteria/criteria.yml`](https://github.com/coreinfrastructure/best-practices-badge/blob/main/criteria/criteria.yml)
in `coreinfrastructure/best-practices-badge`, excluding criteria marked future or obsolete:
67 in all. Answers: 50 Met, 6 Unmet, 11 N/A.

## What blocks the passing badge

Passing needs every MUST to be Met or N/A. One MUST is not, and a second follows from it:

- **`warnings`** and **`warnings_fixed`**: no linter or warnings mode runs in CI.
  `node --check` and `py_compile` find syntax errors only. Python's `-W error` and Node's built-in
  warnings need no install, so the standard-library rule does not stand in the way.

These answers also need a maintainer before submission:

- `vulnerability_report_response`: whether any private report arrived in the last six months.
- `static_analysis_fixed`: the current state of the code scanning alerts.
- `release_notes_vulns`, `vulnerabilities_fixed_60_days`: confirm against the advisories page and
  the Scorecard result on the day.
- `know_secure_design`, `know_common_errors`: an attestation about the maintainers, which a file
  cannot make.
- `report_responses`: Met on thin evidence; every issue so far was filed by a maintainer.

SHOULD and SUGGESTED criteria left Unmet (`test_most`, `warnings_strict`, `dynamic_analysis`,
`dynamic_analysis_enable_assertions`) do not block passing.

## Basics

| criterion | level | answer | justification | evidence |
|---|---|---|---|---|
| `description_good` | MUST | **Met** | The README's first sentence says what the software does: last-mile GRC engineering plugins for Claude Code and Codex, recorded in Noru. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md> |
| `interact` | MUST | **Met** | The README covers install, use and contribution; issues and discussions are open on GitHub. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md#install> |
| `contribution` | MUST | **Met** | CONTRIBUTING.md describes the pull-request process. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md> |
| `contribution_requirements` | SHOULD | **Met** | CONTRIBUTING.md states the public-content rules, ground rules (stdlib only, every claim gets a test), the verification block and style; the pull-request template repeats the checklist. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#ground-rules> |
| `floss_license` | MUST | **Met** | MIT. The vendored Fideslang taxonomy data is CC BY 4.0, as NOTICE states. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/LICENSE> |
| `floss_license_osi` | SUGGESTED | **Met** | MIT is OSI-approved. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/LICENSE> |
| `license_location` | MUST | **Met** | LICENSE at the repository root; NOTICE for the CC BY 4.0 data. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/LICENSE> |
| `documentation_basics` | MUST | **Met** | README, docs/ (client guides, CI mode, repository enforcement, verification) and a README per piece. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md> |
| `documentation_interface` | MUST | **Met** | The external interfaces are documented: slash commands per piece, the piece contract, CI-mode flags and exit codes, every action's inputs and outputs, and a page per finding kind. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/ci-mode.md#exit-codes> |
| `sites_https` | MUST | **Met** | The project site is the GitHub repository, served over HTTPS; installs and releases come from GitHub over HTTPS. | <https://github.com/noru-tech/noru-grc-engineering> |
| `discussion` | MUST | **Met** | GitHub Issues and GitHub Discussions: searchable, URL-addressable, no proprietary client needed. | <https://github.com/noru-tech/noru-grc-engineering/discussions> |
| `english` | SHOULD | **Met** | Documentation is in English and issues are accepted in English. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md> |
| `maintained` | MUST | **Met** | Regular releases (0.9.1 on 2026-10-01) and commits on main. | <https://github.com/noru-tech/noru-grc-engineering/releases> |

## Basics: source repository, versions and release notes

| criterion | level | answer | justification | evidence |
|---|---|---|---|---|
| `repo_public` | MUST | **Met** | Public GitHub repository. | <https://github.com/noru-tech/noru-grc-engineering> |
| `repo_track` | MUST | **Met** | Git records who changed what and when. | <https://github.com/noru-tech/noru-grc-engineering/commits/main> |
| `repo_interim` | MUST | **Met** | Every change lands on main as a reviewed pull request between releases, not only at release time. | <https://github.com/noru-tech/noru-grc-engineering/pulls?q=is%3Apr> |
| `repo_distributed` | SUGGESTED | **Met** | Git. | <https://github.com/noru-tech/noru-grc-engineering> |
| `version_unique` | MUST | **Met** | One version number shared by every plugin and action; scripts/check_repo.py fails while any copy disagrees. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#releasing> |
| `version_semver` | SUGGESTED | **Met** | Semantic versioning, currently 0.x. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CHANGELOG.md> |
| `version_tags` | SUGGESTED | **Met** | Each release is a vX.Y.Z tag with a GitHub release; released tags are never moved. | <https://github.com/noru-tech/noru-grc-engineering/tags> |
| `release_notes` | MUST | **Met** | CHANGELOG.md has a section per release, written for users. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CHANGELOG.md> |
| `release_notes_vulns` | MUST | **N/A** | No release has fixed a publicly known vulnerability with a CVE assignment; the repository has no published security advisories. Confirm against the advisories page before submitting. | <https://github.com/noru-tech/noru-grc-engineering/security/advisories> |

## Reporting

| criterion | level | answer | justification | evidence |
|---|---|---|---|---|
| `report_process` | MUST | **Met** | Bug-report and feature-request issue forms; the template chooser routes security reports to private reporting. | <https://github.com/noru-tech/noru-grc-engineering/issues/new/choose> |
| `report_tracker` | SHOULD | **Met** | GitHub Issues. | <https://github.com/noru-tech/noru-grc-engineering/issues> |
| `report_responses` | MUST | **Met** | Thin evidence: the issues filed so far were opened by a maintainer and closed. Re-check when external reports arrive. | <https://github.com/noru-tech/noru-grc-engineering/issues?q=is%3Aissue> |
| `enhancement_responses` | SHOULD | **Met** | Thin evidence, as above: no external enhancement requests in the period. | <https://github.com/noru-tech/noru-grc-engineering/issues?q=is%3Aissue> |
| `report_archive` | MUST | **Met** | Issues and their discussion are public and searchable. | <https://github.com/noru-tech/noru-grc-engineering/issues?q=is%3Aissue> |
| `vulnerability_report_process` | MUST | **Met** | SECURITY.md describes how to report. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md> |
| `vulnerability_report_private` | MUST | **Met** | GitHub private vulnerability reporting (preferred) or security@noru.tech. | <https://github.com/noru-tech/noru-grc-engineering/security/advisories/new> |
| `vulnerability_report_response` | MUST | **N/A** | SECURITY.md commits to acknowledgement within 5 business days. Private reports are not visible from the repository, so a maintainer must confirm whether any arrived in the last six months and how fast they were answered; mark Met with that figure if so. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md#reporting-a-vulnerability> |

## Quality

| criterion | level | answer | justification | evidence |
|---|---|---|---|---|
| `build` | MUST | **N/A** | Nothing is built: Node .mjs and Python run from source with no install step, on purpose. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md#development> |
| `build_common_tools` | SUGGESTED | **N/A** | No build. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md#development> |
| `build_floss_tools` | SHOULD | **N/A** | No build. Running it needs only Node and Python 3, both FLOSS. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md#development> |
| `test` | MUST | **Met** | An automated suite under scripts/ (validators, collectors, idempotency, the contract test, CI mode, enforcement, review, hub, mirror check), released with the source and documented in CONTRIBUTING.md. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#verification> |
| `test_invocation` | SHOULD | **Met** | Each suite is `python3 scripts/<suite>.py`, listed in CONTRIBUTING.md. There is no single wrapper command. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#verification> |
| `test_most` | SUGGESTED | **Unmet** | Coverage is not measured: the zero-dependency rule keeps coverage tools out of CI, and no figure has been taken locally. Claims are tested individually, but nobody can say what share of branches the suite reaches. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/verification.md> |
| `test_continuous_integration` | SUGGESTED | **Met** | The ci workflow runs every suite on each push and pull request, across Node and Python versions and both YAML loaders. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/workflows/ci.yml> |
| `test_policy` | MUST | **Met** | "Every claim gets a test" is a ground rule in CONTRIBUTING.md and AGENTS.md; the pull-request template asks which check asserts the new behaviour. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#ground-rules> |
| `tests_are_added` | MUST | **Met** | Changes ship with the tests for what they add: the commit history shows test suites changing alongside features, and the pull-request template asks which check asserts the new behaviour. | <https://github.com/noru-tech/noru-grc-engineering/commits/main> |
| `tests_documented_added` | SUGGESTED | **Met** | CONTRIBUTING.md and the pull-request template checklist. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/PULL_REQUEST_TEMPLATE.md> |
| `warnings` | MUST | **Unmet** | CI runs `node --check` and `python3 -m py_compile` on every file, which catch syntax errors only, and CodeQL. No linter or warnings-as-errors mode runs (no ESLint, no Ruff, no `python3 -W error`). The standard-library-only rule rules out installed linters in CI but not Python's or Node's built-in warning switches. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/workflows/ci.yml> |
| `warnings_fixed` | MUST | **Unmet** | Follows from `warnings`: there is no warning output to address. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/workflows/ci.yml> |
| `warnings_strict` | SUGGESTED | **Unmet** | As above. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/workflows/ci.yml> |

## Security

| criterion | level | answer | justification | evidence |
|---|---|---|---|---|
| `know_secure_design` | MUST | **Met** | Maintainer attestation required. Evidence of the design work: the threat model in SECURITY.md (prompt injection, unreviewed writes, credential exposure, scope) and the controls the contract test enforces. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md#threat-model> |
| `know_common_errors` | MUST | **Met** | Maintainer attestation required. Evidence: secret redaction on every child-process output, NORU_API_KEY stripped from every step that does not push, a credential-shaped-string scan in check_repo.py, pinned and least-privilege workflows. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md#threat-model> |
| `crypto_published` | MUST | **Met** | The only cryptography is SHA-256 digests (manifest and plan binding, evidence upload integrity, enforcement fingerprints), a published algorithm. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/plugins/evidence-push/README.md> |
| `crypto_call` | SHOULD | **Met** | SHA-256 comes from Python's hashlib and Node's node:crypto; nothing is re-implemented. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#ground-rules> |
| `crypto_floss` | MUST | **Met** | Python and Node standard libraries. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#ground-rules> |
| `crypto_keylength` | MUST | **N/A** | No keys are generated or used for encryption or signing. The one credential, NORU_API_KEY, is created in Noru and only passed through. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md#3-credential-exposure> |
| `crypto_working` | MUST | **Met** | No broken algorithm (MD5, SHA-1, DES, RC4) is used. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md> |
| `crypto_weaknesses` | SHOULD | **Met** | SHA-256 only. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md> |
| `crypto_pfs` | SHOULD | **N/A** | The project implements no key agreement; HTTPS to api.noru.tech and the forge APIs is the runtime's TLS. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md> |
| `crypto_password_storage` | MUST | **N/A** | No passwords are stored; authentication belongs to the MCP host. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/SECURITY.md#3-credential-exposure> |
| `crypto_random` | MUST | **N/A** | No keys or nonces are generated. audit-pack's sample is deliberately deterministic, seeded from the population file's digest so anyone can redraw it; it is not a security control. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/plugins/audit-pack/README.md> |
| `delivery_mitm` | MUST | **Met** | Distributed from GitHub over HTTPS and git; workflows pin third-party actions to full commit SHAs. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md#trust> |
| `delivery_unsigned` | MUST | **Met** | No hash is fetched over HTTP. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/README.md#trust> |
| `vulnerabilities_fixed_60_days` | MUST | **Met** | No dependency manifests (stdlib and built-ins only), and no publicly known unpatched vulnerability. Re-check the OpenSSF Scorecard Vulnerabilities result before submitting. | <https://scorecard.dev/viewer/?uri=github.com/noru-tech/noru-grc-engineering> |
| `vulnerabilities_critical_fixed` | SHOULD | **Met** | As above. | <https://scorecard.dev/viewer/?uri=github.com/noru-tech/noru-grc-engineering> |
| `no_leaked_credentials` | MUST | **Met** | scripts/check_repo.py scans for credential-shaped strings on every build, and CONTRIBUTING.md has the git grep checks; examples use `<NORU_API_KEY>`. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#verification> |

## Analysis

| criterion | level | answer | justification | evidence |
|---|---|---|---|---|
| `static_analysis` | MUST | **Met** | CodeQL on every push to main, every pull request and weekly. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/workflows/codeql.yml> |
| `static_analysis_common_vulnerabilities` | SUGGESTED | **Met** | CodeQL's security queries look for common vulnerability classes. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/workflows/codeql.yml> |
| `static_analysis_fixed` | MUST | **Met** | Confirm against the code scanning alerts page before submitting: there must be no open medium-or-higher exploitable alert. | <https://github.com/noru-tech/noru-grc-engineering/security/code-scanning> |
| `static_analysis_often` | SUGGESTED | **Met** | Every push and pull request. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/.github/workflows/codeql.yml> |
| `dynamic_analysis` | SUGGESTED | **Unmet** | No fuzzer or other dynamic analysis tool runs. The test suites execute every tool against fixtures, including the refusal paths, but that is testing, not dynamic analysis. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/verification.md> |
| `dynamic_analysis_unsafe` | SUGGESTED | **N/A** | No memory-unsafe language: Python and JavaScript only. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/CONTRIBUTING.md#ground-rules> |
| `dynamic_analysis_enable_assertions` | SUGGESTED | **Unmet** | No dynamic analysis runs (see above). | <https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/verification.md> |
| `dynamic_analysis_fixed` | MUST | **N/A** | No dynamic analysis is performed, so there are no findings from it to fix. | <https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/verification.md> |
