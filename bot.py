import asyncio
import os

import discord
from dotenv import load_dotenv

load_dotenv()

import tools  # noqa: E402  (after load_dotenv so env is set)
from agent import Agent  # noqa: E402

ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
ADMIN_CHANNEL = int(os.getenv("ADMIN_CHANNEL_ID", "0") or 0)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
agent = Agent()
locks = {}


async def _send(channel_id, text):
    ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    await ch.send(text[:2000])
    return f"sent to {channel_id}"


async def _notify(text):
    if ADMIN_CHANNEL:
        return await _send(ADMIN_CHANNEL, f"🚨 {text}")
    for uid in ADMINS:
        await (await client.fetch_user(uid)).send(text[:2000])
    return "admins notified"


tools.SEND, tools.NOTIFY = _send, _notify


@client.event
async def on_ready():
    print(f"Nexus online as {client.user} | admins: {len(ADMINS)}")


@client.event
async def on_message(m: discord.Message):
    if m.author.bot or m.author.id not in ADMINS:
        return
    addressed = m.guild is None or client.user in m.mentions or agent.has_pending(m.channel.id, m.author.id)
    if not addressed:
        return
    text = m.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
    if not text:
        return
    async with locks.setdefault(m.channel.id, asyncio.Lock()):
        async with m.channel.typing():
            try:
                reply = await agent.handle(m.channel.id, m.author.id, text)
            except Exception as e:
                reply = f"Something broke on my side ({type(e).__name__}). Check my console."
                print("agent error:", repr(e))
    chunks = [reply[i:i + 1900] for i in range(0, len(reply), 1900)] or ["..."]
    await m.reply(chunks[0], mention_author=False)
    for c in chunks[1:]:
        await m.channel.send(c)


client.run(os.environ["DISCORD_TOKEN"])
