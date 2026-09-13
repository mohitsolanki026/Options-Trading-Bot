"""The dashboard's HTTP surface: locked by default, read-only until a bot is bound."""
import pytest
from fastapi.testclient import TestClient

from utils import control
from utils.paper_trader import PaperTrader
from utils.risk_manager import RiskManager
from utils.runtime import RUNTIME
from web import auth, server


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client on a fresh app, with auth on and a throwaway token file."""
    monkeypatch.setattr(auth, "WEB_UI_AUTH", "on")
    monkeypatch.setattr(auth, "TOKEN_FILE", str(tmp_path / "web_token.txt"))
    monkeypatch.setattr(auth, "_token", None)
    with TestClient(server.create_app()) as c:
        yield c


@pytest.fixture
def signed_in(client):
    assert client.post("/api/login", json={"token": auth.get_token()}).status_code == 200
    return client


@pytest.fixture
def bot():
    """Bind a minimal live STATE, and always unbind it again."""
    state = {
        "paper_trader":   PaperTrader(starting_capital=500000),
        "risk_manager":   RiskManager(),
        "active_indices": ["NIFTY"],
        "index_data":     {},
    }
    RUNTIME.bind_state(state)
    yield state
    RUNTIME.bind_state(None)
    RUNTIME.series.clear()


# ── access control ─────────────────────────

def test_api_without_a_token_is_401(client):
    response = client.get("/api/state")
    assert response.status_code == 401
    assert "Sign in" in response.json()["detail"]


def test_a_page_without_a_token_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_healthz_and_login_are_public(client):
    assert client.get("/healthz").json() == {"ok": True}
    login = client.get("/login")
    assert login.status_code == 200
    assert "Access token" in login.text


def test_a_wrong_token_is_refused(client):
    response = client.post("/api/login", json={"token": "definitely-not-it"})
    assert response.status_code == 401
    assert client.get("/api/state").status_code == 401


def test_the_right_token_sets_a_cookie_that_opens_the_api(client):
    response = client.post("/api/login", json={"token": auth.get_token()})
    assert response.status_code == 200
    assert auth.COOKIE_NAME in response.cookies
    assert client.get("/api/state").status_code == 200


def test_a_bearer_token_also_works(client):
    response = client.get("/api/state",
                          headers={"Authorization": f"Bearer {auth.get_token()}"})
    assert response.status_code == 200


def test_logout_closes_the_door_again(signed_in):
    assert signed_in.post("/api/logout").status_code == 200
    assert signed_in.get("/api/state").status_code == 401


# ── reads ──────────────────────────────────

def test_state_is_not_ready_before_the_bot_binds(signed_in):
    body = signed_in.get("/api/state").json()
    assert body["ready"] is False
    assert body["positions"] == [] and body["indices"] == []
    assert body["account"] == {} and body["risk"] == {}
    assert body["mode"] == "paper"
    assert body["market"]["phase"] in ("premarket", "open", "closed", "weekend")
    assert body["health"]["total"] > 0


def test_settings_endpoint_returns_groups_and_rows(signed_in):
    body = signed_in.get("/api/settings").json()
    assert body["groups"]["money"] == "Money"
    keys = [row["key"] for row in body["settings"]]
    assert "max_open_positions" in keys and "active_indices" in keys


def test_posting_settings_applies_the_good_and_reports_the_bad(signed_in):
    body = signed_in.post("/api/settings",
                          json={"max_open_positions": 5, "iv_rank_sell": 500}).json()
    assert body["applied"] == {"max_open_positions": 5}
    assert body["errors"] and "above 100" in body["errors"][0]
    row = next(r for r in body["settings"] if r["key"] == "max_open_positions")
    assert row["value"] == 5 and row["overridden"] is True


# ── writes ─────────────────────────────────

def test_pause_is_unavailable_until_the_bot_is_running(signed_in):
    response = signed_in.post("/api/control/pause")
    assert response.status_code == 503
    assert "starting up" in response.json()["detail"]


def test_pause_then_resume_with_a_bot_bound(signed_in, bot):
    assert signed_in.get("/api/state").json()["ready"] is True

    paused = signed_in.post("/api/control/pause").json()
    assert paused["ok"] is True and paused["changed"] is True
    assert bot["risk_manager"].trading_halted is True
    assert signed_in.get("/api/state").json()["risk"]["halted"] is True

    resumed = signed_in.post("/api/control/resume").json()
    assert resumed["ok"] is True and resumed["paused"] is False
    assert bot["risk_manager"].trading_halted is False
    assert signed_in.get("/api/state").json()["risk"]["halted"] is False


def test_closing_a_position_is_queued_for_the_tick_loop(signed_in):
    response = signed_in.post("/api/control/close", json={"index": "NIFTY"})
    assert response.status_code == 200
    assert response.json()["action"] == "close_position"
    assert control.pending() == 1
    assert control.drain()[0]["params"] == {"index": "NIFTY"}


def test_closing_without_an_index_is_a_400(signed_in):
    assert signed_in.post("/api/control/close", json={}).status_code == 400
    assert control.pending() == 0


def test_a_queued_command_reports_pending_then_done(signed_in):
    command_id = signed_in.post("/api/control/rescan").json()["id"]
    assert signed_in.get(f"/api/control/result/{command_id}").json()["status"] == "pending"

    control.complete(control.drain()[0], True, "Scanned")
    done = signed_in.get(f"/api/control/result/{command_id}").json()
    assert done["status"] == "done" and done["ok"] is True
