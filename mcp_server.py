import os
import hmac
import json
import time
import hashlib
import urllib.request
import urllib.error
import urllib.parse

from datetime import datetime, timezone

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

N8N_YOUTUBE_WEBHOOK_URL = os.environ.get("N8N_YOUTUBE_WEBHOOK_URL")
N8N_YOUTUBE_WEBHOOK_KEY = os.environ.get("N8N_YOUTUBE_WEBHOOK_KEY")

N8N_YOUTUBE_WEBHOOK_HEADER = os.environ.get(
    "N8N_YOUTUBE_WEBHOOK_HEADER",
    "x-publish-key",
)

N8N_INSTAGRAM_WEBHOOK_URL = os.environ.get(
    "N8N_INSTAGRAM_WEBHOOK_URL"
)

N8N_INSTAGRAM_WEBHOOK_KEY = os.environ.get(
    "N8N_INSTAGRAM_WEBHOOK_KEY"
)

N8N_INSTAGRAM_WEBHOOK_HEADER = os.environ.get(
    "N8N_INSTAGRAM_WEBHOOK_HEADER",
    "x-instagram-publish-key",
)

INSTAGRAM_VIDEO_URL_TTL_SECONDS = int(
    os.environ.get(
        "INSTAGRAM_VIDEO_URL_TTL_SECONDS",
        "21600",
    )
)


if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured"
    )

if not MCP_API_KEY:
    raise RuntimeError(
        "MCP_API_KEY is not configured"
    )

if not VIDEO_DOWNLOAD_API_KEY:
    raise RuntimeError(
        "VIDEO_DOWNLOAD_API_KEY is not configured"
    )

if not N8N_YOUTUBE_WEBHOOK_URL:
    raise RuntimeError(
        "N8N_YOUTUBE_WEBHOOK_URL is not configured"
    )

if not N8N_YOUTUBE_WEBHOOK_KEY:
    raise RuntimeError(
        "N8N_YOUTUBE_WEBHOOK_KEY is not configured"
    )


def get_db():
    return psycopg2.connect(
        DATABASE_URL
    )


# =========================================================
# DATABASE SCHEMA
# =========================================================

def ensure_database_schema():
    """
    Add YouTube, Instagram and scheduling columns if missing.
    Safe to run on every service start.
    """

    conn = get_db()
    cur = conn.cursor()

    try:

        # -------------------------
        # YouTube
        # -------------------------

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS youtube_title TEXT
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS youtube_description TEXT
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS youtube_video_id TEXT
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS youtube_status TEXT
            DEFAULT 'pending'
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS youtube_last_error TEXT
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS youtube_publish_at TIMESTAMPTZ
            """
        )

        cur.execute(
            """
            UPDATE shorts
            SET youtube_status = 'pending'
            WHERE youtube_status IS NULL
            """
        )

        # -------------------------
        # Instagram
        # -------------------------

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS instagram_caption TEXT
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS instagram_media_id TEXT
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS instagram_status TEXT
            DEFAULT 'pending'
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS instagram_last_error TEXT
            """
        )

        cur.execute(
            """
            ALTER TABLE shorts
            ADD COLUMN IF NOT EXISTS instagram_publish_at TIMESTAMPTZ
            """
        )

        cur.execute(
            """
            UPDATE shorts
            SET instagram_status = 'pending'
            WHERE instagram_status IS NULL
            """
        )

        # -------------------------
        # Scheduling indexes
        # -------------------------

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_shorts_youtube_publish_at
            ON shorts (
                youtube_publish_at
            )
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_shorts_instagram_publish_at
            ON shorts (
                instagram_publish_at
            )
            """
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


# =========================================================
# MCP SERVER
# =========================================================

RAILWAY_HOST = (
    "diplomatic-vitality-production-e565.up.railway.app"
)

RAILWAY_BASE_URL = (
    f"https://{RAILWAY_HOST}"
)

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
# INSTAGRAM SIGNED VIDEO URL
# =========================================================

def create_instagram_video_signature(
    short_id: int,
    expires: int,
) -> str:

    if not N8N_INSTAGRAM_WEBHOOK_KEY:
        raise RuntimeError(
            "N8N_INSTAGRAM_WEBHOOK_KEY "
            "is not configured"
        )

    message = (
        f"{short_id}:{expires}"
    ).encode("utf-8")

    return hmac.new(
        N8N_INSTAGRAM_WEBHOOK_KEY.encode(
            "utf-8"
        ),
        message,
        hashlib.sha256,
    ).hexdigest()


def create_instagram_video_url(
    short_id: int,
) -> dict:

    expires = (
        int(time.time())
        + INSTAGRAM_VIDEO_URL_TTL_SECONDS
    )

    signature = (
        create_instagram_video_signature(
            short_id,
            expires,
        )
    )

    url = (
        f"{RAILWAY_BASE_URL}"
        f"/instagram-video/{short_id}"
        f"?expires={expires}"
        f"&signature={signature}"
    )

    return {
        "url": url,
        "expires": expires,
        "ttl_seconds":
            INSTAGRAM_VIDEO_URL_TTL_SECONDS,
    }


# =========================================================
# READ-ONLY TOOLS
# =========================================================

@mcp.tool()
def get_approved_shorts(
    limit: int = 30,
) -> dict:
    """
    Get approved Shorts waiting for HeyGen generation.
    Read-only.
    """

    limit = max(
        1,
        min(limit, 30),
    )

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
                youtube_title,
                youtube_description,
                youtube_status,
                instagram_caption,
                instagram_status
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
            "youtube_title": row[6],
            "youtube_description": row[7],
            "youtube_status": row[8],
            "instagram_caption": row[9],
            "instagram_status": row[10],
        })

    return {
        "count": len(shorts),
        "shorts": shorts,
    }


@mcp.tool()
def get_short(
    short_id: int,
) -> dict:
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
                last_error,
                youtube_title,
                youtube_description,
                youtube_video_id,
                youtube_status,
                youtube_last_error,
                instagram_caption,
                instagram_media_id,
                instagram_status,
                instagram_last_error
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

            "youtube_title": row[10],
            "youtube_description": row[11],
            "youtube_video_id": row[12],
            "youtube_status": row[13],
            "youtube_last_error": row[14],

            "instagram_caption": row[15],
            "instagram_media_id": row[16],
            "instagram_status": row[17],
            "instagram_last_error": row[18],
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

        cur.execute(
            """
            SELECT youtube_status, COUNT(*)
            FROM shorts
            GROUP BY youtube_status
            """
        )

        youtube_rows = cur.fetchall()

        cur.execute(
            """
            SELECT instagram_status, COUNT(*)
            FROM shorts
            GROUP BY instagram_status
            """
        )

        instagram_rows = cur.fetchall()

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

    youtube_stats = {
        "pending": 0,
        "requested": 0,
        "published": 0,
        "failed": 0,
    }

    for status, count in youtube_rows:
        if status:
            youtube_stats[status] = count

    instagram_stats = {
        "pending": 0,
        "requested": 0,
        "published": 0,
        "failed": 0,
    }

    for status, count in instagram_rows:
        if status:
            instagram_stats[status] = count

    return {
        "total": total,
        **stats,
        "youtube": youtube_stats,
        "instagram": instagram_stats,
    }


# =========================================================
# CREATE BATCH
# =========================================================

@mcp.tool()
def create_shorts_batch(
    shorts: list[dict],
) -> dict:
    """
    Create new approved Shorts in PostgreSQL.

    Required:
    - topic
    - script
    - avatar_id
    - voice_id
    - youtube_title
    - youtube_description

    Optional:
    - instagram_caption

    If instagram_caption is omitted,
    youtube_description is used.

    Maximum 30 Shorts per request.

    Does NOT generate or publish anything.
    """

    if not shorts:
        return {
            "success": False,
            "error":
                "shorts list cannot be empty",
        }

    if len(shorts) > 30:
        return {
            "success": False,
            "error":
                "Maximum 30 Shorts per batch",
        }

    validated = []

    for index, item in enumerate(
        shorts,
        start=1,
    ):

        topic = str(
            item.get("topic", "")
        ).strip()

        script = str(
            item.get("script", "")
        ).strip()

        avatar_id = str(
            item.get("avatar_id", "")
        ).strip()

        voice_id = str(
            item.get("voice_id", "")
        ).strip()

        youtube_title = str(
            item.get(
                "youtube_title",
                "",
            )
        ).strip()

        youtube_description = str(
            item.get(
                "youtube_description",
                "",
            )
        ).strip()

        instagram_caption = str(
            item.get(
                "instagram_caption",
                "",
            )
        ).strip()

        if not topic:
            return {
                "success": False,
                "error":
                    f"Short #{index}: "
                    "topic is required",
            }

        if not script:
            return {
                "success": False,
                "error":
                    f"Short #{index}: "
                    "script is required",
            }

        if not avatar_id:
            return {
                "success": False,
                "error":
                    f"Short #{index}: "
                    "avatar_id is required",
            }

        if not voice_id:
            return {
                "success": False,
                "error":
                    f"Short #{index}: "
                    "voice_id is required",
            }

        if not youtube_title:
            return {
                "success": False,
                "error":
                    f"Short #{index}: "
                    "youtube_title is required",
            }

        if not youtube_description:
            return {
                "success": False,
                "error":
                    f"Short #{index}: "
                    "youtube_description is required",
            }

        if len(youtube_title) > 100:
            return {
                "success": False,
                "error":
                    f"Short #{index}: "
                    "youtube_title must be "
                    "100 characters or less",
            }

        if not instagram_caption:
            instagram_caption = (
                youtube_description
            )

        validated.append({
            "topic": topic,
            "script": script,
            "avatar_id": avatar_id,
            "voice_id": voice_id,
            "youtube_title":
                youtube_title,
            "youtube_description":
                youtube_description,
            "instagram_caption":
                instagram_caption,
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

                    youtube_title,
                    youtube_description,
                    youtube_video_id,
                    youtube_status,
                    youtube_last_error,

                    instagram_caption,
                    instagram_media_id,
                    instagram_status,
                    instagram_last_error,

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

                    %s,
                    %s,
                    NULL,
                    'pending',
                    NULL,

                    %s,
                    NULL,
                    'pending',
                    NULL,

                    NOW(),
                    NOW()
                )
                RETURNING
                    id,
                    topic,
                    status,
                    avatar_id,
                    voice_id,
                    youtube_title,
                    youtube_description,
                    youtube_status,
                    instagram_caption,
                    instagram_status
                """,
                (
                    item["topic"],
                    item["script"],
                    item["avatar_id"],
                    item["voice_id"],
                    item["youtube_title"],
                    item["youtube_description"],
                    item["instagram_caption"],
                ),
            )

            row = cur.fetchone()

            created.append({
                "id": row[0],
                "topic": row[1],
                "status": row[2],
                "avatar_id": row[3],
                "voice_id": row[4],
                "youtube_title": row[5],
                "youtube_description": row[6],
                "youtube_status": row[7],
                "instagram_caption": row[8],
                "instagram_status": row[9],
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
def mark_generating(
    short_id: int,
) -> dict:
    """
    Mark an approved Short as generating.

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
                    "Short not found or is not "
                    "eligible for approved -> "
                    "generating transition"
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
    Mark generating Short as completed.

    generating -> completed

    For burned subtitles use
    captioned_video_url as video_url.
    """

    heygen_video_id = (
        heygen_video_id.strip()
    )

    video_url = video_url.strip()

    if not heygen_video_id:
        return {
            "success": False,
            "error":
                "heygen_video_id cannot be empty",
            "short_id": short_id,
        }

    if not video_url:
        return {
            "success": False,
            "error":
                "video_url cannot be empty",
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
                    "Short not found or is not "
                    "eligible for generating -> "
                    "completed transition"
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
    Mark generating Short as failed.

    generating -> failed
    """

    error_message = (
        error_message.strip()
    )

    if not error_message:
        return {
            "success": False,
            "error":
                "error_message cannot be empty",
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
                    "Short not found or is not "
                    "eligible for generating -> "
                    "failed transition"
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
def download_completed_video(
    short_id: int,
) -> dict:
    """
    Download captioned completed MP4
    to persistent Railway Volume.
    """

    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            SELECT
                id,
                status,
                video_url
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

    (
        short_id_db,
        status,
        video_url,
    ) = row

    if status != "completed":
        return {
            "success": False,
            "error":
                "Short is not completed",
            "short_id": short_id,
            "status": status,
        }

    if not video_url:
        return {
            "success": False,
            "error": "video_url is empty",
            "short_id": short_id,
        }

    download_dir = Path(
        "/videos"
    )

    download_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_path = (
        download_dir
        / f"short_{short_id_db}.mp4"
    )

    try:

        request = urllib.request.Request(
            video_url,
            headers={
                "User-Agent":
                    "Mozilla/5.0",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:

            with open(
                file_path,
                "wb",
            ) as output:

                while True:

                    chunk = response.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    output.write(chunk)

    except Exception as exc:

        if file_path.exists():
            file_path.unlink()

        return {
            "success": False,
            "error":
                f"Download failed: {exc}",
            "short_id": short_id,
        }

    file_size = (
        file_path.stat().st_size
    )

    return {
        "success": True,
        "short_id": short_id,
        "file_name": file_path.name,
        "file_path": str(file_path),
        "size_bytes": file_size,
        "size_mb": round(
            file_size
            / 1024
            / 1024,
            2,
        ),
        "temporary": False,
        "persistent_storage": True,
    }

# =========================================================
# PUBLICATION SCHEDULING
# =========================================================

def parse_publish_at(
    publish_at: str,
):
    """
    Parse ISO-8601 datetime with timezone.

    Examples:
    2026-09-08T18:00:00+03:00
    2026-09-08T15:00:00Z
    """

    value = str(
        publish_at
    ).strip()

    if not value:
        raise ValueError(
            "publish_at cannot be empty"
        )

    if value.endswith("Z"):
        value = (
            value[:-1]
            + "+00:00"
        )

    parsed = datetime.fromisoformat(
        value
    )

    if parsed.tzinfo is None:
        raise ValueError(
            "publish_at must include timezone, "
            "for example +03:00"
        )

    return parsed


@mcp.tool()
def schedule_publication(
    short_id: int,
    platform: str,
    publish_at: str,
) -> dict:
    """
    Schedule one completed Short.

    platform:
    - youtube
    - instagram

    publish_at:
    ISO-8601 datetime with timezone.

    Example:
    2026-09-08T18:00:00+03:00

    Does NOT publish immediately.
    """

    platform = str(
        platform
    ).strip().lower()

    if platform not in {
        "youtube",
        "instagram",
    }:
        return {
            "success": False,
            "error": (
                "platform must be "
                "'youtube' or 'instagram'"
            ),
        }

    try:
        publish_datetime = (
            parse_publish_at(
                publish_at
            )
        )

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            SELECT
                id,
                status,
                youtube_status,
                instagram_status
            FROM shorts
            WHERE id = %s
            """,
            (short_id,),
        )

        row = cur.fetchone()

        if not row:
            return {
                "success": False,
                "error": "Short not found",
                "short_id": short_id,
            }

        (
            short_id_db,
            status,
            youtube_status,
            instagram_status,
        ) = row

        if status != "completed":
            return {
                "success": False,
                "error": (
                    "Only completed Shorts "
                    "can be scheduled"
                ),
                "short_id": short_id,
                "status": status,
            }

        file_path = Path(
            f"/videos/short_{short_id_db}.mp4"
        )

        if not file_path.is_file():
            return {
                "success": False,
                "error": (
                    "Video file is not stored "
                    "in Railway Volume"
                ),
                "short_id": short_id,
            }

        if platform == "youtube":

            if youtube_status == "published":
                return {
                    "success": False,
                    "error": (
                        "Short is already "
                        "published on YouTube"
                    ),
                    "short_id": short_id,
                }

            if youtube_status == "requested":
                return {
                    "success": False,
                    "error": (
                        "YouTube publication "
                        "is already in progress"
                    ),
                    "short_id": short_id,
                }

            cur.execute(
                """
                UPDATE shorts
                SET
                    youtube_publish_at = %s,
                    youtube_status = 'scheduled',
                    youtube_last_error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING
                    id,
                    youtube_publish_at,
                    youtube_status
                """,
                (
                    publish_datetime,
                    short_id,
                ),
            )

        else:

            if instagram_status == "published":
                return {
                    "success": False,
                    "error": (
                        "Short is already "
                        "published on Instagram"
                    ),
                    "short_id": short_id,
                }

            if instagram_status == "requested":
                return {
                    "success": False,
                    "error": (
                        "Instagram publication "
                        "is already in progress"
                    ),
                    "short_id": short_id,
                }

            cur.execute(
                """
                UPDATE shorts
                SET
                    instagram_publish_at = %s,
                    instagram_status = 'scheduled',
                    instagram_last_error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING
                    id,
                    instagram_publish_at,
                    instagram_status
                """,
                (
                    publish_datetime,
                    short_id,
                ),
            )

        updated = cur.fetchone()

        conn.commit()

        return {
            "success": True,
            "short_id": updated[0],
            "platform": platform,
            "publish_at": (
                updated[1].isoformat()
            ),
            "status": updated[2],
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


@mcp.tool()
def get_due_publications(
    limit: int = 30,
) -> dict:
    """
    Return scheduled publications
    whose publish time has arrived.

    Read-only.
    Does NOT publish anything.
    """

    limit = max(
        1,
        min(limit, 30),
    )

    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            SELECT *
            FROM (
                SELECT
                    id,
                    topic,
                    'youtube' AS platform,
                    youtube_publish_at AS publish_at
                FROM shorts
                WHERE status = 'completed'
                  AND youtube_status = 'scheduled'
                  AND youtube_publish_at IS NOT NULL
                  AND youtube_publish_at <= NOW()

                UNION ALL

                SELECT
                    id,
                    topic,
                    'instagram' AS platform,
                    instagram_publish_at AS publish_at
                FROM shorts
                WHERE status = 'completed'
                  AND instagram_status = 'scheduled'
                  AND instagram_publish_at IS NOT NULL
                  AND instagram_publish_at <= NOW()
            ) AS due
            ORDER BY publish_at ASC
            LIMIT %s
            """,
            (limit,),
        )

        rows = cur.fetchall()

    finally:
        cur.close()
        conn.close()

    publications = []

    for row in rows:

        publications.append({
            "short_id": row[0],
            "topic": row[1],
            "platform": row[2],
            "publish_at": (
                row[3].isoformat()
                if row[3]
                else None
            ),
        })

    return {
        "count": len(
            publications
        ),
        "publications":
            publications,
        "checked_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
    }

# =========================================================
# YOUTUBE PUBLISH VIA N8N
# =========================================================

@mcp.tool()
def publish_to_youtube(
    short_id: int,
    force: bool = False,
) -> dict:
    """
    Send completed Short to protected
    n8n YouTube workflow.

    Successful response only means
    n8n accepted the request.

    Final success comes through callback.
    """

    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            SELECT
                id,
                status,
                youtube_title,
                youtube_description,
                youtube_video_id,
                youtube_status
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
            "error":
                "Short not found",
            "short_id": short_id,
        }

    (
        short_id_db,
        status,
        youtube_title,
        youtube_description,
        youtube_video_id,
        youtube_status,
    ) = row

    if status != "completed":
        return {
            "success": False,
            "error":
                "Short is not completed",
            "short_id": short_id,
            "status": status,
        }

    file_path = Path(
        f"/videos/short_{short_id_db}.mp4"
    )

    if not file_path.is_file():
        return {
            "success": False,
            "error": (
                "Video file is not stored in "
                "Railway Volume. Run "
                "download_completed_video first."
            ),
            "short_id": short_id,
        }

    if not youtube_title:
        return {
            "success": False,
            "error":
                "youtube_title is empty",
            "short_id": short_id,
        }

    if not youtube_description:
        return {
            "success": False,
            "error":
                "youtube_description is empty",
            "short_id": short_id,
        }

    if not force:

        if youtube_status == "published":
            return {
                "success": False,
                "error":
                    "Short is already published "
                    "on YouTube",
                "short_id": short_id,
                "youtube_video_id":
                    youtube_video_id,
                "youtube_status":
                    youtube_status,
            }

        if youtube_status == "requested":
            return {
                "success": False,
                "error": (
                    "YouTube publication has "
                    "already been requested. "
                    "Use force=true only for "
                    "an intentional retry."
                ),
                "short_id": short_id,
                "youtube_status":
                    youtube_status,
            }

    payload = {
        "short_id": short_id,
        "title": youtube_title,
        "description":
            youtube_description,
    }

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        N8N_YOUTUBE_WEBHOOK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type":
                "application/json",
            N8N_YOUTUBE_WEBHOOK_HEADER:
                N8N_YOUTUBE_WEBHOOK_KEY,
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:

            response_status = (
                response.status
            )

            response_body = (
                response.read(
                    1024 * 1024
                )
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

    except urllib.error.HTTPError as exc:

        error_body = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                UPDATE shorts
                SET
                    youtube_status = 'failed',
                    youtube_last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    (
                        f"n8n HTTP "
                        f"{exc.code}: "
                        f"{error_body}"
                    )[:2000],
                    short_id,
                ),
            )

            conn.commit()

        finally:
            cur.close()
            conn.close()

        return {
            "success": False,
            "error":
                f"n8n HTTP error "
                f"{exc.code}",
            "short_id": short_id,
            "details":
                error_body[:1000],
        }

    except Exception as exc:

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                UPDATE shorts
                SET
                    youtube_status = 'failed',
                    youtube_last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    str(exc)[:2000],
                    short_id,
                ),
            )

            conn.commit()

        finally:
            cur.close()
            conn.close()

        return {
            "success": False,
            "error":
                f"Failed to call n8n: {exc}",
            "short_id": short_id,
        }

    if (
        response_status < 200
        or response_status >= 300
    ):
        return {
            "success": False,
            "error": (
                "Unexpected n8n status "
                f"{response_status}"
            ),
            "short_id": short_id,
        }

    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            UPDATE shorts
            SET
                youtube_status = 'requested',
                youtube_last_error = NULL,
                updated_at = NOW()
            WHERE id = %s
            """,
            (short_id,),
        )

        conn.commit()

    finally:
        cur.close()
        conn.close()

    return {
        "success": True,
        "short_id": short_id,
        "youtube_status":
            "requested",
        "message": (
            "Publication request accepted "
            "by n8n. Waiting for "
            "YouTube callback."
        ),
        "n8n_http_status":
            response_status,
        "n8n_response":
            response_body[:1000],
    }


# =========================================================
# INSTAGRAM PUBLISH VIA N8N
# =========================================================

@mcp.tool()
def publish_to_instagram(
    short_id: int,
    force: bool = False,
) -> dict:
    """
    Send completed Short to protected
    n8n Instagram Reels workflow.

    The video is exposed to Meta through
    a temporary HMAC-signed URL.

    Successful response only means
    n8n accepted the request.

    Final success comes through callback.
    """

    if not N8N_INSTAGRAM_WEBHOOK_URL:
        return {
            "success": False,
            "error": (
                "N8N_INSTAGRAM_WEBHOOK_URL "
                "is not configured"
            ),
        }

    if not N8N_INSTAGRAM_WEBHOOK_KEY:
        return {
            "success": False,
            "error": (
                "N8N_INSTAGRAM_WEBHOOK_KEY "
                "is not configured"
            ),
        }

    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            SELECT
                id,
                topic,
                status,
                youtube_title,
                youtube_description,
                instagram_caption,
                instagram_media_id,
                instagram_status
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
            "error":
                "Short not found",
            "short_id": short_id,
        }

    (
        short_id_db,
        topic,
        status,
        youtube_title,
        youtube_description,
        instagram_caption,
        instagram_media_id,
        instagram_status,
    ) = row

    if status != "completed":
        return {
            "success": False,
            "error":
                "Short is not completed",
            "short_id": short_id,
            "status": status,
        }

    file_path = Path(
        f"/videos/short_{short_id_db}.mp4"
    )

    if not file_path.is_file():
        return {
            "success": False,
            "error": (
                "Video file is not stored in "
                "Railway Volume. Run "
                "download_completed_video first."
            ),
            "short_id": short_id,
        }

    if not force:

        if (
            instagram_status
            == "published"
        ):
            return {
                "success": False,
                "error": (
                    "Short is already "
                    "published on Instagram"
                ),
                "short_id": short_id,
                "instagram_media_id":
                    instagram_media_id,
                "instagram_status":
                    instagram_status,
            }

        if (
            instagram_status
            == "requested"
        ):
            return {
                "success": False,
                "error": (
                    "Instagram publication "
                    "has already been requested. "
                    "Use force=true only for "
                    "an intentional retry."
                ),
                "short_id": short_id,
                "instagram_status":
                    instagram_status,
            }

    final_caption = (
        instagram_caption
        or youtube_description
        or youtube_title
        or topic
        or ""
    ).strip()

    if not final_caption:
        return {
            "success": False,
            "error":
                "Instagram caption is empty",
            "short_id": short_id,
        }

    # Persist fallback caption if needed.
    if not instagram_caption:

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                UPDATE shorts
                SET
                    instagram_caption = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    final_caption,
                    short_id,
                ),
            )

            conn.commit()

        finally:
            cur.close()
            conn.close()

    signed_video = (
        create_instagram_video_url(
            short_id
        )
    )

    payload = {
        "short_id": short_id,
        "caption": final_caption,
        "video_url":
            signed_video["url"],
    }

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    # Mark requested BEFORE calling n8n.
    # This prevents a fast callback from
    # being overwritten by requested later.
    conn = get_db()
    cur = conn.cursor()

    try:

        cur.execute(
            """
            UPDATE shorts
            SET
                instagram_status = 'requested',
                instagram_last_error = NULL,
                updated_at = NOW()
            WHERE id = %s
            """,
            (short_id,),
        )

        conn.commit()

    finally:
        cur.close()
        conn.close()

    request = urllib.request.Request(
        N8N_INSTAGRAM_WEBHOOK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type":
                "application/json",
            N8N_INSTAGRAM_WEBHOOK_HEADER:
                N8N_INSTAGRAM_WEBHOOK_KEY,
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:

            response_status = (
                response.status
            )

            response_body = (
                response.read(
                    1024 * 1024
                )
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

    except urllib.error.HTTPError as exc:

        error_body = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        error_message = (
            f"n8n HTTP {exc.code}: "
            f"{error_body}"
        )[:2000]

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                UPDATE shorts
                SET
                    instagram_status = 'failed',
                    instagram_last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    error_message,
                    short_id,
                ),
            )

            conn.commit()

        finally:
            cur.close()
            conn.close()

        return {
            "success": False,
            "error":
                f"n8n HTTP error "
                f"{exc.code}",
            "short_id": short_id,
            "details":
                error_body[:1000],
        }

    except Exception as exc:

        error_message = (
            str(exc)[:2000]
        )

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                UPDATE shorts
                SET
                    instagram_status = 'failed',
                    instagram_last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    error_message,
                    short_id,
                ),
            )

            conn.commit()

        finally:
            cur.close()
            conn.close()

        return {
            "success": False,
            "error":
                f"Failed to call n8n: {exc}",
            "short_id": short_id,
        }

    if (
        response_status < 200
        or response_status >= 300
    ):

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                UPDATE shorts
                SET
                    instagram_status = 'failed',
                    instagram_last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    (
                        "Unexpected n8n status "
                        f"{response_status}"
                    ),
                    short_id,
                ),
            )

            conn.commit()

        finally:
            cur.close()
            conn.close()

        return {
            "success": False,
            "error": (
                "Unexpected n8n status "
                f"{response_status}"
            ),
            "short_id": short_id,
        }

    return {
        "success": True,
        "short_id": short_id,
        "instagram_status":
            "requested",
        "message": (
            "Instagram publication request "
            "accepted by n8n. Waiting for "
            "Instagram callback."
        ),
        "video_url_expires_at":
            signed_video["expires"],
        "video_url_ttl_seconds":
            signed_video["ttl_seconds"],
        "n8n_http_status":
            response_status,
        "n8n_response":
            response_body[:1000],
    }


# =========================================================
# API KEY PROTECTION
# VIDEO DELIVERY
# CALLBACKS
# =========================================================

class APIKeyMiddleware:

    def __init__(
        self,
        app,
    ):
        self.app = app

    async def send_json(
        self,
        send,
        status_code: int,
        payload: dict,
    ):

        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

        await send({
            "type":
                "http.response.start",
            "status":
                status_code,
            "headers": [
                (
                    b"content-type",
                    b"application/json; charset=utf-8",
                ),
                (
                    b"content-length",
                    str(
                        len(body)
                    ).encode(),
                ),
            ],
        })

        await send({
            "type":
                "http.response.body",
            "body":
                body,
        })

    async def read_body(
        self,
        receive,
    ) -> bytes:

        chunks = []

        while True:

            message = (
                await receive()
            )

            if (
                message["type"]
                != "http.request"
            ):
                continue

            body = message.get(
                "body",
                b"",
            )

            if body:
                chunks.append(
                    body
                )

            if not message.get(
                "more_body",
                False,
            ):
                break

        return b"".join(
            chunks
        )

    async def stream_video(
        self,
        send,
        file_path: Path,
        short_id: int,
        method: str,
        attachment: bool = False,
    ):

        file_size = (
            file_path.stat().st_size
        )

        disposition_type = (
            "attachment"
            if attachment
            else "inline"
        )

        await send({
            "type":
                "http.response.start",
            "status":
                200,
            "headers": [
                (
                    b"content-type",
                    b"video/mp4",
                ),
                (
                    b"content-length",
                    str(
                        file_size
                    ).encode(),
                ),
                (
                    b"content-disposition",
                    (
                        f'{disposition_type}; '
                        f'filename="short_'
                        f'{short_id}.mp4"'
                    ).encode(),
                ),
                (
                    b"accept-ranges",
                    b"bytes",
                ),
            ],
        })

        if method == "HEAD":

            await send({
                "type":
                    "http.response.body",
                "body":
                    b"",
                "more_body":
                    False,
            })

            return

        with open(
            file_path,
            "rb",
        ) as video_file:

            while True:

                chunk = video_file.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                await send({
                    "type":
                        "http.response.body",
                    "body":
                        chunk,
                    "more_body":
                        True,
                })

        await send({
            "type":
                "http.response.body",
            "body":
                b"",
            "more_body":
                False,
        })

    async def __call__(
        self,
        scope,
        receive,
        send,
    ):

        if scope["type"] != "http":

            await self.app(
                scope,
                receive,
                send,
            )

            return

        headers = {
            key.decode(
                "latin-1"
            ).lower():
                value.decode(
                    "latin-1"
                )
            for key, value
            in scope.get(
                "headers",
                [],
            )
        }

        supplied_key = (
            headers.get(
                "x-api-key",
                "",
            )
        )

        path = scope.get(
            "path",
            "",
        )

        method = scope.get(
            "method",
            "GET",
        ).upper()

        # =================================================
        # PRIVATE VIDEO ENDPOINT FOR N8N/YOUTUBE
        #
        # GET /video/14
        # x-api-key: ...
        # =================================================

        if path.startswith(
            "/video/"
        ):

            if method not in {
                "GET",
                "HEAD",
            }:

                await self.send_json(
                    send,
                    405,
                    {
                        "error":
                            "Method not allowed",
                    },
                )

                return

            if not hmac.compare_digest(
                supplied_key,
                VIDEO_DOWNLOAD_API_KEY,
            ):

                await self.send_json(
                    send,
                    401,
                    {
                        "error":
                            "Unauthorized",
                    },
                )

                return

            short_id_text = path[
                len("/video/"):
            ].strip("/")

            if (
                not short_id_text
                .isdigit()
            ):

                await self.send_json(
                    send,
                    400,
                    {
                        "error":
                            "Invalid short_id",
                    },
                )

                return

            short_id = int(
                short_id_text
            )

            file_path = Path(
                f"/videos/"
                f"short_{short_id}.mp4"
            )

            if not file_path.is_file():

                await self.send_json(
                    send,
                    404,
                    {
                        "error":
                            "Video not found",
                    },
                )

                return

            await self.stream_video(
                send,
                file_path,
                short_id,
                method,
                attachment=True,
            )

            return

        # =================================================
        # TEMPORARY SIGNED VIDEO URL FOR INSTAGRAM / META
        #
        # GET /instagram-video/14
        # ?expires=...
        # &signature=...
        #
        # No x-api-key required.
        # =================================================

        if path.startswith(
            "/instagram-video/"
        ):

            if method not in {
                "GET",
                "HEAD",
            }:

                await self.send_json(
                    send,
                    405,
                    {
                        "error":
                            "Method not allowed",
                    },
                )

                return

            short_id_text = path[
                len(
                    "/instagram-video/"
                ):
            ].strip("/")

            if (
                not short_id_text
                .isdigit()
            ):

                await self.send_json(
                    send,
                    400,
                    {
                        "error":
                            "Invalid short_id",
                    },
                )

                return

            short_id = int(
                short_id_text
            )

            query_string = (
                scope.get(
                    "query_string",
                    b"",
                )
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

            query = (
                urllib.parse.parse_qs(
                    query_string
                )
            )

            expires_text = (
                query.get(
                    "expires",
                    [""],
                )[0]
            )

            supplied_signature = (
                query.get(
                    "signature",
                    [""],
                )[0]
            )

            if (
                not expires_text
                .isdigit()
            ):

                await self.send_json(
                    send,
                    401,
                    {
                        "error":
                            "Invalid or missing expiry",
                    },
                )

                return

            expires = int(
                expires_text
            )

            if (
                int(time.time())
                > expires
            ):

                await self.send_json(
                    send,
                    401,
                    {
                        "error":
                            "Video URL expired",
                    },
                )

                return

            if (
                not
                N8N_INSTAGRAM_WEBHOOK_KEY
            ):

                await self.send_json(
                    send,
                    500,
                    {
                        "error":
                            "Instagram signing "
                            "key is not configured",
                    },
                )

                return

            expected_signature = (
                create_instagram_video_signature(
                    short_id,
                    expires,
                )
            )

            if not hmac.compare_digest(
                supplied_signature,
                expected_signature,
            ):

                await self.send_json(
                    send,
                    401,
                    {
                        "error":
                            "Invalid signature",
                    },
                )

                return

            file_path = Path(
                f"/videos/"
                f"short_{short_id}.mp4"
            )

            if not file_path.is_file():

                await self.send_json(
                    send,
                    404,
                    {
                        "error":
                            "Video not found",
                    },
                )

                return

            await self.stream_video(
                send,
                file_path,
                short_id,
                method,
                attachment=False,
            )

            return

        # =================================================
        # YOUTUBE CALLBACK
        # =================================================

        if path.startswith(
            "/youtube-callback/"
        ):

            callback_key = (
                headers.get(
                    N8N_YOUTUBE_WEBHOOK_HEADER
                    .lower(),
                    "",
                )
            )

            if not hmac.compare_digest(
                callback_key,
                N8N_YOUTUBE_WEBHOOK_KEY,
            ):

                await self.send_json(
                    send,
                    401,
                    {
                        "error":
                            "Unauthorized",
                    },
                )

                return

            if method != "POST":

                await self.send_json(
                    send,
                    405,
                    {
                        "error":
                            "Method not allowed",
                    },
                )

                return

            short_id_text = path[
                len(
                    "/youtube-callback/"
                ):
            ].strip("/")

            if (
                not short_id_text
                .isdigit()
            ):

                await self.send_json(
                    send,
                    400,
                    {
                        "error":
                            "Invalid short_id",
                    },
                )

                return

            short_id = int(
                short_id_text
            )

            raw_body = (
                await self.read_body(
                    receive
                )
            )

            try:

                payload = json.loads(
                    raw_body.decode(
                        "utf-8"
                    )
                )

            except Exception:

                await self.send_json(
                    send,
                    400,
                    {
                        "error":
                            "Invalid JSON",
                    },
                )

                return

            youtube_status = str(
                payload.get(
                    "status",
                    "",
                )
            ).strip()

            youtube_video_id = str(
                payload.get(
                    "youtube_video_id",
                    "",
                )
            ).strip()

            error_message = str(
                payload.get(
                    "error_message",
                    "",
                )
            ).strip()

            if youtube_status not in {
                "published",
                "failed",
            }:

                await self.send_json(
                    send,
                    400,
                    {
                        "error": (
                            "status must be "
                            "'published' or "
                            "'failed'"
                        ),
                    },
                )

                return

            if (
                youtube_status
                == "published"
                and not
                youtube_video_id
            ):

                await self.send_json(
                    send,
                    400,
                    {
                        "error": (
                            "youtube_video_id "
                            "is required for "
                            "published status"
                        ),
                    },
                )

                return

            conn = get_db()
            cur = conn.cursor()

            try:

                if (
                    youtube_status
                    == "published"
                ):

                    cur.execute(
                        """
                        UPDATE shorts
                        SET
                            youtube_status = 'published',
                            youtube_video_id = %s,
                            youtube_last_error = NULL,
                            updated_at = NOW()
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            youtube_video_id,
                            short_id,
                        ),
                    )

                else:

                    cur.execute(
                        """
                        UPDATE shorts
                        SET
                            youtube_status = 'failed',
                            youtube_last_error = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            error_message[
                                :2000
                            ],
                            short_id,
                        ),
                    )

                updated = (
                    cur.fetchone()
                )

                if not updated:

                    conn.rollback()

                    await self.send_json(
                        send,
                        404,
                        {
                            "error":
                                "Short not found",
                        },
                    )

                    return

                conn.commit()

            except Exception:

                conn.rollback()
                raise

            finally:
                cur.close()
                conn.close()

            await self.send_json(
                send,
                200,
                {
                    "success": True,
                    "short_id":
                        short_id,
                    "youtube_status":
                        youtube_status,
                    "youtube_video_id":
                        (
                            youtube_video_id
                            or None
                        ),
                },
            )

            return

        # =================================================
        # INSTAGRAM CALLBACK FROM N8N
        #
        # POST /instagram-callback/14
        #
        # Header:
        # x-instagram-publish-key: ...
        #
        # Success body:
        # {
        #   "status": "published",
        #   "instagram_media_id": "..."
        # }
        #
        # Failed:
        # {
        #   "status": "failed",
        #   "error_message": "..."
        # }
        # =================================================

        if path.startswith(
            "/instagram-callback/"
        ):

            if (
                not
                N8N_INSTAGRAM_WEBHOOK_KEY
            ):

                await self.send_json(
                    send,
                    500,
                    {
                        "error":
                            "Instagram webhook "
                            "key is not configured",
                    },
                )

                return

            callback_key = (
                headers.get(
                    N8N_INSTAGRAM_WEBHOOK_HEADER
                    .lower(),
                    "",
                )
            )

            if not hmac.compare_digest(
                callback_key,
                N8N_INSTAGRAM_WEBHOOK_KEY,
            ):

                await self.send_json(
                    send,
                    401,
                    {
                        "error":
                            "Unauthorized",
                    },
                )

                return

            if method != "POST":

                await self.send_json(
                    send,
                    405,
                    {
                        "error":
                            "Method not allowed",
                    },
                )

                return

            short_id_text = path[
                len(
                    "/instagram-callback/"
                ):
            ].strip("/")

            if (
                not short_id_text
                .isdigit()
            ):

                await self.send_json(
                    send,
                    400,
                    {
                        "error":
                            "Invalid short_id",
                    },
                )

                return

            short_id = int(
                short_id_text
            )

            raw_body = (
                await self.read_body(
                    receive
                )
            )

            try:

                payload = json.loads(
                    raw_body.decode(
                        "utf-8"
                    )
                )

            except Exception:

                await self.send_json(
                    send,
                    400,
                    {
                        "error":
                            "Invalid JSON",
                    },
                )

                return

            instagram_status = str(
                payload.get(
                    "status",
                    "",
                )
            ).strip()

            instagram_media_id = str(
                payload.get(
                    "instagram_media_id",
                    "",
                )
            ).strip()

            error_message = str(
                payload.get(
                    "error_message",
                    "",
                )
            ).strip()

            if instagram_status not in {
                "published",
                "failed",
            }:

                await self.send_json(
                    send,
                    400,
                    {
                        "error": (
                            "status must be "
                            "'published' or "
                            "'failed'"
                        ),
                    },
                )

                return

            if (
                instagram_status
                == "published"
                and not
                instagram_media_id
            ):

                await self.send_json(
                    send,
                    400,
                    {
                        "error": (
                            "instagram_media_id "
                            "is required for "
                            "published status"
                        ),
                    },
                )

                return

            conn = get_db()
            cur = conn.cursor()

            try:

                if (
                    instagram_status
                    == "published"
                ):

                    cur.execute(
                        """
                        UPDATE shorts
                        SET
                            instagram_status = 'published',
                            instagram_media_id = %s,
                            instagram_last_error = NULL,
                            updated_at = NOW()
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            instagram_media_id,
                            short_id,
                        ),
                    )

                else:

                    if not error_message:
                        error_message = (
                            "Instagram publication "
                            "failed in n8n"
                        )

                    cur.execute(
                        """
                        UPDATE shorts
                        SET
                            instagram_status = 'failed',
                            instagram_last_error = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            error_message[
                                :2000
                            ],
                            short_id,
                        ),
                    )

                updated = (
                    cur.fetchone()
                )

                if not updated:

                    conn.rollback()

                    await self.send_json(
                        send,
                        404,
                        {
                            "error":
                                "Short not found",
                        },
                    )

                    return

                conn.commit()

            except Exception:

                conn.rollback()
                raise

            finally:
                cur.close()
                conn.close()

            await self.send_json(
                send,
                200,
                {
                    "success": True,
                    "short_id":
                        short_id,
                    "instagram_status":
                        instagram_status,
                    "instagram_media_id":
                        (
                            instagram_media_id
                            or None
                        ),
                },
            )

            return
        
        # =================================================
        # NORMAL MCP AUTH
        # =================================================

        if not hmac.compare_digest(
            supplied_key,
            MCP_API_KEY,
        ):

            await self.send_json(
                send,
                401,
                {
                    "error":
                        "Unauthorized",
                },
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

    print(
        "CHECKING DATABASE SCHEMA...",
        flush=True,
    )

    ensure_database_schema()

    print(
        "DATABASE SCHEMA OK",
        flush=True,
    )

    registered_tools = (
        mcp._tool_manager.list_tools()
    )

    print(
        "REGISTERED MCP TOOLS:",
        flush=True,
    )

    for tool in registered_tools:

        print(
            f"- {tool.name}",
            flush=True,
        )

    mcp_app = (
        mcp.streamable_http_app()
    )

    app = APIKeyMiddleware(
        mcp_app
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
