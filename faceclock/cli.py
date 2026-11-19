"""
Command-line administration (``flask --app faceclock <command>``)
=================================================================
"""

import secrets
from datetime import timedelta

import click

from .db import execute, get_db, now_iso, query, to_iso, utcnow
from .security import hash_password, hash_pin, hash_token, new_kiosk_token
from .services.audit import audit, verify_chain

DEFAULT_BREAK_TYPES = [
    # code, label, paid, daily allowance (min), max single break (min)
    ("SMOKE", "Smoking break", 1, 20, 10),
    ("COFFEE", "Coffee break", 1, 20, 15),
    ("LUNCH", "Lunch break", 0, 0, 90),
    ("PERSONAL", "Personal break", 0, 0, 0),
]


def ensure_defaults():
    for code, label, paid, allowance, max_d in DEFAULT_BREAK_TYPES:
        execute("INSERT OR IGNORE INTO break_types (code, label, paid, daily_allowance_minutes, "
                "max_duration_minutes) VALUES (?, ?, ?, ?, ?)", (code, label, paid, allowance, max_d))


def create_kiosk(name, site_id):
    token = new_kiosk_token()
    kid = execute("INSERT INTO kiosks (name, site_id, token_hash) VALUES (?, ?, ?)",
                  (name, site_id, hash_token(token)))
    audit("cli", "KIOSK_CREATED", "kiosk", kid, {"name": name, "site_id": site_id})
    return kid, token


def register_cli(app):

    @app.cli.command("init-db")
    def init_db_cmd():
        """Create tables and default break types."""
        ensure_defaults()
        click.echo("Database ready.")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.option("--role", default="ADMIN", type=click.Choice(["ADMIN", "HR", "MANAGER"]))
    @click.option("--department", default=None, help="Department code (for managers)")
    @click.password_option()
    def create_admin_cmd(username, role, department, password):
        """Create a back-office user."""
        dep_id = None
        if department:
            dep = query("SELECT id FROM departments WHERE code = ?", (department,), one=True)
            if not dep:
                raise click.ClickException(f"Unknown department {department}")
            dep_id = dep["id"]
        uid = execute("INSERT INTO users (username, password_hash, role, department_id) "
                      "VALUES (?, ?, ?, ?)", (username, hash_password(password), role, dep_id))
        audit("cli", "USER_CREATED", "user", uid, {"username": username, "role": role})
        click.echo(f"User '{username}' created with role {role}.")

    @app.cli.command("create-kiosk")
    @click.argument("name")
    @click.argument("site_name")
    def create_kiosk_cmd(name, site_name):
        """Register a punch terminal and print its secret token (shown once)."""
        site = query("SELECT id FROM sites WHERE name = ?", (site_name,), one=True)
        if not site:
            raise click.ClickException(f"Unknown site '{site_name}'")
        _, token = create_kiosk(name, site["id"])
        click.echo(f"Kiosk '{name}' created. Open: /kiosk?token={token}")

    @app.cli.command("verify-audit")
    def verify_audit_cmd():
        """Check that the audit log has not been tampered with."""
        ok, broken = verify_chain()
        if ok:
            click.echo("Audit log integrity: OK")
        else:
            raise click.ClickException(f"Audit log broken at entry #{broken}")

    @app.cli.command("purge")
    @click.option("--dry-run", is_flag=True)
    def purge_cmd(dry_run):
        """Apply GDPR retention rules (old punches, incidents, departed staff biometrics)."""
        cfg = app.config
        punch_cut = to_iso(utcnow() - timedelta(days=cfg["PUNCH_RETENTION_DAYS"]))
        inc_cut = to_iso(utcnow() - timedelta(days=cfg["INCIDENT_RETENTION_DAYS"]))
        counts = {
            "punches": query("SELECT COUNT(*) n FROM punches WHERE ts < ?", (punch_cut,), one=True)["n"],
            "incidents": query("SELECT COUNT(*) n FROM incidents WHERE ts < ?", (inc_cut,), one=True)["n"],
            "templates_of_inactive": query(
                "SELECT COUNT(*) n FROM face_templates t JOIN employees e ON e.id = t.employee_id "
                "WHERE e.active = 0 OR e.biometric_consent = 0", one=True)["n"],
            "used_tickets": query("SELECT COUNT(*) n FROM used_tickets", one=True)["n"],
        }
        click.echo(f"To purge: {counts}")
        if dry_run:
            return
        execute("DELETE FROM punches WHERE ts < ?", (punch_cut,))
        execute("DELETE FROM incidents WHERE ts < ?", (inc_cut,))
        execute("DELETE FROM face_templates WHERE employee_id IN "
                "(SELECT id FROM employees WHERE active = 0 OR biometric_consent = 0)")
        execute("DELETE FROM used_tickets")
        audit("cli", "RETENTION_PURGE", details=counts)
        click.echo("Purge done.")

    @app.cli.command("seed-demo")
    def seed_demo_cmd():
        """Fill the database with a demo company (2 buildings, 3 directions)."""
        if query("SELECT id FROM sites LIMIT 1", one=True):
            raise click.ClickException("Database is not empty, demo data not loaded.")
        ensure_defaults()

        hq = execute("INSERT INTO sites (name, address, timezone) VALUES (?, ?, ?)",
                     ("Headquarters - Building A", "1 Main Street", "Europe/Paris"))
        plant = execute("INSERT INTO sites (name, address, timezone) VALUES (?, ?, ?)",
                        ("Plant - Building B", "Industrial Park", "Europe/Paris"))

        def dep(name, code, parent=None):
            return execute("INSERT INTO departments (name, code, parent_id) VALUES (?, ?, ?)",
                           (name, code, parent))

        gen = dep("General Management", "GM")
        fin = dep("Finance Direction", "FIN", gen)
        acc = dep("Accounting", "FIN-ACC", fin)
        ops = dep("Operations Direction", "OPS", gen)
        log = dep("Logistics", "OPS-LOG", ops)
        mnt = dep("Maintenance", "OPS-MNT", ops)
        it = dep("IT Direction", "IT", gen)

        day = execute("INSERT INTO schedules (name, start_time, end_time, workdays, grace_minutes, "
                      "expected_minutes) VALUES ('Office 08:30-17:00', '08:30', '17:00', "
                      "'1,2,3,4,5', 5, 450)")
        night = execute("INSERT INTO schedules (name, start_time, end_time, workdays, grace_minutes, "
                        "expected_minutes) VALUES ('Night 22:00-06:00', '22:00', '06:00', "
                        "'1,2,3,4,5', 5, 450)")

        people = [
            ("E1001", "Alice", "Martin", acc, hq, day),
            ("E1002", "Karim", "Benali", fin, hq, day),
            ("E1003", "Sophie", "Durand", log, plant, day),
            ("E1004", "Lucas", "Petit", mnt, plant, night),
            ("E1005", "Nadia", "Haddad", it, hq, day),
            ("E1006", "Thomas", "Moreau", ops, plant, day),
        ]
        click.echo("\nDemo employees (code / PIN):")
        for code, first, last, dep_id, site_id, sched in people:
            pin = f"{secrets.randbelow(9000) + 1000}"
            while len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
                pin = f"{secrets.randbelow(9000) + 1000}"
            execute("INSERT INTO employees (employee_code, first_name, last_name, department_id, "
                    "home_site_id, schedule_id, pin_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (code, first, last, dep_id, site_id, sched, hash_pin(pin), now_iso()))
            click.echo(f"  {code}  {first} {last:<8}  PIN {pin}")

        password = secrets.token_urlsafe(9)
        execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'ADMIN')",
                (hash_password(password),))
        mgr_pwd = secrets.token_urlsafe(9)
        execute("INSERT INTO users (username, password_hash, role, department_id) "
                "VALUES ('ops.manager', ?, 'MANAGER', ?)", (hash_password(mgr_pwd), ops))

        _, t1 = create_kiosk("Entrance A", hq)
        _, t2 = create_kiosk("Entrance B", plant)
        get_db().commit()
        audit("cli", "DEMO_SEEDED")
        click.echo(f"\nBack-office:  admin / {password}   (manager: ops.manager / {mgr_pwd})")
        click.echo(f"Kiosk A:      /kiosk?token={t1}")
        click.echo(f"Kiosk B:      /kiosk?token={t2}\n")
