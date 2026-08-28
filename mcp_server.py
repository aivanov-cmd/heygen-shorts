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


# =========================================================
# READ-ONLY TOOLS
# =========================================================

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


# =========================================================
# WRITE TOOLS
# =========================================================

@mcp.tool()
def mark_generating(short_id: int) -> dict:
    """
    Mark an approved Short as generating.
    Only approved -> generating is allowed.
    """

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            UPDATE shorts
            SET status = 'generating',
                last_error = NULL,
                updated_at = NOW()
            WHERE id = %s
              AND status = 'approved'
              AND heygen_video_id IS NULL
            RETURNING id, status
            """,
            (short_id,),
        )

        row = cur.fetchone()

        if not row:
            conn.rollback()
            return {
                "success": False,
                "error": "Short not found or is not eligible for approved -> generating transition",
                "short_id": short_id,
            }

        conn.commit()

        return {
            "success": True,
            "short_id": row[0],
            "status": row[1],
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


@mcp.tool()
def mark_completed(
    short_id: int,
    heygen_video_id: str,
    video_url: str,
    subtitle_url: str | None = None,
) -> dict:
    """
    Mark a generating Short as completed and save HeyGen result.
    Only generating -> completed is allowed.
    """

    if not heygen_video_id.strip():
        return {
            "success": False,
            "error": "heygen_video_id cannot be empty",
            "short_id": short_id,
        }

    if not video_url.strip():
        return {
            "success": False,
            "error": "video_url cannot be empty",
            "short_id": short_id,
        }

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            UPDATE shorts
            SET status = 'completed',
                heygen_video_id = %s,
                video_url = %s,
                subtitle_url = %s,
                last_error = NULL,
                updated_at = NOW()
            WHERE id = %s
              AND status = 'generating'
            RETURNING id, status, heygen_video_id, video_url, subtitle_url
            """,
            (
                heygen_video_id,
                video_url,
                subtitle_url,
                short_id,
            ),
        )

        row = cur.fetchone()

        if not row:
            conn.rollback()
            return {
                "success": False,
                "error": "Short not found or is not eligible for generating -> completed transition",
                "short_id": short_id,
            }

        conn.commit()

        return {
            "success": True,
            "short_id": row[0],
            "status": row[1],
            "heygen_video_id": row[2],
            "video_url": row[3],
            "subtitle_url": row[4],
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


@mcp.tool()
def mark_failed(short_id: int, error_message: str) -> dict:
    """
    Mark a generating Short as failed and save the error.
    Only generating -> failed is allowed.
    """

    if not error_message.strip():
        return {
            "success": False,
            "error": "error_message cannot be empty",
            "short_id": short_id,
        }

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            UPDATE shorts
            SET status = 'failed',
                last_error = %s,
                updated_at = NOW()
            WHERE id = %s
              AND status = 'generating'
            RETURNING id, status, last_error
            """,
            (
                error_message[:2000],
                short_id,
            ),
        )

        row = cur.fetchone()

        if not row:
            conn.rollback()
            return {
                "success": False,
                "error": "Short not found or is not eligible for generating -> failed transition",
                "short_id": short_id,
            }

        conn.commit()

        return {
            "success": True,
            "short_id": row[0],
            "status": row[1],
            "last_error": row[2],
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


# =========================================================
# API KEY PROTECTION
# =========================================================

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
