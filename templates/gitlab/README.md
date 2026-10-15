# GitLab CI/CD components

Two components, the GitLab counterparts of the two GitHub Actions:

| Component | Counterpart | What it does |
|---|---|---|
| [`noru-ci.yml`](./noru-ci.yml) | `noru-ci` action | Runs one piece's CI-mode check — drift, expiry, privacy baseline, and, with `trust-id` or `NORU_API_KEY`, the `state`, `diff` and `push` steps |
| [`noru-review.yml`](./noru-review.yml) | `noru-review` action and [`../github/noru-grc-review.yml`](../github/noru-grc-review.yml) | Routes a merge request's changes to the pieces they implicate and runs only their local checks. No `diff` or `push`, ever |

Include them from a **release tag** of this repository. Replace `vX.Y.Z` with the tag of the
[release](https://github.com/noru-tech/noru-grc-engineering/releases) you want; the job runs the
toolkit at that same tag:

```yaml
include:
  - remote: https://raw.githubusercontent.com/noru-tech/noru-grc-engineering/vX.Y.Z/templates/gitlab/noru-review.yml
  - remote: https://raw.githubusercontent.com/noru-tech/noru-grc-engineering/vX.Y.Z/templates/gitlab/noru-ci.yml
    inputs:
      piece: ai-inventory
      mode: warn
```

GitLab.com and GitLab Self-Managed both work this way; `include: remote` needs only that the runner
and the instance can reach GitHub. The CI/CD Catalog (`include: component:`) can list components only
from a project on the same GitLab instance, which this repository is not; once a GitLab component
project is published from it, the same templates are available there too. The full walkthrough —
inputs, the reports each job writes, the merge request note and its token, and what GitLab shows on
which tier — is [docs/ci-mode.md, "GitLab CI"](../../docs/ci-mode.md#gitlab-ci).
