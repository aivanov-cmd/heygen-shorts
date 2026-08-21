from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List


app = FastAPI(
    title="HeyGen Shorts Generator"
)


# -----------------------------
# ПОСТОЯННЫЕ НАСТРОЙКИ
# -----------------------------

AVATAR_1 = "f2813391b4a74544bd18d0b22c2251c0"
AVATAR_2 = "edd35073c03b4af2a8ddb07b0c62e9cc"
AVATAR_3 = "31c27d30df2d447089cd1fb41e58959e"

VOICE_ID = "ba1544b5eae84eae9cb92598f078b6b0"

TEMPLATE_ID = "2596b61c4be848bf90b321ab6ebdb158"


# -----------------------------
# ФОРМАТ ОДНОГО SHORTS
# -----------------------------

class ShortsRequest(BaseModel):
    topic: str
    scene_1: str
    scene_2: str
    scene_3: str


# -----------------------------
# ФОРМАТ ПАКЕТА SHORTS
# -----------------------------

class BatchRequest(BaseModel):
    shorts: List[ShortsRequest]


# -----------------------------
# СЛУЖЕБНЫЕ ENDPOINTS
# -----------------------------

@app.get("/")
def home():
    return {
        "service": "HeyGen Shorts",
        "status": "working"
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }


@app.get("/heygen-config")
def heygen_config():
    return {
        "status": "waiting_for_oauth",
        "mcp_url": "https://mcp.heygen.com/mcp/v1/",
        "template_id": TEMPLATE_ID,
        "billing_mode_required": "web_plan_oauth",
        "render_enabled": False
    }


# -----------------------------
# ПОДГОТОВКА ОДНОГО SHORTS
# -----------------------------

def prepare_short(short: ShortsRequest):
    return {
        "status": "ready",
        "topic": short.topic,

        "scene_1": {
            "text": short.scene_1,
            "avatar_id": AVATAR_1
        },

        "scene_2": {
            "text": short.scene_2,
            "avatar_id": AVATAR_2
        },

        "scene_3": {
            "text": short.scene_3,
            "avatar_id": AVATAR_3
        },

        "voice_id": VOICE_ID,

        "settings": {
            "format": "9:16",
            "avatar_engine": "Avatar IV",
            "template_id": TEMPLATE_ID
        }
    }


# -----------------------------
# ОДИН SHORTS
# -----------------------------

@app.post("/shorts")
def create_shorts(request: ShortsRequest):
    return prepare_short(request)


# -----------------------------
# ПАКЕТ SHORTS
# -----------------------------

@app.post("/batch")
def create_batch(request: BatchRequest):
    prepared = []

    for index, short in enumerate(request.shorts, start=1):
        item = prepare_short(short)
        item["number"] = index
        prepared.append(item)

    return {
        "status": "batch_ready",
        "count": len(prepared),
        "shorts": prepared
    }


# -----------------------------
# ПРОСТАЯ ПАНЕЛЬ
# -----------------------------

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
            max-width: 1100px;
            margin: 40px auto;
            padding: 20px;
        }

        textarea {
            width: 100%;
            height: 420px;
            font-family: monospace;
            font-size: 14px;
            padding: 12px;
        }

        button {
            margin-top: 15px;
            padding: 14px 24px;
            font-size: 16px;
            cursor: pointer;
        }

        pre {
            margin-top: 25px;
            background: #f4f4f4;
            padding: 15px;
            white-space: pre-wrap;
        }
    </style>
</head>

<body>

<h1>HeyGen Shorts Generator</h1>

<p>
Вставь JSON с готовыми Shorts от ChatGPT.
</p>

<textarea id="input">
{
  "shorts": [
    {
      "topic": "Почему одна команда ускоряет рост байера",
      "scene_1": "Текст первой сцены",
      "scene_2": "Текст второй сцены",
      "scene_3": "Текст третьей сцены"
    }
  ]
}
</textarea>

<br>

<button onclick="sendBatch()">
    Подготовить Shorts
</button>

<pre id="result"></pre>

<script>

async function sendBatch() {

    const result = document.getElementById("result");

    try {

        const data = JSON.parse(
            document.getElementById("input").value
        );

        result.textContent = "Отправка...";

        const response = await fetch("/batch", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(data)
        });

        const answer = await response.json();

        result.textContent =
            JSON.stringify(answer, null, 2);

    } catch (error) {

        result.textContent =
            "Ошибка: " + error.message;

    }

}

</script>

</body>
</html>
"""
