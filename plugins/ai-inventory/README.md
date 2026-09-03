# ai-inventory

> Inventory the AI systems a repository actually contains, review it as a committed manifest, and
> land it in Noru as assets, vendors and evidence.

This is the piece a server-side integration structurally cannot do. The truth about which model is
called from which line, what data reaches it, whether a person approves the output, and whether
anything fails when the evals fail lives in the repository — not in an API a scheduled job can poll.

**What it targets:** `iso_42001` and `eu_ai_act` — the frameworks that ask what AI systems you run,
who oversees them, and what the provider does with your data. It is not aimed at ISO 27001 or SOC 2,
and it should not be read as a route to either; for those, work your organization's own evidence
queue with [`evidence-push`](../evidence-push/README.md).

**What it is not.** The EU AI Act does not require an organization to keep an inventory of its AI
systems. No article imposes one. What the Regulation has is *registration* in the Commission's
public EU database under Articles 49 and 71, which falls on the provider of an Annex III high-risk
system, and on a deployer only where that deployer is a public authority or an EU institution, body,
office or agency. A private-sector organization deploying a third-party model has no registration
duty at all. The register-adjacent duties are per-system documentation rather than an inventory:
Article 11 with Annex IV technical documentation, Article 12 logging, Article 26(6) deployer log
retention, and the Article 27 fundamental rights impact assessment for the deployers that article
names.

So the honest claim is the narrower one, and it is still worth making. You cannot tell whether
anything you run touches Article 5, which of your systems trigger Article 50, or whether you are a
provider or a deployer, without knowing what AI you have. An inventory makes obligations
*determinable and demonstrable*; it is not itself an obligation. ISO/IEC 42001 is the better anchor
for keeping one — it is certifiable, and documented information about AI systems is something a
management system expects rather than infers.

## What it finds, in the order it is enforceable

The findings block is ordered on purpose, and not in the interesting-first order:

| # | Category | Applies from | What the finding is |
|---|---|---|---|
| 1 | `prohibited_practices` | 2 February 2025 (Art. 113(a)) | An Article 5(1) practice. Rare, and the only category whose answer is **stop**, not document |
| 2 | `transparency_obligations` | 2 August 2026 (Art. 113) | An Article 50 trigger **and whether the disclosure or marking it requires is actually present** |
| 3 | `role_and_risk` | the date the finding states in `enforceable_from` | Role, tier, Annex III screening, and the Article 6(3) assessment where that is the conclusion |
| 4 | `standards_alignment` | n/a | ISO/IEC 42001 references and NIST AI RMF function tags |

Category 2 is where the value is right now, and it is the one a repository scan can genuinely see.
Category 3 is real and it is kept, but the obligations that follow from a high-risk tier are not in
application on the same timetable as the two above it. Check the current text of the Regulation for
the date that applies to your system and record it in `enforceable_from`, so that a finding serving
a future deadline is never read as one that is due today. The validator will not let you omit it.

### The Article 50 check, and what it can be wrong about

Detecting that a system qualifies is the easy half and not the useful one. The finding is whether
the required disclosure is there:

- **`present`** — a disclosure signal of the required kind is in the *same file* as the model call.
- **`unclear`** — a signal exists in the same directory, or elsewhere in the repository, but nothing
  in the scan ties it to this call site.
- **`absent`** — no signal of the required kind was found in anything the scan read.

`absent` never means "there is no disclosure", which is why the schema requires `searched` on every
absent finding. A repository scan cannot see a notice rendered by a design system, injected by a
CMS, shown only in a mobile client, spoken from an IVR script, or living in another repository. Nor
can it see that a notice which *is* in the code sits behind a flag that is off, appears three screens
later, or is buried in a terms page rather than shown "at the latest at the time of the first
interaction or exposure", which is what Article 50(5) asks for. A matching string is not compliance,
and proximity is not proof — the states above are a repository fact for a person to confirm.

Two distinctions the check keeps, because collapsing them would make it wrong:

- **Informing a person and marking an output are different duties.** Article 50(2) asks for a
  machine-readable mark that travels with the artifact; a visible "AI-generated" caption in the
  interface does not satisfy it, and the check will not accept one for it.
- **Emotion recognition is grounded in biometric data** (Art. 3(39)). Sentiment analysis of text is
  not an emotion recognition system, and is deliberately not a trigger here.

### The Article 5 screen

Seven of the eight Article 5(1) practices have a detection pattern; Article 5(1)(b), exploitation of
vulnerabilities, does not, because it turns on who the users are and what the system does to them
rather than on anything visible in a line of code. It stays in the vocabulary so a person can record
it.

The scan says which practices it screened even when it finds nothing, because "the screen ran and
found nothing" and silence are different statements and only one of them is worth anything. A
pattern match is never written as a determination: the collector proposes `needs_legal_review` with
`needs_review: true`, and only a person writes `indicated`.

## Commands

| Command | Writes to Noru? | What it does |
|---|---|---|
| `/ai-inventory:scan` | no | Deterministic offline scan → `.noru/ai-inventory.yml` |
| `/ai-inventory:diff` | no | Reads current state, prints the exact plan |
| `/ai-inventory:push` | **yes** | Executes the confirmed plan |

## Scopes

Least privilege. Start read-only — `:scan` and `:diff` are useful before the piece is ever allowed
to write.

| Capability | Scopes |
|---|---|
| `:scan` (repository only) | none — it makes no Noru call |
| `:diff` | `read:organization`, `read:frameworks`, `read:controls`, `read:evidence`, `read:assets`, `read:vendors` |
| `:push` | the above plus `write:assets`, `write:vendors`, `write:evidence` |

## Artifact

`.noru/ai-inventory.yml`, schema at [`contract/ai-inventory.schema.json`](../../contract/ai-inventory.schema.json).

```yaml
version: 0.1.0
piece: ai-inventory
source: { slug, commit_sha, branch, generated_by, derived_digest }
providers:
  - key: …
    vendor_name: …
    claims: [{ kind, value, source: { type, ref|url, retrieved_on }, interpretation }]
    refs: ["path/to/file.ts:37"]
    interpretation: { owner, decided_at, expires_at, rationale }
ai_systems:
  - key: …          # stable: it becomes half the Noru asset upsert key
    name: …
    purpose: …
    deployment: hosted_api | self_hosted | on_device | embedded_library
    autonomy: assistive | supervised | autonomous
    human_oversight: [{ type, description, refs }]
    retrieval: [{ name, kind, refs }]
    evals: { suites: [{ name, path }], ci_gated: true|false }
    data_categories: [fideslang keys]
    refs: [...]
    interpretation: { … }
findings:                    # ordered: what is enforceable today comes first
  prohibited_practices:
    - system: …
      practice: emotion_inference_workplace_or_education
      article: "Article 5(1)(f)"
      determination: indicated | needs_legal_review | no_indication
      action: …              # required unless the determination is no_indication
      status: suggested      # always; a human accepts or rejects in Noru
      refs: [...]
      interpretation: { … }
  transparency_obligations:
    - system: …
      trigger: direct_human_interaction
      article: "Article 50(1)"
      required_action: inform_natural_person | machine_readable_marking | disclose_artificial_content
      disclosure:
        state: present | unclear | absent
        mechanism: …         # required when present
        gap: …               # required when absent or unclear
        searched: [...]      # required when absent
        refs: [...]          # required when present or unclear
      status: suggested
      refs: [...]
      interpretation: { … }
  role_and_risk:
    - system: …
      role: deployer
      role_article: "Article 3(4)"
      tier: not_high_risk
      tier_article: "Article 6(3)"
      annex_iii_area: employment_and_worker_management
      not_high_risk_assessment: { ground, profiling, article }
      enforceable_from: 2027-12-02   # required; the date the obligations start to apply
      status: suggested
      refs: [...]
      interpretation: { … }
  standards_alignment:
    - system: …
      scheme: iso_42001 | nist_ai_rmf
      value: …
      status: suggested
      refs: [...]
      interpretation: { … }
```

Commit it. `.noru/.cache/` is machine state — keep it out of git.

## Idempotency

| Write | Kind | Key |
|---|---|---|
| `createAsset` | server upsert | `(source, externalId)` where source is `noru-ai-inventory` — documented upsert behaviour |
| `createVendor` | server dedupe | vendor name — the published tool description says an existing record is returned |
| `createEvidence` | server key | a stable content-addressed `idempotencyKey`; the description marker remains the legacy fallback |
| `linkEvidenceToControl` | server dedupe | the evidence/control/evidence-item tuple; repeats return `reused` |

The marker probes are compatibility fallbacks, not the primary concurrency boundary. Each is
written down in [`piece.json`](./piece.json) with the contract it was checked against.

A second `:scan` + `:diff` on an unchanged repository must produce a plan of all `skip`. If it does
not, that is a bug.

## Verify

```bash
node    plugins/ai-inventory/scripts/collect.mjs --repo=. --output=json
python3 plugins/ai-inventory/scripts/validate_manifest.py .noru/ai-inventory.yml
node    plugins/ai-inventory/scripts/diff.mjs --repo=.
node    plugins/ai-inventory/scripts/push.mjs --repo=. --confirm
```

Exit codes: `0` success · `1` drift, validation failure, or a missing prerequisite · `2` usage,
including a push without `--confirm`.

## What it scans

**Tracked files, wherever there is a git to ask** — `git ls-files`, which is the same set
`actions/checkout` gives CI, and which honours `.gitignore`, `.git/info/exclude` and your global
excludes file without this collector reimplementing any of them. That is what makes a scan on your
machine and a scan in CI the same question: a working tree usually holds more than the repository
does — worktrees, scratch checkouts, unpacked archives — and each of those is a full copy of your
model calls as far as a directory walk can tell. Inventorying them attributes a provider, a model
id and an Article 50 trigger site to a path that is not in the repository, so the inventory names
call sites nobody can open, and the drift between the two scans cannot be resolved: the committed
manifest can match one environment or the other and never both.

Three consequences worth knowing. A **tracked** file that an ignore rule also matches is still in
scope — it is in the checkout, so it is in the inventory. An index entry that is not on disk (a
sparse checkout, a pending deletion) is not, because a file the collector cannot open is not one it
can cite. And a model call you have written but not yet `git add`ed is not inventoried either: it
is not in the checkout, so recording it would put back the same disagreement in a smaller form.
Stage it and scan again. Vendored and build directories (`vendor/`, `dist/`, `node_modules/`, …)
stay excluded even when committed, along with the extension and size limits below.

Scanning something that is **not** a work tree — an exported tarball, a directory with no `.git` —
is a legitimate thing to do, and there the collector reads what is on disk instead. That is a
different question, so it says which one it answered: `coverage.enumerated_by` in the derived facts
is `git` or `walk`, and the scan summary says so in words. Same files either way means the same
`derived_digest`, so an export and a checkout of one commit do not read as drift.

## Detection coverage

`scripts/collect.mjs` recognises OpenAI, Anthropic, AWS Bedrock, Google Vertex, Azure OpenAI,
Mistral, Cohere, Ollama and Hugging Face; the Vercel AI SDK, LangChain, LlamaIndex, Semantic
Kernel and MCP; Pinecone, Weaviate, Qdrant, pgvector, Chroma, Milvus, LanceDB and FAISS. Model ids
are matched by shape, not by an enumerated list, so a model released after this collector was
written is still found.

For Article 50 it recognises chat and assistant surfaces, image, speech and video generation,
face and voice biometric calls, emotion inference from biometric input, and deep-fake and
voice-cloning code. For disclosures it recognises English notice text, notice-shaped identifiers and
localisation keys, a concept-stem pair that catches notices in other European languages, and — for
the marking duty specifically — content-provenance work: C2PA and Content Credentials, SynthID, IPTC
digital source type, and provenance manifests. A bare watermarking reference is treated as intent
rather than as a mark, and downgrades the finding to `unclear`.

It will miss a provider called through a hand-rolled HTTP client with no recognisable import, and it
will miss a disclosure that is not in this repository at all. That is what the reviewing human is
for — the collector is a first pass, not an oracle, and everything it proposes arrives as
`needs_review: true` for exactly that reason.

`scripts/test_collectors.py` is where each of these claims is asserted. If you change a pattern,
change the assertion with it.
