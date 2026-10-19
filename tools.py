"""Nexus tools. Nothing here ever runs a raw shell string; every command is an argv list."""
import asyncio
import json
import os
import re
import shlex
import urllib.request
from pathlib import Path

BOTS = json.loads(Path(os.getenv("BOTS_FILE", "bots.json")).read_text())
SSH_TARGET = os.getenv("SSH_TARGET", "")          # "user@host" to run remotely, empty = local
POWER_ON_URL = os.getenv("POWER_ON_WEBHOOK", "")  # provider API/webhook for server_power("on")

ALLOWLIST = ("systemctl status", "systemctl restart", "df -h", "free -m", "uptime", "ps aux")
BAD_CHARS = re.compile(r"[;&|`$<>\n\\(){}]")
SECRET_KEY = re.compile(r"token|secret|password|passwd|api[_-]?key|webhook", re.I)
DISCORD_TOKEN = re.compile(r"[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}")
KV_SECRET = re.compile(r"(?i)((?:token|secret|password|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',]+")
PKG_OK = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.\-/@\[\]=<>~!,]*$")

SEND = None    # async (channel_id:int, text:str) -> str   (set by bot.py)
NOTIFY = None  # async (text:str) -> str                   (set by bot.py)


def redact(text: str) -> str:
    return KV_SECRET.sub(r"\1[redacted]", DISCORD_TOKEN.sub("[redacted]", text))


async def run(args, stdin=None, timeout=20, ok=(0,)):
    if SSH_TARGET:
        args = ["ssh", "-o", "BatchMode=yes", SSH_TARGET, shlex.join(args)]
    p = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise RuntimeError("command timed out")
    text = out.decode(errors="replace").strip()
    if p.returncode not in ok:
        raise RuntimeError(text[-500:] or f"exit code {p.returncode}")
    return text


def _bot(bot_id):
    for b in BOTS:
        if str(b["id"]) == str(bot_id) or b["name"].lower() == str(bot_id).lower():
            return b
    raise ValueError(f"unknown bot '{bot_id}'. Try list_bots.")


def _path(b, key):
    return str(Path(b["dir"]) / b[key])


# ───────── bot control ─────────
async def start_bot(bot_id):
    b = _bot(bot_id)
    await run(["sudo", "systemctl", "start", b["service"]])
    return f"{b['name']} started"


async def stop_bot(bot_id):
    b = _bot(bot_id)
    await run(["sudo", "systemctl", "stop", b["service"]])
    return f"{b['name']} stopped"


async def restart_bot(bot_id):
    b = _bot(bot_id)
    await run(["sudo", "systemctl", "restart", b["service"]])
    return f"{b['name']} restarted"


async def kill_bot(bot_id):
    b = _bot(bot_id)
    await run(["sudo", "systemctl", "kill", "-s", "SIGKILL", b["service"]])
    return f"{b['name']} force-killed"


async def get_status(bot_id):
    b = _bot(bot_id)
    out = await run(["systemctl", "show", b["service"], "-p", "ActiveState", "-p", "ActiveEnterTimestamp", "-p", "NRestarts"])
    return f"{b['name']}: " + out.replace("\n", " | ")


async def list_bots():
    return "\n".join(f"#{b['id']} {b['name']} ({b.get('runtime', 'python')})" for b in BOTS)


# ───────── server control ─────────
async def server_power(action):
    if action == "off":
        await run(["sudo", "shutdown", "-h", "+1"])
        return "server shutting down in ~1 min"
    if action == "reboot":
        await run(["sudo", "shutdown", "-r", "+1"])
        return "server rebooting in ~1 min"
    if action == "on":
        if not POWER_ON_URL:
            raise RuntimeError("power-on isn't configured (set POWER_ON_WEBHOOK)")
        req = urllib.request.Request(POWER_ON_URL, method="POST")
        await asyncio.to_thread(urllib.request.urlopen, req, None, 15)
        return "power-on signal sent"
    raise ValueError("action must be on/off/reboot")


async def get_server_stats():
    up = await run(["uptime"])
    mem = await run(["free", "-m"])
    disk = await run(["df", "-h", "/"])
    return f"{up}\n{mem}\n{disk}"


async def ssh_exec(command):
    c = " ".join(command.split())
    if BAD_CHARS.search(c) or not any(c == a or c.startswith(a + " ") for a in ALLOWLIST):
        raise ValueError("that command isn't on the allowlist")
    argv = shlex.split(c)
    if argv[0] == "systemctl":
        services = {b["service"] for b in BOTS}
        if len(argv) < 3 or argv[2] not in services:
            raise ValueError("systemctl only works on registered bot services")
    return (await run(argv))[-3000:]


# ───────── logs & diagnostics ─────────
async def get_logs(bot_id, lines=30):
    b = _bot(bot_id)
    n = max(1, min(int(lines), 200))
    return await run(["tail", "-n", str(n), _path(b, "log_file")])


async def search_logs(bot_id, keyword):
    b = _bot(bot_id)
    out = await run(["grep", "-i", "-n", "-m", "40", "-e", keyword, _path(b, "log_file")], ok=(0, 1))
    return out or "no matches"


HINTS = [
    (r"No module named '([\w.]+)'", "Missing Python module `{0}` (pip name may differ, e.g. discord -> discord.py)."),
    (r"Cannot find module '([^']+)'", "Missing npm module `{0}`."),
    (r"EADDRINUSE|[Aa]ddress already in use", "Port already in use by another process."),
    (r"KeyError: '([A-Z_]+)'", "Missing env var / config key `{0}`."),
    (r"LoginFailure|Improper token|invalid token", "Discord rejected the token. It needs replacing (I can't show or read it)."),
    (r"PrivilegedIntentsRequired", "Enable the privileged intents in the Discord developer portal."),
]


async def diagnose_error(bot_id):
    b = _bot(bot_id)
    tail = await run(["tail", "-n", "300", _path(b, "log_file")])
    lines = tail.splitlines()
    idx = max((i for i, l in enumerate(lines) if re.search(r"Traceback|Error|Exception", l)), default=None)
    if idx is None:
        return "no recent errors in the last 300 log lines"
    block = "\n".join(lines[max(0, idx - 6): idx + 4])
    hints = []
    for pat, msg in HINTS:
        m = re.search(pat, block)
        if m:
            hints.append(msg.format(*m.groups()))
    return block + ("\n\nKnown pattern: " + " ".join(hints) if hints else "")


# ───────── config & fixes ─────────
def _mask(o):
    if isinstance(o, dict):
        return {k: "[redacted]" if SECRET_KEY.search(k) else _mask(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(i) for i in o]
    return o


async def read_config(bot_id):
    b = _bot(bot_id)
    raw = await run(["cat", _path(b, "config")])
    try:
        return json.dumps(_mask(json.loads(raw)), indent=2)
    except ValueError:
        return raw  # non-JSON: redact() in call() still scrubs secrets


async def edit_config(bot_id, key, value):
    b = _bot(bot_id)
    path = _path(b, "config")
    data = json.loads(await run(["cat", path]))
    try:
        val = json.loads(value)
    except ValueError:
        val = value
    *parents, last = key.split(".")
    d = data
    for p in parents:
        d = d.setdefault(p, {})
    d[last] = val
    await run(["cp", path, path + ".bak"])
    await run(["tee", path], stdin=json.dumps(data, indent=2))
    return f"set `{key}` (backup: {Path(path).name}.bak). Restart to apply."


async def install_package(bot_id, package):
    b = _bot(bot_id)
    if not PKG_OK.match(package):
        raise ValueError("invalid package name")
    if b.get("runtime", "python") == "node":
        argv = ["npm", "install", "--prefix", b["dir"], package]
    else:
        argv = [str(Path(b["dir"]) / b.get("venv", "venv") / "bin" / "pip"), "install", package]
    await run(argv, timeout=180)
    return f"installed {package}"


# ───────── communication ─────────
async def send_message(channel_id, text):
    return await SEND(int(channel_id), text)


async def notify_admin(text):
    return await NOTIFY(text)


# ───────── schemas ─────────
B = {"bot_id": "string"}
SPEC = {
    "start_bot": ("Start the hosted bot", B),
    "stop_bot": ("Stop the hosted bot (graceful)", B),
    "restart_bot": ("Restart the hosted bot", B),
    "kill_bot": ("Force kill. Only if stop fails", B),
    "get_status": ("Online/offline state, uptime, restart count", B),
    "list_bots": ("List all registered bots", {}),
    "server_power": ("Server power action", {"action": ["on", "off", "reboot"]}),
    "get_server_stats": ("CPU/RAM/disk/uptime", {}),
    "ssh_exec": ("Run an allowlisted command: " + ", ".join(ALLOWLIST), {"command": "string"}),
    "get_logs": ("Last N log lines (max 200)", {**B, "lines": "integer"}),
    "search_logs": ("Search logs for a keyword", {**B, "keyword": "string"}),
    "diagnose_error": ("Analyze the latest error in the logs", B),
    "read_config": ("Read bot config (secrets masked)", B),
    "edit_config": ("Set a config value (dotted key)", {**B, "key": "string", "value": "string"}),
    "install_package": ("pip/npm install into the bot's environment", {**B, "package": "string"}),
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
    """Enforced in code, not just in the prompt."""
    if name in {"stop_bot", "kill_bot", "edit_config", "install_package"}:
        return True
    if name == "server_power":
        return args.get("action") in ("off", "reboot")
    if name == "ssh_exec":
        return " ".join(str(args.get("command", "")).split()).startswith("systemctl restart")
    return False


IMPL = {n: globals()[n] for n in SPEC}


async def call(name, args):
    if name not in IMPL:
        raise ValueError(f"unknown tool {name}")
    return redact(str(await IMPL[name](**args)))
