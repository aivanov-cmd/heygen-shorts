import os
import psycopg2

from mcp.server.fastmcp import FastMCP


DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")


mcp = FastMCP(
    "HeyGen Shorts Queue",
    stateless_http=True,
    json_response=True
)


def get_db():
    return psycopg2.connect(DATABASE_URL)


@mcp.tool()
def get_approved_shorts(limit: int = 30) -> dict:
    """
    Get approved Shorts waiting for HeyGen generation.
    Read-only.
    """

    limit = max(1, min(limit, 30))

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            id,
            topic,
            script,
            avatar_id,
            voice_id,
            status
        FROM shorts
        WHERE status = 'approved'
          AND heygen_video_id IS NULL
        ORDER BY id ASC
        LIMIT %s
        """,
        (limit,)
    )

    rows = cur.fetchall()

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
            "status": row[5]
        })

    return {
        "count": len(shorts),
        "shorts": shorts
    }


@mcp.tool()
def get_short(short_id: int) -> dict:
    """
    Get one Short by ID.
    Read-only.
    """

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            id,
            topic,
            script,
            avatar_id,
            voice_id,
            status,
            heygen_video_id,
            video_url,
            subtitle_url,
            last_error
        FROM shorts
        WHERE id = %s
        """,
        (short_id,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return {
            "found": False,
            "error": "Short not found"
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
            "last_error": row[9]
        }
    }


@mcp.tool()
def get_queue_stats() -> dict:
    """
    Get queue statistics.
    Read-only.
    """

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT status, COUNT(*)
        FROM shorts
        GROUP BY status
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    stats = {
        "ready": 0,
        "approved": 0,
        "generating": 0,
        "completed": 0,
        "failed": 0
    }

    total = 0

    for status, count in rows:
        stats[status] = count
        total += count

    return {
        "total": total,
        **stats
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        mcp.streamable_http_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
    )
