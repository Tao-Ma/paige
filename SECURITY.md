# Security policy

Thanks for taking the time to look at paige's security surface.

## Reporting a vulnerability

Please **do not** open a public issue for a vulnerability.

Email the maintainer privately at the address linked from the
project's GitHub profile, or open a private security advisory on
the repository (GitHub → Security → Report a vulnerability).

What to include:
- A description of the issue and the steps to reproduce.
- The paige version (`./scripts/prod.sh status` shows the wheel).
- Whether the issue is exploitable today, or only under specific
  configuration.

What to expect:
- Acknowledgement within a few business days.
- A fix or mitigation timeline once the issue is confirmed.
- Credit in the release notes if you'd like (let us know in your
  report).

## Scope

paige's threat surface is small but non-trivial:

- **IM credentials** — `PAIGE_FEISHU_APP_ID` /
  `PAIGE_FEISHU_APP_SECRET` live in `~/.paige/.env` and grant
  outbound message + card access. The repo's `prod.sh` strips
  outbound-proxy env vars before launch.
- **tmux pane control** — paige sends keystrokes into the bound
  pane. A vulnerability that lets an unauthorised user trigger
  `send_keys` is effectively remote code execution on the host
  (whatever `claude` will run).
- **Access control** — `PAIGE_ALLOWED_USERS` / `PAIGE_ADMIN_USERS`
  gate who can interact with the bot. Default is **open** to anyone
  the bot can hear from; setting a real allow-list is strongly
  recommended on any shared deploy.

## Out of scope

- Findings against the IM backend itself (Lark / Feishu) — please
  report those to the backend vendor.
- Findings against `claude` (the agent paige bridges to) — please
  report those to Anthropic.
- Issues that require root or local-shell access on the host
  paige runs on (already game-over by other means).

## Supported versions

paige is pre-1.0 and ships from `main`. Security fixes target the
latest released wheel; no long-term support branches yet.
