"""
Dashboard backend — read-only against the DB the bot writes to (data.db),
except for its own `snapshot` table, which it uses to remember what was
last shown so /api/refresh can report what's new/gone.

Also serves dashboard.html as the root page.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

import csv
import io
import os
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

app = FastAPI(title="Bot Dashboard API")

# Local network only anyway, but keeps things simple if you ever open the
# dashboard HTML from a different port/host during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_snapshot_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot (guild_id TEXT PRIMARY KEY)")
    conn.commit()


def sanitize_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


@app.get("/")
def serve_dashboard():
    if not os.path.exists(DASHBOARD_HTML):
        raise HTTPException(status_code=404, detail="dashboard.html not found next to main.py")
    return FileResponse(DASHBOARD_HTML)


@app.get("/api/servers")
def get_servers():
    conn = get_conn()
    rows = conn.execute(
        "SELECT guild_id, name, member_count, icon_url, first_seen_at FROM servers ORDER BY name"
    ).fetchall()
    conn.close()
    return {
        "servers": [
            {
                "id": r["guild_id"],
                "name": r["name"],
                "memberCount": r["member_count"],
                "iconUrl": r["icon_url"],
                "joinedAt": r["first_seen_at"][:10] if r["first_seen_at"] else None,
            }
            for r in rows
        ]
    }


@app.post("/api/refresh")
def refresh():
    """
    Diffs the current `servers` table against the last-acknowledged
    `snapshot` table, then updates the snapshot to match current state.
    The bot keeps `servers` live via events + its own periodic resync —
    this endpoint doesn't talk to Discord at all, it just compares.
    """
    conn = get_conn()
    ensure_snapshot_table(conn)

    current = conn.execute("SELECT guild_id, name, member_count, icon_url, first_seen_at FROM servers").fetchall()
    current_ids = {r["guild_id"] for r in current}

    previous_ids = {r["guild_id"] for r in conn.execute("SELECT guild_id FROM snapshot").fetchall()}

    added_ids = current_ids - previous_ids
    removed_ids = previous_ids - current_ids

    added = [
        {"id": r["guild_id"], "name": r["name"], "memberCount": r["member_count"]}
        for r in current
        if r["guild_id"] in added_ids
    ]
    removed = [{"id": gid} for gid in removed_ids]

    conn.execute("DELETE FROM snapshot")
    conn.executemany("INSERT INTO snapshot (guild_id) VALUES (?)", [(gid,) for gid in current_ids])
    conn.commit()
    conn.close()

    return {
        "servers": [
            {
                "id": r["guild_id"],
                "name": r["name"],
                "memberCount": r["member_count"],
                "iconUrl": r["icon_url"],
                "joinedAt": r["first_seen_at"][:10] if r["first_seen_at"] else None,
            }
            for r in current
        ],
        "added": added,
        "removed": removed,
    }


@app.get("/api/servers/{guild_id}/members.csv")
def download_members_csv(guild_id: str):
    conn = get_conn()
    server = conn.execute("SELECT name FROM servers WHERE guild_id = ?", (guild_id,)).fetchone()
    if not server:
        conn.close()
        raise HTTPException(status_code=404, detail="Server not found")

    members = conn.execute(
        "SELECT user_id, username, display_name, is_bot FROM members WHERE guild_id = ? ORDER BY username",
        (guild_id,),
    ).fetchall()
    conn.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "username", "display_name", "is_bot"])
    for m in members:
        writer.writerow([m["user_id"], m["username"], m["display_name"], bool(m["is_bot"])])
    buffer.seek(0)

    filename = f"{sanitize_filename(server['name'])}_members.csv"
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/lookup")
def lookup(query: str = Query(..., min_length=1)):
    """
    Looks up a member by exact Discord ID or partial username/display name
    match, across every server the bot is in.
    """
    conn = get_conn()
    q = query.strip()
    rows = conn.execute(
        """
        SELECT m.user_id, m.username, m.display_name, m.is_bot, m.guild_id, s.name AS server_name
        FROM members m
        JOIN servers s ON s.guild_id = m.guild_id
        WHERE m.user_id = ?
           OR m.username LIKE ?
           OR m.display_name LIKE ?
        ORDER BY s.name, m.username
        """,
        (q, f"%{q}%", f"%{q}%"),
    ).fetchall()
    conn.close()

    return {
        "results": [
            {
                "userId": r["user_id"],
                "username": r["username"],
                "displayName": r["display_name"],
                "isBot": bool(r["is_bot"]),
                "serverId": r["guild_id"],
                "serverName": r["server_name"],
            }
            for r in rows
        ]
    }
