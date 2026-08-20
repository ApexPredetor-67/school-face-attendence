from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index


db = SQLAlchemy()


def now_local():
    from zoneinfo import ZoneInfo
    import os
    return datetime.now(
        ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
    ).replace(tzinfo=None)


class Admin(db.Model):
    __tablename__ = "admin"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(
        db.String(80),
        unique=True,
        nullable=False,
        index=True,
    )
    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime,
        default=now_local,
        nullable=False,
    )
    last_login = db.Column(
        db.DateTime,
        nullable=True,
    )


class SchoolClock(db.Model):
    __tablename__ = "school_clock"

    id = db.Column(db.Integer, primary_key=True)
    override_time = db.Column(db.String(5), nullable=True)
    updated_at = db.Column(db.DateTime, default=now_local, nullable=False)


class Teacher(db.Model):
    __tablename__ = "teacher"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    name = db.Column(
        db.String(120),
        nullable=False,
    )

    username = db.Column(
        db.String(80),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )

    class_name = db.Column(
        db.String(30),
        nullable=True,
        index=True,
    )

    section = db.Column(
        db.String(10),
        nullable=True,
        index=True,
    )

    active = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
    )

    # Present in the deployed Supabase teacher table.
    # Adding it here makes SQLAlchemy populate it on INSERT.
    created_at = db.Column(
        db.DateTime,
        default=now_local,
        nullable=False,
    )

    last_login = db.Column(
        db.DateTime,
        nullable=True,
    )


class Student(db.Model):
    __tablename__ = "student"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    admission_number = db.Column(
        db.String(50),
        unique=True,
        nullable=False,
        index=True,
    )

    roll_number = db.Column(
        db.String(30),
        nullable=True,
    )

    name = db.Column(
        db.String(120),
        nullable=False,
        index=True,
    )

    class_name = db.Column(
        db.String(30),
        nullable=False,
        index=True,
    )

    section = db.Column(
        db.String(10),
        nullable=False,
        index=True,
    )

    active = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    face_trained = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        index=True,
    )

    # JSON list of 128-value encodings.
    # Raw camera images are never stored in DB.
    face_encodings = db.Column(
        db.Text,
        nullable=True,
    )

    training_date = db.Column(
        db.DateTime,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime,
        default=now_local,
        nullable=False,
    )

    attendances = db.relationship(
        "Attendance",
        backref="student",
        lazy=True,
        cascade="all, delete-orphan",
    )


class Attendance(db.Model):
    __tablename__ = "attendance"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    student_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "student.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    date = db.Column(
        db.Date,
        nullable=False,
        default=lambda: now_local().date(),
        index=True,
    )

    time_in = db.Column(
        db.Time,
        nullable=True,
    )

    status = db.Column(
        db.String(20),
        default="present",
        nullable=False,
        index=True,
    )

    source = db.Column(
        db.String(20),
        default="face",
        nullable=False,
        index=True,
    )

    marked_by = db.Column(
        db.String(120),
        nullable=True,
    )

    note = db.Column(
        db.String(255),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_attendance_student_date",
            "student_id",
            "date",
        ),
        Index(
            "ux_attendance_student_date",
            "student_id",
            "date",
            unique=True,
        ),
    )


class SchoolCalendar(db.Model):
    __tablename__ = "school_calendar"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    date = db.Column(
        db.Date,
        unique=True,
        nullable=False,
        index=True,
    )

    is_working = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    reason = db.Column(
        db.String(255),
        nullable=True,
    )

    created_by = db.Column(
        db.String(120),
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime,
        default=now_local,
        nullable=False,
    )

    updated_at = db.Column(
        db.DateTime,
        default=now_local,
        onupdate=now_local,
        nullable=False,
    )


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    created_at = db.Column(
        db.DateTime,
        default=now_local,
        nullable=False,
        index=True,
    )

    actor_type = db.Column(
        db.String(20),
        nullable=False,
        index=True,
    )

    actor_name = db.Column(
        db.String(120),
        nullable=False,
    )

    action = db.Column(
        db.String(60),
        nullable=False,
        index=True,
    )

    target_type = db.Column(
        db.String(60),
        nullable=True,
    )

    target_id = db.Column(
        db.Integer,
        nullable=True,
    )

    message = db.Column(
        db.String(500),
        nullable=False,
    )
