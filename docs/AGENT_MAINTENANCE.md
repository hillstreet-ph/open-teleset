# Repository maintenance agent

Autonomous / semi-autonomous loop for maintaining https://github.com/hillstreet-ph/open-teleset

## Goals
- Keep CI green on `main` and `development`
- Apply schema migrations safely
- Never expose secrets
- Prefer small PRs over direct pushes to main

## Triggers
- Failed GitHub Action
- Dependabot PR
- Scheduled validate-heal job failure
- New issue labeled `bug` or `security`

## Procedure
1. Checkout `development`
2. Reproduce failure locally (tests, docker build)
3. Implement minimal fix
4. Add/adjust test
5. Open PR → `development`
6. After green CI, PR `development` → `main`
7. On release: bump `VERSION`, update `CHANGELOG.md`, tag `vX.Y.Z`

## Forbidden
- Committing `.env`, tokens, session strings
- Force-push to `main`
- Disabling security jobs to "make green"
