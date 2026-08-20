# Render deployment checklist

## 1. Put the project in GitHub
Push the whole folder to a repository. Keep `.env` and real secrets out of the repository.

## 2. Create a Render Blueprint
In Render, create a new Blueprint from the repository. The included `render.yaml` creates:

- Python web service
- Render Postgres database
- Persistent disk mounted at `/var/data`
- 5-minute cron job for absence alerts

The web service stores face images/encodings under `/var/data/face_data` and backups under `/var/data/backups`.

## 3. Provide the prompted secrets
At first Blueprint creation, enter:

`ADMIN_PASSWORD`

`APP_BASE_URL` — for example `https://student-attendance.onrender.com`

`EMAIL_ADDRESS`

`EMAIL_PASSWORD`

`ADMIN_EMAIL`

Optional:

`TELEGRAM_BOT_TOKEN`

`TWILIO_ACCOUNT_SID`

`TWILIO_AUTH_TOKEN`

`TWILIO_PHONE_NUMBER`

## 4. Gmail verification email setup
Use a Gmail App Password, not the normal Google account password.

The authentication emails do not depend on the attendance-email checkbox. Teacher verification and password reset use the SMTP settings directly.

The default SMTP settings are:

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true

## 5. First admin login
Use the admin username/password you supplied in the Blueprint environment variables.

The first login forces a password change.

## 6. Create a teacher
Admin → Teachers → Add teacher.

After creation:

- If SMTP works: the teacher receives a verification email.
- If SMTP fails: the admin page displays a direct verification link and the SMTP error.

This makes local testing and Render diagnostics much easier.

## 7. Teacher login
Use:

`/teacher/login`

The username lookup is case-insensitive.

The account must be active and its email must be verified.

If the teacher forgets their password:

- Teacher → Forgot password → email reset.
- Or Admin → Teachers → Reset password.
- Or Admin → Teachers → Set password for an immediate administrator-issued password change.

## 8. Health check
Render uses:

`/healthz`

It checks that the application can reach the configured database.

## 9. Absence alerts
The cron job runs every five minutes. It uses `APP_TIMEZONE=Asia/Kolkata` and only sends/records absence alerts once the configured `ABSENCE_ALERT_TIME` is reached.

## 10. Backups
On Postgres, DB backup downloads are JSON exports. Full backups are ZIP files containing:

- attendance_database.json
- face_data/

On local SQLite installations, the normal DB backup remains a `.db` file.

## 11. Persistent face data
Do not remove the Render persistent disk. The application needs it for face images and `.npy` encodings. Render's default filesystem is ephemeral; only the persistent disk survives restarts/deploys.

## Render dlib build note

This deployment installs `dlib-bin` as a prebuilt wheel and installs `face-recognition` with `--no-deps` so Render does not compile dlib from source and exhaust the build memory budget.
