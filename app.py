from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="HeyGen Shorts Generator"
)


class ShortsRequest(BaseModel):
    topic: str


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


@app.post("/preview")
def preview(request: ShortsRequest):
    return {
        "status": "preview",
        "topic": request.topic,
        "scene_1": "Здесь будет первая сцена",
        "scene_2": "Здесь будет вторая сцена",
        "scene_3": "Здесь будет третья сцена"
    }
# preview endpoint added
