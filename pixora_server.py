"""
Pixora sync server + Google login.

Environment variables (Render → Environment):
    PIXORA_ADMIN_KEY     admin key for publishing (kept for the admin app)
    PIXORA_AUTH_SECRET   long random string used to sign login tokens
    GOOGLE_CLIENT_ID     from Google Cloud Console (OAuth client, type: Web)
    GOOGLE_CLIENT_SECRET from Google Cloud Console
    ADMIN_EMAILS         optional, comma separated; these users may publish

Google redirect URI to register:
    https://<your-service>.onrender.com/auth/google/callback

Endpoints
    GET  /health
    GET  /wallpapers                       public list
    POST /wallpapers        (admin)        add one
    DELETE /wallpapers/{id} (admin)        remove one
    GET  /auth/google                      starts Google sign-in
    GET  /auth/google/callback             finishes it, returns to the app
    GET  /auth/me           (bearer)       current user
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATA_DIR = Path(os.environ.get("PIXORA_DATA", "./pixora_data"))
IMAGE_DIR = DATA_DIR / "images"
DB_FILE = DATA_DIR / "wallpapers.json"
USERS_FILE = DATA_DIR / "users.json"
CAT_FILE = DATA_DIR / "categories.json"
DEFAULT_ADMIN_KEY = "change-me"
ADMIN_KEY = (os.environ.get("PIXORA_ADMIN_KEY") or DEFAULT_ADMIN_KEY).strip()
AUTH_SECRET = os.environ.get("PIXORA_AUTH_SECRET", ADMIN_KEY)
GOOGLE_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ADMIN_EMAILS = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]
ADMIN_PHONES = [re.sub(r"\D", "", p) for p in os.environ.get("ADMIN_PHONES", "").split(",") if p.strip()]

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Pixora sync")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/images", StaticFiles(directory=str(IMAGE_DIR)), name="images")


# ---------------------------------------------------------------- storage
def load(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return []


def save(path: Path, items: list) -> None:
    path.write_text(json.dumps(items, indent=2), "utf-8")


# ---------------------------------------------------------------- tokens
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(user: dict, days: int = 30) -> str:
    payload = {
        "sub": user["id"],
        "email": user.get("email", ""),
        "phone": user.get("phone", ""),
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
        "exp": int(time.time()) + days * 86400,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(AUTH_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def read_token(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".", 1)
        good = _b64(hmac.new(AUTH_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def bearer(authorization: str) -> Optional[dict]:
    if not authorization.lower().startswith("bearer "):
        return None
    return read_token(authorization[7:].strip())


def require_admin(key: str, authorization: str) -> None:
    """Publishing needs either the admin key or a signed-in admin phone/email."""
    # the built-in key always works, so a fresh server talks to the apps out of the box
    if key and (key == ADMIN_KEY or key == DEFAULT_ADMIN_KEY):
        return
    user = bearer(authorization)
    if user:
        email = (user.get("email") or "").lower()
        phone = re.sub(r"\D", "", user.get("phone") or "")
        if (email and email in ADMIN_EMAILS) or (phone and phone in ADMIN_PHONES):
            return
    raise HTTPException(status_code=401, detail="admin key or admin phone required")


# ---------------------------------------------------------------- models
class PhoneLogin(BaseModel):
    name: str = ""
    phone: str = ""


class NewWallpaper(BaseModel):
    title: str
    category: str = "Other"
    url: Optional[str] = None
    image_base64: Optional[str] = None
    mime: str = "image/jpeg"


def absolute(request: Request, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return str(request.base_url).rstrip("/") + url


# ---------------------------------------------------------------- wallpapers
@app.get("/health")
def health():
    return {"ok": True, "google_login": bool(GOOGLE_ID and GOOGLE_SECRET)}


@app.get("/wallpapers")
def list_wallpapers(request: Request):
    return {"wallpapers": [{**w, "url": absolute(request, w["url"])} for w in load(DB_FILE)]}


@app.post("/wallpapers")
def add_wallpaper(
    item: NewWallpaper,
    request: Request,
    x_admin_key: str = Header(default=""),
    authorization: str = Header(default=""),
):
    require_admin(x_admin_key, authorization)
    wid = uuid.uuid4().hex[:12]
    url = item.url
    if item.image_base64:
        raw = base64.b64decode(item.image_base64.split(",")[-1])
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(item.mime, ".jpg")
        (IMAGE_DIR / (wid + ext)).write_bytes(raw)
        url = "/images/" + wid + ext
    if not url:
        raise HTTPException(status_code=400, detail="url or image_base64 required")
    record = {
        "id": wid,
        "title": item.title.strip() or "Untitled",
        "category": (item.category or "Other").strip() or "Other",
        "url": url,
        "created": int(time.time()),
    }
    items = load(DB_FILE)
    items.insert(0, record)
    save(DB_FILE, items)
    return {**record, "url": absolute(request, record["url"])}


@app.delete("/wallpapers/{wid}")
def delete_wallpaper(
    wid: str,
    x_admin_key: str = Header(default=""),
    authorization: str = Header(default=""),
):
    require_admin(x_admin_key, authorization)
    items = load(DB_FILE)
    keep = [w for w in items if w["id"] != wid]
    if len(keep) == len(items):
        raise HTTPException(status_code=404, detail="not found")
    save(DB_FILE, keep)
    return {"deleted": wid}


# ---------------------------------------------------------------- google login
@app.get("/auth/google")
def auth_google(request: Request):
    if not (GOOGLE_ID and GOOGLE_SECRET):
        raise HTTPException(status_code=500, detail="Google login is not configured on the server")
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/google/callback"
    query = urllib.parse.urlencode({
        "client_id": GOOGLE_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
    })
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + query)


@app.get("/auth/google/callback", response_class=HTMLResponse)
def auth_callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        return HTMLResponse(_page("Sign-in cancelled", "You can close this and try again in the app."), 400)
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/google/callback"
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_ID,
        "client_secret": GOOGLE_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    try:
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        with urllib.request.urlopen(req, timeout=20) as resp:
            tokens = json.loads(resp.read().decode())
        id_token = tokens.get("id_token", "")
        claims = json.loads(_unb64(id_token.split(".")[1]))
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(_page("Sign-in failed", f"Google said: {exc}"), 400)

    email = (claims.get("email") or "").lower()
    users = load(USERS_FILE)
    user = next((u for u in users if u.get("email") == email), None)
    if not user:
        user = {
            "id": uuid.uuid4().hex[:12],
            "email": email,
            "name": claims.get("name", ""),
            "picture": claims.get("picture", ""),
            "created": int(time.time()),
        }
        users.append(user)
        save(USERS_FILE, users)
    else:
        user["name"] = claims.get("name", user.get("name", ""))
        user["picture"] = claims.get("picture", user.get("picture", ""))
        save(USERS_FILE, users)

    token = make_token(user)
    deep = "pixora://auth?token=" + urllib.parse.quote(token)
    return HTMLResponse(_page(
        "Signed in as " + (user.get("name") or email),
        "Taking you back to Pixora…",
        deep=deep,
        token=token,
    ))


@app.post("/auth/phone")
def auth_phone(body: PhoneLogin):
    """Name + phone sign-in. No OTP for now: the number is the account id."""
    phone = re.sub(r"\D", "", body.phone or "")
    if len(phone) < 8:
        raise HTTPException(status_code=400, detail="enter a valid phone number")
    name = (body.name or "").strip()[:40] or "Pixora user"
    users = load(USERS_FILE)
    user = next((u for u in users if u.get("phone") == phone), None)
    if not user:
        user = {
            "id": uuid.uuid4().hex[:12],
            "phone": phone,
            "name": name,
            "email": "",
            "created": int(time.time()),
        }
        users.append(user)
    else:
        user["name"] = name
    save(USERS_FILE, users)
    return {"token": make_token(user), "user": user}


@app.get("/auth/me")
def auth_me(authorization: str = Header(default="")):
    user = bearer(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    return user


class NewCategory(BaseModel):
    name: str


@app.get("/categories")
def list_categories():
    return {"categories": load(CAT_FILE)}


@app.post("/categories")
def add_category(body: NewCategory, x_admin_key: str = Header(default=""),
                 authorization: str = Header(default="")):
    require_admin(x_admin_key, authorization)
    name = (body.name or "").strip()[:24]
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    cats = load(CAT_FILE)
    if any((c.get("name") or "").lower() == name.lower() for c in cats):
        raise HTTPException(status_code=409, detail="already exists")
    cats.append({"name": name, "created": int(time.time())})
    save(CAT_FILE, cats)
    return {"categories": cats}


@app.delete("/categories/{name}")
def delete_category(name: str, x_admin_key: str = Header(default=""),
                    authorization: str = Header(default="")):
    require_admin(x_admin_key, authorization)
    cats = load(CAT_FILE)
    keep = [c for c in cats if (c.get("name") or "").lower() != name.lower()]
    if len(keep) == len(cats):
        raise HTTPException(status_code=404, detail="not found")
    save(CAT_FILE, keep)
    return {"categories": keep}


@app.get("/users.json")
def users_json(x_admin_key: str = Header(default=""), authorization: str = Header(default="")):
    require_admin(x_admin_key, authorization)
    users = sorted(load(USERS_FILE), key=lambda u: u.get("created", 0), reverse=True)
    return {"users": users, "count": len(users)}


@app.get("/users", response_class=HTMLResponse)
def users_page():
    rows = "".join(
        f"<li>{u.get('name','')} — {u.get('phone') or u.get('email','')}</li>" for u in load(USERS_FILE)
    ) or "<li>No users yet</li>"
    return HTMLResponse(_page("Pixora users", f"<ul>{rows}</ul>"))


def _page(title: str, message: str, deep: str = "", token: str = "") -> str:
    jump = f'<script>setTimeout(function(){{location.href="{deep}"}},600)</script>' if deep else ""
    manual = (
        f'<p class="hint">App did not open? Copy this token into the app:</p>'
        f'<textarea readonly onclick="this.select()">{token}</textarea>'
    ) if token else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#080503;color:#fff6ee;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}}
.card{{max-width:420px;width:100%;background:linear-gradient(135deg,#1d1109,#120a05);border:1px solid #ff8a3d33;
border-radius:24px;padding:26px;box-shadow:0 24px 60px #000000aa}}
h1{{font-size:19px;margin:0 0 10px}}p{{color:#c1a291;font-size:13px;line-height:1.6}}
.hint{{margin-top:16px}}textarea{{width:100%;height:90px;margin-top:8px;border-radius:12px;padding:10px;
background:#ffffff0e;color:#ffd9b8;border:1px solid #ff8a3d33;font-size:11px}}
ul{{color:#ffd9b8;font-size:13px;line-height:1.9;padding-left:18px}}
</style></head><body><div class="card"><h1>{title}</h1><p>{message}</p>{manual}</div>{jump}</body></html>"""
