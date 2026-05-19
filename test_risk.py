from utils.risk_manager import RiskManager

rm = RiskManager()

print("\n" + "="*50)
print("TEST 1: Normal trade approval")
result = rm.approve_trade(capital=100000, option_price=127)
print(f"Approved : {result['approved']}")
print(f"Lots     : {result['lots']}")
print(f"Exposure : ₹{result.get('exposure', 0)}")

print("\n" + "="*50)
print("TEST 2: Simulate 3 losing trades → hit daily limit")
rm.daily_pnl.add_trade(-2000, "NIFTY24100CE", "CLOSE")
rm.daily_pnl.add_trade(-2000, "NIFTY24100PE", "CLOSE")
rm.daily_pnl.add_trade(-1500, "NIFTY24000CE", "CLOSE")
print(f"Day P&L  : ₹{rm.daily_pnl.total_pnl}")

result2 = rm.approve_trade(capital=100000, option_price=127)
print(f"Approved : {result2['approved']}")
print(f"Reason   : {result2['reason']}")

print("\n" + "="*50)
print("TEST 3: Position sizing for different capitals")
for capital in [50000, 100000, 200000]:
    s = rm.calculate_position_size(capital, 127)
    print(f"Capital ₹{capital:,} → {s['recommended_lots']} lot(s) | Exposure ₹{s['total_exposure']:,}")

print("\n" + "="*50)
print("TEST 4: Risk status")
status = rm.get_status()
for k, v in status.items():
    print(f"  {k}: {v}")
