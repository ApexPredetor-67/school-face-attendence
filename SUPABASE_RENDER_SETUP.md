# Supabase + Render setup

## 1. Supabase
Create a Supabase project and copy a PostgreSQL connection string from **Connect**.

Recommended for a Render web service: use the Supabase **Session Pooler** connection string.

Set it in Render as:

`SUPABASE_DB_URL=<your Supabase PostgreSQL URL>`

The application uses SQLAlchemy, creates the schema automatically, and runs the existing lightweight migrations at startup.

## 2. Render
Use the included `render.yaml` as a Blueprint.

Set these secrets:
- `SUPABASE_DB_URL`
- `ADMIN_PASSWORD`
- `APP_BASE_URL`

`APP_BASE_URL` must be the exact public Render URL, such as `https://your-service.onrender.com`.

The web service still needs the Render persistent disk at `/var/data` because the face-recognition images and `.npy` encodings are stored there.

## 3. First login
The default admin username is `admin`. The initial password is whatever you set as `ADMIN_PASSWORD`.

On first login the existing password-change flow may require changing that password.

## 4. Teacher accounts
Open `/teachers` while signed in as admin.

Create a teacher with:
- Name
- Username
- Class
- Section
- Optional email/phone
- Initial password

If a student is registered with the same Class + Section, the active teacher is automatically associated with that student.

## 5. Student registration
The new registration UI asks only:
- Admission number
- Roll number
- Name
- Class
- Section
- Face

At least 10 valid face samples are required. The original face quality checks and training pipeline are retained.

## 6. Calendar permissions
Administrators can edit the school calendar.

Teachers can only view it. The teacher calendar page disables date editing and does not expose the calendar POST/reset controls.

## 7. Face-data backup
Supabase contains the application database. Face samples and encodings are stored on the Render persistent disk. Back up both if you need a complete restore.
