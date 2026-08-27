# Classification guide

For the half of the work the collector cannot do.

`classification.json` holds the names that mean the same thing in **every** schema — `email`,
`password_hash`, `last_login_ip`. The collector applies that table mechanically, which is what lets
it be deterministic, and marks everything else `needs_review: true`.

This file is for those. It is guidance for a judgement, not a lookup table, and that is why it is
prose the agent reads rather than data the collector applies. If a rule here ever becomes true in
every schema regardless of context, move it into `classification.json` instead.

Every key below was checked against the bundled snapshot in `references/taxonomy/`. That snapshot is
the offline floor; where you can reach Noru, `getPrivacyTaxonomy` is the truth. **Never invent a
key** — if the one you want does not exist, the answer is a different key or an unresolved flag.

## Principles

1. **Most-specific wins.** A column literally named `email` is `user.contact.email`, not
   `user.contact`.
2. **A field can carry several categories.** A `billing_address` blob may warrant
   `user.contact.address.street` + `.city` + `.postal_code`.
3. **Operational columns are not personal data.** Surrogate keys, timestamps, soft-delete flags, row
   versions: `system.operations`, or an empty `data_categories`. Do not force a `user.*` label onto
   them.
4. **A wrong label is worse than an unresolved one.** The unresolved one gets reviewed. The wrong
   one gets signed, and then it is in the record with somebody's name against it. If you cannot
   tell, say so and ask.

## Names that need context, not a lookup

These are the ones that reach you, and they are all cases where the same name means different things
in different schemas.

| Name | It depends on |
|---|---|
| `name` | Whose name? On `products` or `tags` it is not personal data at all. On a person row, `user.name`. |
| `id`, `account_id`, `customer_id` | `user.unique_id` **only when the row is a person**. A join key to a non-person table is `system.operations`. |
| `role` | Usually an RBAC role (`admin`, `member`) → `user.account` or `system.operations`. `user.job_title` only if the surrounding model is clearly about employment. |
| `title` | A job title, a document title, or a salutation. Read the neighbours. |
| `address` | A postal address (`user.contact.address.*`), an email address, or a wallet/IP address. |
| `location` | `user.location.precise` for coordinates, `user.location.imprecise` for a region or city-level guess. |
| `notes`, `comment`, `bio`, `description` | Free text a person wrote: `user.content.public` or `user.content.private` depending on who can see it. Free text *about* a person may hold anything, including Article 9 data — flag it. |
| `token`, `key`, `secret` | `user.authorization.credentials` if it authenticates a person; `system.operations` if it is an idempotency key or a feature flag. |
| `age` | `user.demographic.age_range` for a bucket, `user.demographic.date_of_birth` if it is derived from one. |

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
