"""
Pixora sync server — the smallest thing that makes "admin adds a wallpaper,
every user's phone shows it" work.

Run locally:
    pip install fastapi uvicorn
    PIXORA_ADMIN_KEY=my-secret uvicorn pixora_server:app --host 0.0.0.0 --port 8000

Then in the app: Admin panel -> Cloud sync
    Server URL : https://your-server        (must be https for the app)
    Admin key  : my-secret

Endpoints
    GET    /wallpapers            -> {"wallpapers":[{id,title,category,url}]}
    POST   /wallpapers            -> add one (header X-Admin-Key)
                                     body: {title, category, url}  or
                                           {title, category, image_base64, mime}
    DELETE /wallpapers/{id}       -> remove one (header X-Admin-Key)
    GET    /health                -> {"ok":true}

Storage is a JSON file plus an images folder, so it needs no database.
Deploy free on Render / Railway / Fly.io / a VPS. Set PIXORA_ADMIN_KEY in the
environment and point the app at the https URL.
"""
import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATA_DIR = Path(os.environ.get("PIXORA_DATA", "./pixora_data"))
IMAGE_DIR = DATA_DIR / "images"
DB_FILE = DATA_DIR / "wallpapers.json"
ADMIN_KEY = os.environ.get("PIXORA_ADMIN_KEY", "change-me")

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Pixora sync")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/images", StaticFiles(directory=str(IMAGE_DIR)), name="images")


class NewWallpaper(BaseModel):
    title: str
    category: str = "Other"
    url: Optional[str] = None
    image_base64: Optional[str] = None
    mime: str = "image/jpeg"


def load() -> list:
    if not DB_FILE.exists():
        return []
    try:
        return json.loads(DB_FILE.read_text("utf-8"))
    except Exception:
        return []


def save(items: list) -> None:
    DB_FILE.write_text(json.dumps(items, indent=2), "utf-8")


def require_admin(key: str) -> None:
    if key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="bad admin key")


def absolute(request: Request, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return str(request.base_url).rstrip("/") + url


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/wallpapers")
def list_wallpapers(request: Request):
    items = [{**w, "url": absolute(request, w["url"])} for w in load()]
    return {"wallpapers": items}


@app.post("/wallpapers")
def add_wallpaper(item: NewWallpaper, request: Request, x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    wid = uuid.uuid4().hex[:12]
    url = item.url

    if item.image_base64:
        raw = base64.b64decode(item.image_base64.split(",")[-1])
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(item.mime, ".jpg")
        name = wid + ext
        (IMAGE_DIR / name).write_bytes(raw)
        url = "/images/" + name

    if not url:
        raise HTTPException(status_code=400, detail="url or image_base64 required")

    record = {
        "id": wid,
        "title": item.title.strip() or "Untitled",
        "category": (item.category or "Other").strip() or "Other",
        "url": url,
        "created": int(time.time()),
    }
    items = load()
    items.insert(0, record)
    save(items)
    return {**record, "url": absolute(request, record["url"])}


@app.delete("/wallpapers/{wid}")
def delete_wallpaper(wid: str, x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    items = load()
    keep = [w for w in items if w["id"] != wid]
    if len(keep) == len(items):
        raise HTTPException(status_code=404, detail="not found")
    save(keep)
    return {"deleted": wid}
