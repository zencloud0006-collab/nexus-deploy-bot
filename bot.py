import asyncio
import json
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402
import tools  # noqa: E402
from agent import Agent, describe  # noqa: E402
from ops import Ops, panel_embed, panel_view  # noqa: E402

OWNERS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
GUILD_IDS = [int(x) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip()]
ADMIN_CHANNEL = int(os.getenv("ADMIN_CHANNEL_ID", "0") or 0)
MISSING = discord.utils.MISSING

intents = discord.Intents.default()
intents.message_content = os.getenv("MESSAGE_CONTENT", "1") == "1"


class Nexus(discord.Client):
    async def setup_hook(self):
        for uid in OWNERS:
            await db.set_role(uid, "owner")
        await agent.load()
        if GUILD_IDS:
            for g in GUILD_IDS:
                tree.copy_global_to(guild=discord.Object(g))
                print(f"synced {len(await tree.sync(guild=discord.Object(g)))} commands to guild {g}")
        else:
            print(f"synced {len(await tree.sync())} global commands (can take a while to appear)")
        asyncio.create_task(ops.run())


client = Nexus(intents=intents, allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(client)
agent = Agent()
locks = {}


# ───────── plumbing ─────────
async def _send(channel_id, text):
    ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    await ch.send(text[:2000])
    return f"sent to {channel_id}"


async def notify(text=None, embed=None, view=None):
    if ADMIN_CHANNEL:
        ch = client.get_channel(ADMIN_CHANNEL) or await client.fetch_channel(ADMIN_CHANNEL)
        return await ch.send(content=text, embed=embed, view=view or MISSING)
    for uid in await db.owners():
        try:
            await (await client.fetch_user(uid)).send(content=text, embed=embed, view=view or MISSING)
        except discord.HTTPException:
            pass


async def _notify_text(text):
    await notify(text=f"🚨 {text}")
    return "admins notified"


tools.SEND, tools.NOTIFY = _send, _notify_text
ops = Ops(client, notify, agent)


def chunk(text):
    return [text[i:i + 1900] for i in range(0, len(text), 1900)] or ["..."]


async def run_agent(channel_id, user_id, text):
    async with locks.setdefault(channel_id, asyncio.Lock()):
        try:
            reply = await agent.handle(channel_id, user_id, text)
        except Exception as e:
            print("agent error:", repr(e))
            reply = f"Something broke on my side ({type(e).__name__}). Check the logs."
    return chunk(reply), (Confirm(user_id, channel_id) if agent.has_pending(channel_id, user_id) else None)


async def run_tool(send, name, args, user):
    try:
        out = await tools.call(name, args, user)
    except PermissionError as e:
        out = f"Denied: {e}"
    except Exception as e:
        out = f"Failed: {tools.redact(str(e))[:500]}"
    await send(f"```\n{out[:1800]}\n```")


class Confirm(discord.ui.View):
    """Yes/No for LLM-initiated actions (the agent holds the pending action)."""
    def __init__(self, user_id, channel_id):
        super().__init__(timeout=600)
        self.user_id, self.channel_id = user_id, channel_id

    async def interaction_check(self, i):
        return i.user.id == self.user_id

    async def _go(self, i, answer):
        await i.response.defer()
        await i.edit_original_response(view=None)
        chunks, view = await run_agent(self.channel_id, self.user_id, answer)
        await i.followup.send(chunks[0], view=view or MISSING)
        for c in chunks[1:]:
            await i.followup.send(c)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def yes(self, i, b):
        await self._go(i, "yes")

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, i, b):
        await self._go(i, "no")


class DirectConfirm(discord.ui.View):
    """Yes/No for slash commands that call a tool directly."""
    def __init__(self, user_id, name, args):
        super().__init__(timeout=300)
        self.user_id, self.name, self.args = user_id, name, args

    async def interaction_check(self, i):
        return i.user.id == self.user_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, i, b):
        await i.response.defer()
        await i.edit_original_response(view=None)
        await run_tool(i.followup.send, self.name, self.args, self.user_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, i, b):
        await i.response.edit_message(content="Cancelled.", view=None)


async def guard(i, need="viewer", ephemeral=False):
    role = await db.get_role(i.user.id)
    if not role or tools.RANK[role] < tools.RANK[need]:
        await i.response.send_message(f"You need the **{need}** role for that.", ephemeral=True)
        return None
    await i.response.defer(thinking=True, ephemeral=ephemeral)  # must ack within 3s or Discord says "application didn't respond"
    return role


async def direct(i, name, args, confirm=False):
    if not await guard(i, tools.NEEDS[name]):
        return
    if confirm and tools.needs_confirm(name, args):
        return await i.followup.send(f"This will run `{describe(name, args)}`. Confirm?", view=DirectConfirm(i.user.id, name, args))
    await run_tool(i.followup.send, name, args, i.user.id)


async def bot_ac(i, current: str):
    return [app_commands.Choice(name=b["name"], value=b["name"]) for b in tools.BOTS if current.lower() in b["name"].lower()][:25]


# ───────── slash commands ─────────
@tree.command(name="nexus", description="Talk to Nexus")
@app_commands.describe(message="What do you need?")
async def nexus_cmd(i: discord.Interaction, message: str):
    if not await guard(i):
        return
    chunks, view = await run_agent(i.channel_id or i.user.id, i.user.id, message)
    await i.followup.send(chunks[0], view=view or MISSING)
    for c in chunks[1:]:
        await i.followup.send(c)


@tree.command(name="bots", description="List registered bots")
async def bots_cmd(i: discord.Interaction):
    await direct(i, "list_bots", {})


@tree.command(name="stats", description="Server CPU/RAM/disk")
async def stats_cmd(i: discord.Interaction):
    await direct(i, "get_server_stats", {})


@tree.command(name="incidents", description="Recent incidents")
async def incidents_cmd(i: discord.Interaction):
    await direct(i, "get_incidents", {})


@tree.command(name="status", description="Status of a bot")
@app_commands.describe(bot="Bot name")
@app_commands.autocomplete(bot=bot_ac)
async def status_cmd(i: discord.Interaction, bot: str):
    await direct(i, "get_status", {"bot_id": bot})


@tree.command(name="logs", description="Recent logs of a bot")
@app_commands.autocomplete(bot=bot_ac)
async def logs_cmd(i: discord.Interaction, bot: str, lines: app_commands.Range[int, 1, 40] = 15):
    await direct(i, "get_logs", {"bot_id": bot, "lines": lines})


@tree.command(name="diagnose", description="Diagnose a bot's latest error")
@app_commands.autocomplete(bot=bot_ac)
async def diagnose_cmd(i: discord.Interaction, bot: str):
    await direct(i, "diagnose_error", {"bot_id": bot})


@tree.command(name="start", description="Start a bot")
@app_commands.autocomplete(bot=bot_ac)
async def start_cmd(i: discord.Interaction, bot: str):
    await direct(i, "start_bot", {"bot_id": bot})


@tree.command(name="restart", description="Restart a bot")
@app_commands.autocomplete(bot=bot_ac)
async def restart_cmd(i: discord.Interaction, bot: str):
    await direct(i, "restart_bot", {"bot_id": bot})


@tree.command(name="stop", description="Stop a bot")
@app_commands.autocomplete(bot=bot_ac)
async def stop_cmd(i: discord.Interaction, bot: str):
    await direct(i, "stop_bot", {"bot_id": bot}, confirm=True)


@tree.command(name="deploy", description="Pull latest code, restart, auto-rollback if it fails")
@app_commands.autocomplete(bot=bot_ac)
async def deploy_cmd(i: discord.Interaction, bot: str):
    await direct(i, "deploy_bot", {"bot_id": bot}, confirm=True)


@tree.command(name="fix", description="Apply the proposed fix for a bot's open incident")
@app_commands.autocomplete(bot=bot_ac)
async def fix_cmd(i: discord.Interaction, bot: str):
    if not await guard(i, "operator"):
        return
    b = tools._bot(bot)
    inc = await db.latest_fix_incident(b["name"])
    if not inc:
        return await i.followup.send("No open incident with a proposed fix.")
    ok, steps = await ops.apply_fix(b, json.loads(inc["fix"]), i.user.id)
    await db.set_incident(inc["id"], "fixed" if ok else "escalated")
    await i.followup.send(("✅ Fixed. " if ok else "❌ Still failing. ") + "; ".join(steps))


@tree.command(name="panel", description="Post a live status panel in this channel")
async def panel_cmd(i: discord.Interaction):
    if not await guard(i, "operator", True):
        return
    msg = await i.channel.send(embed=await panel_embed(), view=panel_view())
    await ops.add_panel(msg.channel.id, msg.id)
    await i.followup.send("Panel posted. It refreshes automatically.", ephemeral=True)


@tree.command(name="autofix", description="Set watchdog auto-fix mode")
@app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in ("off", "suggest", "auto")])
async def autofix_cmd(i: discord.Interaction, mode: app_commands.Choice[str]):
    await direct(i, "set_autofix", {"mode": mode.value})


@tree.command(name="role", description="Set a user's role (owner only)")
@app_commands.choices(role=[app_commands.Choice(name=r, value=r) for r in ("viewer", "operator", "owner", "none")])
async def role_cmd(i: discord.Interaction, user: discord.User, role: app_commands.Choice[str]):
    if not await guard(i, "owner", True):
        return
    if user.id in OWNERS and role.value != "owner":
        return await i.followup.send("That user is a permanent owner (ADMIN_IDS).")
    if role.value == "none":
        await db.del_role(user.id)
    else:
        await db.set_role(user.id, role.value)
    await db.audit(i.user.id, "set_role", {"user": user.id, "role": role.value}, True, "")
    await i.followup.send(f"{user.display_name} → {role.value}")


@tree.command(name="audit", description="Recent actions (owner only)")
async def audit_cmd(i: discord.Interaction):
    if not await guard(i, "owner", True):
        return
    import time
    rows = await db.recent_audit(10)
    lines = [f"{time.strftime('%m-%d %H:%M', time.gmtime(r['ts']))} <@{r['user_id']}> {r['action']} {'✅' if r['ok'] else '❌'} {r['args'][:60]}" for r in rows]
    await i.followup.send("\n".join(lines) or "empty", ephemeral=True)


@tree.command(name="simulate", description="Break a mock bot to test the watchdog (owner, mock backend only)")
@app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in ("missing_module", "crash", "loop", "bad_deploy")])
@app_commands.autocomplete(bot=bot_ac)
async def simulate_cmd(i: discord.Interaction, bot: str, kind: app_commands.Choice[str]):
    if not await guard(i, "owner", True):
        return
    if tools.B.name != "mock":
        return await i.followup.send("Only available on the mock backend.", ephemeral=True)
    tools.B.simulate(tools._bot(bot), kind.value)
    await i.followup.send(f"Simulated `{kind.value}` on {bot}. The watchdog notices within {os.getenv('WATCH_INTERVAL', '60')}s.", ephemeral=True)


@tree.error
async def on_tree_error(i: discord.Interaction, e: app_commands.AppCommandError):
    print("command error:", repr(e))
    try:
        send = i.followup.send if i.response.is_done() else i.response.send_message
        await send("Command failed. Check my logs.", ephemeral=True)
    except discord.HTTPException:
        pass


# ───────── panel / alert buttons ─────────
@client.event
async def on_interaction(i: discord.Interaction):
    if i.type != discord.InteractionType.component:
        return
    cid = (i.data or {}).get("custom_id", "")
    if not cid.startswith("nx:"):
        return
    role = await db.get_role(i.user.id)
    if not role:
        return await i.response.send_message("Not allowed.", ephemeral=True)
    parts = cid.split(":")
    if parts[1] == "refresh":
        await i.response.defer()
        return await ops.refresh_message(i.message)
    if tools.RANK[role] < tools.RANK["operator"]:
        return await i.response.send_message("You need the **operator** role for that.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        if parts[1] == "restart":
            await run_tool(lambda t: i.followup.send(t, ephemeral=True), "restart_bot", {"bot_id": parts[2]}, i.user.id)
        elif parts[1] == "fix":
            inc = await db.get_incident(int(parts[2]))
            if not inc or inc["status"] not in ("open", "escalated"):
                return await i.followup.send("That incident is already closed.", ephemeral=True)
            ok, steps = await ops.apply_fix(tools._bot(inc["bot"]), json.loads(inc["fix"]), i.user.id)
            await db.set_incident(inc["id"], "fixed" if ok else "escalated")
            await i.followup.send(("✅ Fixed. " if ok else "❌ Still failing. ") + "; ".join(steps), ephemeral=True)
    except Exception as e:
        await i.followup.send(f"Failed: {tools.redact(str(e))[:300]}", ephemeral=True)


# ───────── mentions / DMs ─────────
@client.event
async def on_ready():
    print(f"Nexus online as {client.user} | backend: {tools.B.name} | owners: {len(OWNERS)}")


@client.event
async def on_message(m: discord.Message):
    if m.author.bot or not await db.get_role(m.author.id):
        return
    if not (m.guild is None or client.user in m.mentions or agent.has_pending(m.channel.id, m.author.id)):
        return
    text = m.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
    if not text:
        return
    async with m.channel.typing():
        chunks, view = await run_agent(m.channel.id, m.author.id, text)
    await m.reply(chunks[0], mention_author=False, view=view or MISSING)
    for c in chunks[1:]:
        await m.channel.send(c)


client.run(os.environ["DISCORD_TOKEN"])
