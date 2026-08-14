from fastapi import FastAPI

app = FastAPI()


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
