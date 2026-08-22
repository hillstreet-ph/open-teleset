# Changelog

All notable changes to open-teleset are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-22

### Added
- Production Supabase schema and Auth profile triggers
- Encrypted session storage layer (`src/open_teleset`)
- GitHub Actions: test, migrate, Docker build, Cloudflare deploy
- Docker production image and compose stack
- Development branch workflow and platform automation stubs

### Security
- Secrets must only live in GitHub Actions / host secret stores
