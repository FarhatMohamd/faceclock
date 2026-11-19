-- =====================================================================
-- FaceClock database schema (SQLite)
-- All timestamps are stored in UTC, ISO-8601 format.
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Organisation: sites (buildings) and departments (directions/services)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    address     TEXT,
    timezone    TEXT NOT NULL DEFAULT 'Europe/Paris',
    active      INTEGER NOT NULL DEFAULT 1
);

-- Departments form a tree: Direction > Service > Team ...
CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL UNIQUE,
    parent_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL
);

-- ---------------------------------------------------------------------
-- Work schedules and break policies
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    start_time      TEXT NOT NULL,              -- 'HH:MM' local time
    end_time        TEXT NOT NULL,              -- 'HH:MM' (may be < start for night shifts)
    workdays        TEXT NOT NULL DEFAULT '1,2,3,4,5',  -- ISO weekdays (1 = Monday)
    grace_minutes   INTEGER NOT NULL DEFAULT 5,
    expected_minutes INTEGER NOT NULL DEFAULT 420
);

-- Break types: a "paid" break counts as working time up to its daily allowance.
CREATE TABLE IF NOT EXISTS break_types (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    code                     TEXT NOT NULL UNIQUE,     -- SMOKE, COFFEE, LUNCH, PERSONAL ...
    label                    TEXT NOT NULL,
    paid                     INTEGER NOT NULL DEFAULT 1,
    daily_allowance_minutes  INTEGER NOT NULL DEFAULT 0,  -- 0 = unlimited when paid
    max_duration_minutes     INTEGER NOT NULL DEFAULT 0,  -- 0 = no alert threshold
    active                   INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------
-- Employees
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employees (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_code       TEXT NOT NULL UNIQUE,
    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    email               TEXT,
    department_id       INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    home_site_id        INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    schedule_id         INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
    pin_hash            TEXT NOT NULL,
    pin_failed_attempts INTEGER NOT NULL DEFAULT 0,
    pin_locked_until    TEXT,
    biometric_consent   INTEGER NOT NULL DEFAULT 0,
    consent_at          TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);

-- Optional restriction: if an employee has rows here, they may only punch at these sites.
CREATE TABLE IF NOT EXISTS employee_sites (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    PRIMARY KEY (employee_id, site_id)
);

-- Face templates: only numeric embeddings are stored, never raw photos.
CREATE TABLE IF NOT EXISTS face_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    engine      TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    quality     REAL,
    created_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Kiosks (punch terminals), one or more per site
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kiosks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    last_seen   TEXT,
    camera_ok   INTEGER,
    active      INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------
-- Punches (clock events)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS punches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    site_id       INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    kiosk_id      INTEGER REFERENCES kiosks(id) ON DELETE SET NULL,
    punch_type    TEXT NOT NULL CHECK (punch_type IN
                    ('IN','OUT','BREAK_START','BREAK_END','SITE_CHANGE')),
    break_type_id INTEGER REFERENCES break_types(id),
    ts            TEXT NOT NULL,                 -- event time (UTC)
    method        TEXT NOT NULL CHECK (method IN ('FACE','PIN','OFFLINE','ADMIN')),
    reason        TEXT,                          -- why a fallback was used
    confidence    REAL,
    status        TEXT NOT NULL DEFAULT 'VALID'
                    CHECK (status IN ('VALID','PENDING_REVIEW','REJECTED')),
    client_uuid   TEXT UNIQUE,                   -- idempotency key (offline sync)
    note          TEXT,
    created_by    TEXT NOT NULL DEFAULT 'kiosk',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_punches_emp_ts ON punches(employee_id, ts);
CREATE INDEX IF NOT EXISTS idx_punches_site_ts ON punches(site_id, ts);

-- ---------------------------------------------------------------------
-- Correction requests (employee -> manager approval workflow)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS correction_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    punch_id       INTEGER REFERENCES punches(id) ON DELETE SET NULL,
    action         TEXT NOT NULL CHECK (action IN ('ADD','MODIFY','DELETE')),
    proposed_type  TEXT,
    proposed_ts    TEXT,
    break_type_id  INTEGER REFERENCES break_types(id),
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    reviewer       TEXT,
    review_note    TEXT,
    created_at     TEXT NOT NULL,
    reviewed_at    TEXT
);

-- ---------------------------------------------------------------------
-- Back-office users and roles
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('ADMIN','HR','MANAGER')),
    department_id  INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    active         INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------
-- IT incidents (camera failure, offline kiosk, misidentification ...)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kiosk_id    INTEGER REFERENCES kiosks(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,
    details     TEXT,
    ts          TEXT NOT NULL
);

-- One-time identification tickets (prevents replaying a confirmation)
CREATE TABLE IF NOT EXISTS used_tickets (
    nonce    TEXT PRIMARY KEY,
    used_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Tamper-evident audit log (each row chains the hash of the previous one)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    entity     TEXT,
    entity_id  INTEGER,
    details    TEXT,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
