# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Read-only disk usage analysis per server: Docker images, build cache, stopped containers, volumes,
  container logs, system journal and Coolify backups, each with how much it would free, its risk
  (safe / low / medium / danger), what removing it means, and the command to do it yourself.
  Nothing is deleted.
- Disk-pressure and disk-forecast incidents now include this breakdown automatically, plus
  findings such as "Build cache: 21 GB can be freed" and "Docker log rotation is off", and pass it to
  the AI diagnosis.
- Probe v2 with a `disk` request. Servers running probe v1 show a notice to re-run the setup script.
- One token, everything from Coolify:
  - Automatic server metrics: the probe is installed with the SSH key Coolify already has for each
    server (used once, never stored), for new servers and to update outdated probes. Per-server
    "Set up automatically" button; manual setup is still available.
  - Alerts are sent through the team's Coolify notification channels (email via SMTP or Resend,
    Discord, Slack, Telegram, Pushover, webhook), skipping alerts Coolify already sends itself.
    Alerts include the likely cause and a link to the incident.
  - Logs for databases and services, not only applications.
  - Settings shows what the token gives OpsMender and what's missing (for example read:sensitive).

### Changed

- Existing databases are migrated automatically on start.

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
