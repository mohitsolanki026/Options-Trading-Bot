"""Runtime settings: env defaults, validated writes, and no half-applied edits."""
from config import settings as env
from utils import settings_store as ss


def test_untouched_key_falls_back_to_env_default():
    assert ss.get("max_open_positions") == env.RISK_RULES["max_open_positions"]
    assert ss.get("max_daily_loss") == env.RISK_RULES["max_daily_loss"]
    assert ss.override("max_open_positions") is None


def test_update_applies_and_override_reports_it():
    applied, errors = ss.update({"max_open_positions": 5})
    assert errors == []
    assert applied == {"max_open_positions": 5}
    assert ss.get("max_open_positions") == 5
    assert ss.override("max_open_positions") == 5
    assert ss.override("entry_threshold") is None      # untouched


def test_out_of_range_is_rejected_and_old_value_survives():
    ss.update({"max_open_positions": 5})
    applied, errors = ss.update({"max_open_positions": 99})
    assert applied == {}
    assert len(errors) == 1 and "above 10" in errors[0]
    assert ss.get("max_open_positions") == 5


def test_setting_back_to_default_drops_the_override():
    ss.update({"max_open_positions": 5})
    assert ss.override("max_open_positions") == 5
    applied, errors = ss.update({"max_open_positions": ss.BY_KEY["max_open_positions"].default})
    assert errors == [] and "max_open_positions" in applied
    assert ss.override("max_open_positions") is None
    assert ss.get("max_open_positions") == ss.BY_KEY["max_open_positions"].default


def test_buy_threshold_above_sell_threshold_is_rejected_for_both():
    applied, errors = ss.update({"iv_rank_buy": 70, "iv_rank_sell": 60})
    assert applied == {}
    assert any("lower than" in e for e in errors)
    assert ss.override("iv_rank_buy") is None
    assert ss.override("iv_rank_sell") is None


def test_entry_window_must_start_before_it_ends():
    applied, errors = ss.update({"entry_window_start": "14:30", "entry_window_end": "10:00"})
    assert applied == {}
    assert any("start before it ends" in e for e in errors)
    assert ss.override("entry_window_start") is None
    assert ss.override("entry_window_end") is None


def test_non_editable_key_is_refused():
    applied, errors = ss.update({"paper_capital": 999999})
    assert applied == {}
    assert errors and "cannot be changed" in errors[0]
    assert ss.override("paper_capital") is None


def test_unknown_key_is_refused():
    applied, errors = ss.update({"go_faster": 1})
    assert applied == {}
    assert errors == ["Unknown setting 'go_faster'."]


def test_version_only_moves_on_a_real_change():
    before = ss.version()
    ss.update({"max_open_positions": 99})           # invalid
    ss.update({"nope": 1})                          # unknown
    assert ss.version() == before
    ss.update({"max_open_positions": 5})            # valid
    assert ss.version() == before + 1


def test_describe_gives_the_ui_one_row_per_setting():
    rows = ss.describe()
    assert len(rows) == len(ss.SPEC)
    assert [r["key"] for r in rows] == [s.key for s in ss.SPEC]
    needed = {"key", "group", "groupLabel", "label", "help", "kind", "value",
              "default", "min", "max", "choices", "editable", "overridden"}
    for row in rows:
        assert needed <= set(row)
        assert row["groupLabel"] == ss.GROUP_LABELS[row["group"]]


def test_indices_accept_a_comma_string():
    applied, errors = ss.update({"active_indices": "NIFTY, FINNIFTY"})
    assert errors == []
    assert applied["active_indices"] == ["NIFTY", "FINNIFTY"]
    assert ss.get("active_indices") == ["NIFTY", "FINNIFTY"]


def test_indices_reject_an_unknown_index():
    applied, errors = ss.update({"active_indices": "NIFTY, DOWJONES"})
    assert applied == {}
    assert any("unknown index DOWJONES" in e for e in errors)
    assert ss.override("active_indices") is None


def test_indices_reject_an_empty_list():
    applied, errors = ss.update({"active_indices": ""})
    assert applied == {}
    assert any("at least one index" in e for e in errors)
