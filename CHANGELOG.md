# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Optimize page: right-sizing advice for every app from the last 7 days of metrics. Flags apps
  close to their memory limit (with the bigger limit to set), apps with no memory limit, limits far
  above real use, apps using a large share of a server that's short of memory, memory leaks (with
  the time until the limit is reached), CPU-heavy and idle apps, servers that are short of memory,
  overcommitted or oversized, and disk space that can be freed. Each item says exactly what to change
  in Coolify; for applications and databases, the suggested memory limit can be applied from
  OpsMender (with Actions on, confirmed and audited). Shown on resource pages and the overview too.
- Memory-leak incidents: an app whose memory keeps growing opens an incident when it's predicted to
  reach its limit (or the server's free memory) within 24 hours.
- Container metrics are kept as hourly rollups for 14 days (previously 24 hours), and an app's
  history is kept across redeploys.
- Fix pull requests (GitHub): from an incident on an application deployed from GitHub, the AI reads
  the repository (read-only), drafts the smallest fix and shows the diff for review. Edits are checked
  (exact matches, size, Python/JSON/TOML syntax, no literal secrets, no CI workflow changes). Only
  "Open pull request" writes to GitHub: one commit on a new branch and a draft pull request.
  OpsMender never pushes to existing branches and never merges. Connect a fine-grained token in
  Settings → GitHub; GitHub Enterprise is supported.
- Per-resource alert rules: send a resource's or server's alerts only to chosen destinations, only
  above a minimum severity, also to its own webhook, or mute them. Set from the Alerts card on each
  resource and server page; a server's rule covers its resources unless they have their own.
- AI investigations: instead of answering from the initial evidence alone, the AI can call read-only
  tools to fetch more logs, deployment build logs, configuration, environment variable names (never
  values), server and container metrics, disk usage, other resources and past incidents. Capped at
  8 lookups per diagnosis; every lookup and its result is listed under the diagnosis. Works with
  Claude and OpenAI-compatible providers, and falls back to a single request for models that can't
  call tools. On by default; turn it off in Settings → AI provider.
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
- Requires `anthropic` 1.15 or later.
- Lower memory use: about 9 MB less at idle (the SSH library loads only when metrics are used), and
  `MALLOC_ARENA_MAX=2` in the Docker image. The login rate limiter no longer keeps expired addresses.
- `OPSMENDER_SECURE_COOKIES` defaults to per-request: session cookies are Secure on HTTPS requests.

### Fixed

- "Form expired" on every form when OpsMender is served over plain HTTP (such as Coolify's
  generated sslip.io domains): the session cookie was marked Secure based on a guessed https:// URL.
- When Coolify's API is turned off, the Coolify connection test says so and where to enable it,
  instead of reporting a token permission problem.

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
