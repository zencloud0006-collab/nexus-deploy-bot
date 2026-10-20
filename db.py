"""SQLite persistence: roles, audit log, chat history, pending confirmations, usage, incidents, samples."""
import asyncio
import json
import os
import sqlite3
import threading
import time

PATH = os.getenv("DB_PATH", "nexus.db")
_conn = sqlite3.connect(PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.Lock()
_conn.executescript("""
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS roles(user_id INTEGER PRIMARY KEY, role TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_id INTEGER, action TEXT, args TEXT, ok INTEGER, result TEXT);
CREATE TABLE IF NOT EXISTS hist(channel INTEGER PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS pending(channel INTEGER, user_id INTEGER, name TEXT, args TEXT, PRIMARY KEY(channel,user_id));
CREATE TABLE IF NOT EXISTS usage(day TEXT, user_id INTEGER, tokens INTEGER, cost REAL, PRIMARY KEY(day,user_id));
CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bot TEXT, kind TEXT, summary TEXT, fix TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS samples(ts REAL, cpu REAL, mem REAL, disk REAL);
""")


def _run(sql, args=(), rid=False):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        if rid:
            return cur.lastrowid
        return [dict(r) for r in cur.fetchall()] if cur.description else []


async def q(sql, *args):
    return await asyncio.to_thread(_run, sql, args)


async def ins(sql, *args):
    return await asyncio.to_thread(_run, sql, args, True)


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


# kv
async def kv_get(k, default=None):
    r = await q("SELECT value FROM kv WHERE key=?", k)
    return r[0]["value"] if r else default


async def kv_set(k, v):
    await q("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, str(v))


# roles
async def get_role(uid):
    r = await q("SELECT role FROM roles WHERE user_id=?", uid)
    return r[0]["role"] if r else None


async def set_role(uid, role):
    await q("INSERT INTO roles(user_id,role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role", uid, role)


async def del_role(uid):
    await q("DELETE FROM roles WHERE user_id=?", uid)


async def owners():
    return [r["user_id"] for r in await q("SELECT user_id FROM roles WHERE role='owner'")]


# audit
async def audit(uid, action, args, ok, result):
    await q("INSERT INTO audit(ts,user_id,action,args,ok,result) VALUES(?,?,?,?,?,?)",
            time.time(), uid, action, json.dumps(args)[:500], int(ok), str(result)[:300])


async def recent_audit(n=10):
    return await q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", n)


# chat state
async def load_hist(ch):
    r = await q("SELECT data FROM hist WHERE channel=?", ch)
    return json.loads(r[0]["data"]) if r else []


async def save_hist(ch, msgs):
    await q("INSERT INTO hist(channel,data) VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET data=excluded.data",
            ch, json.dumps(msgs[-24:]))


async def save_pending(ch, uid, name, args):
    await q("INSERT OR REPLACE INTO pending(channel,user_id,name,args) VALUES(?,?,?,?)", ch, uid, name, json.dumps(args))


async def del_pending(ch, uid):
    await q("DELETE FROM pending WHERE channel=? AND user_id=?", ch, uid)


async def all_pending():
    return [(r["channel"], r["user_id"], r["name"], json.loads(r["args"])) for r in await q("SELECT * FROM pending")]


# usage
async def add_usage(uid, tokens, cost):
    await q("INSERT INTO usage(day,user_id,tokens,cost) VALUES(?,?,?,?) "
            "ON CONFLICT(day,user_id) DO UPDATE SET tokens=tokens+excluded.tokens, cost=cost+excluded.cost",
            today(), uid, int(tokens), float(cost))


async def usage_today(uid):
    r = await q("SELECT tokens,cost FROM usage WHERE day=? AND user_id=?", today(), uid)
    return (r[0]["tokens"], r[0]["cost"]) if r else (0, 0.0)


# incidents
async def add_incident(bot, kind, summary, fix):
    return await ins("INSERT INTO incidents(ts,bot,kind,summary,fix,status) VALUES(?,?,?,?,?,'open')",
                     time.time(), bot, kind, summary, fix)


async def set_incident(iid, status):
    await q("UPDATE incidents SET status=? WHERE id=?", status, iid)


async def open_incident(bot):
    r = await q("SELECT * FROM incidents WHERE bot=? AND status IN ('open','escalated') ORDER BY id DESC LIMIT 1", bot)
    return r[0] if r else None


async def get_incident(iid):
    r = await q("SELECT * FROM incidents WHERE id=?", iid)
    return r[0] if r else None


async def latest_fix_incident(bot):
    r = await q("SELECT * FROM incidents WHERE bot=? AND fix!='' AND status IN ('open','escalated') ORDER BY id DESC LIMIT 1", bot)
    return r[0] if r else None


async def recent_incidents(n=10):
    return await q("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", n)


async def incidents_since(ts):
    return await q("SELECT * FROM incidents WHERE ts>=? ORDER BY id", ts)


# resource samples
async def add_sample(cpu, mem, disk):
    await q("INSERT INTO samples VALUES(?,?,?,?)", time.time(), cpu, mem, disk)
    await q("DELETE FROM samples WHERE ts<?", time.time() - 7 * 86400)


async def samples_since(ts):
    return await q("SELECT * FROM samples WHERE ts>=?", ts)
