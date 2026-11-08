"""
shared_store.py - the shared communication layer between patient and clinician (SQLite, stdlib only).

One thread per patient. Everything that happens between the two sides is a thread event:
  - a message from the patient or the clinician,
  - a system event: profile shared / withdrawn, movement session completed, EEG report attached.

Consent: the clinician sees a patient's profile only while a share is active (shared, not withdrawn).
The patient never receives EEG numbers through the thread: attaching a report posts a plain sentence.

Identity is a demo stand-in (a fixed demo patient and a role chosen in the UI); there are no accounts
yet, so only fictional data belongs here. Database: agent/data/fieldnote.db (gitignored).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "fieldnote.db"
DEMO_PATIENT = {"id": "P-0001", "name": "Jordan M.", "age": "68", "hand": "Right"}
ROLES = ("patient", "clinician")
MAX_BODY = 4000
_PID = re.compile(r"^P-\d{4}$")
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, age TEXT, hand TEXT, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shares (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL REFERENCES patients(id),
  snapshot TEXT NOT NULL, shared_utc TEXT NOT NULL, withdrawn_utc TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL REFERENCES patients(id),
  author TEXT NOT NULL CHECK (author IN ('patient','clinician','system')),
  kind TEXT NOT NULL DEFAULT 'message', body TEXT NOT NULL, attachment TEXT,
  visible_to TEXT NOT NULL DEFAULT 'both' CHECK (visible_to IN ('both','clinician')),
  created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reads (
  patient_id TEXT NOT NULL, role TEXT NOT NULL, last_read_id INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (patient_id, role));
CREATE TABLE IF NOT EXISTS links (
  patient_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('session','report')), ref TEXT NOT NULL,
  created_utc TEXT NOT NULL, PRIMARY KEY (patient_id, kind, ref));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.execute("INSERT OR IGNORE INTO patients (id, name, age, hand, created_utc) VALUES (?,?,?,?,?)",
                      (DEMO_PATIENT["id"], DEMO_PATIENT["name"], DEMO_PATIENT["age"], DEMO_PATIENT["hand"], _now()))
        _conn.commit()
    return _conn


def use_db(path: Path) -> None:
    """Point the store at another database file (tests)."""
    global _conn, DB_PATH
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, DB_PATH = None, Path(path)


def check_pid(pid) -> str:
    if not isinstance(pid, str) or not _PID.match(pid):
        raise KeyError(pid)
    with _lock:
        if db().execute("SELECT 1 FROM patients WHERE id=?", (pid,)).fetchone() is None:
            raise KeyError(pid)
    return pid


def _role(role) -> str:
    if role not in ROLES:
        raise ValueError("role must be 'patient' or 'clinician'")
    return role


# ------------------------------------------------------------------ messages
def _post(pid: str, author: str, body: str, kind: str = "message", attachment: dict | None = None,
          visible_to: str = "both") -> dict:
    with _lock:
        c = db()
        cur = c.execute("INSERT INTO messages (patient_id, author, kind, body, attachment, visible_to, created_utc)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (pid, author, kind, body, json.dumps(attachment) if attachment else None, visible_to, _now()))
        c.commit()
        row = c.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
    return _msg(row)


def _msg(r) -> dict:
    return {"id": r["id"], "author": r["author"], "kind": r["kind"], "body": r["body"],
            "attachment": json.loads(r["attachment"]) if r["attachment"] else None,
            "visible_to": r["visible_to"], "created_utc": r["created_utc"]}


def post_message(pid: str, role: str, body) -> dict:
    check_pid(pid)
    _role(role)
    if not isinstance(body, str) or not body.strip():
        raise ValueError("message is empty")
    return _post(pid, role, body.strip()[:MAX_BODY])


def messages(pid: str, role: str, after_id: int = 0) -> list[dict]:
    check_pid(pid)
    _role(role)
    vis = ("both",) if role == "patient" else ("both", "clinician")
    if not 0 <= int(after_id) < 2**63:
        raise ValueError("bad message position")
    with _lock:
        rows = db().execute(
            f"SELECT * FROM messages WHERE patient_id=? AND id>? AND visible_to IN ({','.join('?' * len(vis))})"
            " ORDER BY id", (pid, int(after_id), *vis)).fetchall()
    return [_msg(r) for r in rows]


def mark_read(pid: str, role: str, upto_id: int) -> None:
    check_pid(pid)
    _role(role)
    if not 0 <= int(upto_id) < 2**63:
        raise ValueError("bad read position")
    with _lock:
        c = db()
        c.execute("INSERT INTO reads (patient_id, role, last_read_id) VALUES (?,?,?) ON CONFLICT(patient_id, role)"
                  " DO UPDATE SET last_read_id=MAX(last_read_id, excluded.last_read_id)", (pid, role, int(upto_id)))
        c.commit()


# System events caused by each role's own actions: not "unread" for that role.
_OWN_EVENTS = {"patient": ("profile_shared", "profile_withdrawn", "session"),
               "clinician": ("report", "report_patient")}


def unread(pid: str, role: str) -> int:
    """Messages the other side (or the system, for events this role did not cause) posted since last read."""
    vis = ("both",) if role == "patient" else ("both", "clinician")
    own = _OWN_EVENTS[role]
    with _lock:
        c = db()
        r = c.execute("SELECT last_read_id FROM reads WHERE patient_id=? AND role=?", (pid, role)).fetchone()
        last = r["last_read_id"] if r else 0
        n = c.execute(f"SELECT COUNT(*) FROM messages WHERE patient_id=? AND id>? AND author!=?"
                      f" AND visible_to IN ({','.join('?' * len(vis))})"
                      f" AND NOT (author='system' AND kind IN ({','.join('?' * len(own))}))",
                      (pid, last, role, *vis, *own)).fetchone()[0]
    return n


# ------------------------------------------------------------------ consent-scoped profile sharing
_SNAPSHOT_KEYS = ("name", "age", "hand", "summary", "records", "sharedAt")


def share_profile(pid: str, snapshot) -> dict:
    check_pid(pid)
    if not isinstance(snapshot, dict) or not str(snapshot.get("summary") or "").strip():
        raise ValueError("snapshot needs a confirmed summary")
    snap = json.dumps({k: snapshot.get(k) for k in _SNAPSHOT_KEYS})
    if len(snap) > 200_000:
        raise ValueError("profile too large to share")
    with _lock:
        c = db()
        c.execute("UPDATE shares SET withdrawn_utc=? WHERE patient_id=? AND withdrawn_utc IS NULL", (_now(), pid))
        c.execute("INSERT INTO shares (patient_id, snapshot, shared_utc) VALUES (?,?,?)",
                  (pid, snap, _now()))
        c.commit()
    _post(pid, "system", "Profile shared with the clinician.", kind="profile_shared")
    return active_share(pid)


def withdraw_profile(pid: str) -> None:
    check_pid(pid)
    with _lock:
        c = db()
        n = c.execute("UPDATE shares SET withdrawn_utc=? WHERE patient_id=? AND withdrawn_utc IS NULL",
                      (_now(), pid)).rowcount
        c.commit()
    if n:
        _post(pid, "system", "Profile access withdrawn by the patient.", kind="profile_withdrawn")


def active_share(pid: str) -> dict | None:
    with _lock:
        r = db().execute("SELECT * FROM shares WHERE patient_id=? AND withdrawn_utc IS NULL ORDER BY id DESC LIMIT 1",
                         (pid,)).fetchone()
    if not r:
        return None
    try:
        return {"snapshot": json.loads(r["snapshot"]), "shared_utc": r["shared_utc"]}
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ sessions and reports
def link_session(pid: str, session_id: str, summary: dict) -> None:
    """Called by the server when a movement session finishes."""
    check_pid(pid)
    with _lock:
        c = db()
        new = c.execute("INSERT OR IGNORE INTO links (patient_id, kind, ref, created_utc) VALUES (?,?,?,?)",
                        (pid, "session", session_id, _now())).rowcount
        c.commit()
    if not new:
        return
    res = summary.get("result") or {}
    full = summary.get("mode") == "full"
    if res.get("completed"):
        body = "Completed a movement session." if full else "Completed a practice rehearsal."
    else:
        body = f"Movement session stopped early ({res.get('aborted_reason') or 'incomplete'})."
    _post(pid, "system", body, kind="session",
          attachment={"type": "session", "session_id": session_id, "mode": summary.get("mode"),
                      "completed": bool(res.get("completed")), "cues": summary.get("cues_marked")})


def link_report(pid: str, recording_id: str, answer: str, simulated: bool) -> None:
    """Clinician attaches an EEG report. The clinician sees the label; the patient sees a plain sentence."""
    check_pid(pid)
    with _lock:
        c = db()
        new = c.execute("INSERT OR IGNORE INTO links (patient_id, kind, ref, created_utc) VALUES (?,?,?,?)",
                        (pid, "report", recording_id, _now())).rowcount
        c.commit()
    if not new:
        return
    _post(pid, "system", f"EEG report {recording_id} attached ({answer}{', simulated' if simulated else ''}).",
          kind="report", attachment={"type": "report", "recording_id": recording_id, "answer": answer,
                                     "simulated": simulated}, visible_to="clinician")
    _post(pid, "system", "Your care team has the results of your movement session and will go through "
          "them with you.", kind="report_patient")


def links(pid: str, kind: str) -> list[str]:
    with _lock:
        return [r["ref"] for r in db().execute(
            "SELECT ref FROM links WHERE patient_id=? AND kind=? ORDER BY created_utc DESC", (pid, kind))]


# ------------------------------------------------------------------ views
def patients_overview() -> list[dict]:
    with _lock:
        rows = db().execute("SELECT * FROM patients ORDER BY id").fetchall()
    out = []
    for r in rows:
        pid = r["id"]
        with _lock:
            last = db().execute("SELECT * FROM messages WHERE patient_id=? ORDER BY id DESC LIMIT 1",
                                (pid,)).fetchone()
        out.append({"id": pid, "name": r["name"], "shared": active_share(pid) is not None,
                    "unread": unread(pid, "clinician"), "last": _msg(last) if last else None,
                    "reports": links(pid, "report"), "sessions": len(links(pid, "session"))})
    return out


def patient_state(pid: str, role: str) -> dict:
    """Everything one side needs for one patient. The patient gets no report IDs."""
    check_pid(pid)
    _role(role)
    with _lock:
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    share = active_share(pid)
    out = {"patient": {"id": pid, "name": p["name"]}, "shared": share is not None,
           "unread": unread(pid, role), "sessions": links(pid, "session")}
    if role == "clinician":
        out["share"] = share
        out["reports"] = links(pid, "report")
    return out
