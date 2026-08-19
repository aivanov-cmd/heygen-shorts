from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="HeyGen Shorts Generator"
)


class ShortsRequest(BaseModel):
    topic: str
    scene_1: str
    scene_2: str
    scene_3: str


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


@app.post("/shorts")
def create_shorts(request: ShortsRequest):
    return {
        "status": "ready",
        "topic": request.topic,

        "scene_1": {
            "text": request.scene_1,
            "avatar_id": "f2813391b4a74544bd18d0b22c2251c0"
        },

        "scene_2": {
            "text": request.scene_2,
            "avatar_id": "edd35073c03b4af2a8ddb07b0c62e9cc"
        },

        "scene_3": {
            "text": request.scene_3,
            "avatar_id": "31c27d30df2d447089cd1fb41e58959e"
        },

        "voice_id": "ba1544b5eae84eae9cb92598f078b6b0",

        "settings": {
            "format": "9:16",
            "avatar_engine": "Avatar IV",
            "template_id": "2596b61c4be848bf90b321ab6ebdb158"
        }
    }
