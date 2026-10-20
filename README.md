# Nexus

Discord ops bot (OpenRouter LLM + tools) for hosted bots and your server.

## Deploy
1. Upload all files to your GitHub repo (replace old ones), Render auto-redeploys.
2. Render env vars: `DISCORD_TOKEN`, `OPENROUTER_API_KEY`, `ADMIN_IDS`, `GUILD_IDS` (+ optional ones in `.env.example`).
3. Invite the bot with scopes `bot` + `applications.commands`.

## Test on Render (mock backend)
No systemd on Render, so `BACKEND=auto` runs **simulated bots** (musicbot, modbot). Try:
- `/panel`, `/status`, `/logs`, `/stats`
- `/autofix auto`, then `/simulate musicbot missing_module` -> watchdog installs the package and restarts
- `/autofix suggest`, `/simulate modbot crash` -> alert with an "Apply fix" button
- `/simulate musicbot bad_deploy` then `/deploy musicbot` -> health check fails, auto-rollback
- `/role @user operator`, `/audit`, `/incidents`
Render's disk is ephemeral: the SQLite DB (roles, audit, incidents) resets on redeploy. Fine for testing.

## Move to your VPS
    pip install -r requirements.txt && cp .env.example .env
    edit bots.json (service name, dir, log_file, config; each bot dir is a git repo for /deploy)
    python bot.py      # run under systemd; BACKEND=auto picks real systemd control
Passwordless sudo for the Nexus user (visudo):

    nexus ALL=(root) NOPASSWD: /usr/bin/systemctl start *.service, /usr/bin/systemctl stop *.service, \
      /usr/bin/systemctl restart *.service, /usr/bin/systemctl kill *.service, /usr/sbin/shutdown

## Roles
viewer: status/logs/stats/incidents. operator: start/restart/stop/deploy/edit/install/panel. owner: kill, power, ssh_exec, autofix, roles, audit.
`ADMIN_IDS` are permanent owners.

## Safety (enforced in code)
Role checks + audit log on every action; destructive actions need a confirm click; log text is filtered for prompt injection;
auto-fix only installs packages the bot already lists in its requirements/package.json; max 2 auto-fixes per bot per 30 min;
secrets redacted everywhere; per-user daily token/USD caps; model fallback chain.
Not built: external VPS agent (by design), web dashboard, multi-server.
