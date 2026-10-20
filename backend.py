"""Backends. Local = systemd on this machine (your VPS). Mock = simulated bots for testing on hosts without systemd (Render)."""
import asyncio
import json
import os
import re
import shutil
import time
import urllib.request
from pathlib import Path

PIPMAP = {"discord": "discord.py", "yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python",
          "dotenv": "python-dotenv", "bs4": "beautifulsoup4", "sklearn": "scikit-learn"}


async def run(argv, stdin=None, timeout=30, ok=(0,), cwd=None):
    p = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd)
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise RuntimeError("command timed out")
    text = out.decode(errors="replace").strip()
    if p.returncode not in ok:
        raise RuntimeError(text[-500:] or f"exit code {p.returncode}")
    return text


def sudo(argv):
    return argv if (hasattr(os, "geteuid") and os.geteuid() == 0) else ["sudo", "-n", *argv]


def fmt_dur(s):
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"


def stats():
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    mem = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", total)
    d = shutil.disk_usage("/")
    try:
        up = float(open("/proc/uptime").read().split()[0])
    except OSError:
        up = None
    return {"cpu": min(100.0, load / (os.cpu_count() or 1) * 100), "load": load,
            "mem_used": total - avail, "mem_total": total, "mem": ((total - avail) / total * 100) if total else 0.0,
            "disk_used": d.used / 2**30, "disk_total": d.total / 2**30, "disk": d.used / d.total * 100, "uptime": up}


def bot_path(b, key):
    base = Path(b["dir"]).resolve()
    p = (base / b[key]).resolve()
    if p != base and base not in p.parents:
        raise ValueError("path escapes the bot directory")
    return p


def tail_lines(path, n):
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 262144))
        data = f.read()
    return data.decode(errors="replace").splitlines()[-n:]


def _norm(s):
    return re.split(r"[<>=!~\[; ]", s.strip().lower())[0].replace("_", "-")


class Local:
    name = "local"

    async def control(self, b, action):
        argv = ["systemctl", "kill", "-s", "SIGKILL", b["service"]] if action == "kill" else ["systemctl", action, b["service"]]
        await run(sudo(argv))

    async def info(self, b):
        out = await run(["systemctl", "show", b["service"], "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "NRestarts"])
        kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        pid, up = int(kv.get("MainPID", "0") or 0), None
        if pid:
            try:
                up = int((await run(["ps", "-o", "etimes=", "-p", str(pid)])).strip())
            except Exception:
                pass
        return {"state": kv.get("ActiveState", "unknown"), "sub": kv.get("SubState", ""),
                "uptime": up, "restarts": int(kv.get("NRestarts", "0") or 0)}

    async def health(self, b):
        url = b.get("health_url")
        if not url:
            return None
        t = time.perf_counter()
        try:
            await asyncio.to_thread(urllib.request.urlopen, url, None, 5)
            return int((time.perf_counter() - t) * 1000)
        except Exception:
            return -1

    async def tail(self, b, n):
        return await asyncio.to_thread(tail_lines, bot_path(b, "log_file"), n)

    async def read(self, b):
        return await asyncio.to_thread(bot_path(b, "config").read_text)

    async def write(self, b, text):
        p = bot_path(b, "config")
        await asyncio.to_thread(shutil.copy, p, str(p) + ".bak")
        await asyncio.to_thread(p.write_text, text)

    async def install(self, b, pkg):
        if b.get("runtime", "python") == "node":
            argv = ["npm", "install", "--prefix", b["dir"], pkg]
        else:
            argv = [str(Path(b["dir"]) / b.get("venv", "venv") / "bin" / "pip"), "install", pkg]
        return await run(argv, timeout=180)

    async def listed(self, b, pkg):
        """Is this package declared by the bot itself? Gate for unattended installs."""
        try:
            d = Path(b["dir"])
            if b.get("runtime", "python") == "node":
                j = json.loads((d / "package.json").read_text())
                names = set(j.get("dependencies", {})) | set(j.get("devDependencies", {}))
            else:
                names = {l for l in (d / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")}
            return _norm(pkg) in {_norm(n) for n in names}
        except Exception:
            return False

    async def deploy(self, b):
        d = b["dir"]
        prev = (await run(["git", "rev-parse", "HEAD"], cwd=d)).strip()
        await run(["git", "pull", "--ff-only"], cwd=d, timeout=120)
        new = (await run(["git", "rev-parse", "HEAD"], cwd=d)).strip()
        if prev != new:
            changed = await run(["git", "diff", "--name-only", prev, new], cwd=d)
            if "requirements.txt" in changed and b.get("runtime", "python") != "node":
                await run([str(Path(d) / b.get("venv", "venv") / "bin" / "pip"), "install", "-r", "requirements.txt"], cwd=d, timeout=300)
            if "package.json" in changed and b.get("runtime") == "node":
                await run(["npm", "install"], cwd=d, timeout=300)
        return {"prev": prev, "new": new}

    async def rollback(self, b, prev):
        await run(["git", "reset", "--hard", prev], cwd=b["dir"])

    async def power(self, action):
        if action == "on":
            url = os.getenv("POWER_ON_WEBHOOK", "")
            if not url:
                raise RuntimeError("power-on isn't configured (set POWER_ON_WEBHOOK)")
            await asyncio.to_thread(urllib.request.urlopen, urllib.request.Request(url, method="POST"), None, 15)
            return "power-on signal sent"
        await run(sudo(["shutdown", "-h" if action == "off" else "-r", "+1"]))
        return f"server {'shutting down' if action == 'off' else 'rebooting'} in ~1 min"

    async def exec(self, argv):
        return await run(argv)


class Mock:
    """In-memory fake bots so you can test every feature (watchdog, auto-fix, deploy, rollback) on Render."""
    name = "mock"

    def __init__(self, bots):
        self.s = {b["id"]: {"state": "active", "since": time.time(), "restarts": 0, "missing": None, "bad_deploy": False,
                            "sha": "a1b2c3d", "logs": [], "cfg": {"prefix": "!", "token": "MOCK.TOKEN.NOT-REAL", "log_level": "INFO"}}
                  for b in bots}
        for b in bots:
            self._log(b, f"{b['name']} ready")

    def _s(self, b):
        return self.s[b["id"]]

    def _log(self, b, msg):
        self._s(b)["logs"].append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

    def _trace(self, b, err):
        for l in ("Traceback (most recent call last):", '  File "bot.py", line 3, in <module>', err):
            self._log(b, l)

    async def control(self, b, action):
        s = self._s(b)
        if action in ("stop", "kill"):
            s["state"] = "inactive"
            self._log(b, f"stopped ({action})")
        elif s["missing"]:
            s["state"] = "failed"
            s["restarts"] += 1
            self._trace(b, f"ModuleNotFoundError: No module named '{s['missing']}'")
        else:
            s["state"], s["since"] = "active", time.time()
            self._log(b, f"{action}ed, ready")

    async def info(self, b):
        s = self._s(b)
        return {"state": s["state"], "sub": "running" if s["state"] == "active" else "dead",
                "uptime": int(time.time() - s["since"]) if s["state"] == "active" else None, "restarts": s["restarts"]}

    async def health(self, b):
        return 35 if self._s(b)["state"] == "active" else -1

    async def tail(self, b, n):
        return self._s(b)["logs"][-n:]

    async def read(self, b):
        return json.dumps(self._s(b)["cfg"])

    async def write(self, b, text):
        self._s(b)["cfg"] = json.loads(text)

    async def install(self, b, pkg):
        s = self._s(b)
        m = s["missing"]
        if m and _norm(pkg) in {_norm(m), _norm(PIPMAP.get(m, ""))}:
            s["missing"] = None
        self._log(b, f"pip: Successfully installed {pkg}")
        return f"Successfully installed {pkg}"

    async def listed(self, b, pkg):
        return True

    async def deploy(self, b):
        s = self._s(b)
        s["n"] = s.get("n", 0) + 1
        prev, s["sha"] = s["sha"], f"{(int(time.time()) * 31 + s['n']) % 0xfffffff:07x}"
        if s["bad_deploy"]:
            s["missing"], s["bad_deploy"] = "yaml", False
        self._log(b, f"deployed {prev[:7]} -> {s['sha'][:7]}")
        return {"prev": prev, "new": s["sha"]}

    async def rollback(self, b, prev):
        s = self._s(b)
        s["sha"], s["missing"] = prev, None
        self._log(b, f"rolled back to {prev[:7]}")

    async def power(self, action):
        return f"(simulated) server {action}. Mock backend doesn't touch the real host."

    async def exec(self, argv):
        return await run(argv)

    def simulate(self, b, kind):
        s = self._s(b)
        if kind == "missing_module":
            s["missing"], s["state"] = "yaml", "failed"
            s["restarts"] += 1
            self._trace(b, "ModuleNotFoundError: No module named 'yaml'")
        elif kind == "crash":
            s["state"] = "failed"
            s["restarts"] += 1
            self._trace(b, "RuntimeError: unhandled exception in on_message")
        elif kind == "loop":
            s["state"] = "failed"
            s["restarts"] += 4
            self._trace(b, "RuntimeError: crashed on startup")
        elif kind == "bad_deploy":
            s["bad_deploy"] = True
        else:
            raise ValueError("kind: missing_module | crash | loop | bad_deploy")
