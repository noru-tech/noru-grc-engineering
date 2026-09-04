# Rollout modes

Use **strict** when all configured manifests already pass. No baseline is permitted.

Use **ratchet** when exact legacy violations must be accepted temporarily. Each fingerprint needs a
named human owner, rationale, decision date, and expiry. New, changed, increased, expired, resolved,
or later-reintroduced violations fail.

Use ruleset `evaluate` only where the GitHub plan supports it. Otherwise install the workflow as a
non-required check for an observation period, then explicitly plan and activate enforcement.

Repository rulesets are the pilot path. Organisation rulesets are preferable in production because
central owners can require a central workflow and target repositories by custom property; repository
rules may add restrictions but must not weaken the parent.
