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
    assert fetch_required_margin(Boom(), LEGS) is None
