# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-18

### Added

- Coolify adapter: sync servers, applications, services, databases and deployments; track
  deployments to completion; optional webhook receiver for instant updates.
- Incidents for failed deployments, crashes, unhealthy resources, crash loops, OOM kills, high CPU
  and memory, disk pressure, and disk-full forecasts, with deduplication and auto-resolve.
- Agentless server and container metrics through a read-only SSH probe with a restricted key,
  generated setup script, and host key pinning.
- Rule-based log diagnosis (about 20 failure patterns), log fingerprinting, and secret redaction.
- Bring-your-own-key AI diagnosis: Anthropic Claude and any OpenAI-compatible provider (Gemini,
  DeepSeek, OpenAI, OpenRouter, Groq, Mistral, Ollama).
- Web UI: overview, servers, resources, incidents, activity and audit log, settings.
- Opt-in, confirmed, audited actions: restart, redeploy, stop, start.
- Slack/Discord-compatible alert webhook.
- Dockerfile and Coolify-ready `docker-compose.yml`.

[Unreleased]: https://github.com/abiola814/OpsMender/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/abiola814/OpsMender/releases/tag/v0.1.0
