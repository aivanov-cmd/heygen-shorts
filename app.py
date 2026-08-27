import os
import psycopg2

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional

from mcp.server.fastmcp import FastMCP


# ============================================================
# CONFIG
# ============================================================

AVATARS = [
    "f2813391b4a74544bd18d0b22c2251c0",
    "edd35073c03b4af2a8ddb07b0c62e9cc",
    "31c27d30df2d447089cd1fb41e58959e",
]

VOICE_ID = "ba1544b5eae84eae9cb92598f078b6b0"

MAX_DAILY_VIDEOS = 30

INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg2.connect(database_url)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS shorts (
            id SERIAL PRIMARY KEY,
            topic TEXT NOT NULL,

            scene_1 TEXT NOT NULL DEFAULT '',
            scene_2 TEXT NOT NULL DEFAULT '',
            scene_3 TEXT NOT NULL DEFAULT '',

            avatar_1 TEXT NOT NULL DEFAULT '',
            avatar_2 TEXT NOT NULL DEFAULT '',
            avatar_3 TEXT NOT NULL DEFAULT '',

            voice_id TEXT NOT NULL DEFAULT '',
            template_id TEXT NOT NULL DEFAULT '',

            status TEXT NOT NULL DEFAULT 'ready',

            heygen_video_id TEXT,
            video_url TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            script TEXT,
            avatar_id TEXT,
            subtitle_url TEXT,
            last_error TEXT
        )
    """)

    cur.execute("""
        ALTER TABLE shorts
        ADD COLUMN IF NOT EXISTS script TEXT
    """)

    cur.execute("""
        ALTER TABLE shorts
        ADD COLUMN IF NOT EXISTS avatar_id TEXT
    """)

    cur.execute("""
        ALTER TABLE shorts
        ADD COLUMN IF NOT EXISTS subtitle_url TEXT
    """)

    cur.execute("""
        ALTER TABLE shorts
        ADD COLUMN IF NOT EXISTS last_error TEXT
    """)

    old_columns = [
        "scene_1",
        "scene_2",
        "scene_3",
        "avatar_1",
        "avatar_2",
        "avatar_3",
        "voice_id",
        "template_id",
    ]

    for column in old_columns:
        cur.execute(
            f"""
            ALTER TABLE shorts
            ALTER COLUMN {column} SET DEFAULT ''
            """
        )

    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# DATABASE HELPERS
# ============================================================

def fetch_stats():
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
        "failed": 0,
    }

    total = 0

    for status, count in rows:
        stats[status] = count
        total += count

    return {
        "total": total,
        **stats
    }


def fetch_approved_shorts(limit=30):
    limit = max(1, min(limit, MAX_DAILY_VIDEOS))

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

    result = []

    for row in rows:
        result.append({
            "id": row[0],
            "topic": row[1],
            "script": row[2],
            "avatar_id": row[3],
            "voice_id": row[4],
            "status": row[5],
        })

    return result


def fetch_short(short_id):
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
            last_error,
            created_at,
            updated_at
        FROM shorts
        WHERE id = %s
        """,
        (short_id,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return None

    return {
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
        "created_at": str(row[10]),
        "updated_at": str(row[11]),
    }


# ============================================================
# MCP SERVER
# ============================================================

mcp = FastMCP(
    "HeyGen Shorts Queue",
    stateless_http=True
)


@mcp.tool()
def get_approved_shorts(limit: int = 30) -> dict:
    """
    Get approved Shorts that are waiting for HeyGen generation.

    This tool is read-only.
    It does not change statuses and does not call HeyGen.
    """

    shorts = fetch_approved_shorts(limit)

    return {
        "count": len(shorts),
        "shorts": shorts
    }


@mcp.tool()
def get_short(short_id: int) -> dict:
    """
    Get one Short from the Railway queue by ID.

    This tool is read-only.
    """

    short = fetch_short(short_id)

    if not short:
        return {
            "found": False,
            "error": "Short not found"
        }

    return {
        "found": True,
        "short": short
    }


@mcp.tool()
def get_queue_stats() -> dict:
    """
    Get current statistics for the Shorts queue.

    This tool is read-only.
    """

    return fetch_stats()


# ============================================================
# MCP ASGI APPLICATION
# ============================================================

mcp_app = mcp.streamable_http_app()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="HeyGen Shorts Generator",
    lifespan=mcp_app.lifespan
)


# Mount MCP.
#
# Because FastMCP's Streamable HTTP app itself uses /mcp,
# mounting it at "/" exposes the public MCP endpoint at:
#
# https://YOUR-DOMAIN/mcp

app.mount("/", mcp_app)


# ============================================================
# MODELS
# ============================================================

class ShortRequest(BaseModel):
    topic: str
    script: str


class BatchRequest(BaseModel):
    shorts: List[ShortRequest]


class ShortUpdate(BaseModel):
    topic: str
    script: str


class CompleteRequest(BaseModel):
    heygen_video_id: str
    video_url: str
    subtitle_url: Optional[str] = None


class FailRequest(BaseModel):
    error: str


# ============================================================
# SECURITY
# ============================================================

def verify_internal_key(x_internal_key: Optional[str]):
    if not INTERNAL_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="INTERNAL_API_KEY is not configured"
        )

    if not x_internal_key:
        raise HTTPException(
            status_code=401,
            detail="Missing internal API key"
        )

    if x_internal_key != INTERNAL_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid internal API key"
        )


# ============================================================
# HELPERS
# ============================================================

def clean_text(value: str):
    return value.strip()


def validate_short(topic: str, script: str):
    if not clean_text(topic):
        raise HTTPException(
            status_code=400,
            detail="Topic cannot be empty"
        )

    if not clean_text(script):
        raise HTTPException(
            status_code=400,
            detail="Script cannot be empty"
        )


def get_next_avatar():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM shorts")
    count = cur.fetchone()[0]

    cur.close()
    conn.close()

    return AVATARS[count % len(AVATARS)]


def save_short(short: ShortRequest):
    validate_short(short.topic, short.script)

    avatar_id = get_next_avatar()

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO shorts (
            topic,
            script,
            avatar_id,
            voice_id,
            status
        )
        VALUES (%s, %s, %s, %s, 'ready')
        RETURNING id
        """,
        (
            clean_text(short.topic),
            clean_text(short.script),
            avatar_id,
            VOICE_ID
        )
    )

    short_id = cur.fetchone()[0]

    conn.commit()
    cur.close()
    conn.close()

    return short_id, avatar_id


# ============================================================
# HOME / HEALTH
# ============================================================

@app.get("/")
def home():
    return {
        "service": "HeyGen Shorts",
        "status": "working",
        "mode": "single_avatar_mcp",
        "mcp": "/mcp"
    }


@app.get("/health")
def health():
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT 1")
        cur.fetchone()

        cur.close()
        conn.close()

        return {
            "status": "ok",
            "database": "connected",
            "mode": "single_avatar_mcp",
            "internal_api_key_configured": bool(INTERNAL_API_KEY),
            "mcp_enabled": True,
            "mcp_endpoint": "/mcp"
        }

    except Exception as error:
        return {
            "status": "error",
            "database": "disconnected",
            "error": str(error)
        }


# ============================================================
# CREATE ONE
# ============================================================

@app.post("/shorts")
def create_short(short: ShortRequest):
    short_id, avatar_id = save_short(short)

    return {
        "status": "ready",
        "id": short_id,
        "topic": clean_text(short.topic),
        "avatar_id": avatar_id
    }


# ============================================================
# CREATE BATCH
# ============================================================

@app.post("/batch")
def create_batch(request: BatchRequest):
    if not request.shorts:
        raise HTTPException(
            status_code=400,
            detail="No shorts supplied"
        )

    if len(request.shorts) > MAX_DAILY_VIDEOS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_DAILY_VIDEOS} shorts per batch"
        )

    created = []

    for short in request.shorts:
        short_id, avatar_id = save_short(short)

        created.append({
            "id": short_id,
            "topic": clean_text(short.topic),
            "avatar_id": avatar_id,
            "status": "ready"
        })

    return {
        "status": "saved",
        "count": len(created),
        "shorts": created
    }


# ============================================================
# QUEUE
# ============================================================

@app.get("/queue")
def get_queue():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
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
            created_at,
            updated_at
        FROM shorts
        ORDER BY id DESC
        LIMIT 200
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    result = []

    for row in rows:
        result.append({
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
            "created_at": str(row[10]),
            "updated_at": str(row[11])
        })

    return {
        "count": len(result),
        "shorts": result
    }


# ============================================================
# GET ONE
# ============================================================

@app.get("/shorts/{short_id}")
def get_short_http(short_id: int):
    short = fetch_short(short_id)

    if not short:
        raise HTTPException(
            status_code=404,
            detail="Short not found"
        )

    return short


# ============================================================
# APPROVED QUEUE
# ============================================================

@app.get("/queue/approved")
def get_approved_queue(limit: int = 30):
    shorts = fetch_approved_shorts(limit)

    return {
        "count": len(shorts),
        "shorts": shorts
    }


# ============================================================
# STATS
# ============================================================

@app.get("/stats")
def get_stats():
    return fetch_stats()


# ============================================================
# APPROVE ONE
# ============================================================

@app.post("/shorts/{short_id}/approve")
def approve_short(short_id: int):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE shorts
        SET
            status = 'approved',
            last_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND status IN ('ready', 'failed')
          AND heygen_video_id IS NULL
        RETURNING id
        """,
        (short_id,)
    )

    result = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    if not result:
        raise HTTPException(
            status_code=409,
            detail="Short cannot be approved"
        )

    return {
        "id": short_id,
        "status": "approved"
    }


# ============================================================
# APPROVE ALL
# ============================================================

@app.post("/approve-all")
def approve_all():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE shorts
        SET
            status = 'approved',
            last_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'ready'
          AND heygen_video_id IS NULL
        RETURNING id
    """)

    rows = cur.fetchall()

    conn.commit()
    cur.close()
    conn.close()

    return {
        "status": "approved",
        "count": len(rows)
    }


# ============================================================
# EDIT
# ============================================================

@app.put("/shorts/{short_id}")
def update_short(short_id: int, update: ShortUpdate):
    validate_short(update.topic, update.script)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE shorts
        SET
            topic = %s,
            script = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND status IN ('ready', 'approved', 'failed')
          AND heygen_video_id IS NULL
        RETURNING id
        """,
        (
            clean_text(update.topic),
            clean_text(update.script),
            short_id
        )
    )

    result = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    if not result:
        raise HTTPException(
            status_code=409,
            detail="Short cannot be edited"
        )

    return {
        "id": short_id,
        "status": "updated"
    }


# ============================================================
# DELETE
# ============================================================

@app.delete("/shorts/{short_id}")
def delete_short(short_id: int):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM shorts
        WHERE id = %s
          AND status NOT IN ('generating', 'completed')
        RETURNING id
        """,
        (short_id,)
    )

    result = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    if not result:
        raise HTTPException(
            status_code=409,
            detail="Short cannot be deleted"
        )

    return {
        "status": "deleted",
        "id": short_id
    }


# ============================================================
# INTERNAL: START GENERATION
# ============================================================

@app.post("/internal/shorts/{short_id}/start")
def start_generation(
    short_id: int,
    x_internal_key: Optional[str] = Header(
        default=None,
        alias="X-Internal-Key"
    )
):
    verify_internal_key(x_internal_key)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE shorts
        SET
            status = 'generating',
            last_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND status = 'approved'
          AND heygen_video_id IS NULL
        RETURNING
            id,
            topic,
            script,
            avatar_id,
            voice_id
        """,
        (short_id,)
    )

    row = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(
            status_code=409,
            detail="Short is not available for generation"
        )

    return {
        "id": row[0],
        "topic": row[1],
        "script": row[2],
        "avatar_id": row[3],
        "voice_id": row[4],
        "status": "generating"
    }


# ============================================================
# INTERNAL: COMPLETE GENERATION
# ============================================================

@app.post("/internal/shorts/{short_id}/complete")
def complete_generation(
    short_id: int,
    request: CompleteRequest,
    x_internal_key: Optional[str] = Header(
        default=None,
        alias="X-Internal-Key"
    )
):
    verify_internal_key(x_internal_key)

    if not clean_text(request.heygen_video_id):
        raise HTTPException(
            status_code=400,
            detail="heygen_video_id cannot be empty"
        )

    if not clean_text(request.video_url):
        raise HTTPException(
            status_code=400,
            detail="video_url cannot be empty"
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE shorts
        SET
            status = 'completed',
            heygen_video_id = %s,
            video_url = %s,
            subtitle_url = %s,
            last_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND status = 'generating'
        RETURNING id
        """,
        (
            clean_text(request.heygen_video_id),
            clean_text(request.video_url),
            clean_text(request.subtitle_url)
            if request.subtitle_url
            else None,
            short_id
        )
    )

    result = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    if not result:
        raise HTTPException(
            status_code=409,
            detail="Short is not currently generating"
        )

    return {
        "id": short_id,
        "status": "completed",
        "heygen_video_id": clean_text(
            request.heygen_video_id
        ),
        "video_url": clean_text(
            request.video_url
        ),
        "subtitle_url": (
            clean_text(request.subtitle_url)
            if request.subtitle_url
            else None
        )
    }


# ============================================================
# INTERNAL: FAILED GENERATION
# ============================================================

@app.post("/internal/shorts/{short_id}/fail")
def fail_generation(
    short_id: int,
    request: FailRequest,
    x_internal_key: Optional[str] = Header(
        default=None,
        alias="X-Internal-Key"
    )
):
    verify_internal_key(x_internal_key)

    if not clean_text(request.error):
        raise HTTPException(
            status_code=400,
            detail="Error cannot be empty"
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE shorts
        SET
            status = 'failed',
            last_error = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND status = 'generating'
        RETURNING id
        """,
        (
            clean_text(request.error),
            short_id
        )
    )

    result = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    if not result:
        raise HTTPException(
            status_code=409,
            detail="Short is not currently generating"
        )

    return {
        "id": short_id,
        "status": "failed",
        "error": clean_text(request.error)
    }


# ============================================================
# DRY RUN
# ============================================================

@app.get("/dry-run/next")
def dry_run_next():
    shorts = fetch_approved_shorts(1)

    if not shorts:
        return {
            "status": "empty",
            "message": "No approved Shorts available"
        }

    short = shorts[0]

    return {
        "status": "dry_run",
        "heygen_called": False,
        "credits_used": False,

        "short": {
            "id": short["id"],
            "topic": short["topic"]
        },

        "heygen_payload": {
            "title": (
                f'Short #{short["id"]} - '
                f'{short["topic"]}'
            ),
            "avatarId": short["avatar_id"],
            "voiceId": short["voice_id"],
            "script": short["script"],
            "aspectRatio": "9:16",
            "resolution": "1080p",
            "outputFormat": "mp4",
            "caption": {
                "enabled": True,
                "file_format": "srt",
                "burn_into_video": True
            }
        }
    }


# ============================================================
# PANEL
# ============================================================

@app.get("/panel", response_class=HTMLResponse)
def panel():
    return """
<!DOCTYPE html>
<html lang="ru">

<head>

<meta charset="UTF-8">
<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>HeyGen Shorts</title>

<style>

body {
    font-family: Arial, sans-serif;
    max-width: 1100px;
    margin: 35px auto;
    padding: 20px;
    color: #222;
}

h1 {
    margin-bottom: 5px;
}

.subtitle {
    color: #666;
    margin-top: 0;
}

.topbar {
    padding: 18px;
    margin: 25px 0;
    background: #f5f5f5;
    border-radius: 10px;
}

textarea,
input {
    box-sizing: border-box;
    width: 100%;
    padding: 10px;
    margin: 5px 0 10px 0;
}

textarea {
    min-height: 120px;
}

#input {
    min-height: 280px;
    font-family: monospace;
}

button {
    padding: 9px 14px;
    margin: 5px 5px 5px 0;
    cursor: pointer;
}

.short {
    border: 1px solid #ddd;
    padding: 18px;
    margin: 15px 0;
    border-radius: 10px;
}

.ready {
    background: #fff8dc;
}

.approved {
    background: #e8f5e9;
}

.generating {
    background: #e3f2fd;
}

.completed {
    background: #e8f5e9;
}

.failed {
    background: #ffebee;
}

.script {
    white-space: pre-wrap;
    line-height: 1.5;
    margin: 15px 0;
}

.meta {
    font-size: 12px;
    color: #666;
    word-break: break-all;
}

.status {
    font-weight: bold;
}

.edit-box {
    display: none;
    margin-top: 15px;
    padding-top: 15px;
    border-top: 1px solid #ddd;
}

.message {
    margin-left: 10px;
    font-weight: bold;
}

.error {
    padding: 10px;
    margin-top: 10px;
    background: #fff;
    border: 1px solid #e57373;
    border-radius: 6px;
}

</style>

</head>

<body>

<h1>HeyGen Shorts Generator</h1>

<p class="subtitle">
1 Shorts = 1 script + 1 Nik Look
</p>


<div class="topbar">

    <div id="stats">
        Загрузка...
    </div>

    <br>

    <button onclick="approveAll()">
        Approve all ready
    </button>

</div>


<h2>Добавить Shorts</h2>

<p>
Вставь пакет topic + script.
</p>

<textarea id="input">
{
  "shorts": [
    {
      "topic": "Тема ролика",
      "script": "Полный сценарий одного Shorts."
    }
  ]
}
</textarea>

<br>

<button onclick="sendBatch()">
    Добавить в очередь
</button>

<span
    id="message"
    class="message"
></span>


<hr>

<h2>Очередь</h2>

<button onclick="loadAll()">
    Обновить
</button>

<div id="queue"></div>


<script>

function escapeHtml(text) {
    return String(text ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}


async function sendBatch() {
    const message =
        document.getElementById("message");

    try {
        const data = JSON.parse(
            document.getElementById("input").value
        );

        message.textContent = "Сохраняю...";

        const response = await fetch(
            "/batch",
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(data)
            }
        );

        const answer = await response.json();

        if (!response.ok) {
            throw new Error(
                answer.detail || "Ошибка"
            );
        }

        message.textContent =
            "Добавлено: " + answer.count;

        await loadAll();

    } catch (error) {
        message.textContent =
            "Ошибка: " + error.message;
    }
}


async function loadStats() {
    const response =
        await fetch("/stats");

    const stats =
        await response.json();

    document.getElementById("stats").innerHTML =
        "<strong>Всего:</strong> " + stats.total +
        " | Ready: " + stats.ready +
        " | Approved: " + stats.approved +
        " | Generating: " + stats.generating +
        " | Completed: " + stats.completed +
        " | Failed: " + stats.failed;
}


async function loadQueue() {
    const response =
        await fetch("/queue");

    const data =
        await response.json();

    const container =
        document.getElementById("queue");

    container.innerHTML = "";

    data.shorts.forEach(short => {
        const item =
            document.createElement("div");

        item.className =
            "short " + short.status;

        let video = "";

        if (short.video_url) {
            video = `
                <p>
                    <a
                        href="${escapeHtml(short.video_url)}"
                        target="_blank"
                        rel="noopener noreferrer"
                    >
                        Открыть видео
                    </a>
                </p>
            `;
        }

        let error = "";

        if (short.last_error) {
            error = `
                <div class="error">
                    <strong>Ошибка:</strong>
                    ${escapeHtml(short.last_error)}
                </div>
            `;
        }

        let approveButton = "";

        if (
            short.status === "ready" ||
            short.status === "failed"
        ) {
            approveButton = `
                <button
                    onclick="approveShort(${short.id})"
                >
                    Approve
                </button>
            `;
        }

        let editButton = "";

        if (
            short.status === "ready" ||
            short.status === "approved" ||
            short.status === "failed"
        ) {
            editButton = `
                <button
                    onclick="showEdit(${short.id})"
                >
                    Edit
                </button>
            `;
        }

        let deleteButton = "";

        if (
            short.status !== "generating" &&
            short.status !== "completed"
        ) {
            deleteButton = `
                <button
                    onclick="deleteShort(${short.id})"
                >
                    Delete
                </button>
            `;
        }

        let heygenMeta = "";

        if (short.heygen_video_id) {
            heygenMeta = `
                <br>
                HeyGen Video ID:
                ${escapeHtml(short.heygen_video_id)}
            `;
        }

        item.innerHTML = `

            <h3>
                #${short.id}
                ${escapeHtml(short.topic)}
            </h3>

            <p class="status">
                Статус:
                ${escapeHtml(short.status)}
            </p>

            <div class="script">
                ${escapeHtml(short.script || "")}
            </div>

            <div class="meta">
                Avatar ID:
                ${escapeHtml(short.avatar_id || "—")}
                <br>
                Voice ID:
                ${escapeHtml(short.voice_id || "—")}
                ${heygenMeta}
            </div>

            ${video}
            ${error}

            <br>

            ${editButton}
            ${approveButton}
            ${deleteButton}

            <div
                class="edit-box"
                id="edit-${short.id}"
            >

                <label>Тема</label>

                <input
                    id="topic-${short.id}"
                    value="${escapeHtml(short.topic)}"
                >

                <label>Полный script</label>

                <textarea
                    id="script-${short.id}"
                >${escapeHtml(short.script || "")}</textarea>

                <button
                    onclick="saveEdit(${short.id})"
                >
                    Save
                </button>

                <button
                    onclick="hideEdit(${short.id})"
                >
                    Cancel
                </button>

            </div>
        `;

        container.appendChild(item);
    });
}


function showEdit(id) {
    document.getElementById(
        "edit-" + id
    ).style.display = "block";
}


function hideEdit(id) {
    document.getElementById(
        "edit-" + id
    ).style.display = "none";
}


async function saveEdit(id) {
    const body = {
        topic:
            document.getElementById(
                "topic-" + id
            ).value,

        script:
            document.getElementById(
                "script-" + id
            ).value
    };

    const response = await fetch(
        "/shorts/" + id,
        {
            method: "PUT",
            headers: {
                "Content-Type":
                    "application/json"
            },
            body: JSON.stringify(body)
        }
    );

    const answer =
        await response.json();

    if (!response.ok) {
        alert(
            answer.detail ||
            "Ошибка сохранения"
        );

        return;
    }

    await loadAll();
}


async function approveShort(id) {
    const response = await fetch(
        "/shorts/" + id + "/approve",
        {
            method: "POST"
        }
    );

    const answer =
        await response.json();

    if (!response.ok) {
        alert(
            answer.detail ||
            "Ошибка"
        );

        return;
    }

    await loadAll();
}


async function approveAll() {
    if (!confirm(
        "Одобрить все Shorts со статусом ready?"
    )) {
        return;
    }

    const response = await fetch(
        "/approve-all",
        {
            method: "POST"
        }
    );

    const answer =
        await response.json();

    if (!response.ok) {
        alert(
            answer.detail ||
            "Ошибка"
        );

        return;
    }

    alert(
        "Одобрено: " +
        answer.count
    );

    await loadAll();
}


async function deleteShort(id) {
    if (!confirm(
        "Удалить этот Shorts?"
    )) {
        return;
    }

    const response = await fetch(
        "/shorts/" + id,
        {
            method: "DELETE"
        }
    );

    const answer =
        await response.json();

    if (!response.ok) {
        alert(
            answer.detail ||
            "Ошибка удаления"
        );

        return;
    }

    await loadAll();
}


async function loadAll() {
    await loadStats();
    await loadQueue();
}


loadAll();

</script>

</body>
</html>
"""
