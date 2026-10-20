"""Nexus tools. Every action is an argv list or backend call. Roles, audit and redaction are enforced here, not in the prompt."""
import asyncio
import json
import os
import re
import shlex
import shutil
from pathlib import Path

import backend as bk
import db
from backend import PIPMAP, fmt_dur, stats as stats_dict  # noqa: F401

BOTS = json.loads(Path(os.getenv("BOTS_FILE", "bots.json")).read_text())
_mode = os.getenv("BACKEND", "auto").lower()
if _mode == "local" or (_mode == "auto" and shutil.which("systemctl") and os.path.isdir("/run/systemd/system")):
    B = bk.Local()
else:
    B = bk.Mock(BOTS)

DEPLOY_WAIT = float(os.getenv("DEPLOY_WAIT", "8"))
ALLOWLIST = ("systemctl status", "systemctl restart", "df -h", "free -m", "uptime", "ps aux")
BAD_CHARS = re.compile(r"[;&|`$<>\n\\(){}]")
SECRET_KEY = re.compile(r"token|secret|password|passwd|api[_-]?key|webhook", re.I)
DISCORD_TOKEN = re.compile(r"[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}")
KV_SECRET = re.compile(r"(?i)((?:token|secret|password|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',]+")
PKG_OK = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.\-/@\[\]=<>~!,]*$")
INJECT = re.compile(r"(?i)ignore (all |any )?(previous|prior|above)|system prompt|you are now|disregard|assistant:|</?(system|tool)|call the tool|run the tool|new instructions")

SEND = None    # async (channel_id:int, text:str) -> str
NOTIFY = None  # async (text:str) -> str

RANK = {"viewer": 1, "operator": 2, "owner": 3}
NEEDS = {
    "list_bots": "viewer", "get_status": "viewer", "get_server_stats": "viewer", "get_logs": "viewer",
    "search_logs": "viewer", "diagnose_error": "viewer", "get_incidents": "viewer",
    "read_config": "operator", "start_bot": "operator", "restart_bot": "operator", "stop_bot": "operator",
    "install_package": "operator", "edit_config": "operator", "deploy_bot": "operator",
    "send_message": "operator", "notify_admin": "operator",
    "kill_bot": "owner", "server_power": "owner", "ssh_exec": "owner", "set_autofix": "owner",
}


def redact(t):
    return KV_SECRET.sub(r"\1[redacted]", DISCORD_TOKEN.sub("[redacted]", t))


def clean_log(lines):
    """Logs are untrusted: drop instruction-like lines, clip long ones, fence the block."""
    out = ["[filtered]" if INJECT.search(l) else l[:300] for l in lines]
    return "<untrusted_logs>\n" + "\n".join(out) + "\n</untrusted_logs>"


def _bot(bot_id):
    for b in BOTS:
        if str(b["id"]) == str(bot_id) or b["name"].lower() == str(bot_id).lower():
            return b
    raise ValueError(f"unknown bot '{bot_id}'. Try list_bots.")


# ───────── bot control ─────────
async def start_bot(bot_id):
    b = _bot(bot_id)
    await B.control(b, "start")
    await db.kv_set(f"desired:{b['id']}", "running")
    return f"{b['name']} started"


async def stop_bot(bot_id):
    b = _bot(bot_id)
    await B.control(b, "stop")
    await db.kv_set(f"desired:{b['id']}", "stopped")
    return f"{b['name']} stopped"


async def restart_bot(bot_id):
    b = _bot(bot_id)
    await B.control(b, "restart")
    await db.kv_set(f"desired:{b['id']}", "running")
    return f"{b['name']} restarted"


async def kill_bot(bot_id):
    b = _bot(bot_id)
    await B.control(b, "kill")
    await db.kv_set(f"desired:{b['id']}", "stopped")
    return f"{b['name']} force-killed"


async def get_status(bot_id):
    b = _bot(bot_id)
    i = await B.info(b)
    ping = await B.health(b)
    parts = [f"{b['name']}: {i['state']}" + (f"/{i['sub']}" if i["sub"] else "")]
    if i["uptime"] is not None:
        parts.append("up " + fmt_dur(i["uptime"]))
    parts.append(f"restarts {i['restarts']}")
    if ping is not None:
        parts.append("ping down" if ping < 0 else f"ping {ping}ms")
    return " | ".join(parts)


async def list_bots():
    return "\n".join(f"#{b['id']} {b['name']} ({b.get('runtime', 'python')})" for b in BOTS)


async def deploy_bot(bot_id):
    b = _bot(bot_id)
    before = (await B.info(b))["restarts"]
    res = await B.deploy(b)
    if res["prev"] == res["new"]:
        return f"{b['name']} already up to date ({res['new'][:7]})"
    await B.control(b, "restart")
    await asyncio.sleep(DEPLOY_WAIT)
    i = await B.info(b)
    if i["state"] != "active" or i["restarts"] > before:
        await B.rollback(b, res["prev"])
        await B.control(b, "restart")
        await asyncio.sleep(DEPLOY_WAIT / 2)
        i = await B.info(b)
        return f"deploy {res['new'][:7]} FAILED the health check. Rolled back to {res['prev'][:7]}; bot is {i['state']}"
    return f"deployed {res['prev'][:7]} -> {res['new'][:7]}; {b['name']} is {i['state']}"


# ───────── server ─────────
async def server_power(action):
    if action not in ("on", "off", "reboot"):
        raise ValueError("action must be on/off/reboot")
    return await B.power(action)


async def get_server_stats():
    s = stats_dict()
    up = f" | up {fmt_dur(s['uptime'])}" if s["uptime"] else ""
    return (f"CPU {s['cpu']:.0f}% (load {s['load']:.2f}) | RAM {s['mem_used']}/{s['mem_total']} MB ({s['mem']:.0f}%) | "
            f"Disk {s['disk_used']:.1f}/{s['disk_total']:.1f} GB ({s['disk']:.0f}%){up} | backend: {B.name}")


async def ssh_exec(command):
    c = " ".join(command.split())
    if BAD_CHARS.search(c) or not any(c == a or c.startswith(a + " ") for a in ALLOWLIST):
        raise ValueError("that command isn't on the allowlist")
    argv = shlex.split(c)
    if argv[0] == "systemctl":
        svc = {b["service"]: b for b in BOTS}
        if len(argv) < 3 or argv[2] not in svc:
            raise ValueError("systemctl only works on registered bot services")
        if argv[1] == "restart":
            await B.control(svc[argv[2]], "restart")
            return f"{svc[argv[2]]['name']} restarted"
        return await get_status(svc[argv[2]]["id"])
    return (await B.exec(argv))[-3000:]


# ───────── logs & diagnosis ─────────
async def get_logs(bot_id, lines=30):
    return clean_log(await B.tail(_bot(bot_id), max(1, min(int(lines), 200))))


async def search_logs(bot_id, keyword):
    hits = [l for l in await B.tail(_bot(bot_id), 2000) if keyword.lower() in l.lower()][-40:]
    return clean_log(hits) if hits else "no matches"


async def diagnose(b):
    """Structured diagnosis used by the tool, the watchdog and auto-fix."""
    lines = await B.tail(b, 300)
    info = await B.info(b)
    down = info["state"] not in ("active", "activating")
    restart = {"tool": "restart_bot", "args": {"bot_id": b["id"]}, "label": f"restart {b['name']}", "auto_safe": True}
    idx = max((i for i, l in enumerate(lines) if re.search(r"Traceback|Error|Exception|FATAL", l)), default=None)
    if idx is None:
        return {"summary": "No errors in recent logs." + (" Bot is down." if down else ""), "excerpt": "",
                "fix": restart if down else None}
    block = [("[filtered]" if INJECT.search(l) else l[:200]) for l in lines[max(0, idx - 6): idx + 4]]
    raw = "\n".join(lines[max(0, idx - 6): idx + 4])
    excerpt = "\n".join(block)
    m = re.search(r"No module named '([\w.]+)'", raw)
    if m:
        mod = m.group(1).split(".")[0]
        pkg = PIPMAP.get(mod, mod)
        return {"summary": f"Missing Python module `{mod}`.", "excerpt": excerpt,
                "fix": {"tool": "install_package", "args": {"bot_id": b["id"], "package": pkg},
                        "label": f"pip install {pkg}, then restart", "auto_safe": await B.listed(b, pkg)}}
    m = re.search(r"Cannot find module '([^']+)'", raw)
    if m and not m.group(1).startswith((".", "/")):
        pkg = m.group(1)
        return {"summary": f"Missing npm module `{pkg}`.", "excerpt": excerpt,
                "fix": {"tool": "install_package", "args": {"bot_id": b["id"], "package": pkg},
                        "label": f"npm install {pkg}, then restart", "auto_safe": await B.listed(b, pkg)}}
    if re.search(r"EADDRINUSE|[Aa]ddress already in use", raw):
        return {"summary": "Port already in use by another process.", "excerpt": excerpt, "fix": restart}
    if re.search(r"LoginFailure|Improper token|invalid token", raw):
        return {"summary": "Discord rejected the bot token. It needs replacing (manual).", "excerpt": excerpt, "fix": None}
    if "PrivilegedIntentsRequired" in raw:
        return {"summary": "Privileged intents are disabled in the Discord developer portal (manual).", "excerpt": excerpt, "fix": None}
    last = next((l for l in reversed(raw.splitlines()) if re.search(r"Error|Exception", l)), "unknown error")
    return {"summary": f"Crash: {last[:200]}", "excerpt": excerpt, "fix": restart if down else None}


async def diagnose_error(bot_id):
    d = await diagnose(_bot(bot_id))
    out = d["summary"]
    if d["excerpt"]:
        out += "\n" + clean_log(d["excerpt"].splitlines())
    return out + (f"\nSuggested fix: {d['fix']['label']}" if d["fix"] else "\nNo automatic fix known.")


async def get_incidents():
    rows = await db.recent_incidents(8)
    if not rows:
        return "no incidents recorded"
    import time
    return "\n".join(f"#{r['id']} {time.strftime('%m-%d %H:%M', time.gmtime(r['ts']))} {r['bot']} {r['kind']} [{r['status']}] {r['summary'][:80]}" for r in rows)


# ───────── config & fixes ─────────
def _mask(o):
    if isinstance(o, dict):
        return {k: "[redacted]" if SECRET_KEY.search(k) else _mask(v) for k, v in o.items()}
    return [_mask(i) for i in o] if isinstance(o, list) else o


async def read_config(bot_id):
    raw = await B.read(_bot(bot_id))
    try:
        return json.dumps(_mask(json.loads(raw)), indent=2)
    except ValueError:
        return raw


async def edit_config(bot_id, key, value):
    b = _bot(bot_id)
    data = json.loads(await B.read(b))
    try:
        val = json.loads(value)
    except ValueError:
        val = value
    *parents, last = key.split(".")
    d = data
    for p in parents:
        d = d.setdefault(p, {})
    d[last] = val
    await B.write(b, json.dumps(data, indent=2))
    return f"set `{key}` (backup saved). Restart to apply."


async def install_package(bot_id, package):
    b = _bot(bot_id)
    if not PKG_OK.match(package):
        raise ValueError("invalid package name")
    await B.install(b, package)
    return f"installed {package}"


async def set_autofix(mode):
    if mode not in ("off", "suggest", "auto"):
        raise ValueError("mode must be off/suggest/auto")
    await db.kv_set("autofix", mode)
    return f"auto-fix mode: {mode}"


# ───────── communication ─────────
async def send_message(channel_id, text):
    return await SEND(int(channel_id), text)


async def notify_admin(text):
    return await NOTIFY(text)


# ───────── schemas ─────────
BID = {"bot_id": "string"}
SPEC = {
    "start_bot": ("Start the hosted bot", BID),
    "stop_bot": ("Stop the hosted bot (graceful)", BID),
    "restart_bot": ("Restart the hosted bot", BID),
    "kill_bot": ("Force kill. Only if stop fails", BID),
    "get_status": ("Online/offline, uptime, restarts, ping", BID),
    "list_bots": ("List all registered bots", {}),
    "deploy_bot": ("Pull latest code, restart, health-check, auto-rollback on failure", BID),
    "server_power": ("Server power action", {"action": ["on", "off", "reboot"]}),
    "get_server_stats": ("CPU/RAM/disk/uptime", {}),
    "ssh_exec": ("Run an allowlisted host command: " + ", ".join(ALLOWLIST), {"command": "string"}),
    "get_logs": ("Last N log lines (max 200)", {**BID, "lines": "integer"}),
    "search_logs": ("Search logs for a keyword", {**BID, "keyword": "string"}),
    "diagnose_error": ("Analyze the latest error in the logs", BID),
    "get_incidents": ("Recent incidents detected by the watchdog", {}),
    "read_config": ("Read bot config (secrets masked)", BID),
    "edit_config": ("Set a config value (dotted key)", {**BID, "key": "string", "value": "string"}),
    "install_package": ("pip/npm install into the bot's environment", {**BID, "package": "string"}),
    "set_autofix": ("Set watchdog auto-fix mode", {"mode": ["off", "suggest", "auto"]}),
    "send_message": ("Send a message to a Discord channel", {"channel_id": "string", "text": "string"}),
    "notify_admin": ("Alert the admins", {"text": "string"}),
}


def tool_schemas():
    out = []
    for name, (desc, params) in SPEC.items():
        props = {k: ({"type": "string", "enum": v} if isinstance(v, list) else {"type": v}) for k, v in params.items()}
        out.append({"name": name, "description": desc,
                    "input_schema": {"type": "object", "properties": props, "required": list(params)}})
    return out


def needs_confirm(name, args):
    if name in {"stop_bot", "kill_bot", "edit_config", "install_package", "deploy_bot"}:
        return True
    if name == "server_power":
        return args.get("action") in ("off", "reboot")
    if name == "ssh_exec":
        return " ".join(str(args.get("command", "")).split()).startswith("systemctl restart")
    return False


IMPL = {n: globals()[n] for n in SPEC}


async def allowed(name, user):
    if name not in NEEDS:
        return False
    if user == 0:
        return True
    role = await db.get_role(user)
    return bool(role) and RANK[role] >= RANK[NEEDS[name]]


async def call(name, args, user=0):
    """user=0 is the system (watchdog). Everything else is checked against the roles table."""
    if name not in IMPL:
        raise ValueError(f"unknown tool {name}")
    if not await allowed(name, user):
        raise PermissionError(f"`{name}` needs the {NEEDS.get(name, 'owner')} role")
    logged = RANK[NEEDS[name]] >= 2
    shown = dict(args)
    if name == "edit_config" and SECRET_KEY.search(str(shown.get("key", ""))):
        shown["value"] = "[hidden]"
    try:
        out = redact(str(await IMPL[name](**args)))
    except Exception as e:
        if logged:
            await db.audit(user, name, shown, False, redact(str(e)))
        raise
    if logged:
        await db.audit(user, name, shown, True, out)
    return out
