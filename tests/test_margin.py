"""fetch_required_margin must parse the broker response and fail safe."""
from utils.angel_helper import fetch_required_margin

LEGS = [
    {"token": "240001", "action": "SELL", "lots": 1, "lot_size": 75, "entry_ltp": 120},
    {"token": "240002", "action": "SELL", "lots": 1, "lot_size": 75, "entry_ltp": 110},
]


class FakeObj:
    def __init__(self, resp):
        self._resp = resp
        self.last_params = None

    def getMarginApi(self, params):
        self.last_params = params
        return self._resp


def test_parses_total_margin_required():
    obj = FakeObj({"status": True, "data": {"totalMarginRequired": 145000.0}})
    margin = fetch_required_margin(obj, LEGS)
    assert margin == 145000.0
    # Quantities are lots * lot_size
    assert obj.last_params["positions"][0]["qty"] == 75
    assert obj.last_params["positions"][0]["tradeType"] == "SELL"


def test_alternate_field_name():
    obj = FakeObj({"status": True, "data": {"totalMargin": 90000}})
    assert fetch_required_margin(obj, LEGS) == 90000.0


def test_returns_none_on_failure_status():
    obj = FakeObj({"status": False, "message": "bad"})
    assert fetch_required_margin(obj, LEGS) is None


def test_returns_none_without_obj_or_legs():
    assert fetch_required_margin(None, LEGS) is None
    assert fetch_required_margin(FakeObj({}), []) is None


def test_returns_none_on_exception():
    class Boom:
        def getMarginApi(self, params):
            raise RuntimeError("network down")
    assert fetch_required_margin(Boom(), LEGS, retries=1) is None


def test_retries_then_succeeds_on_transient_empty_body(monkeypatch):
    """First call throws (empty body / throttle), second returns a real margin."""
    import utils.angel_helper as ah
    monkeypatch.setattr(ah.time, "sleep", lambda *_: None)   # no real backoff in test

    class Flaky:
        def __init__(self):
            self.calls = 0
        def getMarginApi(self, params):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("Couldn't parse the JSON response: b''")
            return {"status": True, "data": {"totalMarginRequired": 145000.0}}
    obj = Flaky()
    assert fetch_required_margin(obj, LEGS, retries=2) == 145000.0
    assert obj.calls == 2


def test_payload_has_no_orderType_and_is_intraday():
    obj = FakeObj({"status": True, "data": {"totalMarginRequired": 1.0}})
    fetch_required_margin(obj, LEGS)
    pos = obj.last_params["positions"][0]
    assert "orderType" not in pos              # extra keys make the gateway 500/empty
    assert pos["productType"] == "INTRADAY"
