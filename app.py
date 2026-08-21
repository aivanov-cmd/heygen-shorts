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

    conn.commit()

    cur.close()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


# ============================================================
# REQUEST MODELS
# ============================================================

class ShortsRequest(BaseModel):
    topic: str
    scene_1: str
    scene_2: str
    scene_3: str


class BatchRequest(BaseModel):
    shorts: List[ShortsRequest]


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
        "status": "waiting_for_oauth",
        "template_id": TEMPLATE_ID,
        "billing_mode_required": "web_plan_oauth",
        "render_enabled": False
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
            status
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready')
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
            TEMPLATE_ID
        )
    )

    short_id = cur.fetchone()[0]

    conn.commit()

    cur.close()
    conn.close()

    return short_id


# ============================================================
# ONE SHORT
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
# BATCH
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
# GET QUEUE
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
            created_at
        FROM shorts
        ORDER BY id DESC
        LIMIT 100
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
            "created_at": str(row[8])
        })

    return {
        "count": len(result),
        "shorts": result
    }


# ============================================================
# APPROVE
# ============================================================

@app.post("/shorts/{short_id}/approve")
def approve_short(short_id: int):

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE shorts
        SET status = 'approved',
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
# DELETE
# ============================================================

@app.delete("/shorts/{short_id}")
def delete_short(short_id: int):

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM shorts WHERE id = %s RETURNING id",
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

textarea {
    width: 100%;
    height: 300px;
    font-family: monospace;
    padding: 12px;
}

button {
    padding: 10px 16px;
    margin: 5px;
    cursor: pointer;
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

</style>

</head>

<body>

<h1>HeyGen Shorts Generator</h1>

<h2>Добавить Shorts</h2>

<p>
Вставь JSON из ChatGPT:
</p>

<textarea id="input">
{
  "shorts": [
    {
      "topic": "Тема ролика",
      "scene_1": "Первая сцена",
      "scene_2": "Вторая сцена",
      "scene_3": "Третья сцена"
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

<button onclick="loadQueue()">
Обновить
</button>

<div id="queue"></div>


<script>


async function sendBatch() {

    const message =
        document.getElementById("message");

    try {

        const data =
            JSON.parse(
                document.getElementById("input").value
            );

        message.textContent =
            "Сохраняю...";

        const response =
            await fetch("/batch", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(data)
            });

        const answer =
            await response.json();

        if (!response.ok) {
            throw new Error(
                answer.detail || "Ошибка"
            );
        }

        message.textContent =
            "Добавлено: " + answer.count;

        loadQueue();

    }

    catch (error) {

        message.textContent =
            "Ошибка: " + error.message;

    }

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
                ${short.topic}
            </h3>

            <p>
                <strong>Статус:</strong>
                ${short.status}
            </p>

            <div class="scene">
                <strong>Scene 1:</strong>
                ${short.scene_1}
            </div>

            <div class="scene">
                <strong>Scene 2:</strong>
                ${short.scene_2}
            </div>

            <div class="scene">
                <strong>Scene 3:</strong>
                ${short.scene_3}
            </div>

            ${video}

            <button
                onclick="approveShort(${short.id})">
                Approve
            </button>

            <button
                onclick="deleteShort(${short.id})">
                Delete
            </button>
        `;

        container.appendChild(item);

    });

}


async function approveShort(id) {

    await fetch(
        "/shorts/" + id + "/approve",
        {
            method: "POST"
        }
    );

    loadQueue();

}


async function deleteShort(id) {

    if (!confirm(
        "Удалить этот Shorts?"
    )) {
        return;
    }

    await fetch(
        "/shorts/" + id,
        {
            method: "DELETE"
        }
    );

    loadQueue();

}


loadQueue();


</script>

</body>

</html>
"""
