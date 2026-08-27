import os
import psycopg2

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List


app = FastAPI(title="HeyGen Shorts Generator")


# ============================================================
# НАСТРОЙКИ
# ============================================================

AVATAR_1 = "f2813391b4a74544bd18d0b22c2251c0"
AVATAR_2 = "edd35073c03b4af2a8ddb07b0c62e9cc"
AVATAR_3 = "31c27d30df2d447089cd1fb41e58959e"

VOICE_ID = "ba1544b5eae84eae9cb92598f078b6b0"
TEMPLATE_ID = "2596b61c4be848bf90b321ab6ebdb158"

MAX_DAILY_VIDEOS = 30
RENDER_ENABLED = False


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

    # Старая структура сохраняется для совместимости.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shorts (
            id SERIAL PRIMARY KEY,
            topic TEXT NOT NULL,

            scene_1 TEXT NOT NULL,
            scene_2 TEXT NOT NULL,
            scene_3 TEXT NOT NULL,

            avatar_1 TEXT NOT NULL,
            avatar_2 TEXT NOT NULL,
            avatar_3 TEXT NOT NULL,

            voice_id TEXT NOT NULL,
            template_id TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'ready',

            heygen_video_id TEXT,
            video_url TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # Новые поля для будущей схемы:
    # 1 Shorts = 1 script + 1 avatar
    # --------------------------------------------------------

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

    # Старые записи не теряем.
    # Собираем их три сцены в один script.
    cur.execute("""
        UPDATE shorts
        SET script = CONCAT_WS(' ', scene_1, scene_2, scene_3)
        WHERE script IS NULL
    """)

    # Старым записям временно назначаем первый Look.
    cur.execute(
        """
        UPDATE shorts
        SET avatar_id = %s
        WHERE avatar_id IS NULL
        """,
        (AVATAR_1,)
    )

    conn.commit()

    cur.close()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


# ============================================================
# MODELS
# ============================================================

class ShortsRequest(BaseModel):
    topic: str
    scene_1: str
    scene_2: str
    scene_3: str


class BatchRequest(BaseModel):
    shorts: List[ShortsRequest]


class ShortsUpdate(BaseModel):
    topic: str
    scene_1: str
    scene_2: str
    scene_3: str


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def home():
    return {
        "service": "HeyGen Shorts",
        "status": "working"
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
            "database": "connected"
        }

    except Exception as error:
        return {
            "status": "error",
            "database": "disconnected",
            "error": str(error)
        }


# ============================================================
# HEYGEN CONFIG
# ============================================================

@app.get("/heygen-config")
def heygen_config():
    return {
        "status": "mcp_mode",
        "template_id": TEMPLATE_ID,
        "billing_mode_required": "web_plan_oauth",
        "render_enabled": RENDER_ENABLED
    }


# ============================================================
# SAVE SHORT
# ============================================================

def save_short(short: ShortsRequest):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO shorts (
            topic,

            scene_1,
            scene_2,
            scene_3,

            avatar_1,
            avatar_2,
            avatar_3,

            voice_id,
            template_id,

            script,
            avatar_id,

            status
        )
        VALUES (
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            'ready'
        )
        RETURNING id
        """,
        (
            short.topic,

            short.scene_1,
            short.scene_2,
            short.scene_3,

            AVATAR_1,
            AVATAR_2,
            AVATAR_3,

            VOICE_ID,
            TEMPLATE_ID,

            " ".join([
                short.scene_1,
                short.scene_2,
                short.scene_3
            ]),

            AVATAR_1
        )
    )

    short_id = cur.fetchone()[0]

    conn.commit()

    cur.close()
    conn.close()

    return short_id


# ============================================================
# CREATE ONE SHORT
# ============================================================

@app.post("/shorts")
def create_short(short: ShortsRequest):
    short_id = save_short(short)

    return {
        "status": "ready",
        "id": short_id,
        "topic": short.topic
    }


# ============================================================
# CREATE BATCH
# ============================================================

@app.post("/batch")
def create_batch(request: BatchRequest):
    if len(request.shorts) == 0:
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
        short_id = save_short(short)

        created.append({
            "id": short_id,
            "topic": short.topic,
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

            scene_1,
            scene_2,
            scene_3,

            status,
            heygen_video_id,
            video_url,
            created_at,

            script,
            avatar_id,
            subtitle_url,
            last_error

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

            "scene_1": row[2],
            "scene_2": row[3],
            "scene_3": row[4],

            "status": row[5],
            "heygen_video_id": row[6],
            "video_url": row[7],
            "created_at": str(row[8]),

            "script": row[9],
            "avatar_id": row[10],
            "subtitle_url": row[11],
            "last_error": row[12]
        })

    return {
        "count": len(result),
        "shorts": result
    }


# ============================================================
# STATS
# ============================================================

@app.get("/stats")
def get_stats():
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
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
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
            status_code=404,
            detail="Short not found"
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
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'ready'
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
# EDIT SHORT
# ============================================================

@app.put("/shorts/{short_id}")
def update_short(short_id: int, update: ShortsUpdate):
    conn = get_db()
    cur = conn.cursor()

    full_script = " ".join([
        update.scene_1,
        update.scene_2,
        update.scene_3
    ])

    cur.execute(
        """
        UPDATE shorts
        SET
            topic = %s,

            scene_1 = %s,
            scene_2 = %s,
            scene_3 = %s,

            script = %s,

            updated_at = CURRENT_TIMESTAMP

        WHERE id = %s
        RETURNING id
        """,
        (
            update.topic,

            update.scene_1,
            update.scene_2,
            update.scene_3,

            full_script,

            short_id
        )
    )

    result = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    if not result:
        raise HTTPException(
            status_code=404,
            detail="Short not found"
        )

    return {
        "id": short_id,
        "status": "updated"
    }


# ============================================================
# DELETE SHORT
# ============================================================

@app.delete("/shorts/{short_id}")
def delete_short(short_id: int):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM shorts
        WHERE id = %s
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
            status_code=404,
            detail="Short not found"
        )

    return {
        "status": "deleted",
        "id": short_id
    }


# ============================================================
# FUTURE GENERATE ENDPOINT
# ============================================================

@app.post("/generate-approved")
def generate_approved():
    if not RENDER_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Rendering is handled through ChatGPT + HeyGen MCP"
        )

    return {
        "status": "not_implemented_yet"
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
<title>HeyGen Shorts Generator</title>

<style>

body {
    font-family: Arial, sans-serif;
    max-width: 1200px;
    margin: 40px auto;
    padding: 20px;
}

h1 {
    margin-bottom: 5px;
}

.topbar {
    margin: 20px 0;
    padding: 15px;
    background: #f6f6f6;
    border-radius: 8px;
}

textarea {
    width: 100%;
    min-height: 120px;
    font-family: monospace;
    padding: 10px;
}

button {
    padding: 10px 16px;
    margin: 5px 5px 5px 0;
    cursor: pointer;
}

button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
}

.short {
    border: 1px solid #ddd;
    padding: 15px;
    margin: 15px 0;
    border-radius: 8px;
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

.scene {
    margin: 8px 0;
}

.script {
    margin: 12px 0;
    padding: 10px;
    background: rgba(255,255,255,0.6);
    border-radius: 6px;
}

.edit-box {
    display: none;
    margin-top: 15px;
}

.edit-box input,
.edit-box textarea {
    width: 100%;
    margin-bottom: 10px;
}

.status-badge {
    font-weight: bold;
}

.avatar {
    font-size: 12px;
    color: #666;
}

</style>
</head>

<body>

<h1>HeyGen Shorts Generator</h1>
<p>Очередь Shorts для HeyGen</p>

<div class="topbar">

    <div id="stats">
        Загрузка статистики...
    </div>

    <br>

    <button onclick="approveAll()">
        Approve all
    </button>

    <button disabled
        title="Генерация будет запускаться через ChatGPT + HeyGen MCP">
        Generate approved 🔒
    </button>

</div>


<h2>Добавить Shorts</h2>

<p>Пока используется старый JSON-формат. На следующем этапе заменим его на один script.</p>

<textarea id="input">
{
  "shorts": [
    {
      "topic": "Тема ролика",
      "scene_1": "Первая часть",
      "scene_2": "Вторая часть",
      "scene_3": "Третья часть"
    }
  ]
}
</textarea>

<br>

<button onclick="sendBatch()">
    Добавить в очередь
</button>

<span id="message"></span>

<hr>

<h2>Очередь</h2>

<button onclick="loadAll()">
    Обновить
</button>

<div id="queue"></div>


<script>

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

        const answer =
            await response.json();

        if (!response.ok) {
            throw new Error(
                answer.detail || "Ошибка"
            );
        }

        message.textContent =
            "Добавлено: " + answer.count;

        loadAll();

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

            video =
                '<p><a href="' +
                short.video_url +
                '" target="_blank">Открыть видео</a></p>';
        }

        item.innerHTML = `

            <h3>
                #${short.id}
                ${escapeHtml(short.topic)}
            </h3>

            <p>
                <span class="status-badge">
                    Статус: ${short.status}
                </span>
            </p>

            <p class="avatar">
                Avatar:
                ${short.avatar_id || "—"}
            </p>

            <div class="scene">
                <strong>Scene 1:</strong>
                ${escapeHtml(short.scene_1)}
            </div>

            <div class="scene">
                <strong>Scene 2:</strong>
                ${escapeHtml(short.scene_2)}
            </div>

            <div class="scene">
                <strong>Scene 3:</strong>
                ${escapeHtml(short.scene_3)}
            </div>

            <div class="script">
                <strong>Future full script:</strong><br>
                ${escapeHtml(short.script || "")}
            </div>

            ${video}

            <button onclick="showEdit(${short.id})">
                Edit
            </button>

            ${
                short.status === "ready"
                ?
                `<button onclick="approveShort(${short.id})">
                    Approve
                </button>`
                :
                ""
            }

            <button onclick="deleteShort(${short.id})">
                Delete
            </button>

            <div
                class="edit-box"
                id="edit-${short.id}"
            >

                <input
                    id="topic-${short.id}"
                    value="${escapeHtml(short.topic)}"
                >

                <textarea
                    id="scene1-${short.id}"
                >${escapeHtml(short.scene_1)}</textarea>

                <textarea
                    id="scene2-${short.id}"
                >${escapeHtml(short.scene_2)}</textarea>

                <textarea
                    id="scene3-${short.id}"
                >${escapeHtml(short.scene_3)}</textarea>

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


function escapeHtml(text) {

    return String(text ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
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

        scene_1:
            document.getElementById(
                "scene1-" + id
            ).value,

        scene_2:
            document.getElementById(
                "scene2-" + id
            ).value,

        scene_3:
            document.getElementById(
                "scene3-" + id
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

    if (!response.ok) {

        const answer =
            await response.json();

        alert(
            answer.detail ||
            "Ошибка сохранения"
        );

        return;
    }

    loadAll();
}


async function approveShort(id) {

    const response = await fetch(
        "/shorts/" + id + "/approve",
        {
            method: "POST"
        }
    );

    if (!response.ok) {

        const answer =
            await response.json();

        alert(
            answer.detail ||
            "Ошибка"
        );

        return;
    }

    loadAll();
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
        "Одобрено Shorts: " +
        answer.count
    );

    loadAll();
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

    if (!response.ok) {

        const answer =
            await response.json();

        alert(
            answer.detail ||
            "Ошибка удаления"
        );

        return;
    }

    loadAll();
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
