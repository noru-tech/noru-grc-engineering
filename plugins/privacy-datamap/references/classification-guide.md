# Classification guide

Classification is a plugin-owned investigation of value meaning, person association and category fit.
`classification.json` supplies field-name hints only. Its legacy `exact` key does not authorize any
classification: `email` can be a shared mailbox, `fingerprint` a file checksum, and `race` a sporting
event. Every observed field enters the canonical evidence review, including operational-looking names.

Every key below was checked against the bundled snapshot in `references/taxonomy/`. That snapshot is
the offline floor; where you can reach Noru, `getPrivacyTaxonomy` is the truth. **Never invent a
key** — if the one you want does not exist, the answer is a different key or an unresolved flag.

## Principles

1. **Most-specific wins.** An evidenced individual email address uses `user.contact.email`, not
   `user.contact`. The name alone is insufficient.
2. **A field can carry several categories.** A `billing_address` blob may warrant
   `user.contact.address.street` + `.city` + `.postal_code`.
3. **Operational columns are not personal data when their context proves it.** Timestamps,
   soft-delete flags and row versions are often operational. Generic identifiers and state flags
   are not safe global shortcuts: they can identify a person or record behaviour. Do not force a
   `user.*` label onto operational data, and do not dismiss context-sensitive data without reading
   its collection and usage.
4. **A wrong label is worse than an unresolved one.** The unresolved one gets reviewed. The wrong
   one gets signed, and then it is in the record with somebody's name against it. If source investigation cannot
   resolve meaning, retain the specific gap. Ask only for business context unavailable from code.

## Field-level classification policy

Use this policy for contextual enrichment. Fides categories describe both identifiers and
information about a person; do not present every `user.*` category as a direct identifier or
collapse the distinction into an unexplained PII flag.

For each proposed field, establish its meaning from the schema and write/read path, establish
whether it identifies or describes a natural person, and cite the evidence for the chosen category.
A foreign key can establish linkage, but is not required: serializers, lookup paths and attribution
can establish personal association too. Linkage or proximity alone does not explain a field's meaning.
Never assign `user.account` or `user.behavior` to every remaining field in a person-related table.
Never infer human activity merely from a timestamp suffix or a row update.

| Field meaning | Proposed treatment | Exclusion or uncertainty |
|---|---|---|
| Identifier for a person or individual account | `user.unique_id`, including opaque join keys | Organization and service identities are distinct from individuals. Task, record and catalogue keys require lookup, correlation and attribution investigation; mixed principals remain explicit. |
| Role or permission assigned to an individual | `user.authorization` when it describes access | Role definitions, permission catalogues and organization-wide requirements are non-personal. An employment title uses `user.job_title`. |
| Individual MFA enrolment or exemption state | `user.authorization` | A global MFA requirement is configuration. Free-text exemption reasons require a separate content assessment. |
| Individual training completion or policy acknowledgement | `user.behavior` for the evidenced completion/acknowledgement event or state | Course/policy definitions and generic processing statuses are non-personal. Assignment and due dates alone do not prove behaviour. |
| Individual login or activity event | `user.behavior` for the event; classify its identifiers separately | Background tasks, synthetic probes and unattributable operational metrics are non-personal. |
| Identifier linking requests to an individual session | `user.unique_id`; `user.authorization.credentials` if it authenticates access | Background-process correlation IDs without an individual association are non-personal. Do not collect credential values. |
| Generic record-maintenance timestamps such as `created_at` and `updated_at` | Investigate producers, defaults, consumers and personal association; no automatic non-personal default | Lack of a collected writer is missing evidence, not proof of non-personal meaning. Bookkeeping and individual events require separate assessments. |
| Event timestamps such as `completed_at` or `last_active_at` | Review the event and person linkage before proposing `user.behavior` | Timestamp names alone do not establish personal data. Expiry, synchronization, scheduling and maintenance do not by themselves establish behaviour. |
| Free text, documents or JSON payloads | Propose supported content categories when the contract or usage establishes personal content | Mark possible personal content as ambiguous when the evidence cannot establish its meaning; do not assert every value contains personal information or infer a specific sensitive category. |

These are mapping conventions and evidence requirements, not claims that technical metadata can
never relate to a person. Keep non-personal fields in the audit inventory even when omitted from
Fides export. Do not silently change accepted classifications: use the existing reconciliation,
proposal, semantic-change and acceptance workflow for any correction.

Reusable classification policy belongs in this plugin. Repository-specific evidence and proposals
belong in the analysis cache. Do not introduce private helper scripts or collection-wide fallback
rules as an alternative policy. Batching proposals is acceptable only when each field retains its
own supported meaning and rationale. Unknown meaning remains ambiguous; completeness checks do not
establish semantic correctness. These guidelines require agent judgment and human review; the
deterministic gate does not prove that an agent followed them.

## Evidence before category selection

Record these distinctions in `classification_review` within the existing analysis cache, using the
plugin decision API, source evidence and confidence. They are agent investigation requirements;
structural validation does not establish the semantic truth of a rationale:

- **Meaning and shape:** inspect the type and serialized value. An account-sounding field may be
  a boolean, a foreign key or an object. Read its write path before choosing a category.
- **Identification basis:** distinguish direct identification, relational identification,
  person-linked attributes/events, and content-dependent identification. Name the referenced
  identity entity and explain why it represents people; distinguish service accounts and companies.
  A join establishes linkage, not the semantic category of every other column on that row.
- **Category fit:** read the bundled category description and choose the most specific category
  supported by the value's meaning. Do not use a broad ancestor to hide unresolved category fit.
  `user.authorization` describes scope of
  permissions and access; it is not a catch-all for OAuth protocol artifacts. A user-linked nonce,
  verifier digest or transaction reference may have established personal linkage but an unresolved
  taxonomy mapping. Preserve both facts instead of changing it to non-personal or inventing a key.
- **Security sensitivity:** credentials and secrets require protection independently of whether
  they identify people. Do not infer personal status from secrecy, or credential status from a
  cryptographic-looking value. Never inspect production secrets to settle this distinction.
- **Attribution and purpose:** distinguish an actual person event from provenance/source labels,
  expiry and bookkeeping. Browser-local settings require evidence of identifiable attribution;
  origin-local storage alone does not establish it. A marketing profile is not necessarily a
  user-selected setting. Use `user.demographic.profile` only when profile meaning is evidenced.

The collector never assigns personal meaning from a name. Compiler-backed evidence resolves supported
TypeScript aliases, imports and object/array members; opaque or open shapes remain explicit gaps.
For composite values, model evidenced members separately and retain unknown-member questions.
Withdrawing an overbroad parent category does not remove known personal members from scope.
Distinguish **unmodeled known personal members**, **unknown content**, **unknown identity linkage**
and **unresolved category fit** in the question and resolution; do not present all four as unknown
PII presence. Complete code-resolvable modeling as agent work before requesting business context.
A review-ready flag checks the recorded investigation, not its semantic truth or acceptance.

## Names that need context, not a lookup

Corporate geography (`city`, `country`, `country_code`, `state`), `display_name`, `national_id`,
`website` and `homepage` require context and enter the proposal queue. A company address, display
label or website is not automatically a person attribute. A registration number requires evidence
of whether it identifies a legal entity or an individual, including sole traders.

These are the ones that reach you, and they are all cases where the same name means different things
in different schemas.

| Name | It depends on |
|---|---|
| `name` | Whose name? On `products` or `tags` it is not personal data at all. On a person row, `user.name`. |
| `id`, `uuid`, `account_id`, `customer_id` | `user.unique_id` **only when the row identifies a person or their account**. A join key to a non-person table is operational. |
| `status`, `enabled`, `is_active` | State on a person, account, subscription or activity record may describe behaviour or account settings. A service-health or migration flag may be operational. |
| `role` | An individual access assignment can be `user.authorization`; a role catalogue definition is non-personal. Use `user.job_title` only for an employment title. |
| `title` | A job title, a document title, or a salutation. Read the neighbours. |
| `address` | A postal address (`user.contact.address.*`), an email address, or a wallet/IP address. |
| `location` | `user.location.precise` for coordinates, `user.location.imprecise` for a region or city-level guess. |
| `notes`, `comment`, `bio`, `description` | Free text a person wrote: `user.content.public` or `user.content.private` depending on who can see it. Free text *about* a person may hold anything, including Article 9 data — flag it. |
| `token`, `key`, `secret` | `user.authorization.credentials` if it authenticates a person; `system.operations` if it is an idempotency key or a feature flag. |
| `age` | `user.demographic.age_range` for a bucket, `user.demographic.date_of_birth` if it is derived from one. |

## Contextual examples and limits

Use `classification-examples.json` as worked examples for agent reasoning, not as automatic
field-name rules. The examples are synthetic and inspired by Ethyca's
[CookieHouse dataset](https://github.com/ethyca/fides-cicd-quickstart/blob/302b394e28ef713a1531ada541dbe3dc63fcb9e6/.fides/cookiehouse_core.yml).
That quickstart demonstrates CI coverage and policy checks; its individual annotations are not a
universal classification authority. In particular, do not copy its operational email annotation or
its inconsistent timestamp treatment as general rules.

Establish these distinctions from current schemas and relevant producers/consumers:

- **Person identity versus record identity:** a customer or employee key can identify a natural
  person; a product, address or order key identifies a record. Investigate lookup, correlation and
  attribution before deciding whether that record key also identifies a person. A foreign key
  proves a relationship, not the meaning of every field in the joined row.
- **Value meaning versus table proximity:** a product label is not a person's name. An order's
  customer reference and product reference have different meanings even though they share a row.
- **Specific content categories:** use the most specific supported address component categories
  once the address is established as a person's contact address. Corporate geography is not
  automatically an individual's address.
- **Relationships versus categories:** Fides relationship and identity metadata supports traversal
  and matching. It does not substitute for `data_categories` or make an operational label proof
  that the record has no privacy relevance.
- **Events versus bookkeeping:** login/visit times need context about the event and its subject.
  Do not generalize an example timestamp label to every timestamp. Likewise, aggregate counts
  require evidence of their aggregation scope; a count on a person row may describe that person.

Use these examples alongside the canonical field-review workflow. Tests check taxonomy validity,
expected distinctions in the examples, and that context-sensitive field names remain queued for
agent review; tests cannot prove an agent's semantic judgment correct.

## Signals the exact table deliberately leaves out

Use these when the context supports them.

| Signal | Category |
|---|---|
| `url`, `website`, `homepage` on a person row | `user.contact.url` |
| `employer`, `company`, `organization` | `user.contact.organization` |
| `language`, `locale` | `user.demographic.language` |
| `marital_status` | `user.demographic.marital_status` |
| `gender`, `sex` | `user.demographic.gender` |
| `salary`, `income`, `amount_paid` | `user.financial` or `user.payment` |
| `avatar`, `profile_picture`, `photo` | `user.content.self_image` |
| `viewed`, `clicks`, `events`, `pageviews` | `user.behavior` |
| `search_query`, `query_history` | `user.behavior.search_history` |
| `orders`, `purchase_history` | `user.behavior.purchase_history` |
| `udid`, `idfa`, `gaid` | `user.device.device_id` |
| `tax_id`, `nino` | `user.government_id.national_identification_number` |
| `diagnosis`, `medical_*`, `health_*` | `user.health_and_medical` |
| `genetic_*`, `dna` | `user.health_and_medical.genetic` |
| `faceid`, `face_id` | `user.authorization.biometric` |

## Special-category data

GDPR Article 9, plus Article 10 for criminal offences. These are listed in
`classification.json` under `special_categories`, and the collector collects their citations into
`special_category_refs` for exactly one reason: **so nobody has to go looking for them.**

- `user.biometric`, `user.biometric.*`, `user.authorization.biometric`
- `user.health_and_medical`, `user.health_and_medical.*`
- `user.demographic.race_ethnicity`
- `user.demographic.religious_belief`
- `user.demographic.political_opinion`
- `user.demographic.sexual_orientation`
- `user.criminal_history` (Article 10, not Article 9, but treat it the same)

This changes how you **report**, not how you classify — still apply the most specific key. Two
things follow from assigning one: surface it as its own section in your report, and know that the
collection's review horizon halves, from 365 days to 183.

Watch for these arriving indirectly. A `notes` column on a patient table, a `tags` array on a
membership record, a free-text `reason` on a leave request — none of them look like Article 9 data
from the name, and all of them routinely hold it.

## Building the system half

The collector discovers services; it cannot know what they use data **for**. One system per
deployable service — in a monorepo, usually one per top-level service directory.

`system_type` is free text. Conventional: `Application`, `Service`, `Database`, `Data Warehouse`,
`Third Party`, `Integration`.

`dataset_references` are the `fides_key`s of datasets **defined in this same manifest**. A reference
to anything else is a dangling edge and the validator rejects it.

`privacy_declarations` — one per distinct purpose, each with a `data_use` from `data_uses.json`:

| What the code is doing | data_use |
|---|---|
| login, sessions, the core CRUD the product needs | `essential.service`, `essential.service.authentication` |
| usage metrics, dashboards, reporting | `analytics.reporting` |
| email or SMS campaigns | `marketing.communications.email`, `marketing.communications.sms` |
| ads, retargeting | `marketing.advertising`, `marketing.advertising.third_party.targeted` |
| recommendations, personalised feed | `personalize.content` |
| recruiting, HR | `employment.recruitment` |
| billing, invoicing, tax | `finance` |
| sending data to a third-party processor | `third_party_sharing` |
| training or fine-tuning a model on user data | `train_ai_system` |

And `data_subjects` from `data_subjects.json`. The full set is small — `anonymous_user`,
`citizen_voter`, `commuter`, `consultant`, `customer`, `employee`, `job_applicant`, `next_of_kin`,
`passenger`, `patient`, `prospect`, `shareholder`, `supplier_vendor`, `trainee`, `visitor`:

| Context | data_subject |
|---|---|
| storefront or SaaS end users | `customer`, plus `anonymous_user` for un-authenticated traffic |
| leads, not-yet-customers | `prospect` |
| HR, payroll, internal tooling | `employee` |
| applicant tracking | `job_applicant` |
| healthcare | `patient` |
| marketplace or B2B suppliers | `supplier_vendor` |

## Third-party flows

A system that sends personal data outward usually needs a `third_party_sharing` declaration. The
signals, all visible in code:

- **SDK imports and clients** — Stripe or Braintree (payments), Segment, Amplitude, Mixpanel or GA
  (analytics), Sentry (telemetry), Twilio, SendGrid or Mailgun (comms), Salesforce or HubSpot (CRM),
  OpenAI or Anthropic (AI).
- **Environment variables** — `*_API_KEY`, `*_DSN`, `*_WEBHOOK` name an integrated system.
- **Directory layout** — `services/*`, `apps/*`, or a separate `Dockerfile` usually means one system
  each.

An import is evidence of a flow, not proof of one: a client that is constructed and never called
sends nothing. Cite the line, say what you found, and let the person who knows the service decide.

## Modeled semantics and execution

Use the same meaning and person-association standard for real implementations, mocks and fixtures.
Describe modeled information first, then separately identify implemented, simulated and absent
storage/transmission operations. Constants and discarded records do not establish that modeled
personal fields are non-personal. Investigate record identifiers through lookup, correlation and
attribution consumers. Distinguish person actions from system outcomes and bookkeeping; association
with a user cannot alone support `user.behavior`. Hash inputs and uses, not cryptographic appearance,
establish whether a digest represents a file, a person-derived identifier, a biometric or a credential.

Complete the API's six `semantic_review` findings with current evidence for each field decision.
Maintain explicit unknowns: what is known, what evidence is absent, and which conclusion depends
on it. No broad category or approval substitutes for missing evidence.
