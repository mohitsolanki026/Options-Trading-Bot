from utils.angel_helper import get_angel_client, fetch_ltp
from utils.telegram_helper import send_market_update, send_options_summary
from datetime import datetime
from utils.options_helper import (
    download_scrip_master, get_available_expiries,
    get_nifty_options, fetch_oi_data, summarise_options_chain
)

if __name__ == "__main__":
    obj = get_angel_client()


    if not obj:
        print("❌ Login failed.")
        exit()

    # --- Live prices ---
    nifty_ltp = fetch_ltp(obj, "NSE", "Nifty 50", "26000")
    vix_ltp   = fetch_ltp(obj, "NSE", "India VIX", "99926017")

    print(f"\n📊 Nifty: ₹{nifty_ltp} | VIX: {vix_ltp}")
    send_market_update(nifty_ltp, vix_ltp)

    # --- Options Chain ---
    df_scrip  = download_scrip_master()
    expiries  = get_available_expiries(df_scrip)

    print(f"\n📅 Available expiries: {expiries[:5]}")

    # Use nearest expiry (index 0)
    # nearest_expiry = expiries[0]
    # print(f"Using expiry: {nearest_expiry}")

    def parse_expiry(e):
        try:
            return datetime.strptime(e, "%d%b%Y")
        except:
            return datetime.max

    expiries_sorted = sorted(expiries, key=parse_expiry)
    nearest_expiry  = expiries_sorted[0]
    print(f"Available expiries: {expiries_sorted[:5]}")
    print(f"Using expiry: {nearest_expiry}")

    options_df = get_nifty_options(df_scrip, nearest_expiry)

    # Fetch OI for 10 strikes around ATM
    print("\n⏳ Fetching OI data (takes ~30 sec)...")
    df_oi = fetch_oi_data(obj, options_df, nifty_ltp, num_strikes=10)

    print("\n📋 Options Chain (ATM ± 10 strikes):")
    print(df_oi.to_string(index=False))

    # Summary
    summary = summarise_options_chain(df_oi, nifty_ltp)
    print(f"\n🎯 Summary: {summary}")
    send_options_summary(summary)
