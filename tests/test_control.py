"""The dashboard's command queue: receipts in, one drain out, bounded results."""
import pytest

from utils import control


def test_submit_returns_a_receipt_and_drain_empties_the_queue():
    receipt = control.submit("close_position", index="NIFTY")
    assert receipt["action"] == "close_position"
    assert receipt["status"] == "queued"
    assert receipt["id"] > 0
    assert control.pending() == 1

    taken = control.drain()
    assert len(taken) == 1
    assert taken[0]["id"] == receipt["id"]
    assert taken[0]["params"] == {"index": "NIFTY"}
    assert control.pending() == 0
    assert control.drain() == []


def test_unknown_action_is_refused():
    with pytest.raises(control.UnknownAction):
        control.submit("sell_the_house")
    assert control.pending() == 0


def test_complete_records_a_result_that_can_be_looked_up():
    receipt = control.submit("rescan")
    cmd = control.drain()[0]
    assert control.result(receipt["id"]) is None

    control.complete(cmd, True, "Scanned")
    outcome = control.result(receipt["id"])
    assert outcome["ok"] is True
    assert outcome["message"] == "Scanned"
    assert outcome["action"] == "rescan"
    assert outcome["finished_at"]


def test_results_are_capped_so_they_cannot_grow_forever():
    ids = []
    for _ in range(control.MAX_RESULTS + 10):
        receipt = control.submit("rescan")
        ids.append(receipt["id"])
        control.complete(control.drain()[0], True)

    assert control.result(ids[0]) is None          # oldest evicted
    assert control.result(ids[-1]) is not None     # newest kept
    kept = [i for i in ids if control.result(i) is not None]
    assert len(kept) == control.MAX_RESULTS
