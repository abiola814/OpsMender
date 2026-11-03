# OpsMender

**An open-source, lightweight AI DevOps companion for [Coolify](https://coolify.io).**
Deploy it on your Coolify server and get one dashboard for every server, app, service, database,
deployment and incident, with plain-language diagnosis from the AI provider you choose.

- **See everything**: servers, applications, services, databases, deployments, live status.
- **Server and container metrics**: CPU, memory, disk, restarts and OOM kills, with no agent left running.
- **Incidents with evidence**: failed deployments, crashes, crash loops, unhealthy containers,
  high CPU/memory, full disks, and a disk-full forecast.
- **Disk usage analysis**: see what's filling the disk (images, build cache, logs, volumes, backups),
  how much each cleanup would free and how risky it is. Read-only.
- **Rule-based diagnosis, free**: about 20 known failure patterns (database refused, out of memory,
  port in use, missing module, missing env var…) explained with concrete fix steps.
- **AI root-cause analysis, bring your own key**: Claude, Gemini, DeepSeek, OpenAI, OpenRouter,
  Groq, Mistral, a local Ollama model, or any OpenAI-compatible endpoint. The AI investigates on
  its own with read-only tools (more logs, build logs, metrics, disk usage, config, environment
  variable names) and shows each lookup it made.
- **Optimize**: which apps are close to their memory limit, have none, leak memory, keep the CPU
  busy or sit idle, and what to set in Coolify instead. Disk space you can free safely, too.
- **Fix pull requests**: the AI reads the app's GitHub repository, drafts the smallest fix and shows
  you the diff. You open it as a draft pull request with one click. OpsMender never merges.
- **Safe fixes**: restart, redeploy, stop and start. Off by default, confirmed by typing the
  resource name, and recorded in an audit log.
- **Alerts** through your Coolify notification channels or a webhook, with per-resource rules:
  send an app's alerts to its team's channel, only critical ones, or mute it.

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
   (Coolify → Keys & Tokens → API tokens) with **read** and **read:sensitive**.
   Add write/deploy only if you want restart and redeploy buttons.

That's it. With that one token, OpsMender uses what you've already set up in Coolify:

- **Server metrics set themselves up.** OpsMender uses the SSH key Coolify already has for each
  server, once, to install its read-only probe. Coolify's key is never stored.
- **Alerts go to your existing Coolify notification channels** (email, Discord, Slack, Telegram,
  Pushover, webhook). Alerts Coolify already sends itself aren't duplicated.
- **Logs** for applications, databases and services come from Coolify.

The Settings page shows what the token gives OpsMender. Optional extras:

- Add an AI provider in **Settings → AI provider**.
- Paste the webhook URL from **Settings → Instant updates** into Coolify's webhook notification
  settings, so failures appear immediately instead of within one poll interval.

### Run anywhere else

```sh
docker compose up -d --build        # then open http://localhost:8000
```

Or without Docker: `pip install . && OPSMENDER_DATA_DIR=./data python -m opsmender`.

## Server metrics

Metrics are set up automatically when the Coolify token has `read:sensitive` (you can turn this off
in Settings). Each server page also has a **Set up automatically** button. To set it up by hand
instead, click **Set up manually** and download `opsmender-setup.sh`. Read it, then run it once as
root on that server. Either way the same script runs. It:

1. creates a system user `opsmender` that has no password and can't get an interactive shell;
2. installs a read-only probe at `/usr/local/lib/opsmender/probe`;
3. allows that user to run *only the probe* as root, which is needed to list containers through Docker;
4. authorizes OpsMender's own SSH key with `restrict` and a forced command. The key can't open a
   shell, allocate a terminal, forward ports or run anything except the probe.

The probe accepts three requests: `snapshot`, `disk` (sizes only), and `logs <container> <lines>`
(strictly validated). It never deletes or changes anything.
Host keys are trusted on first use and then pinned.

For the server Coolify itself runs on, use `host.docker.internal` as the host. The compose file
maps it to the Docker host.

To remove it: `userdel -r opsmender; rm -rf /usr/local/lib/opsmender /etc/sudoers.d/opsmender`.

## Optimize

The **Optimize** page turns the last 7 days of container metrics into concrete advice. It needs
server metrics, and about a day of data before sizing advice appears.

| Finding | What OpsMender suggests |
|---|---|
| Close to its memory limit, or killed for running out of memory | A bigger limit: the peak plus 30% |
| No memory limit | A limit of the peak plus 50%, so one leak can't take the whole server down |
| Limit far above real use | A tighter limit (it frees nothing today, but protects the other apps) |
| Memory that only goes up | A likely leak, with how long until it hits its limit; an incident once that's under 24 hours |
| CPU-heavy or idle for days | A CPU limit or its own server; stopping it if nobody uses it |
| Server short of memory, overcommitted, or mostly unused | The biggest users; a smaller or bigger server |
| Disk space | Low-risk cleanups from the last disk analysis |

Every item says exactly what to change in Coolify. For applications and databases, the suggested
memory limit can be applied from OpsMender when Actions are on (confirmed, audited, and effective
on the next restart). Services take limits from their compose file, so OpsMender shows the line to add.

## Fix pull requests

For an incident on an application deployed from GitHub, click **Draft a fix**. The AI reads the
repository at the deployed branch (read-only), along with the usual investigation tools, and
proposes the smallest change. OpsMender checks every edit before you see it: it must match the file
exactly, stay small, parse (Python, JSON, TOML), contain no literal secrets, and not touch CI
workflows. You review the diff on the incident page.

Only **Open draft pull request** writes to GitHub: one commit on a new `opsmender/…` branch and a
draft pull request into the deployed branch. If one of the changed files changed upstream since the
draft, it refuses and asks you to draft again. OpsMender never pushes to existing branches and never
merges; merging deploys through Coolify as usual.

Connect GitHub in **Settings → GitHub** with a fine-grained personal access token limited to the
repositories Coolify deploys, with **Contents** and **Pull requests** set to read and write.
GitHub Enterprise works with its API URL.

## Alert rules

By default alerts go to every enabled Coolify notification channel and the optional OpsMender
webhook. The **Alerts** card on a resource or server page changes that for it: send only to chosen
destinations, only warnings and critical (or only critical), also to its own webhook (the owning
team's Slack channel, say), or mute it. A server's rule covers the resources on it unless they have
their own. Settings → Notifications lists every rule.

## AI providers

| Provider | Notes |
|---|---|
| Anthropic Claude | Native Messages API via the official SDK. Default model `claude-opus-5`. |
| Google Gemini | OpenAI-compatible endpoint |
| DeepSeek, OpenAI, OpenRouter, Groq, Mistral | OpenAI-compatible |
| Ollama | Local and free. Defaults to `http://host.docker.internal:11434/v1`. |
| Anything else | Any OpenAI-compatible `/chat/completions` endpoint |

OpsMender sends the AI an incident's evidence: status, recent deployments, metrics, rule findings
and a redacted log excerpt. It does so only when you click **Diagnose with AI** or enable automatic
diagnosis.

If the evidence isn't enough, the AI can look further with up to 8 read-only lookups per diagnosis:

| Tool | What it returns |
|---|---|
| `get_logs` | Recent runtime logs of a resource (redacted) |
| `get_deployments`, `get_deployment_log` | Recent deployments and a deployment's build log (redacted) |
| `get_resource` | Build pack, ports, health check, limits, commands, image, domain, status |
| `get_env_var_names` | Environment variable **names** from Coolify. Values are never read. |
| `get_server_metrics`, `get_container_stats` | CPU, memory, disk, restarts and OOM kills, now and over recent hours |
| `get_disk_usage` | What is using a server's disk |
| `list_resources`, `get_history` | Other resources and their status; past incidents and events |

Each lookup and its result is listed under the diagnosis. None of them can change anything. Turn this
off in **Settings → AI provider → Let the AI look further**; models that can't call tools (some
Ollama models, for example) automatically get a single request instead.

## Configuration

Set these as environment variables. Everything else is configured in the UI.

| Variable | Default | Purpose |
|---|---|---|
| `OPSMENDER_DATA_DIR` | `./data` (`/data` in Docker) | SQLite database and generated secret key |
| `OPSMENDER_SECRET_KEY` | generated into the data dir | Signs sessions and encrypts stored secrets |
| `OPSMENDER_BASE_URL` | derived from the request | Public URL (with `http://` or `https://`), used for the webhook URL |
| `OPSMENDER_SECURE_COOKIES` | per request | `true` marks session cookies Secure even on plain-HTTP requests (otherwise only HTTPS requests get them) |
| `OPSMENDER_ADMIN_USER` / `OPSMENDER_ADMIN_PASSWORD` | – | Create the admin account on first start |
| `OPSMENDER_COOLIFY_URL` / `OPSMENDER_COOLIFY_TOKEN` | – | Prefill the Coolify connection |
| `PORT` | `8000` | HTTP port |

## Security

- Admin login uses scrypt password hashing and login rate limiting. Sessions are signed
  `SameSite=Strict` cookies, and every form carries a CSRF token.
- The Coolify token, AI keys, GitHub token, SSH private key and webhook URLs are encrypted at rest.
- A Coolify token with `read:sensitive` can read your servers' SSH keys and notification secrets.
  Treat it like root access. OpsMender uses Coolify's SSH keys only for the one-time probe install
  and never stores them.
- OpsMender mounts no Docker socket and no host filesystem.
- Secrets are redacted from log text before it is stored or sent to an AI provider, and from
  repository files before the AI reads them.
- Fix pull requests need a separate GitHub token and a click to open. The AI can't push, merge, or
  edit CI workflows.
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

v0.2 in progress, early. Tested against a simulated Coolify API, a simulated GitHub API and real
Linux servers over SSH, but **not yet against a wide range of live Coolify versions**. If something doesn't match your Coolify
release, please open an issue with your version.
