# Branching strategy

| Branch | Purpose | Deploy |
|--------|---------|--------|
| `main` | Production-ready, tagged releases | Cloudflare + production host |
| `development` | Integration / nightly validation | Staging (optional) |
| `feature/*` | Short-lived features | — |
| `fix/*` | Bug fixes | — |
| `hotfix/*` | Production emergency fixes → main + development | Production |

## Rules
1. Never commit secrets or `.env`.
2. All changes to `main` via PR from `development` or `hotfix/*`.
3. CI must pass on PR before merge.
4. Releases: tag `vX.Y.Z` on `main` → DockerHub + changelog.

## Flow
```
feature/* → development → main → tag v*
```
