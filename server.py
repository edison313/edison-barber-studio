#!/usr/bin/env python3
"""
Edison Barber Studio — standalone booking server.

Zero external dependencies: Python 3 standard library only
(http.server + sqlite3). Serves the single-page app in index.html and a
small JSON API. Data lives in bookings.db (SQLite) next to this file.

Run:  python3 server.py          (listens on $PORT, default 8000)

API:
  GET  /api/config                 services, hours, business info
  GET  /api/day?date=YYYY-MM-DD    booked blocks for a day
  POST /api/book                   {name, phone, services:[ids], date, start:"HH:MM"}
  GET  /api/mine?phone=            appointments for a phone number
  POST /api/cancel                 {id, phone}
  GET  /api/agenda?key=            barber view (key = barber phone digits)

A background thread scans for appointments starting in ~1 hour and
records a "reminder" entry in the notifications log (visible in the
Agenda view). No external messaging is sent — by the owner's decision.
"""

import json
import os
import sqlite3
import threading
from datetime import date, datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BOOKINGS_DB", os.path.join(BASE_DIR, "bookings.db"))
PORT = int(os.environ.get("PORT", "8000"))
TZ = ZoneInfo("America/New_York")
BARBER_KEY_DIGITS = "6463798816"

BUSINESS = {
    "name": "Edison Barber Studio",
    "owner": "Edison Pena",
    "phone": "646-379-8816",
    "phoneDigits": BARBER_KEY_DIGITS,
    "email": "Veterano7211@yahoo.com",
    "address": "132-13 14 Ave, College Point, NY 11356",
    "mapsUrl": "https://www.google.com/maps/search/?api=1&query=132-13%2014%20Ave%20College%20Point%20NY%2011356",
    "locationLine": "Whitestone Shopping Center — between the pizzeria and the liquor store",
    "reviewsLine": "⭐ 5.0 on Booksy — 260+ verified client reviews",
    "licenseLine": "Licensed Cosmetologist — State of New York · Valid through Oct. 2027",
}

# id, name, price (cash USD), minutes, photo file, description EN, description ES
SERVICES = [
    ("regular-cut", "Regular Cut", 30, 30, "regular-cut.jpg",
     "Classic cut with clippers & scissors, finished with a clean lineup.",
     "Corte clásico con máquina y tijera, terminado con un delineado limpio."),
    ("fades", "Fades", 30, 30, "fades.jpg",
     "Low, mid or high fade blended to your style, with lineup.",
     "Degradado bajo, medio o alto difuminado a tu estilo, con delineado."),
    ("hot-towel-shave", "Hot Towel Shave", 30, 30, "hot-towel-shave.jpg",
     "Straight-razor shave with hot towel and a smooth finish.",
     "Afeitado con navaja y toalla caliente, con un acabado suave."),
    ("beard-trim", "Beard Trim Shape Up", 15, 20, "beard-trim.jpg",
     "Beard trimmed and shaped, edges defined with the razor.",
     "Barba recortada y perfilada, bordes definidos con la navaja."),
    ("shape-up", "Shape Up With Razor", 15, 15, "shape-up.jpg",
     "Hairline and edges cleaned up sharp with a straight razor.",
     "Línea del cabello y bordes bien limpios y marcados con navaja."),
    ("kids-cut", "Kids Under 12", 25, 30, "kids-cut.jpg",
     "Haircut for kids under 12 — patient and careful.",
     "Corte para niños menores de 12 años — con paciencia y cuidado."),
    ("goatee", "Goatee Trim And Shape", 15, 15, "goatee.jpg",
     "Goatee trimmed, shaped and lined up clean.",
     "Perilla recortada, perfilada y delineada, bien limpia."),
    ("shampoo", "Shampoo", 12, 15, "shampoo.jpg",
     "Wash with scalp massage, before or after your cut.",
     "Lavado con masaje del cuero cabelludo, antes o después del corte."),
    ("face-mask", "Face Mask", 15, 15, "face-mask.jpg",
     "Black mask that deep-cleans the skin and pores.",
     "Mascarilla negra que limpia a fondo la piel y los poros."),
    ("eyebrows", "Eyebrows", 10, 10, "eyebrows.jpg",
     "Eyebrow cleanup and shaping for a clean look.",
     "Limpieza y perfilado de cejas para un aspecto limpio."),
    ("full-service", "Haircut Shave & Shampoo", 60, 60, "full-service.jpg",
     "The full service: haircut, hot towel shave and shampoo.",
     "El servicio completo: corte, afeitado con toalla caliente y lavado."),
]
SERVICE_BY_ID = {s[0]: s for s in SERVICES}

# Opening blocks per weekday (Python: Mon=0 .. Sun=6), minutes from midnight.
# Split shift 9:00-13:00 + 14:00-19:00; Sunday 9:00-12:00; Tuesday closed.
HOURS = {
    0: [(540, 780), (840, 1140)],   # Monday
    1: [],                          # Tuesday — CLOSED
    2: [(540, 780), (840, 1140)],   # Wednesday
    3: [(540, 780), (840, 1140)],   # Thursday
    4: [(540, 780), (840, 1140)],   # Friday
    5: [(540, 780), (840, 1140)],   # Saturday
    6: [(540, 720)],                # Sunday 9 AM-12 PM
}
SLOT_STEP = 30          # candidate start times every 30 minutes
MAX_ADVANCE_DAYS = 30

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name   TEXT NOT NULL,
                client_phone  TEXT NOT NULL,
                phone_digits  TEXT NOT NULL,
                date          TEXT NOT NULL,          -- YYYY-MM-DD (shop local)
                start_min     INTEGER NOT NULL,       -- minutes from midnight
                end_min       INTEGER NOT NULL,
                services      TEXT NOT NULL,          -- JSON list of service ids
                service_names TEXT NOT NULL,          -- JSON list of names
                total_mins    INTEGER NOT NULL,
                total_price   INTEGER NOT NULL,
                status        TEXT NOT NULL DEFAULT 'confirmed',
                reminder_logged INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER,
                kind           TEXT NOT NULL,         -- new | cancel | reminder
                message        TEXT NOT NULL,
                created_at     TEXT NOT NULL
            );
            """
        )


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_notification(conn, appt_id, kind, message):
    conn.execute(
        "INSERT INTO notifications (appointment_id, kind, message, created_at)"
        " VALUES (?,?,?,?)",
        (appt_id, kind, message, now_iso()),
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def fmt_time(minutes):
    """Minutes from midnight -> '10:00 AM'."""
    h, m = divmod(minutes, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def parse_hhmm(text):
    try:
        h, m = text.strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return None


def digits_of(phone):
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def same_phone(a, b):
    da, db = digits_of(a), digits_of(b)
    if not da or not db:
        return False
    return da[-10:] == db[-10:]


def appt_to_dict(row):
    return {
        "id": row["id"],
        "clientName": row["client_name"],
        "clientPhone": row["client_phone"],
        "date": row["date"],
        "startTime": fmt_time(row["start_min"]),
        "endTime": fmt_time(row["end_min"]),
        "startMin": row["start_min"],
        "endMin": row["end_min"],
        "services": json.loads(row["service_names"]),
        "serviceIds": json.loads(row["services"]),
        "totalMins": row["total_mins"],
        "totalPrice": row["total_price"],
        "status": row["status"],
        "createdAt": row["created_at"],
    }


def day_blocks(conn, date_str):
    rows = conn.execute(
        "SELECT * FROM appointments WHERE date=? AND status='confirmed'"
        " ORDER BY start_min",
        (date_str,),
    ).fetchall()
    return rows


def validate_booking(date_str, start_min, total_mins):
    """Return (ok, error_code). Checks date range, hours fit, no overlap."""
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        return False, "invalid_date"
    today = datetime.now(TZ).date()
    if d < today or d > today + timedelta(days=MAX_ADVANCE_DAYS):
        return False, "date_out_of_range"
    blocks = HOURS[d.weekday()]
    if not blocks:
        return False, "closed"
    end_min = start_min + total_mins
    fits = any(b0 <= start_min and end_min <= b1 for b0, b1 in blocks)
    if not fits:
        return False, "outside_hours"
    with _db_lock, db() as conn:
        for row in day_blocks(conn, date_str):
            if start_min < row["end_min"] and row["start_min"] < end_min:
                return False, "slot_taken"
    return True, None


# --------------------------------------------------------------------------
# Reminder thread: ~1 hour before each appointment, log a reminder entry.
# --------------------------------------------------------------------------

def reminder_loop():
    while True:
        try:
            now = datetime.now(TZ)
            with _db_lock, db() as conn:
                rows = conn.execute(
                    "SELECT * FROM appointments WHERE status='confirmed'"
                    " AND reminder_logged=0 AND date>=?",
                    (now.date().isoformat(),),
                ).fetchall()
                for row in rows:
                    d = date.fromisoformat(row["date"])
                    start_dt = datetime.combine(
                        d, dtime(row["start_min"] // 60, row["start_min"] % 60), TZ
                    )
                    delta = (start_dt - now).total_seconds()
                    if 0 < delta <= 3600:
                        msg = (
                            f"⏰ Reminder due (1h before) — {', '.join(json.loads(row['service_names']))}"
                            f" · {row['date']} {fmt_time(row['start_min'])}"
                            f" · {row['client_name']} · {row['client_phone']}"
                        )
                        log_notification(conn, row["id"], "reminder", msg)
                        conn.execute(
                            "UPDATE appointments SET reminder_logged=1 WHERE id=?",
                            (row["id"],),
                        )
                conn.commit()
        except Exception as exc:  # never let the thread die
            print("reminder_loop error:", exc)
        threading.Event().wait(60)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "EdisonBarber/1.0"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/api/config":
            return self.api_config()
        if path == "/api/day":
            return self.api_day(qs)
        if path == "/api/mine":
            return self.api_mine(qs)
        if path == "/api/agenda":
            return self.api_agenda(qs)
        if path.startswith("/api/"):
            return self.send_json({"ok": False, "error": "not_found"}, 404)
        return self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/book":
            return self.api_book()
        if path == "/api/cancel":
            return self.api_cancel()
        return self.send_json({"ok": False, "error": "not_found"}, 404)

    # -- static ------------------------------------------------------------
    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        if path == "/privacy-policy":
            path = "/privacy-policy.html"
        if path == "/terms":
            path = "/terms.html"
        full = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
        if not full.startswith(BASE_DIR) or not os.path.isfile(full):
            return self.send_json({"ok": False, "error": "not_found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- API ---------------------------------------------------------------
    def api_config(self):
        services = [
            {
                "id": s[0], "name": s[1], "price": s[2], "mins": s[3],
                "img": "img/" + s[4], "descEn": s[5], "descEs": s[6],
            }
            for s in SERVICES
        ]
        hours = {str(k): v for k, v in HOURS.items()}
        self.send_json({
            "ok": True, "business": BUSINESS, "services": services,
            "hours": hours, "slotStep": SLOT_STEP,
            "maxAdvanceDays": MAX_ADVANCE_DAYS,
        })

    def api_day(self, qs):
        date_str = (qs.get("date") or [""])[0]
        try:
            date.fromisoformat(date_str)
        except Exception:
            return self.send_json({"ok": False, "error": "invalid_date"}, 400)
        with _db_lock, db() as conn:
            rows = day_blocks(conn, date_str)
        blocks = [
            {"startMin": r["start_min"], "endMin": r["end_min"],
             "start": fmt_time(r["start_min"]), "end": fmt_time(r["end_min"])}
            for r in rows
        ]
        self.send_json({"ok": True, "date": date_str, "blocks": blocks})

    def api_book(self):
        data = self.read_json()
        name = str(data.get("name") or "").strip()
        phone = str(data.get("phone") or "").strip()
        service_ids = data.get("services") or []
        date_str = str(data.get("date") or "")
        start_min = parse_hhmm(str(data.get("start") or ""))
        if not name:
            return self.send_json({"ok": False, "error": "name_required"}, 400)
        if len(digits_of(phone)) < 7:
            return self.send_json({"ok": False, "error": "phone_required"}, 400)
        if not service_ids or any(s not in SERVICE_BY_ID for s in service_ids):
            return self.send_json({"ok": False, "error": "invalid_service"}, 400)
        if start_min is None:
            return self.send_json({"ok": False, "error": "invalid_time"}, 400)
        total_mins = sum(SERVICE_BY_ID[s][3] for s in service_ids)
        total_price = sum(SERVICE_BY_ID[s][2] for s in service_ids)
        ok, err = validate_booking(date_str, start_min, total_mins)
        if not ok:
            status = 409 if err == "slot_taken" else 400
            return self.send_json({"ok": False, "error": err}, status)
        names = [SERVICE_BY_ID[s][1] for s in service_ids]
        with _db_lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO appointments (client_name, client_phone, phone_digits,"
                " date, start_min, end_min, services, service_names, total_mins,"
                " total_price, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, phone, digits_of(phone), date_str, start_min,
                 start_min + total_mins, json.dumps(service_ids),
                 json.dumps(names), total_mins, total_price, "confirmed",
                 now_iso()),
            )
            appt_id = cur.lastrowid
            msg = (
                f"🔔 New booking — {', '.join(names)} · {date_str}"
                f" {fmt_time(start_min)} ({total_mins} min) · {name} · {phone}"
                f" · Total ${total_price}"
            )
            log_notification(conn, appt_id, "new", msg)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM appointments WHERE id=?", (appt_id,)
            ).fetchone()
        self.send_json({"ok": True, "appointment": appt_to_dict(row)})

    def api_mine(self, qs):
        phone = (qs.get("phone") or [""])[0]
        if len(digits_of(phone)) < 7:
            return self.send_json({"ok": False, "error": "phone_required"}, 400)
        with _db_lock, db() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments ORDER BY date DESC, start_min DESC"
            ).fetchall()
        mine = [appt_to_dict(r) for r in rows
                if same_phone(r["client_phone"], phone)]
        self.send_json({"ok": True, "appointments": mine})

    def api_cancel(self):
        data = self.read_json()
        appt_id = data.get("id")
        phone = str(data.get("phone") or "")
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT * FROM appointments WHERE id=?", (appt_id,)
            ).fetchone()
            if row is None:
                return self.send_json({"ok": False, "error": "not_found"}, 404)
            if not same_phone(row["client_phone"], phone):
                return self.send_json({"ok": False, "error": "phone_mismatch"}, 403)
            if row["status"] == "cancelled":
                return self.send_json({"ok": False, "error": "already_cancelled"}, 409)
            conn.execute(
                "UPDATE appointments SET status='cancelled' WHERE id=?",
                (appt_id,),
            )
            names = json.loads(row["service_names"])
            msg = (
                f"❌ Booking cancelled — {', '.join(names)} · {row['date']}"
                f" {fmt_time(row['start_min'])} · {row['client_name']}"
                f" · {row['client_phone']}"
            )
            log_notification(conn, appt_id, "cancel", msg)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM appointments WHERE id=?", (appt_id,)
            ).fetchone()
        self.send_json({"ok": True, "appointment": appt_to_dict(row)})

    def api_agenda(self, qs):
        key = digits_of((qs.get("key") or [""])[0])
        if len(key) == 11 and key.startswith("1"):
            key = key[1:]
        if key != BARBER_KEY_DIGITS:
            return self.send_json({"ok": False, "error": "forbidden"}, 403)
        today = datetime.now(TZ).date().isoformat()
        with _db_lock, db() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status='confirmed' AND date>=?"
                " ORDER BY date, start_min",
                (today,),
            ).fetchall()
            notes = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT 100"
            ).fetchall()
        self.send_json({
            "ok": True,
            "appointments": [appt_to_dict(r) for r in rows],
            "notifications": [
                {"id": n["id"], "appointmentId": n["appointment_id"],
                 "kind": n["kind"], "message": n["message"],
                 "createdAt": n["created_at"]}
                for n in notes
            ],
        })


def main():
    init_db()
    threading.Thread(target=reminder_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Edison Barber Studio server listening on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
