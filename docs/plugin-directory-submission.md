# Anthropic plugin directory submission

What to paste into the Anthropic plugin directory submission for each Noru plugin, and how the
manifests did against the directory's own validation.

## How submission works

[`anthropics/claude-plugins-official`](https://github.com/anthropics/claude-plugins-official) lists
third-party plugins under `external_plugins` and in its `.claude-plugin/marketplace.json`. Its README
says third-party plugins are submitted through the
[plugin directory submission form](https://clau.de/plugin-directory-submission), not by pull request
(a workflow closes pull requests from forks). Approved community plugins also appear in
[`anthropics/claude-plugins-community`](https://github.com/anthropics/claude-plugins-community),
which is synced from the same review pipeline.

The form itself could not be opened while preparing this page, so its exact field labels are not
reproduced here. The values below are the fields a directory entry carries: every key that appears
in the official marketplace's external entries (`name`, `displayName`, `description`, `author`,
`category`, `source`, `homepage`, `keywords`, `version`). Map them onto the form's fields when you
fill it in.

## Values shared by every plugin in this repository

| Field | Value |
|---|---|
| `author.name` | `Noru` |
| Contact email | `support@noru.tech` |
| `homepage` | `https://github.com/noru-tech/noru-grc-engineering` |
| Repository | `https://github.com/noru-tech/noru-grc-engineering` |
| License | `MIT` (the vendored Fideslang taxonomy under `privacy-datamap` is CC BY 4.0, see `NOTICE`) |
| `source.source` | `git-subdir` |
| `source.url` | `https://github.com/noru-tech/noru-grc-engineering.git` |
| `source.path` | `plugins/<name>` |
| `source.ref` | `v0.9.1` |
| `source.sha` | `37e17dda3f66b0ed08b106e8147be6f7d4ce9fb6` (the `v0.9.1` tag) |
| `version` | `0.9.1` |
| `category` | `security` (see below) |

**Category.** The official marketplace has no `compliance` category. The values in use there are
`automation`, `database`, `deployment`, `design`, `development`, `learning`, `location`, `math`,
`migration`, `monitoring`, `productivity`, `security` and `testing`. That list is what is in use,
not a published enumeration. `security` is the closest fit for compliance and GRC work. If the form
offers a compliance category, choose it instead. This repository's own marketplace keeps
`compliance`.

**Re-pin before submitting.** If a release is newer than `v0.9.1` when you submit, use that tag and
the full commit it points to.

## Per plugin

Use `name` exactly as written. It is the immutable install slug (`/plugin install <name>@…`).
`description` is the description from this repository's marketplace.

| `name` | `displayName` | `description` |
|---|---|---|
| `ai-inventory` | Noru AI Inventory | Scan a repository for the AI systems, model providers and oversight points it actually contains, review the manifest in a PR, and land it in Noru as assets, vendors and evidence. |
| `audit-pack` | Noru Audit Pack | Assemble the evidence bundle, sampling and workpapers an auditor asks for — for one framework over one audit window — from Noru's graph plus the local files an integration cannot reach, and land the tested conclusion for each control back in Noru. |
| `change-control` | Noru Change Control | Who wrote each change, who approved it, who merged it and who deployed it, for one window — with every separation that did not hold owned by a named person, and the forge configuration that was supposed to keep them apart. |
| `evidence-push` | Noru Evidence Push | Work Noru's own evidence queue: see which catalogue expectations are unmet, stage local artifacts against them, and upload with control mappings. |
| `governance-records` | Noru Governance Records | File the records of human decisions in Noru: minutes, ISMS scope, statement of applicability, internal audit plans and reports, findings and corrective action plans, each attributed and dated. |
| `iac-scan` | Noru IaC Scan | Scan the Terraform, CloudFormation, Kubernetes and CI configuration a repository actually contains, decide what each finding means in this environment, and land the result in Noru as security findings — closing the ones that no longer reproduce. |
| `noru` | Noru GRC Engineering | Run consolidated branch reviews across independently installed GRC pieces, summarize live Noru work requiring attention with read scopes only, and check shared connection and repository context. |
| `privacy-datamap` | Privacy Datamap | Read the schemas a repository actually contains — ORM models, migrations, SQL DDL, API contracts — classify the personal data in them against the Fideslang taxonomy, review the result in a pull request, and land the data map in Noru. |
| `repo-enforcement` | Noru Repository Enforcement | Install, plan, apply, and verify enforceable GitHub merge gates for the GRC Engineering workflow without granting the Noru hub administration rights. |
| `review-signoff` | Noru Review Sign-off | Turn a periodic review of machine output — access, rules, baselines, assets, physical access, vendors — into a named, dated, expiring sign-off in Noru. |

| `name` | `keywords` |
|---|---|
| `ai-inventory` | noru, compliance, ai, iso-42001, eu-ai-act, inventory, mcp |
| `audit-pack` | noru, compliance, audit, workpapers, sampling, evidence, soc2 |
| `change-control` | noru, compliance, change-management, segregation-of-duties, separation-of-duties, soc2, iso27001, code-review |
| `evidence-push` | noru, compliance, evidence, audit, soc2, iso-27001, upload |
| `governance-records` | noru, compliance, governance, minutes, internal-audit, iso-27001, soc2 |
| `iac-scan` | noru, compliance, iac, terraform, kubernetes, cloudformation, security-findings |
| `noru` | noru, compliance, grc, mcp, hub |
| `privacy-datamap` | noru, compliance, privacy, fides, fideslang, data-map, gdpr, ropa |
| `repo-enforcement` | noru, compliance, github, rulesets, branch-protection, grc |
| `review-signoff` | noru, compliance, access-review, attestation, sign-off, iso-27001, soc2 |

### `compliance-assistant`

Lives in [`noru-tech/compliance-assistant`](https://github.com/noru-tech/compliance-assistant).

| Field | Value |
|---|---|
| `name` | `compliance-assistant` |
| `displayName` | Noru Compliance Assistant |
| `description` | Claude Code and Codex plugin that guides SOC 2, ISO 27001 and other framework work through Noru's MCP server. For Noru customers. |
| `author.name` | `Noru` (`support@noru.tech`) |
| `homepage` | `https://github.com/noru-tech/compliance-assistant` |
| License | `MIT` |
| `category` | `security` (same reasoning as above) |
| `keywords` | noru, compliance, mcp, controls, evidence, audit |
| `source` | `git-subdir`, `url` `https://github.com/noru-tech/compliance-assistant.git`, `path` `plugins/compliance-assistant`, `ref` `compliance-assistant--v0.1.1`, `sha` `1601fb3c99c1a8dd2c3f768e8b490f0037979f35` |
| `version` | `0.1.1` |

## Validation against the directory's own tooling

`claude-plugins-official` validates its marketplace with the `validate-plugins` composite action from
`anthropics/claude-plugins-community` at
`426e469f322952061102b286b378c0c9733a0934`. That action runs `claude plugin validate`, which it
treats as the authoritative schema check, plus its own invariants I1 to I11. Its scripts were run
locally against this repository and against `compliance-assistant`. Local run settings: every entry
and folder treated as changed, external entries cloned at their pinned commit, `fail-on-warnings`
on, and a UTF-8 locale, which GitHub's runners use. Under a POSIX locale, I10 misreads the em dash
as a hidden character.

| Step | Scope | Result |
|---|---|---|
| 11 invariants I1–I11 | `.claude-plugin/marketplace.json` | pass: 0 errors, 0 warnings |
| 20 `claude plugin validate` | marketplace | pass |
| 30 external plugins | `compliance-assistant` at `1601fb3c` | pass |
| 40 `claude plugin validate` | each of the ten `plugins/*` | pass |
| 41 auxiliary JSON | every `.mcp.json` | parses |
| 11 + 20 + 40 + 41 | `noru-tech/compliance-assistant` marketplace and plugin | pass |
| `validate-frontmatter.ts` | every skill and command (45 files here, 1 in `compliance-assistant`) | 0 errors, 0 warnings |

The only issue the tooling found was I1 (`plugins[]` not sorted by name) on the previous ordering,
which listed `review-signoff` before `repo-enforcement`. The marketplaces are now sorted. The CLI was
Claude Code 2.1.286.

Two directory checks could not be run locally:

- **MCP URL liveness** (`check-mcp-urls.yml`) probes each `http` server URL and fails only on 404,
  410 or no connection. Every plugin here declares `https://api.noru.tech/v1/mcp`. Outbound requests
  to that host were blocked from the environment this page was prepared in, so check it from a
  normal network.
- **The security and privacy scan** (`scan-plugins.yml`) is a model-based review against the
  Anthropic Software Directory Policy. It reads the whole payload a git source installs, including
  `scripts/`, tests and dot-directories, not only the loaded skills and commands. It checks that the
  description matches the behaviour, that hooks are scoped, and that there is no undisclosed
  telemetry. None of the plugins here registers a hook. Every write to Noru is described in the
  plugin's description and README and happens only after an explicit confirmation.

## Other notes

- `hanko submit-check --marketplace anthropic` could not be verified. There is no such tool on npm
  (the `hanko` package there is unrelated) and none was found on GitHub. Use the validation above
  instead.
- The Apache-2.0 license check in `claude-plugins-official` covers only Anthropic's own `plugins/`
  directory, not external entries.
- Some `plugin.json` descriptions are shorter than the marketplace descriptions above. Claude Code
  prefers the marketplace entry when it displays a plugin. Neither one is a schema violation.
