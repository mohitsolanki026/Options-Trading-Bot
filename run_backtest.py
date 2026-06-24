from utils.backtester import generate_backtest_report, backtest_from_journal
from utils.trade_journal import init_db, get_weekly_summary

init_db()

print("\n" + generate_backtest_report(days_back=30))
print("\n📅 Weekly P&L Summary:")
print(get_weekly_summary())
