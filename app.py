from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
import base64
import io
import json
import os
import secrets
import shutil
import tempfile
import re
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import face_recognition
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for, abort, g
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from models import db, Admin, Teacher, Student, Attendance, SchoolCalendar, AuditLog, SchoolClock
from face_utils import get_face_encodings, recognize_faces, best_match_for_encoding, image_quality
from exports import build_xlsx, build_pdf

try:
    from pypdf import PdfReader
except Exception:  # optional until calendar import is used
    PdfReader = None

try:
    import pytesseract
except Exception:  # optional; image calendar OCR can fail gracefully
    pytesseract = None


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
LOCAL_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))


def _parse_clock_setting(name, default):
    raw = str(os.getenv(name, default)).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        print(f"WARNING: invalid {name}={raw!r}; using {default}")
        return datetime.strptime(default, "%H:%M").time()


ATTENDANCE_PRESENT_FROM = _parse_clock_setting("ATTENDANCE_PRESENT_FROM", "07:30")
ATTENDANCE_LATE_AFTER = _parse_clock_setting("ATTENDANCE_LATE_AFTER", "08:30")
ATTENDANCE_ABSENT_AFTER = _parse_clock_setting("ATTENDANCE_ABSENT_AFTER", "09:00")


def now_local():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def today_local():
    return now_local().date()


def attendance_label(day, attendance=None, current_time=None):
    """Return the user-facing attendance state for a student on a date.

    Stored records always win. For the current day, an unmarked student is
    shown as Absent before 07:30, Not Marked from 07:30 until the late window,
    Late from 08:30 until 09:00, and Absent from 09:00 onward. Historical
    unmarked days are Absent.
    """
    if attendance is not None:
        status = str(attendance.status or "absent").strip().lower()
        if status == "present":
            return "Present"
        if status == "late":
            return "Late"
        return "Absent"

    if day != today_local():
        return "Absent"

    current = current_time if current_time is not None else effective_time_for_request()
    if current < ATTENDANCE_PRESENT_FROM:
        return "Absent"
    if current >= ATTENDANCE_ABSENT_AFTER:
        return "Absent"
    if current >= ATTENDANCE_LATE_AFTER:
        return "Late"
    return "Not Marked"


# ---------------------------------------------------------------------------
# Database: Supabase/Postgres first, local SQLite fallback for development.
# ---------------------------------------------------------------------------
raw_db_url = (os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL") or "").strip()
if raw_db_url.startswith("postgres://"):
    raw_db_url = "postgresql://" + raw_db_url[len("postgres://"):]
if raw_db_url:
    DATABASE_URL = raw_db_url
else:
    DATABASE_URL = f"sqlite:///{(BASE_DIR / 'attendance.db').as_posix()}"
    print("WARNING: SUPABASE_DB_URL/DATABASE_URL is not set; using local SQLite.")


# Ephemeral capture storage only. Trained encodings are persisted in Supabase.
DATA_ROOT = Path(os.getenv("DATA_DIR", str(BASE_DIR))).resolve()
FACE_DATA = DATA_ROOT / "face_data"
SCAN_TMP = FACE_DATA / "_scan_tmp"
PENDING_DIR = FACE_DATA / "_pending"
for folder in (FACE_DATA, SCAN_TMP, PENDING_DIR):
    folder.mkdir(parents=True, exist_ok=True)


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300},
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"),
)
db.init_app(app)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def json_error(message, status=400):
    return jsonify({"error": message}), status


def audit(action, message, target_type=None, target_id=None):
    if session.get("admin_id"):
        actor_type, actor_name = "admin", session.get("admin_username", "admin")
    elif session.get("teacher_id"):
        actor_type, actor_name = "teacher", session.get("teacher_name", "teacher")
    else:
        actor_type, actor_name = "system", "scanner"
    db.session.add(AuditLog(
        actor_type=actor_type,
        actor_name=actor_name,
        action=action,
        target_type=target_type,
        target_id=target_id,
        message=message[:500],
    ))


def current_teacher():
    tid = session.get("teacher_id")
    return db.session.get(Teacher, tid) if tid else None


def session_role():
    if session.get("admin_id"):
        return "admin"
    if current_teacher():
        return "teacher"
    return None


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token()}


def require_csrf():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    expected = session.get("csrf_token")
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
        return json_error("Security token expired. Refresh the page and try again.", 400) if request.path.startswith("/api/") else abort(400, description="Security token expired. Refresh the page and try again.")
    return None


@app.before_request
def before_request():
    # CSRF protects all state-changing browser requests. Public GET scanner remains open.
    return require_csrf()


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self)")
    return response


# ---------------------------------------------------------------------------
# Calendar: original behaviour preserved - overrides beat weekly default.
# ---------------------------------------------------------------------------
_MONTHS = {
    name.lower(): index
    for index, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
_MONTH_ALIASES = {**_MONTHS}
_MONTH_ALIASES.update({
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
})
_DATE_PATTERNS = [
    re.compile(r"\b(?P<y>20\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})\b"),
    re.compile(r"\b(?P<d>\d{1,2})[-/.](?P<m>\d{1,2})[-/.](?P<y>20\d{2})\b"),
    re.compile(r"\b(?P<d>\d{1,2})\s+(?P<m>[A-Za-z]{3,9})\s*,?\s*(?P<y>20\d{2})\b", re.I),
    re.compile(r"\b(?P<m>[A-Za-z]{3,9})\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(?P<y>20\d{2})\b", re.I),
]
_CALENDAR_NONWORKING_WORDS = (
    "holiday", "holidays", "vacation", "vacations", "closed", "non-working",
    "non working", "no school", "school holiday", "festival", "break", "leave",
)
_CALENDAR_WORKING_WORDS = (
    "working day", "working days", "school day", "school days", "instruction day",
    "instruction days", "open", "working",
)


def _calendar_date_from_match(match, fallback_year=None):
    values = match.groupdict()
    raw_year = values.get("y") or fallback_year
    try:
        year = int(raw_year)
        month_raw = values.get("m")
        month = int(month_raw) if str(month_raw).isdigit() else _MONTH_ALIASES.get(str(month_raw).lower())
        day = int(values.get("d"))
        if month is None or not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        return datetime(year, month, day).date()
    except (TypeError, ValueError):
        return None


def parse_calendar_text(text_value):
    """Extract calendar dates and classify them conservatively from nearby text."""
    text_value = re.sub(r"[\u00a0\r]+", " ", text_value or "")
    text_value = re.sub(r"[ \t]+", " ", text_value)
    lines = [line.strip() for line in text_value.split("\n") if line.strip()]
    detected = {}
    year_hints = [int(x) for x in re.findall(r"\b(20\d{2})\b", text_value)]
    fallback_year = year_hints[0] if year_hints else today_local().year

    for line_index, line in enumerate(lines):
        matches = []
        for pattern in _DATE_PATTERNS:
            matches.extend(pattern.finditer(line))
        if not matches:
            continue

        context = " ".join(lines[max(0, line_index - 1):line_index + 1])
        low = context.lower()
        non_working = any(word in low for word in _CALENDAR_NONWORKING_WORDS)
        working = any(word in low for word in _CALENDAR_WORKING_WORDS)
        proposed = False if non_working else True if working else None
        reason = "Holiday / non-working" if non_working else "Working day" if working else "Detected date"

        for match in matches:
            day = _calendar_date_from_match(match, fallback_year=fallback_year)
            if not day:
                continue
            if day in detected and detected[day]["is_working"] is not None and proposed is None:
                continue
            detected[day] = {"date": day.isoformat(), "is_working": proposed, "reason": reason}

    # Dates with no explicit clue fall back to the school's normal weekly rule.
    for day, item in detected.items():
        if item["is_working"] is None:
            item["is_working"] = weekly_default_is_working(day)
            item["reason"] = "Imported; weekly default used"

    rows = sorted(detected.values(), key=lambda x: x["date"])
    return rows


def extract_calendar_upload_text(storage):
    """Read a text-based PDF or OCR an image calendar."""
    filename = (storage.filename or "").lower()
    raw = storage.read()
    if not raw:
        raise ValueError("The calendar file is empty.")

    if filename.endswith(".pdf") or storage.mimetype == "application/pdf":
        if PdfReader is None:
            raise ValueError("PDF import is unavailable because the PDF reader dependency is missing.")
        try:
            reader = PdfReader(io.BytesIO(raw))
            text_parts = [(page.extract_text() or "") for page in reader.pages]
            text_value = "\n".join(text_parts).strip()
        except Exception as exc:
            raise ValueError("The PDF could not be read. Please upload a text-readable school calendar PDF.") from exc
        if not text_value:
            raise ValueError("This PDF contains no selectable text. Try the image version or a text-based PDF.")
        return text_value

    if filename.endswith((".png", ".jpg", ".jpeg", ".webp")) or storage.mimetype.startswith("image/"):
        if pytesseract is None:
            raise ValueError("Image calendar OCR is not installed on this server. A PDF calendar can still be imported.")
        try:
            image = np.frombuffer(raw, dtype=np.uint8)
            frame = cv2.imdecode(image, cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("The calendar image could not be decoded.")
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if max(gray.shape[:2]) < 1600:
                scale = 1600 / max(gray.shape[:2])
                gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            gray = cv2.GaussianBlur(gray, (3, 3), 0)
            text_value = pytesseract.image_to_string(gray, config="--psm 6")
        except Exception as exc:
            raise ValueError("The calendar image could not be read automatically.") from exc
        if not text_value.strip():
            raise ValueError("No calendar dates could be read from that image.")
        return text_value

    raise ValueError("Upload a PDF, PNG, JPG, JPEG or WEBP school calendar.")

def get_working_days_setting():
    try:
        return 5 if int(os.getenv("WORKING_DAYS", "6")) == 5 else 6
    except (TypeError, ValueError):
        return 6


def weekly_default_is_working(day):
    return day.weekday() < get_working_days_setting()


def is_working_day(day):
    override = SchoolCalendar.query.filter_by(date=day).first()
    return bool(override.is_working) if override else weekly_default_is_working(day)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admin authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def teacher_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        teacher = current_teacher()
        if not teacher or not teacher.active:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Teacher authentication required"}), 401
            return redirect(url_for("teacher_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("admin_id") or (current_teacher() and current_teacher().active):
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("teacher_login", next=request.path))
    return wrapped


@app.context_processor
def inject_nav_counts():
    try:
        if session.get("admin_id"):
            return {"nav_students": Student.query.filter_by(active=True).count()}
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Face recognition helpers - the original face_utils algorithm remains intact.
# ---------------------------------------------------------------------------
def decode_data_url(image_data):
    if not isinstance(image_data, str) or "," not in image_data:
        raise ValueError("Invalid image data")
    encoded = image_data.split(",", 1)[1]
    if len(encoded) > 12_000_000:
        raise ValueError("Image is too large")
    raw = base64.b64decode(encoded, validate=True)
    frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Unable to decode image")
    return frame


def encodings_to_json(encodings):
    return json.dumps([np.asarray(enc, dtype=np.float64).tolist() for enc in encodings], separators=(",", ":"))


def json_to_encodings_array(face_encodings_json):
    if not face_encodings_json:
        return np.empty((0, 128), dtype=np.float64)
    arr = np.asarray(json.loads(face_encodings_json), dtype=np.float64)
    if arr.size == 0:
        return np.empty((0, 128), dtype=np.float64)
    if arr.ndim == 1:
        arr = np.expand_dims(arr, 0)
    return arr


def pending_info():
    data = session.get("pending_registration")
    return data if data and data.get("token") else None


def cleanup_pending_registrations():
    cutoff = now_local().timestamp() - 24 * 60 * 60
    for folder in PENDING_DIR.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def ensure_default_admin():
    username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    password = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
    admin = Admin.query.filter(func.lower(Admin.username) == username.lower()).first()
    if admin is None:
        db.session.add(Admin(username=username, password_hash=generate_password_hash(password)))
        db.session.commit()


def normalize_text(value, max_len):
    text_value = " ".join(str(value or "").strip().split())
    return text_value[:max_len]


def normalize_class(value):
    return normalize_text(value, 30).upper()


def normalize_section(value):
    return normalize_text(value, 10).upper()


def uppercase_school_field(value, label, max_len):
    """Validate school-facing fields that must be entered in uppercase.

    The UI also transforms typing to uppercase, while the server enforces the
    rule so lowercase input cannot bypass the form with a direct request.
    """
    raw = normalize_text(value, max_len)
    if not raw:
        return "", None
    if raw != raw.upper() or any(ch.isalpha() and not ch.isupper() for ch in raw):
        return "", f"{label} must be entered in CAPITAL LETTERS."
    return raw, None


def roll_sort_key(value):
    raw = str(value or "").strip()
    if not raw:
        return (1, float("inf"), "")
    match = re.match(r"^(\d+)", raw)
    if match:
        return (0, int(match.group(1)), raw.lower())
    return (1, float("inf"), raw.lower())


def get_school_clock_override():
    """Return the shared school test-clock value, if one is configured."""
    try:
        clock = db.session.get(SchoolClock, 1)
        raw = clock.override_time if clock else None
        if raw:
            try:
                return datetime.strptime(str(raw), "%H:%M").strftime("%H:%M")
            except ValueError:
                if clock:
                    clock.override_time = None
                    clock.updated_at = now_local()
                    db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not read shared school clock")
    return None


def effective_time_for_request():
    """Return the school-facing time for attendance/testing only.

    The real server clock is never changed. When the admin selects a test
    time, only attendance-related behavior uses that HH:MM value.
    """
    override = get_school_clock_override()
    if override:
        return datetime.strptime(override, "%H:%M").time()
    return now_local().time()


def effective_school_datetime():
    """Return the virtual school datetime while preserving the real server date.

    This is intentionally separate from now_local(): audit records, logins,
    jobs, database timestamps, and server diagnostics continue to use the
    actual server time. Only school-facing attendance logic should use this.
    """
    live = now_local()
    override = get_school_clock_override()
    if override:
        return datetime.combine(live.date(), datetime.strptime(override, "%H:%M").time())
    return live


def teacher_scope_filter(teacher):
    if not teacher.class_name:
        return True
    scope = func.lower(Student.class_name) == teacher.class_name.lower()
    if teacher.section:
        scope = scope & (func.lower(Student.section) == teacher.section.lower())
    return scope


# ---------------------------------------------------------------------------
# Register rows / filters
# ---------------------------------------------------------------------------
def parse_register_query():
    date_str = request.args.get("date") or today_local().isoformat()
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        day = today_local()
    class_name = request.args.get("class_name") or None
    section = request.args.get("section") or None
    return day, normalize_class(class_name) if class_name else None, normalize_section(section) if section else None


def fetch_register_rows(day, class_name=None, section=None, teacher=None):
    query = Student.query.filter_by(active=True)
    if class_name:
        query = query.filter(func.upper(Student.class_name) == class_name.upper())
    if section:
        query = query.filter(func.upper(Student.section) == section.upper())
    if teacher and teacher.class_name:
        query = query.filter(teacher_scope_filter(teacher))
    students = query.order_by(Student.class_name, Student.section, Student.name).all()
    students.sort(key=lambda s: (s.class_name or "", s.section or "", roll_sort_key(s.roll_number), (s.name or "").lower()))
    student_ids = [s.id for s in students]
    marks = {}
    if student_ids:
        marks = {a.student_id: a for a in Attendance.query.filter(
            Attendance.date == day, Attendance.student_id.in_(student_ids)
        ).all()}
    rows = []
    effective_now = effective_time_for_request() if day == today_local() else None
    for student in students:
        attendance = marks.get(student.id)
        status_label = attendance_label(day, attendance, effective_now)
        rows.append({
            "id": student.id,
            "admission_number": student.admission_number,
            "roll_number": student.roll_number or "",
            "name": student.name,
            "class_name": student.class_name,
            "section": student.section,
            "class_section": f"{student.class_name}-{student.section}",
            "attendance": status_label,
            "time_in": attendance.time_in.strftime("%I:%M %p") if attendance and attendance.time_in else "—",
            "face_trained": bool(student.face_trained),
            "source": attendance.source if attendance else None,
            "note": attendance.note if attendance else None,
        })
    return rows


def class_tree():
    rows = db.session.query(Student.class_name, Student.section, func.count(Student.id)).filter(
        Student.active.is_(True)
    ).group_by(Student.class_name, Student.section).order_by(Student.class_name, Student.section).all()
    grouped = {}
    for class_name, section, count in rows:
        grouped.setdefault(class_name, []).append({"section": section, "count": count})
    return [{"class_name": key, "sections": value, "total": sum(x["count"] for x in value)} for key, value in grouped.items()]


# ---------------------------------------------------------------------------
# Landing + auth
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    today = today_local()
    return render_template(
        "index.html",
        total_students=Student.query.filter_by(active=True).count(),
        today_present=Attendance.query.filter_by(date=today).count(),
        admin_logged_in=bool(session.get("admin_id")),
        teacher_logged_in=bool(current_teacher()),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_id"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        username = normalize_text(request.form.get("username"), 80)
        password = request.form.get("password", "")
        admin = Admin.query.filter(func.lower(Admin.username) == username.lower()).first()
        if not admin or not check_password_hash(admin.password_hash, password):
            return render_template("login.html", error="Invalid username or password"), 401
        session.clear()
        session.permanent = True
        session["admin_id"] = admin.id
        session["admin_username"] = admin.username
        session["csrf_token"] = secrets.token_urlsafe(32)
        admin.last_login = now_local()
        audit("login", f"Admin {admin.username} logged in")
        db.session.commit()
        return redirect(request.args.get("next") or url_for("admin_dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/teacher/login", methods=["GET", "POST"])
def teacher_login():
    """Login for teacher accounts. Username is case-insensitive.

    The current admin password may also be used as a master override for an
    active teacher account. A teacher session is always created separately
    from an admin session.
    """
    if session.get("teacher_id"):
        teacher = current_teacher()
        if teacher and teacher.active:
            return redirect(url_for("teacher_dashboard"))
        session.clear()

    if session.get("admin_id"):
        return redirect(url_for("admin_dashboard"))

    next_url = (
        request.form.get("next")
        if request.method == "POST"
        else request.args.get("next")
    )
    if next_url and not str(next_url).startswith("/"):
        next_url = None

    if request.method == "POST":
        username = normalize_text(request.form.get("username"), 80)
        password = str(request.form.get("password") or "")

        if not username:
            return render_template("teacher_login.html", error="Enter your teacher username.", username=username, next=next_url), 400
        if not password:
            return render_template("teacher_login.html", error="Enter your password.", username=username, next=next_url), 400

        teacher = Teacher.query.filter(
            func.lower(Teacher.username) == username.lower()
        ).first()

        if not teacher:
            return render_template("teacher_login.html", error="No teacher account was found for that username.", username=username, next=next_url), 401

        if not teacher.active:
            return render_template("teacher_login.html", error="This teacher account is disabled. Contact the administrator.", username=username, next=next_url), 403

        teacher_password_ok = False
        try:
            teacher_password_ok = bool(
                teacher.password_hash
                and check_password_hash(teacher.password_hash, password)
            )
        except Exception:
            app.logger.exception("Teacher password verification failed for %s", username)

        admin_for_master = Admin.query.order_by(Admin.id).first()
        master_password_ok = False
        try:
            master_password_ok = bool(
                admin_for_master
                and admin_for_master.password_hash
                and check_password_hash(admin_for_master.password_hash, password)
            )
        except Exception:
            app.logger.exception("Admin master-password verification failed")

        if not teacher_password_ok and not master_password_ok:
            return render_template("teacher_login.html", error="Incorrect username or password.", username=username, next=next_url), 401

        session.clear()
        session.permanent = True
        session["teacher_id"] = teacher.id
        session["teacher_username"] = teacher.username
        session["teacher_name"] = teacher.name
        session["teacher_admin_override"] = bool(master_password_ok)
        session["csrf_token"] = secrets.token_urlsafe(32)

        teacher.last_login = now_local()
        try:
            audit(
                "teacher_login_admin_override" if master_password_ok else "teacher_login",
                f"Teacher {teacher.username} logged in",
                "Teacher",
                teacher.id,
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not write teacher login audit record")
            # Login itself should not fail merely because audit logging failed.

        return redirect(next_url or url_for("teacher_dashboard"))

    return render_template("teacher_login.html", next=next_url)


@app.route("/teacher/logout")
def teacher_logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Admin dashboard / registration
# ---------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template(
        "admin_dashboard.html",
        working_today=is_working_day(today_local()),
        class_count=len(class_tree()),
        teacher_count=Teacher.query.filter_by(active=True).count(),
        school_clock_override=get_school_clock_override(),
        live_time=now_local().strftime("%H:%M"),
    )


def _registration_pose_signature(rgb_frame, face_location):
    """Return a lightweight normalized pose signature for student coaching.

    It is never used for recognition or acceptance; it only lets the browser
    notice repeated near-identical captures and suggest a small pose change.
    """
    try:
        landmarks = face_recognition.face_landmarks(rgb_frame, [face_location])
        if not landmarks:
            raise ValueError
        lm = landmarks[0]
        left_eye = lm.get("left_eye", [])
        right_eye = lm.get("right_eye", [])
        nose = lm.get("nose_tip", [])
        if not left_eye or not right_eye or not nose:
            raise ValueError

        def center(points):
            return (
                sum(point[0] for point in points) / len(points),
                sum(point[1] for point in points) / len(points),
            )

        lx, ly = center(left_eye)
        rx, ry = center(right_eye)
        nx, ny = center(nose)
        eye_distance = max(((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5, 1.0)
        midpoint_x = (lx + rx) / 2.0
        midpoint_y = (ly + ry) / 2.0
        top, right, bottom, left = face_location
        frame_h, frame_w = rgb_frame.shape[:2]
        face_w = max(right - left, 1)
        face_h = max(bottom - top, 1)
        return {
            "tilt": round(float((ry - ly) / eye_distance), 4),
            "nose_x": round(float((nx - midpoint_x) / eye_distance), 4),
            "nose_y": round(float((ny - midpoint_y) / eye_distance), 4),
            "cx": round(float(((left + right) / 2.0) / frame_w), 4),
            "cy": round(float(((top + bottom) / 2.0) / frame_h), 4),
            "size": round(float(((face_w / frame_w) + (face_h / frame_h)) / 2.0), 4),
        }
    except Exception:
        top, right, bottom, left = face_location
        frame_h, frame_w = rgb_frame.shape[:2]
        return {
            "tilt": 0.0,
            "nose_x": 0.0,
            "nose_y": 0.0,
            "cx": round(float(((left + right) / 2.0) / frame_w), 4),
            "cy": round(float(((top + bottom) / 2.0) / frame_h), 4),
            "size": round(float((((right - left) / frame_w) + ((bottom - top) / frame_h)) / 2.0), 4),
        }


@app.route("/admin/register", methods=["GET", "POST"])
@admin_required
def register():
    if request.method == "POST":
        name, name_error = uppercase_school_field(request.form.get("name"), "Full name", 120)
        admission_number = normalize_text(request.form.get("admission_number"), 50)
        roll_number = normalize_text(request.form.get("roll_number"), 30)
        class_name, class_error = uppercase_school_field(request.form.get("class_name"), "Class", 30)
        raw_section = normalize_text(request.form.get("section"), 10)
        section, section_error = uppercase_school_field(raw_section, "Section", 10) if raw_section else (None, None)
        section_optional = class_name in {"XI", "XII"}
        if name_error or class_error or section_error:
            return json_error(name_error or class_error or section_error)
        if not all([name, admission_number, roll_number, class_name]):
            return json_error("Admission number, roll number, name and class are required")
        if not section_optional and not section:
            return json_error("Section is required for Classes I-X.")
        section = section or None
        if Student.query.filter(func.lower(Student.admission_number) == admission_number.lower()).first():
            return json_error("Admission number is already registered", 409)
        token = secrets.token_urlsafe(24)
        (PENDING_DIR / token).mkdir(parents=True, exist_ok=False)
        session["pending_registration"] = {
            "token": token,
            "name": name,
            "admission_number": admission_number,
            "roll_number": roll_number,
            "class_name": class_name,
            "section": section,
        }
        return jsonify({"message": "Details saved. Face capture is required.", "registration_token": token}), 201
    return render_template("register.html")


@app.route("/api/register/cancel", methods=["POST"])
@admin_required
def register_cancel():
    info = pending_info()
    if info:
        shutil.rmtree(PENDING_DIR / info["token"], ignore_errors=True)
    session.pop("pending_registration", None)
    return jsonify({"message": "Registration cancelled; no student was created"})


@app.route("/api/register/capture/<token>", methods=["POST"])
@admin_required
def register_capture(token):
    info = pending_info()
    if not info or not secrets.compare_digest(str(info.get("token", "")), str(token)):
        return json_error("Registration session expired. Start again.", 403)
    try:
        frame = decode_data_url((request.get_json(silent=True) or {}).get("image"))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb, model="hog", number_of_times_to_upsample=1)
        if len(locations) != 1:
            return json_error("Exactly one face must be visible. No face, multiple faces, or partial faces are not accepted.")
        ok, message = image_quality(frame, locations[0])
        if not ok:
            return json_error(message)

        # Coaching metadata only. The original acceptance/training flow is
        # unchanged; this signature simply helps the UI detect repeated views.
        pose_signature = _registration_pose_signature(rgb, locations[0])
        folder = PENDING_DIR / token
        count = len(list(folder.glob("frame_*.jpg")))
        if count >= 12:
            return json_error("All 12 capture slots are already filled")
        cv2.imwrite(str(folder / f"frame_{count}.jpg"), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 94])
        return jsonify({
            "message": "Good face frame captured",
            "frame_count": count + 1,
            "pose_signature": pose_signature,
        })
    except Exception:
        return json_error("The camera frame could not be processed. Please try again.")


@app.route("/api/register/complete/<token>", methods=["POST"])
@admin_required
def register_complete(token):
    info = pending_info()
    if not info or not secrets.compare_digest(str(info.get("token", "")), str(token)):
        return json_error("Registration session expired. Start again.", 403)
    folder = PENDING_DIR / token
    frames = list(folder.glob("frame_*.jpg"))
    minimum = int(os.getenv("MIN_FACE_SAMPLES", "10"))
    if len(frames) < minimum:
        return json_error(f"Capture at least {minimum} valid frames before completing registration.")
    try:
        encodings = get_face_encodings(str(folder))
        if len(encodings) < minimum:
            return json_error(f"Only {len(encodings)} high-quality face frames could be trained. Please retake the rejected frames.")
        if Student.query.filter(func.lower(Student.admission_number) == info["admission_number"].lower()).first():
            return json_error("This admission number was registered while the form was open.", 409)
        student = Student(
            name=info["name"],
            admission_number=info["admission_number"],
            roll_number=info["roll_number"],
            class_name=info["class_name"],
            section=info["section"],
            face_trained=True,
            training_date=now_local(),
            face_encodings=encodings_to_json(encodings),
        )
        db.session.add(student)
        db.session.flush()
        audit("student_create", f"Registered {student.name} in {student.class_name}-{student.section}", "Student", student.id)
        db.session.commit()
        shutil.rmtree(folder, ignore_errors=True)
        session.pop("pending_registration", None)
        return jsonify({"message": f"Registration complete for {student.name}", "student_id": student.id, "valid_frames": len(encodings)}), 201
    except IntegrityError:
        db.session.rollback()
        return json_error("The student could not be saved because one of the identifiers already exists.", 409)
    except Exception:
        db.session.rollback()
        return json_error("Face training or student creation failed. Please try again.", 500)


# ---------------------------------------------------------------------------
# Retraining
# ---------------------------------------------------------------------------
@app.route("/admin/students/<int:student_id>/retrain")
@admin_required
def retrain_face_page(student_id):
    student = db.session.get(Student, student_id)
    if not student:
        abort(404)
    token = secrets.token_urlsafe(24)
    session["pending_retrain"] = {"token": token, "student_id": student.id}
    (PENDING_DIR / ("retrain_" + token)).mkdir(parents=True, exist_ok=True)
    return render_template("retrain_face.html", student=student, token=token)


@app.route("/api/students/<int:student_id>/retrain/capture/<token>", methods=["POST"])
@admin_required
def retrain_capture(student_id, token):
    pending = session.get("pending_retrain") or {}
    if not pending or not secrets.compare_digest(str(pending.get("token", "")), str(token)) or pending.get("student_id") != student_id:
        return json_error("Retraining session expired. Start again.", 403)
    try:
        frame = decode_data_url((request.get_json(silent=True) or {}).get("image"))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb, model="hog", number_of_times_to_upsample=1)
        if len(locations) != 1:
            return json_error("Exactly one face must be visible")
        ok, message = image_quality(frame, locations[0])
        if not ok:
            return json_error(message)
        folder = PENDING_DIR / ("retrain_" + token)
        count = len(list(folder.glob("frame_*.jpg")))
        if count >= 12:
            return json_error("All 12 capture slots are already filled")
        cv2.imwrite(str(folder / f"frame_{count}.jpg"), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 94])
        return jsonify({"message": "Good frame captured", "frame_count": count + 1})
    except Exception:
        return json_error("The camera frame could not be processed. Please try again.")


@app.route("/api/students/<int:student_id>/retrain/complete/<token>", methods=["POST"])
@admin_required
def retrain_complete(student_id, token):
    pending = session.get("pending_retrain") or {}
    if not pending or not secrets.compare_digest(str(pending.get("token", "")), str(token)) or pending.get("student_id") != student_id:
        return json_error("Retraining session expired. Start again.", 403)
    student = db.session.get(Student, student_id)
    if not student:
        return json_error("Student not found", 404)
    folder = PENDING_DIR / ("retrain_" + token)
    minimum = int(os.getenv("MIN_FACE_SAMPLES", "10"))
    try:
        encodings = get_face_encodings(str(folder))
        if len(encodings) < minimum:
            return json_error(f"Need at least {minimum} good frames; found {len(encodings)}")
        student.face_encodings = encodings_to_json(encodings)
        student.face_trained = True
        student.training_date = now_local()
        audit("student_retrain", f"Updated face model for {student.name}", "Student", student.id)
        db.session.commit()
        shutil.rmtree(folder, ignore_errors=True)
        session.pop("pending_retrain", None)
        return jsonify({"message": f"Face model updated for {student.name}", "valid_frames": len(encodings)})
    except Exception:
        db.session.rollback()
        return json_error("Face training failed. Please try again.", 500)


# ---------------------------------------------------------------------------
# Public attendance scanner
# ---------------------------------------------------------------------------
@app.route("/scan")
def scan_page():
    return render_template("scan.html")


@app.route("/api/school-day")
def school_day_api():
    today = today_local()
    override = SchoolCalendar.query.filter_by(date=today).first()
    effective = effective_time_for_request()
    return jsonify({
        "date": today.isoformat(),
        "is_working": is_working_day(today),
        "reason": override.reason if override else None,
        "override": bool(override),
        "time": effective.strftime("%H:%M"),
        "live_server_time": now_local().strftime("%H:%M"),
        "using_override": bool(get_school_clock_override()),
        "attendance_from": ATTENDANCE_PRESENT_FROM.strftime("%H:%M"),
        "late_after": ATTENDANCE_LATE_AFTER.strftime("%H:%M"),
        "absent_after": ATTENDANCE_ABSENT_AFTER.strftime("%H:%M"),
    })


@app.route("/api/attendance/mark", methods=["POST"])
def mark_attendance():
    data = request.get_json(silent=True) or {}
    images = data.get("images") or ([data.get("image")] if data.get("image") else [])
    images = [x for x in images[:7] if x]
    if not images:
        return json_error("No camera image received")
    today = today_local()
    if not is_working_day(today):
        return json_error("Today is a non-working school day. Attendance cannot be marked.", 409)

    current_time = effective_time_for_request()
    if current_time < ATTENDANCE_PRESENT_FROM:
        return json_error(
            "Attendance opens at 7:30 AM. Please try again during the school attendance window.",
            409,
        )
    if current_time >= ATTENDANCE_ABSENT_AFTER:
        return json_error(
            "The attendance window closed at 9:00 AM. This student remains absent unless an admin manually marks the student present.",
            409,
        )
    scan_status = "late" if current_time >= ATTENDANCE_LATE_AFTER else "present"

    tolerance = float(os.getenv("FACE_RECOGNITION_TOLERANCE", "0.48"))
    scan_dir = Path(tempfile.mkdtemp(prefix="scan_", dir=str(SCAN_TMP)))
    try:
        known_users = []
        for student in Student.query.filter_by(face_trained=True, active=True).all():
            if not student.face_encodings:
                continue
            arr = json_to_encodings_array(student.face_encodings)
            if arr.size == 0:
                continue
            temp_path = scan_dir / f"{student.id}.npy"
            np.save(temp_path, arr)
            known_users.append((student, str(temp_path)))
        if not known_users:
            return json_error("No trained students are available", 400)

        votes, details = {}, {}
        valid_frames = 0
        for image_data in images:
            frame = decode_data_url(image_data)
            locations, encs = recognize_faces(frame, upsample=1)
            if not encs:
                continue
            if len(encs) != 1:
                return json_error("Only one person may be in front of the scanner")
            quality_ok, _ = image_quality(frame, locations[0])
            if not quality_ok:
                continue
            valid_frames += 1
            student, distance, second = best_match_for_encoding(encs[0], known_users, tolerance=tolerance)
            if student is not None:
                votes[student.id] = votes.get(student.id, 0) + 1
                details[student.id] = (student, distance, second)

        if not votes:
            return jsonify({"error": "No confident match. Improve lighting, face the camera, and try again."}), 401
        winner_id, vote_count = max(votes.items(), key=lambda pair: pair[1])
        required_votes = 1 if len(images) == 1 else max(2, int(np.ceil(max(valid_frames, 1) * 0.60)))
        if vote_count < required_votes:
            return jsonify({"error": "Recognition was inconsistent. Please hold still and scan again.", "votes": vote_count, "required_votes": required_votes}), 401

        student, distance, _ = details[winner_id]
        existing = Attendance.query.filter_by(student_id=student.id, date=today).first()
        if existing:
            existing_status = str(existing.status or "").lower()
            if existing.source == "manual" and existing_status == "absent":
                return jsonify({
                    "error": f"{student.name} was manually marked absent by an admin. An admin must correct the record before face attendance can be applied."
                }), 409
            if existing_status in {"present", "late"}:
                return jsonify({
                    "message": f"Attendance already marked for {student.name} ({existing_status.title()})",
                    "student_name": student.name,
                    "already_marked": True,
                    "attendance_status": existing_status,
                    "votes": vote_count,
                }), 200

        if existing is not None:
            existing.status = scan_status
            existing.time_in = effective_time_for_request().replace(microsecond=0)
            existing.source = "face"
            existing.marked_by = "Attendance Scanner"
            existing.note = "Corrected by face-recognition scan"
            attendance = existing
        else:
            attendance = Attendance(
                student_id=student.id,
                date=today,
                time_in=effective_time_for_request().replace(microsecond=0),
                status=scan_status,
                source="face",
                marked_by="Attendance Scanner",
            )
            db.session.add(attendance)
        try:
            audit("attendance_mark", f"Attendance marked for {student.name}", "Student", student.id)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = Attendance.query.filter_by(student_id=student.id, date=today).first()
            if existing:
                return jsonify({"message": f"Attendance already marked for {student.name}", "student_name": student.name, "already_marked": True}), 200
            raise

        return jsonify({
            "message": f"Welcome, {student.name}! Attendance marked as {scan_status.title()}.",
            "student_id": student.id,
            "student_name": student.name,
            "attendance_status": scan_status,
            "school_time": effective_time_for_request().strftime("%H:%M"),
            "distance": round(distance, 4),
            "votes": vote_count,
            "frames_used": valid_frames,
        })
    except ValueError as exc:
        return json_error(str(exc))
    except Exception:
        db.session.rollback()
        return json_error("Attendance scan failed. Please try again.", 500)
    finally:
        shutil.rmtree(scan_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Admin students: class accordion + section tabs/table
# ---------------------------------------------------------------------------
@app.route("/admin/students")
@admin_required
def admin_students_page():
    return render_template("admin_students.html", class_tree=class_tree())


@app.route("/api/admin/students")
@admin_required
def admin_students_api():
    day, class_name, section = parse_register_query()
    return jsonify({"date": day.isoformat(), "students": fetch_register_rows(day, class_name, section)})


@app.route("/api/admin/students/<int:student_id>", methods=["PUT"])
@admin_required
def admin_student_update(student_id):
    student = db.session.get(Student, student_id)
    if not student:
        return json_error("Student not found", 404)
    data = request.get_json(silent=True) or {}
    if "name" in data:
        student.name = normalize_text(data.get("name"), 120)
    if "roll_number" in data:
        student.roll_number = normalize_text(data.get("roll_number"), 30)
    if "class_name" in data:
        student.class_name = normalize_class(data.get("class_name"))
    if "section" in data:
        student.section = normalize_section(data.get("section"))
    if "active" in data:
        student.active = bool(data["active"])
    if not all([student.name, student.admission_number, student.class_name, student.section]):
        return json_error("Name, admission number, class and section are required")
    audit("student_update", f"Updated {student.name}", "Student", student.id)
    db.session.commit()
    return jsonify({"message": "Student updated"})


@app.route("/api/admin/students/<int:student_id>", methods=["DELETE"])
@admin_required
def admin_student_delete(student_id):
    student = db.session.get(Student, student_id)
    if not student:
        return json_error("Student not found", 404)
    name = student.name
    db.session.delete(student)
    audit("student_delete", f"Deleted {name} and related attendance", "Student", student.id)
    db.session.commit()
    return jsonify({"message": f"{name} and all related attendance records were permanently deleted"})

@app.route("/api/admin/attendance/override", methods=["POST"])
@admin_required
def admin_attendance_override():
    data = request.get_json(silent=True) or {}

    try:
        student_id = int(data.get("student_id"))
    except (TypeError, ValueError):
        return json_error("A valid student is required")

    # NOW supports:
    # present
    # late
    # absent
    status = str(data.get("status") or "").strip().lower()

    if status not in {"present", "late", "absent"}:
        return json_error(
            "Status must be present, late or absent"
        )

    student = db.session.get(
        Student,
        student_id
    )

    if not student or not student.active:
        return json_error(
            "Student not found",
            404
        )

    raw_date = str(
        data.get("date")
        or today_local().isoformat()
    )

    try:
        day = datetime.strptime(
            raw_date,
            "%Y-%m-%d"
        ).date()
    except ValueError:
        return json_error(
            "Valid date is required"
        )

    note = normalize_text(
        data.get("note"),
        255
    )

    attendance = Attendance.query.filter_by(
        student_id=student.id,
        date=day
    ).first()

    if attendance is None:
        attendance = Attendance(
            student_id=student.id,
            date=day
        )
        db.session.add(attendance)

    # ---------------------------------------------------------
    # MANUAL ADMIN STATUS
    # ---------------------------------------------------------

    attendance.status = status
    attendance.source = "manual"
    attendance.marked_by = session.get(
        "admin_username",
        "Admin"
    )
    attendance.note = note or None

    # Present and Late both have a time.
    # Absent has no time.
    if status in {"present", "late"}:
        attendance.time_in = effective_time_for_request().replace(microsecond=0)
    else:
        attendance.time_in = None

    audit(
        "attendance_override",
        (
            f"Marked "
            f"{student.name} "
            f"{status} "
            f"for {day.isoformat()} "
            f"manually"
        ),
        "Student",
        student.id
    )

    try:
        db.session.commit()

    except Exception:
        db.session.rollback()

        return json_error(
            "Could not update attendance.",
            500
        )

    return jsonify({
        "message":
            f"{student.name} marked "
            f"{status.title()} for "
            f"{day.isoformat()}",

        "status":
            status,

        "time_in":
            (
                attendance.time_in.strftime(
                    "%I:%M %p"
                )
                if attendance.time_in
                else "—"
            ),

        "source":
            "manual"
    })


@app.route("/admin/students/export.xlsx")
@admin_required
def admin_students_export_xlsx():
    day, class_name, section = parse_register_query()
    rows = fetch_register_rows(day, class_name, section)
    subtitle = " / ".join(x for x in [class_name, section] if x) or "All classes"
    buf = build_xlsx(rows, title="Attendance Register", subtitle=f"{subtitle} - {day.isoformat()}")
    return send_file(buf, as_attachment=True, download_name=f"attendance_{day.isoformat()}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/students/export.pdf")
@admin_required
def admin_students_export_pdf():
    day, class_name, section = parse_register_query()
    rows = fetch_register_rows(day, class_name, section)
    subtitle = " / ".join(x for x in [class_name, section] if x) or "All classes"
    buf = build_pdf(rows, title="Attendance Register", subtitle=f"{subtitle} - {day.isoformat()}")
    return send_file(buf, as_attachment=True, download_name=f"attendance_{day.isoformat()}.pdf", mimetype="application/pdf")


# ---------------------------------------------------------------------------
# Admin account / password
# ---------------------------------------------------------------------------
@app.route("/admin/account", methods=["GET", "POST"])
@admin_required
def admin_account():
    admin = db.session.get(Admin, session.get("admin_id"))
    if not admin:
        session.clear()
        return redirect(url_for("login"))
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(admin.password_hash, current):
            return render_template("account.html", error="Current password is incorrect"), 400
        if len(new_password) < 8:
            return render_template("account.html", error="New password must be at least 8 characters"), 400
        if new_password != confirm:
            return render_template("account.html", error="New passwords do not match"), 400
        admin.password_hash = generate_password_hash(new_password)
        audit("admin_password_change", "Admin password changed", "Admin", admin.id)
        db.session.commit()
        return render_template("account.html", success="Admin password updated successfully")
    return render_template("account.html")


# ---------------------------------------------------------------------------
# Admin teacher management
# ---------------------------------------------------------------------------
@app.route("/admin/teachers")
@admin_required
def admin_teachers_page():
    return render_template("admin_teachers.html")


@app.route("/api/admin/teachers")
@admin_required
def admin_teachers_api():
    teachers = Teacher.query.order_by(Teacher.name).all()
    return jsonify([
        {"id": t.id, "name": t.name, "username": t.username, "class_name": t.class_name or "", "section": t.section or "", "active": bool(t.active), "created_at": t.created_at.isoformat() if t.created_at else None}
        for t in teachers
    ])


def _teacher_assignment_conflict(class_name, section, exclude_teacher_id=None):
    """Return the active teacher already assigned to this class/section, if any.

    A class + section may have at most one active teacher. For classes such as
    XI/XII where section is blank, the class itself is the unique assignment.
    """
    if not class_name:
        return None

    query = Teacher.query.filter(
        Teacher.active.is_(True),
        func.upper(func.trim(Teacher.class_name)) == class_name.upper().strip(),
    )

    section_value = (section or "").strip().upper()
    if section_value:
        query = query.filter(
            func.upper(func.trim(Teacher.section)) == section_value
        )
    else:
        query = query.filter(
            (Teacher.section.is_(None)) | (func.trim(Teacher.section) == "")
        )

    if exclude_teacher_id is not None:
        query = query.filter(Teacher.id != exclude_teacher_id)

    return query.order_by(Teacher.id).first()


@app.route("/api/admin/teachers", methods=["POST"])
@admin_required
def admin_teacher_create():
    data=request.get_json(silent=True) or {}
    name=normalize_text(data.get("name"),120)
    username=normalize_text(data.get("username"),80)
    password=str(data.get("password") or "")
    class_name=normalize_class(data.get("class_name")) if data.get("class_name") else None
    section=normalize_section(data.get("section")) if data.get("section") else None
    if not name or not username or len(password)<8:
        return json_error("Name, username and an 8+ character password are required")
    if Teacher.query.filter(func.lower(Teacher.username)==username.lower()).first():
        return json_error("Teacher username already exists",409)

    conflict = _teacher_assignment_conflict(class_name, section)
    if conflict:
        assignment = class_name if not section else f"{class_name} {section}"
        return json_error(
            f"{assignment} is already assigned to teacher {conflict.name}. Only one active teacher can be assigned to a class/section.",
            409,
        )

    try:
        teacher=Teacher(name=name,username=username,password_hash=generate_password_hash(password),class_name=class_name,section=section,active=True,created_at=now_local())
        db.session.add(teacher)
        db.session.flush()
        audit("teacher_create",f"Created teacher account {name}","Teacher",teacher.id)
        db.session.commit()
        return jsonify({"message":f"Teacher account created for {name}","id":teacher.id}),201
    except IntegrityError:
        db.session.rollback()
        return json_error("Could not create teacher. The username or class assignment may already exist.",409)
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Teacher creation database error")
        return json_error("Could not create teacher because of a database error.",500)


@app.route("/api/admin/teachers/<int:teacher_id>", methods=["PUT"])
@admin_required
def admin_teacher_update(teacher_id):
    teacher=db.session.get(Teacher,teacher_id)
    if not teacher: return json_error("Teacher not found",404)
    data=request.get_json(silent=True) or {}

    new_class_name = (
        normalize_class(data.get("class_name")) if data.get("class_name")
        else teacher.class_name
    )
    new_section = (
        normalize_section(data.get("section")) if data.get("section")
        else teacher.section
    )
    new_active = bool(data["active"]) if "active" in data else bool(teacher.active)

    if new_active:
        conflict = _teacher_assignment_conflict(
            new_class_name, new_section, exclude_teacher_id=teacher.id
        )
        if conflict:
            assignment = new_class_name if not new_section else f"{new_class_name} {new_section}"
            return json_error(
                f"{assignment} is already assigned to teacher {conflict.name}. Only one active teacher can be assigned to a class/section.",
                409,
            )

    if "name" in data: teacher.name=normalize_text(data.get("name"),120)
    if "class_name" in data: teacher.class_name=new_class_name if data.get("class_name") else None
    if "section" in data: teacher.section=new_section if data.get("section") else None
    if "active" in data: teacher.active=new_active
    if "password" in data:
        new_password=str(data.get("password") or "")
        if new_password and len(new_password)<8: return json_error("Password must be at least 8 characters")
        if new_password: teacher.password_hash=generate_password_hash(new_password)
    action="teacher_enable" if data.get("active") is True else "teacher_disable" if data.get("active") is False else "teacher_update"
    audit(action,f"Updated teacher {teacher.name}","Teacher",teacher.id)
    db.session.commit()
    return jsonify({"message":"Teacher updated successfully"})


@app.route("/api/admin/teachers/<int:teacher_id>", methods=["DELETE"])
@admin_required
def admin_teacher_delete(teacher_id):
    teacher=db.session.get(Teacher,teacher_id)
    if not teacher: return json_error("Teacher not found",404)
    name=teacher.name
    db.session.delete(teacher)
    audit("teacher_delete",f"Permanently deleted teacher account {name}","Teacher",teacher_id)
    db.session.commit()
    return jsonify({"message":f"Teacher account {name} permanently deleted"})


# ---------------------------------------------------------------------------
# Audit log: reachable only directly at /audit; no navigation link.
# ---------------------------------------------------------------------------
@app.route("/audit")
@admin_required
def audit_page():
    return render_template("audit.html")


@app.route("/api/admin/audit")
@admin_required
def audit_api():
    limit=min(max(request.args.get("limit",200,type=int),1),500)
    rows=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return jsonify([
        {"timestamp":r.created_at.strftime("%d/%m/%Y %I:%M:%S %p") if r.created_at else "—","actor":r.actor_name,"action":r.action,"description":r.message,"actor_type":r.actor_type,"target_type":r.target_type,"target_id":r.target_id}
        for r in rows
    ])


# ---------------------------------------------------------------------------
# Admin-only School Clock override. It affects admin views/manual testing only.
# ---------------------------------------------------------------------------
@app.route("/api/admin/clock")
@admin_required
def admin_clock_get():
    override = get_school_clock_override()
    return jsonify({
        "override": override,
        "school_time": effective_time_for_request().strftime("%H:%M"),
        "live_time": now_local().strftime("%H:%M"),
        "using_override": bool(override),
    })


@app.route("/api/admin/clock", methods=["POST"])
@admin_required
def admin_clock_set():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("time") or "")
    try:
        selected = datetime.strptime(raw, "%H:%M").strftime("%H:%M")
    except ValueError:
        return json_error("Time must use HH:MM")

    try:
        clock = db.session.get(SchoolClock, 1)
        if clock is None:
            clock = SchoolClock(id=1)
            db.session.add(clock)
        clock.override_time = selected
        clock.updated_at = now_local()
        db.session.commit()
        return jsonify({
            "message": "School test time is now active across attendance features for all users",
            "time": selected,
            "school_time": selected,
            "live_time": now_local().strftime("%H:%M"),
            "using_override": True,
        })
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not set shared school clock")
        return json_error("Could not set the attendance time", 500)


@app.route("/api/admin/clock", methods=["DELETE"])
@admin_required
def admin_clock_reset():
    try:
        clock = db.session.get(SchoolClock, 1)
        if clock:
            clock.override_time = None
            clock.updated_at = now_local()
            db.session.commit()
        return jsonify({"message": "School Clock reset to live time", "using_override": False, "live_time": now_local().strftime("%H:%M")})
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not reset shared school clock")
        return json_error("Could not reset the attendance time", 500)


# ---------------------------------------------------------------------------
# Teacher self-service password quick link.
# ---------------------------------------------------------------------------
@app.route("/teacher/change-password", methods=["GET","POST"])
@teacher_required
def teacher_change_password():
    teacher=current_teacher()
    if request.method=="POST":
        current=str(request.form.get("current_password") or "")
        new_password=str(request.form.get("new_password") or "")
        confirm=str(request.form.get("confirm_password") or "")
        if not check_password_hash(teacher.password_hash,current):
            return render_template("teacher_change_password.html",error="Current password is incorrect",teacher=teacher),400
        if len(new_password)<8:
            return render_template("teacher_change_password.html",error="New password must be at least 8 characters",teacher=teacher),400
        if new_password!=confirm:
            return render_template("teacher_change_password.html",error="New passwords do not match",teacher=teacher),400
        teacher.password_hash=generate_password_hash(new_password)
        audit("teacher_password_change",f"Teacher {teacher.username} changed password","Teacher",teacher.id)
        db.session.commit()
        return render_template("teacher_change_password.html",success="Password changed successfully",teacher=teacher)
    return render_template("teacher_change_password.html",teacher=teacher)


@app.route("/admin/school-time")
@admin_required
def admin_school_time_page():
    return render_template(
        "admin_school_time.html",
        school_clock_override=get_school_clock_override(),
        live_time=now_local().strftime("%H:%M"),
    )


@app.route("/admin/calendar")
@admin_required
def admin_calendar_page():
    return render_template("calendar.html", calendar_readonly=False)


@app.route("/teacher/calendar")
@teacher_required
def teacher_calendar_page():
    return render_template("calendar.html", calendar_readonly=True)


@app.route("/api/calendar")
@staff_required
def calendar_api():
    year = request.args.get("year", now_local().year, type=int)
    month = request.args.get("month", now_local().month, type=int)
    if not 1 <= month <= 12 or not 1900 <= year <= 2200:
        return json_error("Invalid calendar month")
    start = datetime(year, month, 1).date()
    next_month = datetime(year + 1, 1, 1).date() if month == 12 else datetime(year, month + 1, 1).date()
    end = next_month - timedelta(days=1)
    overrides = {r.date: r for r in SchoolCalendar.query.filter(SchoolCalendar.date >= start, SchoolCalendar.date <= end).all()}
    rows=[]
    day=start
    while day <= end:
        override=overrides.get(day)
        rows.append({
            "date": day.isoformat(),
            "is_working": bool(override.is_working) if override else weekly_default_is_working(day),
            "reason": override.reason if override else "",
            "override": bool(override),
        })
        day += timedelta(days=1)
    return jsonify(rows)


@app.route("/api/calendar", methods=["POST"])
@admin_required
def calendar_set():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("date") or "")
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return json_error("Valid date is required")
    row = SchoolCalendar.query.filter_by(date=day).first()
    if row is None:
        row = SchoolCalendar(date=day)
        db.session.add(row)
    row.is_working = bool(data.get("is_working"))
    row.reason = normalize_text(data.get("reason"), 255) or None
    row.created_by = session.get("admin_username", "admin")
    audit("calendar_update", f"Set {day} as {'working' if row.is_working else 'non-working'}: {row.reason or ''}", "SchoolCalendar", row.id)
    db.session.commit()
    return jsonify({"message": "Calendar updated"})


@app.route("/api/calendar/reset", methods=["POST"])
@admin_required
def calendar_reset():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("date") or "")
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return json_error("Valid date is required")
    row = SchoolCalendar.query.filter_by(date=day).first()
    if row:
        db.session.delete(row)
        audit("calendar_reset", f"Reset calendar override for {day}", "SchoolCalendar", row.id)
        db.session.commit()
    return jsonify({"message": "Calendar reset to weekly default", "date": day.isoformat(), "is_working": weekly_default_is_working(day)})


@app.route("/api/calendar/bulk", methods=["POST"])
@admin_required
def calendar_bulk():
    data = request.get_json(silent=True) or {}
    dates = data.get("dates") or []
    is_working = bool(data.get("is_working"))
    reason = normalize_text(data.get("reason"), 255) or None
    changed = 0
    try:
        for raw in dates:
            day = datetime.strptime(str(raw), "%Y-%m-%d").date()
            row = SchoolCalendar.query.filter_by(date=day).first()
            if row is None:
                row = SchoolCalendar(date=day)
                db.session.add(row)
            row.is_working = is_working
            row.reason = reason
            row.created_by = session.get("admin_username", "admin")
            changed += 1
        audit("calendar_bulk_update", f"Updated {changed} calendar date(s)", "SchoolCalendar")
        db.session.commit()
    except ValueError:
        db.session.rollback()
        return json_error("Every date must use YYYY-MM-DD")
    return jsonify({"message": f"Updated {changed} calendar date(s)"})


@app.route("/api/calendar/import", methods=["POST"])
@admin_required
def calendar_import():
    upload = request.files.get("calendar_file")
    uploaded_text = (request.form.get("calendar_text") or "").strip()
    if not upload and not uploaded_text:
        return json_error("Choose a PDF or image school calendar first.")
    try:
        text_value = uploaded_text or extract_calendar_upload_text(upload)
        rows = parse_calendar_text(text_value)
        if not rows:
            return json_error("No school-calendar dates could be detected. Use a clearer PDF/image or mark dates manually.")
        return jsonify({
            "dates": rows,
            "working_count": sum(1 for row in rows if row["is_working"]),
            "non_working_count": sum(1 for row in rows if not row["is_working"]),
            "text_preview": text_value[:2000],
        })
    except ValueError as exc:
        return json_error(str(exc))
    except Exception:
        app.logger.exception("Calendar import failed")
        return json_error("The school calendar could not be imported.", 500)


@app.route("/api/calendar/import/apply", methods=["POST"])
@admin_required
def calendar_import_apply():
    data = request.get_json(silent=True) or {}
    rows = data.get("dates") or []
    if not rows or not isinstance(rows, list):
        return json_error("No detected calendar dates were supplied.")
    changed = 0
    try:
        for item in rows:
            raw_date = str(item.get("date") or "")
            day = datetime.strptime(raw_date, "%Y-%m-%d").date()
            row = SchoolCalendar.query.filter_by(date=day).first()
            if row is None:
                row = SchoolCalendar(date=day)
                db.session.add(row)
            row.is_working = bool(item.get("is_working"))
            row.reason = normalize_text(item.get("reason"), 255) or None
            row.created_by = session.get("admin_username", "admin")
            changed += 1
        audit("calendar_import", f"Imported {changed} school-calendar date(s)", "SchoolCalendar")
        db.session.commit()
    except (TypeError, ValueError):
        db.session.rollback()
        return json_error("One or more detected calendar dates are invalid.")
    except Exception:
        db.session.rollback()
        app.logger.exception("Applying imported school calendar failed")
        return json_error("The imported school calendar could not be saved.", 500)
    return jsonify({"message": f"Applied {changed} calendar date(s)"})


# ---------------------------------------------------------------------------
# Teacher dashboard / students. Assigned teachers see only their class/section;
# unassigned teacher accounts can see the full register.
# ---------------------------------------------------------------------------
@app.route("/teacher")
@app.route("/teacher/dashboard")
@teacher_required
def teacher_dashboard():
    teacher = current_teacher()
    working = is_working_day(today_local())
    student_query = Student.query.filter(Student.active.is_(True))
    if teacher.class_name:
        student_query = student_query.filter(teacher_scope_filter(teacher))
    student_count = student_query.count()
    present_query = Attendance.query.join(Student).filter(Attendance.date == today_local(), Student.active.is_(True))
    if teacher.class_name:
        present_query = present_query.filter(teacher_scope_filter(teacher))
    present_today = present_query.count()
    return render_template("teacher_dashboard.html", teacher=teacher, working_today=working, student_count=student_count, present_today=present_today)


@app.route("/teacher/students")
@teacher_required
def teacher_students_page():
    return render_template("teacher_students.html", teacher=current_teacher())


@app.route("/api/teacher/students")
@teacher_required
def teacher_students_api():
    teacher = current_teacher()
    day, _, _ = parse_register_query()
    return jsonify({"date": day.isoformat(), "students": fetch_register_rows(day, teacher=teacher)})


@app.route("/teacher/students/export.xlsx")
@teacher_required
def teacher_students_export_xlsx():
    teacher = current_teacher()
    day, _, _ = parse_register_query()
    rows = fetch_register_rows(day, teacher=teacher)
    buf = build_xlsx(rows, title="Attendance Register", subtitle=f"{teacher.name} - {day.isoformat()}")
    return send_file(buf, as_attachment=True, download_name=f"attendance_{day.isoformat()}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/teacher/students/export.pdf")
@teacher_required
def teacher_students_export_pdf():
    teacher = current_teacher()
    day, _, _ = parse_register_query()
    rows = fetch_register_rows(day, teacher=teacher)
    buf = build_pdf(rows, title="Attendance Register", subtitle=f"{teacher.name} - {day.isoformat()}")
    return send_file(buf, as_attachment=True, download_name=f"attendance_{day.isoformat()}.pdf", mimetype="application/pdf")


# ---------------------------------------------------------------------------
# Health / errors / startup migration.
# ---------------------------------------------------------------------------
def ensure_schema_compatibility():
    """Create missing tables/columns required by the current application.

    Safe for both local SQLite and Supabase/Postgres. Older deployments often
    already have the teacher table, so create_all() alone is not enough.
    """
    db.create_all()

    additions = {
        "teacher": {
            "class_name": "VARCHAR(30)",
            "section": "VARCHAR(10)",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        },
        "student": {
            "face_encodings": "TEXT",
            "training_date": "TIMESTAMP",
        },
        "attendance": {
            "source": "VARCHAR(20) DEFAULT 'face'",
            "marked_by": "VARCHAR(120)",
            "note": "VARCHAR(255)",
        },
    }

    inspector = inspect(db.engine)
    dialect = db.engine.dialect.name

    for table, columns in additions.items():
        try:
            existing = {c["name"] for c in inspector.get_columns(table)}
        except Exception:
            app.logger.exception("Could not inspect table %s", table)
            continue

        for column_name, column_sql in columns.items():
            if column_name in existing:
                continue
            try:
                if dialect == "postgresql":
                    statement = text(
                        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column_name}" {column_sql}'
                    )
                else:
                    statement = text(
                        f'ALTER TABLE "{table}" ADD COLUMN "{column_name}" {column_sql}'
                    )
                db.session.execute(statement)
                db.session.commit()
                existing.add(column_name)
                app.logger.info("Added missing column %s.%s", table, column_name)
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("Could not add missing column %s.%s", table, column_name)

    ensure_default_admin()
    cleanup_pending_registrations()


@app.route("/healthz")
def healthz():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok", "time": now_local().isoformat()}), 200
    except Exception:
        return jsonify({"status": "error", "database": "unavailable"}), 503


@app.errorhandler(400)
def bad_request(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(error, "description", "Bad request")}), 400
    return render_template("error.html", code=400, message=getattr(error, "description", "Bad request")), 400


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("error.html", code=404, message="Page not found"), 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    app.logger.exception("Unhandled HTTP 500 for %s", request.path)
    if request.path.startswith("/api/"):
        payload = {"error": "Internal server error"}
        if os.getenv("DEBUG_ERRORS", "false").lower() == "true":
            payload["details"] = str(error)
        return jsonify(payload), 500
    return render_template("error.html", code=500, message="Internal server error"), 500


with app.app_context():
    ensure_schema_compatibility()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
