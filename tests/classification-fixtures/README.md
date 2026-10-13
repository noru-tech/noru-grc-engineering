# Classification evaluation fixtures

These are synthetic applications. Their expected decisions were written from the application
scenarios, separately from the classifier implementation. They contain no captured customer data.
The engine never receives `expected.json`; the harness removes it before scanning each repository.

The relational fixture has natural-person contacts, legal entities, both foreign-key targets,
individual completion activity, record-maintenance timestamps, typed nested contact arrays,
an open extension object, mixed person/company content, and deliberately misleading field names.
The build fingerprint is a file digest, `race` is a number, and one `email` is a boolean.
A hashed contact address remains potentially person-linked but is not a raw email address.
Directly stored typed inputs remain open at runtime: their known members are classified, while
extra keys require an unavailable producer/validation contract. The reviewed fixture records these
as explicit unresolved objects; resolving the extraction work does not certify their completeness.
The SQL fixture supplies current declarations independently of migration history.

The evaluation reports separate dimensions:

- inventory coverage, including nested members;
- resolved personal-data precision and false exclusions;
- category accuracy on resolved personal fields;
- resolution coverage and unresolved review work;
- static inference and evidence-backed investigation outcomes.

An unresolved result is not a correct resolved classification. It can satisfy a conservative safety
assertion while still counting against resolution coverage. No numerical accuracy claim follows
from a small synthetic fixture suite, and the fixtures are not a substitute for an independently
maintained evaluation corpus.

Metamorphic checks change irrelevant source placement, imports, formatting and declaration order.
Mutation checks change an imported type, add an alternative writer, or add open nested content;
these must invalidate existing evidence rather than preserve a stale review. Migration declarations
never contribute classified fields.

Run `python3 scripts/test_classification_evaluation.py` from the repository root. The harness uses
only the shipped plugin and local runtimes; it installs nothing and calls no external service.
