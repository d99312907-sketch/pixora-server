# Pixora sync server

Makes "admin adds a wallpaper → every user sees it" work. One file, no database.

## Run locally
```bash
pip install fastapi uvicorn
PIXORA_ADMIN_KEY=my-secret uvicorn pixora_server:app --host 0.0.0.0 --port 8000
```
Open http://localhost:8000/health — you should see `{"ok":true}`.

## Deploy free (5 minutes)
1. Push this folder to a GitHub repo.
2. On **Render** → New → Web Service → connect the repo.
   - Build: `pip install fastapi uvicorn`
   - Start: `uvicorn pixora_server:app --host 0.0.0.0 --port $PORT`
   - Environment: `PIXORA_ADMIN_KEY=<a long random secret>`
3. You get `https://something.onrender.com`. Use that in the app.

Railway, Fly.io, Koyeb or any VPS work the same way. The app talks HTTPS only.

## Connect the app
Admin panel (PIN 2468) → **Cloud sync**:
- Server URL: `https://something.onrender.com`
- Admin key: the same `PIXORA_ADMIN_KEY`

Then "Save server". Every phone that opens the app pulls the list automatically.
Adding a wallpaper in the admin panel publishes it to the server immediately, so
it shows up on all devices on their next open (or when they hit "Sync now").

## Notes
- Images uploaded from the phone are stored under `pixora_data/images/` and served
  from `/images/...`; URL-based wallpapers are just passed through.
- Data lives in `pixora_data/wallpapers.json`. Back that folder up.
- Anyone with the admin key can publish. Keep it out of the app's UI for users —
  it is only stored on the device where you type it.
