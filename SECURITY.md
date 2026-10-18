# Security policy

OpsMender holds sensitive access: a Coolify API token, AI provider keys, and an SSH key for the
server probe. We take security reports seriously.

## Reporting a vulnerability

**Please don't open a public issue.** Report privately through GitHub:
**Security → Report a vulnerability** on this repository
([private vulnerability reporting](https://github.com/abiola814/OpsMender/security/advisories/new)).

Include what you found, how to reproduce it, and the impact you expect. We aim to acknowledge
reports within 3 working days and to agree on a disclosure timeline with you.

## Supported versions

OpsMender is pre-1.0. Security fixes go into the latest release and `main`.

## Scope

In scope, for example:

- Authentication, session, or CSRF bypass in the web UI
- Ways to read stored secrets (Coolify token, AI keys, SSH key, webhook URLs)
- Escaping the probe's restrictions: running anything other than `snapshot` or
  `logs <container> <lines>`, or using the SSH key for shells or port forwarding
- Secrets that survive redaction and reach an AI provider or the UI
- Injection through data returned by Coolify or by a probed server

## Hardening notes for operators

- Serve OpsMender over HTTPS only (Coolify's proxy does this when you set an `https://` domain).
- Use a read-only Coolify API token unless you need actions.
- Keep `OPSMENDER_SECRET_KEY` (or the generated `secret.key` in the data volume) private. It
  encrypts stored secrets and signs sessions.
- To revoke probe access on a server:
  `userdel -r opsmender; rm -rf /usr/local/lib/opsmender /etc/sudoers.d/opsmender`
