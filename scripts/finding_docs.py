"""Where each finding kind is documented — the one place the Python tools learn that URL.

Every finding line the suite prints names a kind in brackets, `[drift]`, `[expired]`. Each kind has
a stable page under docs/findings/ saying what the rule is, why it matters, a failing and a passing
example, and how to fix it or record a disposition. A line that names a kind points at that page,
so the next step is one click from the log rather than a search.

The JSON reports deliberately carry no URL. A report field is part of a machine interface, and
repo-enforcement fingerprints every key of a finding: adding one would re-key every accepted
baseline entry in every repository using the ratchet.

The Node action runtime (actions/enforce/dist/enforce.js) keeps its own copy of the base, because
it cannot import Python; scripts/test_repo_enforcement.py asserts the two agree.
"""

FINDINGS_DOCS_BASE = "https://github.com/noru-tech/noru-grc-engineering/blob/main/docs/findings/"


def finding_url(kind):
    """The documentation page for a finding kind. Kinds are lowercase identifiers by contract."""
    return f"{FINDINGS_DOCS_BASE}{kind}.md"


def see(kind):
    """The suffix a human-readable finding line ends with."""
    return f" (see {finding_url(kind)})"
