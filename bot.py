import asyncio
import io
import json
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path

import discord
from discord import app_commands
from dotenv import dotenv_values, load_dotenv

load_dotenv()
import tools
from agent import Agent

ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
ADMIN_CHANNEL = int(os.getenv("ADMIN_CHANNEL_ID", "0") or 0)
HOST_ROOT = Path(os.getenv("HOST_ROOT", "/tmp/nexus-hosted"))
MAX_ZIP = 25 * 1024 * 1024
MAX_EXPANDED = 150 * 1024 * 1024

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(client)
agent = Agent()
locks = {}
hosted = {}


def is_admin(interaction):
    return interaction.user.id in ADMINS


def safe_project_env(root):
    # Uploaded projects must not inherit Nexus or Render secrets.
    env = {
        "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(root), "LANG": os.getenv("LANG", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1", "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    }
    env_file = root / ".env"
    if env_file.is_file() and env_file.stat().st_size <= 65536:
        for key, value in dotenv_values(env_file).items():
            if key and value is not None and key.replace("_", "").isalnum():
                env[key] = value
    return env


async def run_setup(args, cwd, timeout=240):
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), env=safe_project_env(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Dependency installation timed out.")
    result = output.decode(errors="replace")[-1600:]
    if proc.returncode:
        raise RuntimeError(result or f"Command failed with exit code {proc.returncode}.")
    return result


def safe_extract(zf, destination):
    members = zf.infolist()
    if len(members) > 2000:
        raise ValueError("ZIP contains over 2,000 files.")
    total = 0
    for member in members:
        name = member.filename.replace("\\", "/")
        path = Path(name)
        mode = member.external_attr >> 16
        if path.is_absolute() or ".." in path.parts or (mode and stat.S_ISLNK(mode)):
            raise ValueError("ZIP contains an unsafe path or symbolic link.")
        if member.is_dir():
            continue
        total += member.file_size
        if total > MAX_EXPANDED:
            raise ValueError("ZIP expands beyond 150 MB.")
        target = (destination / path).resolve()
        if destination.resolve() not in target.parents:
            raise ValueError("ZIP contains an unsafe path.")
    zf.extractall(destination)


def find_project_root(folder):
    items = list(folder.iterdir())
    if len(items) == 1 and items[0].is_dir():
        nested = items[0]
        markers = ("requirements.txt", "package.json", "main.py", "bot.py", "app.py", "index.js")
        if any((nested / marker).exists() for marker in markers):
            return nested
    return folder


async def send_channel(channel_id, text):
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    await channel.send(text[:2000])
    return f"sent to {channel_id}"


async def notify_admins(text):
    if ADMIN_CHANNEL:
        return await send_channel(ADMIN_CHANNEL, "Alert: " + text)
    for user_id in ADMINS:
        await (await client.fetch_user(user_id)).send(text[:2000])
    return "admins notified"


tools.SEND, tools.NOTIFY = send_channel, notify_admins


@client.event
async def on_ready():
    HOST_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} Discord slash commands.")
    except Exception as exc:
        print(f"Slash command sync failed: {type(exc).__name__}: {exc}")
    print(f"Nexus online as {client.user} | admins: {len(ADMINS)}")


@tree.command(name="ping", description="Check whether Nexus is responding")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(client.latency * 1000)} ms", ephemeral=True)


@tree.command(name="help", description="Show Nexus commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Nexus commands**\n/host — run a Python or Node.js ZIP\n/bots — list hosted projects\n"
        "/status — check your project\n/logs — show recent output\n/stop — stop your project\n/ping — check Nexus",
        ephemeral=True,
    )


@tree.command(name="host", description="Run a Python or Node.js project from a ZIP")
@app_commands.describe(language="Project runtime", zip_file="ZIP archive containing project source")
@app_commands.choices(language=[
    app_commands.Choice(name="Python", value="python"),
    app_commands.Choice(name="Node.js", value="nodejs"),
])
async def host(interaction: discord.Interaction, language: app_commands.Choice[str], zip_file: discord.Attachment):
    if not is_admin(interaction):
        await interaction.response.send_message("You are not authorized to host projects.", ephemeral=True)
        return
    if not zip_file.filename.lower().endswith(".zip") or zip_file.size > MAX_ZIP:
        await interaction.response.send_message("Upload a ZIP file no larger than 25 MB.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    previous = hosted.get(interaction.user.id)
    if previous and previous["process"].returncode is None:
        await interaction.followup.send("You already have a running project. Use /stop first.", ephemeral=True)
        return

    project_dir = HOST_ROOT / str(interaction.user.id)
    shutil.rmtree(project_dir, ignore_errors=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    archive = None
    try:
        data = await zip_file.read()
        if len(data) > MAX_ZIP:
            raise ValueError("ZIP exceeds 25 MB.")
        archive = zipfile.ZipFile(io.BytesIO(data))
        safe_extract(archive, project_dir)
        root = find_project_root(project_dir)

        if language.value == "python":
            entry = next((root / name for name in ("main.py", "bot.py", "app.py") if (root / name).is_file()), None)
            if entry is None:
                raise ValueError("Python ZIP must contain main.py, bot.py, or app.py.")
            venv = root / ".nexus-venv"
            await run_setup([sys.executable, "-m", "venv", str(venv)], root, timeout=90)
            python_bin = venv / "bin" / "python"
            requirements = root / "requirements.txt"
            if requirements.is_file():
                await run_setup([str(python_bin), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)], root)
            command = [str(python_bin), str(entry)]
        else:
            if not shutil.which("node") or not shutil.which("npm"):
                raise RuntimeError("Node.js/npm is not installed in this Render runtime. Install Node.js alongside Python to use Node.js projects.")
            package_file = root / "package.json"
            if not package_file.is_file():
                raise ValueError("Node.js ZIP must contain package.json.")
            await run_setup(["npm", "install", "--omit=dev", "--no-audit", "--no-fund"], root)
            package = json.loads(package_file.read_text(encoding="utf-8"))
            if package.get("scripts", {}).get("start"):
                command = ["npm", "start"]
            else:
                entry = (root / package.get("main", "index.js")).resolve()
                if root.resolve() not in entry.parents or not entry.is_file():
                    raise ValueError("Node entry file from package.json was not found.")
                command = ["node", str(entry)]

        log_path = project_dir / "runtime.log"
        log_file = open(log_path, "ab", buffering=0)
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(root), env=safe_project_env(root),
            stdout=log_file, stderr=asyncio.subprocess.STDOUT, start_new_session=True
        )
        hosted[interaction.user.id] = {
            "process": process, "runtime": language.value, "log": log_path,
            "log_file": log_file, "project_dir": project_dir,
        }
        await asyncio.sleep(1)
        if process.returncode is not None:
            tail = log_path.read_text(errors="replace")[-1300:]
            await interaction.followup.send(
                f"Project exited with code {process.returncode}.\n```text\n{tail[-1400:]}\n```",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Project started inside Nexus. Runtime: **{language.value}**; PID: {process.pid}. Use /status, /logs, or /stop.",
                ephemeral=True,
            )
    except Exception as exc:
        shutil.rmtree(project_dir, ignore_errors=True)
        message = str(exc)[-1300:]
        await interaction.followup.send(f"Hosting failed: {type(exc).__name__}: {message}", ephemeral=True)
    finally:
        if archive:
            archive.close()


@tree.command(name="bots", description="List projects hosted by Nexus")
async def bots_command(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not hosted:
        message = "No hosted projects since Nexus last restarted."
    else:
        rows = []
        for user_id, item in hosted.items():
            process = item["process"]
            state = "running" if process.returncode is None else f"stopped (exit {process.returncode})"
            rows.append(f"<@{user_id}> — {item['runtime']} — {state} — PID {process.pid}")
        message = "\n".join(rows)
    await interaction.response.send_message(message[:1900], ephemeral=True)


@tree.command(name="status", description="Check your hosted project's status")
async def status_command(interaction: discord.Interaction):
    item = hosted.get(interaction.user.id)
    if not is_admin(interaction) or not item:
        await interaction.response.send_message("No hosted project found for you.", ephemeral=True)
        return
    process = item["process"]
    state = "running" if process.returncode is None else f"stopped (exit {process.returncode})"
    await interaction.response.send_message(f"{item['runtime']} project: {state}; PID {process.pid}", ephemeral=True)


@tree.command(name="stop", description="Stop your hosted project")
async def stop_command(interaction: discord.Interaction):
    item = hosted.get(interaction.user.id)
    if not is_admin(interaction) or not item:
        await interaction.response.send_message("No hosted project found for you.", ephemeral=True)
        return
    process = item["process"]
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=8)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    if not item["log_file"].closed:
        item["log_file"].close()
    await interaction.response.send_message("Project stopped.", ephemeral=True)


@tree.command(name="logs", description="Show recent logs from your hosted project")
@app_commands.describe(lines="Number of recent lines (1–25)")
async def logs_command(interaction: discord.Interaction, lines: app_commands.Range[int, 1, 25] = 15):
    item = hosted.get(interaction.user.id)
    if not is_admin(interaction) or not item:
        await interaction.response.send_message("No hosted project found for you.", ephemeral=True)
        return
    try:
        content = item["log"].read_text(errors="replace")
    except OSError:
        content = "No logs yet."
    tail = "\n".join(content.splitlines()[-lines:]) or "No output yet."
    await interaction.response.send_message(f"```text\n{tail[-1750:]}\n```", ephemeral=True)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author.id not in ADMINS:
        return
    addressed = (
        message.guild is None or client.user in message.mentions
        or agent.has_pending(message.channel.id, message.author.id)
    )
    if not addressed:
        return
    text = message.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
    if not text:
        return
    async with locks.setdefault(message.channel.id, asyncio.Lock()):
        async with message.channel.typing():
            try:
                reply = await agent.handle(message.channel.id, message.author.id, text)
            except Exception as exc:
                reply = f"Something broke ({type(exc).__name__}). Check console."
                print("agent error:", repr(exc))
    chunks = [reply[i:i + 1900] for i in range(0, len(reply), 1900)] or ["..."]
    await message.reply(chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


client.run(os.environ["DISCORD_TOKEN"])
