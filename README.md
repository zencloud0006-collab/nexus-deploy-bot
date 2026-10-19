# Nexus

Discord ops bot: chat + tool-calling to manage hosted bots and your server.

## Run
    pip install -r requirements.txt
    cp .env.example .env   # fill in
    # edit bots.json (one entry per systemd-hosted bot)
    python bot.py

Talk to it via DM or @mention (only users in ADMIN_IDS get answers).

## Needed on the server
Run Nexus as a user with passwordless sudo for just these (visudo):

    nexus ALL=(root) NOPASSWD: /usr/bin/systemctl start *.service, /usr/bin/systemctl stop *.service, \
      /usr/bin/systemctl restart *.service, /usr/bin/systemctl kill *.service, \
      /usr/sbin/shutdown

Also enable "Message Content Intent" for the bot in the Discord dev portal.

## Safety, enforced in code (not just the prompt)
- Destructive tools park as "pending" until the same admin says yes
- ssh_exec: allowlist + metachar block + systemctl limited to registered services
- Secrets masked in config reads and scrubbed from every tool output
- 2 failures on the same tool stops the turn; max 8 steps per turn
- Mentions disabled in sent messages (no @everyone)

Not implemented: ping in get_status (add a heartbeat file/endpoint in your bots if you want it).
