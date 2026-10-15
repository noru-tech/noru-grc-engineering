# Reuse investigated evidence without inventing certainty

Extraction limitations remain agent-owned investigation. These conveniences do not infer privacy
meaning, approval or physical datastore identity. Scoped uncertainty accepted through the existing
uncertainty API remains a valid disposition; unsupported syntax alone is not a permanent veto.

## Preserve resolutions after structural edits

Gap bindings include the complete affected dataset structure and targeted runtime definitions,
observed diagnostic occurrences, the submitted disposition and cited evidence. Adding an unrelated
dataset preserves a resolution after fresh collection and reconciliation. Changing the affected
structure, source evidence or occurrence coverage reopens it. Unscoped findings retain the global
fallback. Unchanged legacy signatures migrate on reconciliation; stale legacy evidence is not refreshed.
Raw findings, dispositions and history remain visible. A stale scan must still be refreshed.

## Shared semantic reasoning

`investigate.py --decisions=<file>` accepts the existing list or this envelope:

```json
{
  "reasoning_groups": {
    "operational": {"semantic_review": {}}
  },
  "decisions": [
    {"entity_id": "store/records/status", "reasoning_group": "operational"}
  ]
}
```

This is a shape example, not a valid completed decision. Supply all six semantic-review dimensions
with current evidence IDs in the group, and the usual observation digest, meaning, person
association, categories and other decision properties per field. A field's `semantic_review`
entries override individual shared dimensions. Only semantic review is inherited. The normal
per-field evidence and consistency checks run on the expanded decisions, which are stored in the
canonical cache. Do not use a group to copy a classification verdict.

## Grouped migration investigation

`migration_groups` lists findings sharing an evidenced datastore/context scope. Unresolved
identities are not grouped. Submit `--gap-groups=<file>` with a list of objects containing
`group_id`, the exact returned `covered_occurrences` mapping, and `resolution` containing the
ordinary gap decision properties. Evidence and coverage bindings must cover all included migrations.
The API expands the shared investigation into separate validated dispositions atomically. It does
not erase raw findings, infer current schema from migration history or treat identity as completeness.
For accepted uncertainty, use the existing scoped acceptance records for each finding.

Source-scoped diagnostics also bind the fields and runtime definitions evidenced by their source
file or project scope. An unrelated dataset does not invalidate that decision; changes within the
represented scope still do. Findings without explicit source, dataset or system scopes retain the
conservative global fallback.

## Apply a candidate draft

`reconcile.py --repo=<repo> --apply-candidate` projects current field decisions and system activities
into the local YAML, updates nested collection signatures and archives applied structural and
relationship proposals. It does not seal, create an interpretation or clear outstanding approval.
Newly projected supplemental members carry their evidenced shape and typed-contract provenance;
existing member metadata is retained. Before writing, application performs a read-only collection
with the proposed manifest inputs and stamps its source digest. Later changes to fields or system
dataset links still require recollection and reconciliation; a digest is not an approval.
Recollect and reconcile afterward before review or sealing. The application writes through a local
recovery journal: retrying after interruption completes the same application, while intervening
file edits stop recovery rather than being overwritten. The lock is untouched.

## Non-personal systems

A system proposal may set `personal_data: no`, `processing_activities: []` and
`non_personal_rationale`, with the normal source citations and four investigation dimensions.
This outcome cannot contradict linked personal or unresolved fields. Projection retains the
review in `meta.non_personal_processing` and emits an empty declaration list rather than invented
subjects or categories. Named-owner interpretation is still required for acceptance. Changed
source or flow evidence requires renewed review. Unknown processing should use investigation and
scoped uncertainty acceptance, not a false non-personal assertion.

## Structural starting points

Unconditional modeled .NET runtime bindings now propose dataset links to discovered runtime
systems. Conditional and ambiguous bindings remain with the agent.

`dotnet.supplemental_candidates` supplies source-defined fields for explicit fully qualified generic
System.Text.Json calls with default options, including inherited/record members, and literal-key
fully qualified MAUI SecureStorage/Preferences calls with supported string values. These are
suggestions, not registered stores or accepted fields. The agent traces actual Redis writes,
message publications, activation and device/container identity before constructing supplemental
inputs. Serializer options, dynamic keys, unsupported overloads, custom converters, aliases and
unqualified/inferred generic calls still need source investigation. Candidate limitations do not
add new publication gates. Literal, unconditional Compile links establish source membership with
the project declaration retained as evidence. Conditional, wildcard and multiply-owned links still
require membership investigation; linked files are not automatically dismissed as helpers.

The API contracts used for these suggestions are documented by Microsoft:
- https://learn.microsoft.com/en-us/dotnet/api/system.text.json.jsonserializer.serialize
- https://learn.microsoft.com/en-us/dotnet/maui/platform-integration/storage/secure-storage
- https://learn.microsoft.com/en-us/dotnet/maui/platform-integration/storage/preferences

Conditions limited to recognized publishing/debug properties do not imply unresolved source
membership; conditions affecting items, references or compilation symbols still do. Alias
observations cite their actual declaration line.
