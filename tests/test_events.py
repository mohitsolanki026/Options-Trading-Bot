"""The event bus must fan out to journal, ring and subscribers — and never throw."""
import queue

import pytest

from utils import events
from utils.trade_journal import get_events


def test_emit_reaches_journal_ring_and_subscriber():
    sub = events.subscribe()
    event = events.emit("test.thing", "Something happened", "with a body",
                        index="NIFTY", level="success")

    assert event["id"] > 0
    assert event["kind"] == "test.thing" and event["index"] == "NIFTY"

    rows = get_events(limit=5)
    assert rows[0]["title"] == "Something happened"
    assert rows[0]["body"] == "with a body"
    assert rows[0]["level"] == "success"

    assert events.recent(5)[0]["title"] == "Something happened"
    assert sub.get_nowait()["title"] == "Something happened"


def test_a_subscriber_that_stops_draining_is_dropped_not_blocking():
    sub = events.subscribe(maxsize=1)
    events.emit("test.one", "First")          # fills the queue
    assert events.subscriber_count() == 1

    event = events.emit("test.two", "Second")  # queue is full → drop the subscriber
    assert event["title"] == "Second"          # the emitter still returned
    assert events.subscriber_count() == 0
    assert sub.qsize() == 1                    # nothing was force-fed to it

    events.emit("test.three", "Third")
    assert events.recent(1)[0]["title"] == "Third"


def test_recent_is_newest_first():
    for title in ("One", "Two", "Three"):
        events.emit("test.order", title)
    assert [e["title"] for e in events.recent(3)] == ["Three", "Two", "One"]


def test_prime_seeds_the_ring_from_the_journal():
    events.emit("test.prime", "Older")
    events.emit("test.prime", "Newer")
    events._reset_for_tests()
    assert events.recent() == []

    events.prime(get_events(limit=10))
    assert [e["title"] for e in events.recent(2)] == ["Newer", "Older"]


def test_emit_survives_a_failing_journal_write(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(events, "log_event", boom)
    event = events.emit("test.broken", "Still reported")
    assert event["id"] == -1
    assert event["title"] == "Still reported"
    assert events.recent(1)[0]["title"] == "Still reported"


def test_unknown_level_falls_back_to_info():
    assert events.emit("test.level", "Odd", level="chartreuse")["level"] == "info"


def test_unsubscribe_removes_the_queue():
    sub = events.subscribe()
    assert events.subscriber_count() == 1
    events.unsubscribe(sub)
    assert events.subscriber_count() == 0
    events.emit("test.gone", "Nobody listening")
    with pytest.raises(queue.Empty):
        sub.get_nowait()
