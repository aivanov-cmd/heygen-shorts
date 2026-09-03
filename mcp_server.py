import os
import hmac
import psycopg2

from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


# =========================================================
# CONFIG
# =========================================================

DATABASE_URL = os.environ.get("DATABASE_URL")
MCP_API_KEY = os.environ.get("MCP_API_KEY")
VIDEO_DOWNLOAD_API_KEY = os.environ.get("VIDEO_DOWNLOAD_API_KEY")


if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")

if not MCP_API_KEY:
    raise RuntimeError("MCP_API_KEY is not configured")

if not VIDEO_DOWNLOAD_API_KEY:
    raise RuntimeError("VIDEO_DOWNLOAD_API_KEY is not configured")


def get_db():
    return psycopg2.connect(DATABASE_URL)


# =========================================================
# MCP SERVER
# =========================================================

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
    """
    Get approved Shorts waiting for HeyGen generation.
    Read-only.
    """

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
    """
    Get one Short by ID.
    Read-only.
    """

    conn = get_db()
    cur = conn.cursor()

    try:
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
    """
    Get queue statistics.
    Read-only.
    """

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
# CREATE BATCH
# =========================================================

@mcp.tool()
def create_shorts_batch(shorts: list[dict]) -> dict:
    """
    Create new approved Shorts in PostgreSQL.

    Required fields for every Short:
    - topic
    - script
    - avatar_id
    - voice_id

    Maximum 30 Shorts per request.

    This tool only creates database records.
    It does NOT call HeyGen and does NOT generate videos.
    """

    if not shorts:
        return {
            "success": False,
            "error": "shorts list cannot be empty",
        }

    if len(shorts) > 30:
        return {
            "success": False,
            "error": "Maximum 30 Shorts per batch",
        }

    validated = []

    for index, item in enumerate(shorts, start=1):

        topic = str(item.get("topic", "")).strip()
        script = str(item.get("script", "")).strip()
        avatar_id = str(item.get("avatar_id", "")).strip()
        voice_id = str(item.get("voice_id", "")).strip()

        if not topic:
            return {
                "success": False,
                "error": f"Short #{index}: topic is required",
            }

        if not script:
            return {
                "success": False,
                "error": f"Short #{index}: script is required",
            }

        if not avatar_id:
            return {
                "success": False,
                "error": f"Short #{index}: avatar_id is required",
            }

        if not voice_id:
            return {
                "success": False,
                "error": f"Short #{index}: voice_id is required",
            }

        validated.append({
            "topic": topic,
            "script": script,
            "avatar_id": avatar_id,
            "voice_id": voice_id,
        })

    conn = get_db()
    cur = conn.cursor()

    created = []

    try:

        for item in validated:

            cur.execute(
                """
                INSERT INTO shorts (
                    topic,
                    script,
                    avatar_id,
                    voice_id,
                    status,
                    heygen_video_id,
                    video_url,
                    subtitle_url,
                    last_error,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    'approved',
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NOW(),
                    NOW()
                )
                RETURNING
                    id,
                    topic,
                    status,
                    avatar_id,
                    voice_id
                """,
                (
                    item["topic"],
                    item["script"],
                    item["avatar_id"],
                    item["voice_id"],
                ),
            )

            row = cur.fetchone()

            created.append({
                "id": row[0],
                "topic": row[1],
                "status": row[2],
                "avatar_id": row[3],
                "voice_id": row[4],
            })

        conn.commit()

        return {
            "success": True,
            "count": len(created),
            "shorts": created,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


# =========================================================
# STATUS WRITE TOOLS
# =========================================================

@mcp.tool()
def mark_generating(short_id: int) -> dict:
    """
    Mark an approved Short as generating.

    Allowed transition:
    approved -> generating
    """

    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            UPDATE shorts
            SET
                status = 'generating',
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
                "error": (
                    "Short not found or is not eligible for "
                    "approved -> generating transition"
                ),
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
    Mark a generating Short as completed
    and save the HeyGen result.

    Allowed transition:
    generating -> completed

    IMPORTANT:
    For videos requiring burned-in subtitles,
    pass HeyGen captioned_video_url as video_url.
    """

    heygen_video_id = heygen_video_id.strip()
    video_url = video_url.strip()

    if not heygen_video_id:
        return {
            "success": False,
            "error": "heygen_video_id cannot be empty",
            "short_id": short_id,
        }

    if not video_url:
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
            SET
                status = 'completed',
                heygen_video_id = %s,
                video_url = %s,
                subtitle_url = %s,
                last_error = NULL,
                updated_at = NOW()
            WHERE id = %s
              AND status = 'generating'
            RETURNING
                id,
                status,
                heygen_video_id,
                video_url,
                subtitle_url
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
                "error": (
                    "Short not found or is not eligible for "
                    "generating -> completed transition"
                ),
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
def mark_failed(
    short_id: int,
    error_message: str,
) -> dict:
    """
    Mark a generating Short as failed
    and save the error.

    Allowed transition:
    generating -> failed
    """

    error_message = error_message.strip()

    if not error_message:
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
            SET
                status = 'failed',
                last_error = %s,
                updated_at = NOW()
            WHERE id = %s
              AND status = 'generating'
            RETURNING
                id,
                status,
                last_error
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
                "error": (
                    "Short not found or is not eligible for "
                    "generating -> failed transition"
                ),
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
# VIDEO DOWNLOAD TO RAILWAY VOLUME
# =========================================================

@mcp.tool()
def download_completed_video(short_id: int) -> dict:
    """
    Download the completed captioned MP4 stored in video_url.

    Read-only for PostgreSQL.
    Does NOT call HeyGen generation.
    Does NOT change Short status.
    """

    import urllib.request

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT id, status, video_url
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
            "success": False,
            "error": "Short not found",
            "short_id": short_id,
        }

    short_id_db, status, video_url = row

    if status != "completed":
        return {
            "success": False,
            "error": "Short is not completed",
            "short_id": short_id,
            "status": status,
        }

    if not video_url:
        return {
            "success": False,
            "error": "video_url is empty",
            "short_id": short_id,
        }

    download_dir = Path("/videos")
    download_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_path = download_dir / f"short_{short_id_db}.mp4"

    try:
        request = urllib.request.Request(
            video_url,
            headers={
                "User-Agent": "Mozilla/5.0",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:

            with open(file_path, "wb") as output:

                while True:
                    chunk = response.read(1024 * 1024)

                    if not chunk:
                        break

                    output.write(chunk)

    except Exception as exc:

        if file_path.exists():
            file_path.unlink()

        return {
            "success": False,
            "error": f"Download failed: {exc}",
            "short_id": short_id,
        }

    file_size = file_path.stat().st_size

    return {
        "success": True,
        "short_id": short_id,
        "file_name": file_path.name,
        "file_path": str(file_path),
        "size_bytes": file_size,
        "size_mb": round(file_size / 1024 / 1024, 2),
        "temporary": False,
        "persistent_storage": True,
    }


# =========================================================
# API KEY PROTECTION + VIDEO DELIVERY FOR N8N
# =========================================================

class APIKeyMiddleware:
    def __init__(self, app):
        self.app = app

    async def send_json(
        self,
        send,
        status_code: int,
        body: bytes,
    ):
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (
                    b"content-type",
                    b"application/json",
                ),
                (
                    b"content-length",
                    str(len(body)).encode(),
                ),
            ],
        })

        await send({
            "type": "http.response.body",
            "body": body,
        })

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower():
                value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        supplied_key = headers.get("x-api-key", "")
        path = scope.get("path", "")

        # =====================================================
        # VIDEO ENDPOINT FOR N8N
        #
        # Example:
        # GET /video/5
        #
        # Returns:
        # /videos/short_5.mp4
        # =====================================================

        if path.startswith("/video/"):

            if not hmac.compare_digest(
                supplied_key,
                VIDEO_DOWNLOAD_API_KEY,
            ):
                await self.send_json(
                    send,
                    401,
                    b'{"error":"Unauthorized"}',
                )
                return

            short_id_text = path[len("/video/"):].strip("/")

            if not short_id_text.isdigit():
                await self.send_json(
                    send,
                    400,
                    b'{"error":"Invalid short_id"}',
                )
                return

            short_id = int(short_id_text)

            file_path = Path(
                f"/videos/short_{short_id}.mp4"
            )

            if not file_path.is_file():
                await self.send_json(
                    send,
                    404,
                    b'{"error":"Video not found"}',
                )
                return

            file_size = file_path.stat().st_size

            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (
                        b"content-type",
                        b"video/mp4",
                    ),
                    (
                        b"content-length",
                        str(file_size).encode(),
                    ),
                    (
                        b"content-disposition",
                        (
                            f'attachment; '
                            f'filename="short_{short_id}.mp4"'
                        ).encode(),
                    ),
                ],
            })

            with open(file_path, "rb") as video_file:

                while True:
                    chunk = video_file.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    await send({
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    })

            await send({
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            })

            return

        # =====================================================
        # NORMAL MCP AUTH
        # =====================================================

        if not hmac.compare_digest(
            supplied_key,
            MCP_API_KEY,
        ):
            await self.send_json(
                send,
                401,
                b'{"error":"Unauthorized"}',
            )
            return

        await self.app(
            scope,
            receive,
            send,
        )


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    import uvicorn

    registered_tools = mcp._tool_manager.list_tools()

    print(
        "REGISTERED MCP TOOLS:",
        flush=True,
    )

    for tool in registered_tools:
        print(
            f"- {tool.name}",
            flush=True,
        )

    mcp_app = mcp.streamable_http_app()

    app = APIKeyMiddleware(
        mcp_app,
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                8000,
            )
        ),
    )
