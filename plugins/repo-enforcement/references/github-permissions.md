# GitHub permissions

Prefer a connected GitHub App, then an existing authenticated `gh` session. Never request a pasted
token. Inspection needs repository metadata and rules read access. Repository ruleset apply needs
Administration write. Organisation rollout additionally needs organisation Administration and team
read. Missing permission blocks only that phase.

The pull-request validation workflow has `contents: read` only. It receives no Noru credential and
no GitHub administration credential.
