<!--
This repository is public, and so is this pull request. Read CONTRIBUTING.md, "Everything here is
public", before you write the description: no private Noru paths, behaviour, analysis or plans.
-->

## What

<!-- The behaviour change, in one or two sentences. -->

## Why

<!-- The problem it solves. Link an issue if there is one. -->

## How it was tested

<!-- Every claim gets a test. Name the check that asserts the new behaviour. -->

## Security implication

<!-- Credentials, scopes, write paths, or "none". -->

## Checklist

- [ ] The CONTRIBUTING.md verification block passes in full, including `python3 scripts/publish_actions.py --check`
- [ ] `git diff --check` is clean
- [ ] No new dependency: Node built-ins and the Python standard library only
- [ ] `CHANGELOG.md` has an entry under `## Unreleased` for anything user-visible
- [ ] No credential, customer identifier or output from a real organization in the diff
