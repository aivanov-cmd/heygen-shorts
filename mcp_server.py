import os
import hmac
import psycopg2
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


DATABASE_URL = os.environ.get("DATABASE_URL")
MCP_API_KEY = os.environ.get("MCP_API_KEY")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")

if not MCP_API_KEY:
    raise RuntimeError("MCP_API_KEY is not configured")


def get_db():
    return psycopg2.connect(DATABASE_URL)


RAILWAY_HOST = "diplomatic-vitality-production-e565.up.railway.app"

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        RAILWAY_HOST,
        f"{RAILWAY_HOST}:*",
    ],
    allowed_origins=[
        f"https://{RAILWAY_HOST}",
        f"https://{RAILWAY_HOST}:*",
    ],
)

mcp = FastMCP(
    "HeyGen Shorts Queue",
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
)


@mcp.tool()
def get_approved_shorts(limit: int = 30) -> dict:
    """Get approved Shorts waiting for HeyGen generation. Read-only."""
    limit = max(1, min(limit, 30))

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT id, topic, script, avatar_id, voice_id, status
            FROM shorts
            WHERE status = 'approved'
              AND heygen_video_id IS NULL
            ORDER BY id ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    shorts = []

    for row in rows:
        shorts.append({
            "id": row[0],
            "topic": row[1],
            "script": row[2],
            "avatar_id": row[3],
            "voice_id": row[4],
            "status": row[5],
        })

    return {
        "count": len(shorts),
        "shorts": shorts,
    }


@mcp.tool()
def get_short(short_id: int) -> dict:
    """Get one Short by ID. Read-only."""

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT id, topic, script, avatar_id, voice_id, status,
                   heygen_video_id, video_url, subtitle_url, last_error
            FROM shorts
            WHERE id = %s
            """,
            (short_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        return {
            "found": False,
            "error": "Short not found",
        }

    return {
        "found": True,
        "short": {
            "id": row[0],
            "topic": row[1],
            "script": row[2],
            "avatar_id": row[3],
            "voice_id": row[4],
            "status": row[5],
            "heygen_video_id": row[6],
            "video_url": row[7],
            "subtitle_url": row[8],
            "last_error": row[9],
        },
    }


@mcp.tool()
def get_queue_stats() -> dict:
    """Get queue statistics. Read-only."""

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT status, COUNT(*)
            FROM shorts
            GROUP BY status
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    stats = {
        "ready": 0,
        "approved": 0,
        "generating": 0,
        "completed": 0,
        "failed": 0,
    }

    total = 0

    for status, count in rows:
        stats[status] = count
        total += count

    return {
        "total": total,
        **stats,
    }


class APIKeyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        supplied_key = headers.get("x-api-key", "")

        if not hmac.compare_digest(supplied_key, MCP_API_KEY):
            body = b'{"error":"Unauthorized"}'

            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })

            await send({
                "type": "http.response.body",
                "body": body,
            })
            return

        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    registered_tools = mcp._tool_manager.list_tools()

    print("REGISTERED MCP TOOLS:", flush=True)

    for tool in registered_tools:
        print(f"- {tool.name}", flush=True)

    mcp_app = mcp.streamable_http_app()

    app = APIKeyMiddleware(mcp_app)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
