# OpsMender plan

OpsMender is an open-source, self-hosted AI DevOps companion for [Coolify](https://coolify.io).
You deploy it *through* Coolify, open its web UI, and see every server, app, service, database,
deployment and incident in one place, with plain-language diagnosis from the AI provider you choose.

Reference product:  OpsMender's goal is the same core value,
free and on infrastructure you control, without slowing down the servers it watches.

## Principles

1. **Coolify does the deploying, OpsMender does the operating.** No second deployment engine.
2. **Lightweight on users' servers.** No Prometheus, Loki, Grafana, Redis, or always-on agent.
   Logs are fetched only when something goes wrong, with line and byte caps.
3. **Rules first, AI second.** Common failures are fingerprinted locally for free.
   The AI (bring your own key) handles the long tail and explains the evidence.
4. **Safe by default.** Read-only until the user turns actions on. Every action needs a click to
   confirm and is written to an audit log. Secrets are encrypted at rest and redacted before
   any log text is sent to an AI provider.

## Where it runs

```
 ┌───────────────────────────── Coolify server ─────────────────────────────┐
 │  Coolify ◄──REST API── OpsMender container (FastAPI + SQLite, one process) │
 │     │                       ▲            │                                │
 │     └──webhook (optional)───┘            │ SSH, forced-command probe       │
 └──────────────────────────────────────────┼────────────────────────────────┘
                                            ▼
                      every Coolify-managed server (including the Coolify host)
                      /usr/local/lib/opsmender/probe  → prints one JSON snapshot, exits
```

Coolify has no dashboard plugin system, so OpsMender ships its own UI on its own domain
(for example `ops.example.com`), deployed as a normal Coolify resource.

## Feature map 

| Capability | v0.1 | How |
|---|---|---|
| See all servers, apps, services, databases | ✅ | Coolify API sync every 60s (+ webhook wake-up) |
| Detect failed deployments | ✅ | Track running deployments, fetch final status and build log |
| Detect crashes / unhealthy / restarting resources | ✅ | Status transitions between syncs |
| CPU, RAM, disk per server | ✅ | SSH probe reads `/proc`, `df`; rates computed in OpsMender |
| CPU, RAM, restarts, OOM per container | ✅ | Probe reads cgroup v1/v2 files + `docker inspect` |
| Threshold alerts (CPU, RAM, disk, crash loops) | ✅ | Sustained-sample rules, auto-resolve when cleared |
| Disk-full forecast | ✅ | Linear trend over up to 7 days |
| Rule-based diagnosis of logs | ✅ | ~20 fingerprints (DB refused, OOM, port in use, missing module…) |
| AI root-cause analysis | ✅ | BYO provider: Claude, Gemini, DeepSeek, OpenAI, OpenRouter, Groq, Mistral, Ollama |
| Restart / start / stop / redeploy | ✅ | Opt-in, confirmed, audited |
| Outbound alerts | ✅ | Generic webhook (Slack- and Discord-compatible payload) |
| AI tool-calling investigations | ✅ | Read-only tools, capped at 8 calls, each shown on the incident |
| Right-sizing advice (memory limits, leaks, idle/busy apps, server size, disk) | ✅ v0.2 | Rules over 7 days of container rollups; limits applied through the Coolify API |
| Memory-leak prediction | ✅ v0.2 | Linear trend of the running container, incident when < 24h to its limit |
| Per-resource notification routing | ✅ v0.2 | Resource → server → default rules: destinations, min severity, own webhook, mute |
| GitHub fix pull requests | ✅ v0.2 | AI reads the repo, checked edits, reviewed diff, one commit on a new branch, draft PR |
| Optional push collector (servers behind NAT) | v0.3 | Tiny Go binary, same JSON as the probe |

## Components

| Module | Responsibility |
|---|---|
| `opsmender/adapters/coolify.py` | Tolerant async client for Coolify's REST API |
| `opsmender/monitor/` | Scheduler: resource sync, deployment tracking, metrics polling, maintenance |
| `opsmender/metrics/` | Probe script, installer generator, SSH runner, rate maths, storage, forecast |
| `opsmender/diagnosis/` | Log fingerprinting, rule engine, secret redaction |
| `opsmender/ai/` | Provider-neutral LLM layer (OpenAI-compatible + native Claude), tool-calling loop, prompts |
| `opsmender/investigation.py` | Read-only tools the AI can call while investigating an incident |
| `opsmender/advice.py` | Right-sizing advice and leak prediction from container history |
| `opsmender/fixes.py`, `opsmender/github.py` | Fix drafting (repo tools, edit checks) and the GitHub client that opens the PR |
| `opsmender/incidents.py` | Open / dedupe / resolve incidents, gather evidence, notify |
| `opsmender/notify.py` | Coolify notification channels, OpsMender webhook, per-resource routing |
| `opsmender/web/` | FastAPI routes, auth, CSRF, Jinja templates, static assets |
| `opsmender/db.py` | SQLite (WAL) schema and helpers |

## Resource budget

- OpsMender container: one Python process, target under ~100 MB RAM, idle CPU near zero.
  Measured (macOS, Python 3.12): ~66 MB idle, ~69 MB after serving pages. The SSH library loads
  only when metrics are used; the Anthropic SDK (~35 MB) only once Claude is the AI provider.
- Each probed server: one short SSH session per minute running a POSIX shell script.
  No daemon, nothing listening, nothing written to disk.
- Coolify API: about 5 requests per sync, plus 1 per finished deployment.
- SQLite: per-minute samples kept 24h; server rollups (15 min) kept 30 days, container rollups
  (hourly) kept 14 days.
- Right-sizing advice is computed on demand (~65 ms for 50 containers), cached a minute for the
  overview.

These are targets. Benchmarks on a 1 vCPU / 1 GB VPS are part of the v0.1 release checklist.

## Security model

- Admin login (scrypt-hashed password), signed session cookie, CSRF tokens on every form.
- Coolify token, AI keys and the SSH private key are encrypted with a key from
  `OPSMENDER_SECRET_KEY` (or a generated key file in the data volume).
- SSH key is OpsMender's own, not Coolify's root key. `authorized_keys` pins it to the probe
  (`restrict` + forced command). The probe accepts only `snapshot` or
  `logs <container> <lines>` with strict validation. Host keys are trusted on first use, then pinned.
- No Docker socket mount, no host filesystem mount.
- Log text is redacted (passwords, tokens, connection strings, JWTs, private keys) before
  it reaches an AI provider. So are repository files read while drafting a fix.
- Fix pull requests use a separate, fine-grained GitHub token. Drafting only reads. Opening needs a
  click and writes one commit on a new branch plus a draft PR; existing branches, merges and CI
  workflows are off limits, and proposed edits are checked for exact matches, syntax and literal
  secrets.

## Milestones

1. **v0.1 (done):** everything marked ✅ above without a version, Docker image, Coolify compose file, tests.
2. **v0.1.x:** verify against several live Coolify versions, benchmark on a 1 vCPU / 1 GB VPS, fix API drift.
3. **v0.2 (built, unreleased):** GitHub fix PRs, per-resource notification routing, right-sizing
   advice and leak prediction, lower memory use.
4. **v0.3:** optional push collector, Coolify one-click service template submission.
