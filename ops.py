"""Watchdog, auto-fix, live panels and daily reports."""
import asyncio
import json
import os
import time

import discord

import db
import tools
from backend import fmt_dur, stats

B = tools.B
INTERVAL = int(os.getenv("WATCH_INTERVAL", "60"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR_UTC", "9"))
FIX_WAIT = float(os.getenv("FIX_WAIT", "8"))
LIMITS = {"disk": 90, "mem": 92, "cpu": 95}
ICON = {"active": "🟢", "activating": "🟡"}


def bar(p):
    n = int(p / 10)
    return "█" * n + "░" * (10 - n) + f" {p:.0f}%"


async def panel_embed():
    s = stats()
    mode = await db.kv_get("autofix", "suggest")
    e = discord.Embed(title="⚡ Nexus panel", color=0x5865F2)
    for b in tools.BOTS:
        i = await B.info(b)
        ping = await B.health(b)
        line = f"{ICON.get(i['state'], '🔴')} **{i['state']}**"
        if i["uptime"] is not None:
            line += f" · up {fmt_dur(i['uptime'])}"
        line += f" · restarts {i['restarts']}"
        if ping is not None:
            line += " · ping down" if ping < 0 else f" · {ping}ms"
        e.add_field(name=f"#{b['id']} {b['name']}", value=line, inline=False)
    e.add_field(name="Server", value=f"CPU `{bar(s['cpu'])}`\nRAM `{bar(s['mem'])}`\nDisk `{bar(s['disk'])}`", inline=False)
    e.set_footer(text=f"backend: {B.name} · auto-fix: {mode}")
    e.timestamp = discord.utils.utcnow()
    return e


def panel_view():
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Refresh", emoji="🔄", custom_id="nx:refresh", style=discord.ButtonStyle.secondary))
    for b in tools.BOTS[:19]:
        v.add_item(discord.ui.Button(label=f"Restart {b['name']}"[:80], custom_id=f"nx:restart:{b['id']}", style=discord.ButtonStyle.primary))
    return v


class Ops:
    def __init__(self, client, notify, agent):
        self.client, self.notify, self.agent = client, notify, agent
        self.last_restarts, self.crashes, self.attempts = {}, {}, {}

    async def run(self):
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            for step in (self.watch, self.refresh_panels, self.report_if_due):
                try:
                    await step()
                except Exception as e:
                    print(f"ops {step.__name__} error:", repr(e))
            await asyncio.sleep(INTERVAL)

    async def _cooldown(self, key, secs):
        last = float(await db.kv_get(key, "0"))
        if time.time() - last > secs:
            await db.kv_set(key, time.time())
            return True
        return False

    # ───────── watchdog ─────────
    async def watch(self):
        s = stats()
        await db.add_sample(s["cpu"], s["mem"], s["disk"])
        for key in ("disk", "mem", "cpu"):
            if s[key] >= LIMITS[key] and await self._cooldown(f"res:{key}", 1800):
                await self.notify(embed=discord.Embed(title=f"⚠️ High {key.upper()}", description=f"{key} at {s[key]:.0f}% (limit {LIMITS[key]}%)", color=0xF1C40F))
        mode = await db.kv_get("autofix", "suggest")
        for b in tools.BOTS:
            try:
                await self._check(b, mode)
            except Exception as e:
                print("watch error", b["name"], repr(e))

    async def _check(self, b, mode):
        bid = b["id"]
        if await db.kv_get(f"desired:{bid}", "running") != "running":
            return
        info = await B.info(b)
        now = time.time()
        prev = self.last_restarts.get(bid, info["restarts"])
        self.last_restarts[bid] = info["restarts"]
        self.crashes.setdefault(bid, []).extend([now] * max(0, info["restarts"] - prev))
        self.crashes[bid] = [t for t in self.crashes[bid] if now - t < 600]
        loop = len(self.crashes[bid]) >= 3
        down = info["state"] not in ("active", "activating")
        open_inc = await db.open_incident(b["name"])
        if not down and not loop:
            if open_inc:
                await db.set_incident(open_inc["id"], "recovered")
                await self.notify(text=f"✅ **{b['name']}** recovered")
            return
        if open_inc:
            return  # already alerted; /fix, the button, or recovery closes it
        diag = await tools.diagnose(b)
        fix = diag["fix"]
        kind = "crash-loop" if loop else "down"
        iid = await db.add_incident(b["name"], kind, diag["summary"], json.dumps(fix) if fix else "")
        emb = discord.Embed(title=f"🔴 {b['name']} {kind}", description=diag["summary"], color=0xE74C3C)
        if diag["excerpt"]:
            emb.add_field(name="Log excerpt", value=f"```\n{diag['excerpt'][:900]}\n```", inline=False)
        recent = [t for t in self.attempts.get(bid, []) if now - t < 1800]
        if mode == "auto" and fix and fix.get("auto_safe") and len(recent) < 2:
            self.attempts[bid] = recent + [now]
            await self.notify(text=f"🛠 auto-fixing **{b['name']}**: {fix['label']}")
            ok, _ = await self.apply_fix(b, fix)
            if ok:
                await db.set_incident(iid, "fixed")
                await self.notify(text=f"✅ **{b['name']}** fixed automatically ({fix['label']})")
            else:
                await db.set_incident(iid, "escalated")
                emb.title = f"🚨 {b['name']} still down after auto-fix"
                await self.notify(embed=emb)
            return
        view = None
        if fix and mode != "off":
            emb.add_field(name="Proposed fix", value=fix["label"] + ("" if fix.get("auto_safe") else "\n⚠️ package isn't in the bot's requirements, so I won't auto-install it"))
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Apply fix", custom_id=f"nx:fix:{iid}", style=discord.ButtonStyle.success))
        elif mode == "off":
            emb.set_footer(text="auto-fix is off")
        await self.notify(embed=emb, view=view)

    async def apply_fix(self, b, fix, user=0):
        steps = []
        if fix["tool"] == "install_package":
            steps.append(await tools.call("install_package", fix["args"], user))
        steps.append(await tools.call("restart_bot", {"bot_id": b["id"]}, user))
        await asyncio.sleep(FIX_WAIT)
        i = await B.info(b)
        return i["state"] == "active", steps

    # ───────── panels ─────────
    async def add_panel(self, channel_id, message_id):
        panels = json.loads(await db.kv_get("panels", "[]"))
        panels.append([channel_id, message_id])
        await db.kv_set("panels", json.dumps(panels[-10:]))

    async def refresh_message(self, msg):
        await msg.edit(embed=await panel_embed(), view=panel_view())

    async def refresh_panels(self):
        panels = json.loads(await db.kv_get("panels", "[]"))
        keep = []
        for ch, mid in panels:
            try:
                channel = self.client.get_channel(ch) or await self.client.fetch_channel(ch)
                await self.refresh_message(await channel.fetch_message(mid))
                keep.append([ch, mid])
            except discord.NotFound:
                pass
            except Exception as e:
                print("panel refresh error:", repr(e))
                keep.append([ch, mid])
        if keep != panels:
            await db.kv_set("panels", json.dumps(keep))

    # ───────── daily report ─────────
    async def report_if_due(self):
        now = time.gmtime()
        today = time.strftime("%Y-%m-%d", now)
        if now.tm_hour == REPORT_HOUR and await db.kv_get("report:last", "") != today:
            await db.kv_set("report:last", today)
            await self.notify(embed=await self.report_embed())

    async def report_embed(self):
        since = time.time() - 86400
        rows = await db.samples_since(since)
        incs = await db.incidents_since(since)
        e = discord.Embed(title="📊 Nexus daily report", color=0x2ECC71)
        lines = []
        if rows:
            for k in ("cpu", "mem", "disk"):
                v = [r[k] for r in rows]
                lines.append(f"{k.upper()}: avg {sum(v) / len(v):.0f}% · max {max(v):.0f}%")
            e.add_field(name="Resources (24h)", value="\n".join(lines), inline=False)
        per = {}
        for r in incs:
            per.setdefault(r["bot"], []).append(r["status"])
        summary = "\n".join(f"**{b}**: {len(s)} incident(s) ({', '.join(sorted(set(s)))})" for b, s in per.items()) or "No incidents 🎉"
        e.add_field(name="Incidents (24h)", value=summary, inline=False)
        for b in tools.BOTS:
            i = await B.info(b)
            e.add_field(name=b["name"], value=f"{i['state']} · restarts {i['restarts']}", inline=True)
        ai = await self.agent.summarize("\n".join(lines) + "\n" + summary)
        if ai:
            e.description = ai
        return e
