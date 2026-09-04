# Noru GRC repository enforcement action

Runs every piece required by `.noru/enforcement.yml` against the whole checkout. It uses the
collectors, validators, and generated registry shipped in the pinned release, not executable code
from the target repository. It has no network step, reads no Noru credential, and requires an
explicit `as-of` date.

Use the generated workflow from `repo-enforcement`; it pins this action to a full commit SHA and
keeps the stable check name `Noru GRC / validate`.
