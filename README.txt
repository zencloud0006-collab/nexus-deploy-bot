NEXUS BOT FIX

This ZIP contains a replacement bot.py and instructions. It does NOT include your existing agent.py or tools.py; keep those files in your repository.

INSTALL
1. Download and extract this ZIP.
2. Back up your current bot.py.
3. Replace the repository's bot.py with this bot.py.
4. Keep your existing agent.py, tools.py, .env.example, and other project files.
5. Ensure Render has DISCORD_TOKEN and ADMIN_IDS environment variables. ADMIN_IDS is a comma-separated list of Discord user IDs.
6. Commit/push bot.py to the branch Render deploys (currently main).
7. Wait for Render to finish deploying, then test /ping, /help, and /host.

HOSTED ZIP FORMAT
Python: main.py, bot.py, or app.py at the project root. requirements.txt is optional.
Node.js: package.json and a start script or main entry (defaults to index.js).

IMPORTANT
- Uploaded projects run as child processes inside the same Render service as Nexus.
- Upload limit: 25 MB. Extracted contents limit: 150 MB.
- Only users listed in ADMIN_IDS can use hosting commands.
- Current Render service uses the Python runtime. Node.js hosting will report an error until Node.js/npm are installed alongside Python.
- Files are stored under /tmp, which is ephemeral. Render restarts/redeploys can erase projects and stop their processes.
- This is not a secure multi-tenant sandbox. Only run trusted code; uploaded code can consume CPU/RAM.
