# Edison Barber Studio — standalone web app

The complete booking app for **Edison Barber Studio** (Edison Pena,
646-379-8816, 132-13 14 Ave, College Point, NY 11356), packaged as a
single self-contained folder that can be hosted on any public web host.
It does **not** depend on Muse: a client who opens the public link (or
scans the QR poster) lands directly in the booking app — no accounts,
no sign-up, nothing to install.

## What's inside

| File | What it is |
|---|---|
| `server.py` | The whole backend: Python 3 **standard library only** (`http.server` + `sqlite3`). Serves the app and the JSON API. No pip installs, ever. |
| `index.html` | The single-page app (booking, Prices, My Appointments, barber Agenda). English primary / Spanish secondary, switch in the header. |
| `img/` | One photo per service + `manifest.json` (photo sources). |
| `icon-192.png`, `icon-512.png`, `apple-touch-icon.png`, `manifest.webmanifest` | App icons / PWA manifest so clients can save it to their home screen. |
| `bookings.db` | Created automatically on first run (SQLite). Holds appointments + the notifications log. **Ships absent/empty on purpose.** |

## What the app does

- Clients book with **name and phone only**, can pick **several services**
  in one appointment; prices and durations add up and the appointment
  blocks the **total** duration. Overlapping bookings and times outside
  opening hours (or Tuesdays, when the shop is closed) are rejected.
- Hours: Sun 9 AM–12 PM; Mon/Wed/Thu/Fri/Sat 9 AM–1 PM and 2 PM–7 PM;
  Tue closed.
- **My Appointments**: look up by phone, cancel (two taps) — cancelling
  frees the slot immediately.
- **Agenda** (barber only): open it by entering the barber phone number
  `6463798816`. Shows upcoming appointments and the notifications log
  (new booking / cancellation entries are recorded automatically).
- A background thread records a **reminder-due entry** in the Agenda log
  about 1 hour before each appointment. It does **not** send any SMS or
  external message — texting is paused by the owner's decision; do not
  wire a messaging service into this without his explicit OK.

API (for reference): `GET /api/config`, `GET /api/day?date=YYYY-MM-DD`,
`POST /api/book`, `GET /api/mine?phone=`, `POST /api/cancel`,
`GET /api/agenda?key=`.

## Run it locally

```bash
cd edison-barber-studio-web
python3 server.py
```

Open http://localhost:8000 — the server listens on `$PORT`
(default `8000`).

## Deploy on Render (free tier)

1. Put this folder in a GitHub repo (or use Render's "deploy from repo").
2. Render → **New → Web Service** → connect the repo.
3. Settings:
   - **Runtime:** Python 3
   - **Build Command:** *(leave empty — there is nothing to install)*
   - **Start Command:** `python3 server.py`
   - **Instance type:** Free
4. Deploy. Render gives you a public URL like
   `https://edison-barber-studio.onrender.com`. That URL **is** the
   client-facing address: send it to clients and put it in the QR code
   on the printed poster (regenerate the poster QR with this real URL —
   do **not** reuse any earlier test QR).

### ⚠️ Honest warning about data on the free tier

On Render's free tier the filesystem is **ephemeral**: `bookings.db`
(SQLite) is **wiped on every redeploy/restart**, and free services also
spin down when idle (first visit after idle takes ~30–60 s to wake).
For a shop taking real bookings, pick one fix:

- **Simplest:** Render paid instance (always on) **+ a Persistent Disk**
  mounted at the app folder, and set the env var
  `BOOKINGS_DB=/path/on/disk/bookings.db` — or
- Move storage to an external database later (e.g. a hosted Postgres);
  the storage layer is isolated in `server.py` (`db()`, `init_db()`).

Also note: anyone who knows the barber's phone number can open the
Agenda view. That matches how the app has always worked in testing; if
that ever needs hardening, add a PIN before real launch.

## Tests (already run against this package)

Local curl checks verified: booking Wed 10:00 AM Fades + Beard Trim =
50 min / $45 / ends 10:50 AM ✓; overlapping 10:30 AM booking rejected
(`slot_taken`) ✓; Tuesday booking rejected (`closed`) ✓; cancelling
frees the slot (day blocks empty) ✓; Agenda endpoint with the barber
key lists the booking and the log entries ✓. Test data was deleted
afterwards — the package ships with an empty database state.
