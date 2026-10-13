# `coverage`

**Rule:** a collector must not report a clean result for schemas it could see but could not parse —
nothing parsed in a repository that visibly has a schema is a broken gate, and a partial parse is
reported.

| | |
|---|---|
| Emitted by | `scripts/ci_check.py` scan step, for any piece whose derived facts carry a `coverage` block (today `privacy-datamap`) |
| Gates by default | **nothing parsed:** always — exit `6`, even under `--mode=warn`. **Partial:** no — advisory; `--fail-on=coverage` makes it exit `6` |
| Accepted in a ratchet baseline | partial: yes. Nothing parsed: the accompanying [`tooling`](./tooling.md) violation cannot be |

## Why it matters

An empty data map and a repository with no personal data in it are the same file, and only one of
them is good news. Drift, expiry and policy all pass on an empty set, so a repository whose schema
the collector cannot read would otherwise get a green build that means nothing. The collector looks
for schema shapes it knows it cannot parse yet — TypeORM, Mongoose, Sequelize, ActiveRecord, Ecto,
GORM, OpenAPI, JSON Schema, Zod, unsafe migration-only state — and reports each one with its
`file:line`. See [ci-mode.md](../ci-mode.md#where-this-is-weaker-than-it-looks).

## Controls

This repository maps no framework control to this finding, on purpose: it ships no control
catalogue ([CONTRIBUTING.md](../../CONTRIBUTING.md#ground-rules), "No framework catalogue"). Which
controls the underlying record supports is decided in your Noru organization, and pieces read control
ids from Noru's queue rather than shipping them
([contract requirement 9](../../contract/README.md#the-nine-requirements)).

## Failing example — nothing parsed

A repository whose only schemas are in formats the collector cannot normalise (the case
`scripts/test_ci_mode.py` runs):

```text
$ python3 scripts/ci_check.py --piece=privacy-datamap --repo=. --output=text --quiet
  BLOCKING [coverage] .noru/privacy-datamap.yml: the collector produced no safe schema, but found 5 structural coverage gap(s) (activerecord, gorm, mongoose, openapi, typeorm). An empty data map is not the same as a repository with no personal data in it, and every check after this one would have passed on the empty set (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/coverage.md)

ERROR: a check could not run — scan: no safe dataset normalized; 5 gap(s) found
This is a tooling failure, not a compliance finding.
```

## Failing example — partial

The same files next to one SQL schema the collector can read:

```text
  warn [coverage] .noru/privacy-datamap.yml: 5 structural observation(s) could not be normalized safely (activerecord, gorm, mongoose, openapi, typeorm), so the data map does not describe them. 1 file(s) were parsed, so the map is partial rather than empty (see https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/coverage.md)
```

## Passing example

A repository with no schema at all, or only schemas the collector parses (SQL DDL, migrations,
protobuf, GraphQL, the supported ORMs), produces no `coverage` line. A JSON Schema that describes a
file format rather than a stored record is not counted.

## How to fix

- Look at the `refs` in the JSON report: each is a file the map does not describe.
- If the schema holds personal data, describe it in a form the collector reads, or record the
  data in the manifest by hand with refs and an interpretation block.
- If it does not, the partial finding stays advisory; leave it, or gate on it when the map is meant
  to be complete.

## Recording a disposition

- **Nothing parsed:** none in CI mode — exit `6` is never suppressed by `--mode=warn`, because a
  check that could not run is not a check that passed ([exit codes](../ci-mode.md#exit-codes)).
- **Partial:** advisory unless `--fail-on=coverage`.
- **Under [repository enforcement](../repository-enforcement.md#install-and-adoption):** `coverage`
  is in the default `fail_on` list, and a partial map can be accepted in a ratchet baseline like
  any other baselineable violation. When nothing was parsed, the piece could not run, so it also
  carries a [`tooling`](./tooling.md) violation, which is gated even if the piece's `fail_on` omits
  `coverage` and can never be baselined.
