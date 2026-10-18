# OpsMender

**An open-source, lightweight AI DevOps companion for [Coolify](https://coolify.io).**
Deploy it on your Coolify server and get one dashboard for every server, app, service, database,
deployment and incident, with plain-language diagnosis from the AI provider you choose.

- **See everything**: servers, applications, services, databases, deployments, live status.
- **Server and container metrics**: CPU, memory, disk, restarts and OOM kills, with no agent left running.
- **Incidents with evidence**: failed deployments, crashes, crash loops, unhealthy containers,
  high CPU/memory, full disks, and a disk-full forecast.
- **Rule-based diagnosis, free**: about 20 known failure patterns (database refused, out of memory,
  port in use, missing module, missing env var…) explained with concrete fix steps.
- **AI root-cause analysis, bring your own key**: Claude, Gemini, DeepSeek, OpenAI, OpenRouter,
  Groq, Mistral, a local Ollama model, or any OpenAI-compatible endpoint.
- **Safe fixes**: restart, redeploy, stop and start. Off by default, confirmed by typing the
  resource name, and recorded in an audit log.
- **Alerts**: Slack- or Discord-compatible webhook.

## Why it's light

OpsMender does not need Prometheus, Grafana, Loki or Redis, and doesn't run an always-on agent.

- One Python process with SQLite. About 55 MB RAM at idle before an AI provider is used.
- Logs are fetched **only when something goes wrong**: the last N lines, capped in bytes, with
  secrets redacted. Full log streams are never stored.
- Server metrics come from a short SSH session once a minute that runs a read-only shell script,
  prints one JSON snapshot and exits. Nothing listens and nothing is written on your servers.

## Install on Coolify

1. In Coolify: **+ New → Public Repository**, point it at this repository, choose the
   **Docker Compose** build pack, and give the `opsmender` service a domain (for example
   `https://ops.example.com`).
2. Deploy. Open the domain and enter the **setup code** printed in the container logs to create
   the admin account. To skip this step, set `OPSMENDER_ADMIN_USER` and
   `OPSMENDER_ADMIN_PASSWORD` instead.
3. In **Settings → Coolify connection**, paste your Coolify URL and an API token
   (Coolify → Keys & Tokens → API tokens).
   - Read access is enough for monitoring. Fetching logs may need `read:sensitive`.
   - Restart and redeploy need write/deploy.
4. Optional: open a server and click **Enable metrics** (see below).
5. Optional: add an AI provider in **Settings → AI provider**.
6. Optional: paste the webhook URL from **Settings → Instant updates** into Coolify's webhook
   notification settings, so failures appear immediately instead of within one poll interval.

### Run anywhere else

```sh
docker compose up -d --build        # then open http://localhost:8000
```

Or without Docker: `pip install . && OPSMENDER_DATA_DIR=./data python -m opsmender`.

## Server metrics

Click **Enable metrics** on a server and download `opsmender-setup.sh`. Read it, then run it once
as root on that server. It:

1. creates a system user `opsmender` that has no password and can't get an interactive shell;
2. installs a read-only probe at `/usr/local/lib/opsmender/probe`;
3. allows that user to run *only the probe* as root, which is needed to list containers through Docker;
4. authorizes OpsMender's own SSH key with `restrict` and a forced command. The key can't open a
   shell, allocate a terminal, forward ports or run anything except the probe.

The probe accepts two requests: `snapshot`, and `logs <container> <lines>` (strictly validated).
Host keys are trusted on first use and then pinned.

For the server Coolify itself runs on, use `host.docker.internal` as the host. The compose file
maps it to the Docker host.

To remove it: `userdel -r opsmender; rm -rf /usr/local/lib/opsmender /etc/sudoers.d/opsmender`.

## AI providers

| Provider | Notes |
|---|---|
| Anthropic Claude | Native Messages API via the official SDK. Default model `claude-opus-5`. |
| Google Gemini | OpenAI-compatible endpoint |
| DeepSeek, OpenAI, OpenRouter, Groq, Mistral | OpenAI-compatible |
| Ollama | Local and free. Defaults to `http://host.docker.internal:11434/v1`. |
| Anything else | Any OpenAI-compatible `/chat/completions` endpoint |

OpsMender sends the AI only an incident's evidence: status, recent deployments, metrics, rule
findings and a redacted log excerpt. It does so only when you click **Diagnose with AI** or
enable automatic diagnosis.

## Configuration

Set these as environment variables. Everything else is configured in the UI.

| Variable | Default | Purpose |
|---|---|---|
| `OPSMENDER_DATA_DIR` | `./data` (`/data` in Docker) | SQLite database and generated secret key |
| `OPSMENDER_SECRET_KEY` | generated into the data dir | Signs sessions and encrypts stored secrets |
| `OPSMENDER_BASE_URL` | derived from the request | Public URL, used for the webhook URL and secure cookies |
| `OPSMENDER_ADMIN_USER` / `OPSMENDER_ADMIN_PASSWORD` | – | Create the admin account on first start |
| `OPSMENDER_COOLIFY_URL` / `OPSMENDER_COOLIFY_TOKEN` | – | Prefill the Coolify connection |
| `PORT` | `8000` | HTTP port |

## Security

- Admin login uses scrypt password hashing and login rate limiting. Sessions are signed
  `SameSite=Strict` cookies, and every form carries a CSRF token.
- The Coolify token, AI keys, SSH private key and webhook URLs are encrypted at rest.
- OpsMender mounts no Docker socket and no host filesystem.
- Secrets are redacted from log text before it is stored or sent to an AI provider.
- Security headers include CSP, X-Frame-Options and nosniff.

Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Development

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

The layout is described in [docs/PLAN.md](docs/PLAN.md). The Coolify adapter
(`opsmender/adapters/coolify.py`) parses API responses defensively because field names vary
between Coolify releases. Additional adapters (plain Docker, Kubernetes) can implement the same
`Provider` interface.

## Contributing

Contributions are welcome, especially testing against different Coolify versions and new diagnosis
rules. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).
Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)

## Status

v0.1, early. Tested against a simulated Coolify API and real Linux servers over SSH, but **not yet
against a wide range of live Coolify versions**. If something doesn't match your Coolify
release, please open an issue with your version.
