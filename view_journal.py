from utils.trade_journal import (
    init_db, print_today_signals, get_weekly_summary
)

init_db()

print("\n📊 TODAY'S SIGNALS:")
print_today_signals()

print("\n📅 WEEKLY SUMMARY:")
print(get_weekly_summary())
