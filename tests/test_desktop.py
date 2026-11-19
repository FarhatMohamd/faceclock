"""First-run wizard and desktop edition helpers."""

import json

import pytest

from faceclock import create_app
from faceclock.config import TestConfig
from faceclock.db import query


@pytest.fixture
def fresh(tmp_path):
    """An application with an empty database, as on the very first start."""
    app = create_app(TestConfig, DATABASE=str(tmp_path / "fresh.db"), DESKTOP_MODE=True,
                     DESKTOP_STATE_FILE=str(tmp_path / "desktop.json"))
    return app, app.test_client(), tmp_path


def test_first_start_redirects_to_setup(fresh):
    _, client, _ = fresh
    assert client.get("/").headers["Location"].endswith("/setup")
    assert client.get("/admin/login").headers["Location"].endswith("/setup")


def test_setup_creates_site_and_admin_and_logs_in(fresh):
    app, client, _ = fresh
    r = client.post("/setup", data={"site_name": "Headquarters", "timezone": "Europe/Paris",
                                    "username": "mohamad", "password": "secret-pass",
                                    "password2": "secret-pass"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/employees")
    assert client.get("/admin/").status_code == 200          # already logged in
    with app.app_context():
        assert query("SELECT role FROM users WHERE username = 'mohamad'", one=True)["role"] == "ADMIN"
        assert query("SELECT COUNT(*) n FROM break_types", one=True)["n"] >= 4
    # the wizard is closed once an account exists
    assert client.get("/setup").headers["Location"].endswith("/admin/login")


def test_setup_validates_input(fresh):
    _, client, _ = fresh
    r = client.post("/setup", data={"site_name": "", "username": "ab", "password": "short",
                                    "password2": "other"})
    assert r.status_code == 200 and b"at least 8 characters" in r.data


def test_desktop_kiosk_is_created_once_and_reused(fresh):
    app, client, tmp = fresh
    client.post("/setup", data={"site_name": "HQ", "username": "mohamad",
                                "password": "secret-pass", "password2": "secret-pass"})
    first = client.get("/terminal").headers["Location"]
    second = client.get("/terminal").headers["Location"]
    assert "token=kio_" in first and first == second
    tokens = json.loads((tmp / "desktop.json").read_text())["kiosk_tokens"]
    assert list(tokens.values())[0] in first
    # old control-window link still works
    assert client.get("/desktop/kiosk").headers["Location"].endswith("/terminal")
    with app.app_context():
        assert query("SELECT COUNT(*) n FROM kiosks", one=True)["n"] == 1


def test_desktop_routes_are_local_only(fresh):
    _, client, _ = fresh
    r = client.get("/desktop/kiosk", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code == 404
    # from another computer, "Start" requires a back-office login
    r = client.get("/terminal", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert "/admin/login" in r.headers["Location"] or r.headers["Location"].endswith("/setup")


def test_desktop_routes_disabled_in_server_mode(tmp_path):
    app = create_app(TestConfig, DATABASE=str(tmp_path / "srv.db"))
    assert app.test_client().get("/desktop/kiosk").status_code == 404


# ---------------------------------------------------------------------------
# "Start terminal" from a browser (server edition)
# ---------------------------------------------------------------------------
def test_start_terminal_requires_login(client):
    r = client.get("/terminal")
    assert "/admin/login" in r.headers["Location"]


def test_start_terminal_chooses_building_then_remembers_it(app, client, seed_data):
    from conftest import login
    login(client, "admin", "admin-pass")
    page = client.get("/terminal")                       # two buildings in the test company
    assert page.status_code == 200 and b"Which building" in page.data
    r1 = client.get(f"/terminal?site_id={seed_data['site_b']}")
    r2 = client.get(f"/terminal?site_id={seed_data['site_b']}")
    assert "token=kio_" in r1.headers["Location"]
    assert r1.headers["Location"] == r2.headers["Location"]   # same kiosk reused via cookie
    with app.app_context():
        n = query("SELECT COUNT(*) n FROM kiosks WHERE site_id = ?", (seed_data["site_b"],), one=True)["n"]
    assert n == 2                                         # seeded kiosk + this browser's terminal
    # next "Start" opens building B directly; "Change building" shows the choice again
    assert "token=kio_" in client.get("/terminal").headers["Location"]
    assert b"Which building" in client.get("/terminal?choose=1").data


def test_started_terminal_works(client, seed_data):
    from conftest import RED, login, make_image
    login(client, "admin", "admin-pass")
    loc = client.get(f"/terminal?site_id={seed_data['site_a']}").headers["Location"]
    token = loc.split("token=")[1]
    r = client.post("/api/kiosk/identify", json={"frames": [make_image(RED)]},
                    headers={"X-Kiosk-Token": token}).get_json()
    assert r["ok"] and r["employee"]["name"] == "Alice M."


def test_navbar_has_start_button(client):
    from conftest import login
    login(client, "admin", "admin-pass")
    assert b"Start terminal" in client.get("/admin/").data
