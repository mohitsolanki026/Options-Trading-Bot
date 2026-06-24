import requests
import pandas as pd
import logging

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"


def download_scrip_master():
    """Download Angel One's full instrument list."""
    try:
        logger.info("📥 Downloading scrip master...")
        data = requests.get(SCRIP_MASTER_URL, timeout=30).json()
        df = pd.DataFrame(data)
        logger.info(f"✅ Scrip master loaded: {len(df)} instruments")
        return df
    except Exception as e:
        logger.error(f"❌ Scrip master download failed: {e}")
        return pd.DataFrame()


def get_nifty_options(df, expiry: str):
    """
    Filter Nifty options for a specific expiry.
    expiry format: '25APR2024' (as in scrip master)
    """
    options = df[
        (df["name"] == "NIFTY") &
        (df["instrumenttype"].isin(["OPTIDX"])) &
        (df["exch_seg"] == "NFO") &
        (df["expiry"] == expiry)
    ].copy()

    options["strike"] = options["strike"].astype(float) / 100
    options = options.sort_values("strike")
    logger.info(f"✅ Found {len(options)} Nifty option contracts for expiry {expiry}")
    return options

def get_index_options(df, index_name: str, expiry: str):
    """
    Get options for any index — NIFTY, BANKNIFTY, FINNIFTY.
    """
    options = df[
        (df["name"] == index_name) &
        (df["instrumenttype"] == "OPTIDX") &
        (df["exch_seg"] == "NFO") &
        (df["expiry"] == expiry)
    ].copy()

    options["strike"] = options["strike"].astype(float) / 100
    options = options.sort_values("strike")
    logger.info(f"✅ Found {len(options)} {index_name} contracts for {expiry}")
    return options


def get_available_expiries(df, name="NIFTY"):
    """List all available expiry dates for Nifty options."""
    options = df[
        (df["name"] == name) &
        (df["instrumenttype"] == "OPTIDX") &
        (df["exch_seg"] == "NFO")
    ]
    expiries = sorted(options["expiry"].unique().tolist())
    return expiries

def get_available_expiries_for_index(df, index_name: str):
    """Get sorted expiry list — nearest first."""
    from datetime import datetime as dt
    options = df[
        (df["name"] == index_name) &
        (df["instrumenttype"] == "OPTIDX") &
        (df["exch_seg"] == "NFO")
    ]
    expiries = options["expiry"].unique().tolist()

    # Filter out expiries more than 45 days away
    today = dt.today()
    def parse(e):
        try:
            return dt.strptime(e, "%d%b%Y")
        except:
            return dt.max

    expiries = [e for e in expiries if (parse(e) - today).days <= 45]
    expiries = sorted(expiries, key=parse)
    return expiries

def fetch_oi_data(obj, options_df, nifty_spot: float, num_strikes: int = 10):
    """
    Fetch OI for strikes around current spot price.
    Uses batch API call — much faster and correct signature.
    """
    # Round spot to nearest 50
    atm_strike = round(nifty_spot / 50) * 50

    # Get strikes around ATM
    all_strikes = sorted(options_df["strike"].unique())
    atm_idx = min(range(len(all_strikes)),
                  key=lambda i: abs(all_strikes[i] - atm_strike))

    start = max(0, atm_idx - num_strikes)
    end   = min(len(all_strikes), atm_idx + num_strikes + 1)
    selected_strikes = all_strikes[start:end]

    # Collect all tokens for batch request
    token_map = {}  # token → {strike, type}
    for strike in selected_strikes:
        for opt_type in ["CE", "PE"]:
            mask = (
                (options_df["strike"] == strike) &
                (options_df["symbol"].str.endswith(opt_type))
            )
            contract = options_df[mask]
            if not contract.empty:
                token = str(contract.iloc[0]["token"])
                token_map[token] = {
                    "strike": strike,
                    "type":   opt_type,
                    "symbol": contract.iloc[0]["symbol"]
                }

    # Batch fetch using correct signature
    all_tokens = list(token_map.keys())
    logger.info(f"📡 Fetching {len(all_tokens)} contracts in batch...")

    try:
        response = obj.getMarketData(
            "FULL",
            {"NFO": all_tokens}
        )
    except Exception as e:
        logger.error(f"❌ Batch market data failed: {e}")
        return pd.DataFrame()

    # Parse response
    fetched = {}
    if response and response.get("status"):
        data_list = response.get("data", {}).get("fetched", [])
        for item in data_list:
            token = str(item.get("symbolToken"))
            fetched[token] = {
                "ltp": item.get("ltp", 0),
                "oi":  item.get("opnInterest", 0)
            }

    # Build results DataFrame
    results = []
    for strike in selected_strikes:
        row = {"strike": strike}
        for opt_type in ["CE", "PE"]:
            # Find token for this strike+type
            token = next(
                (t for t, v in token_map.items()
                 if v["strike"] == strike and v["type"] == opt_type),
                None
            )
            if token and token in fetched:
                row[f"{opt_type}_LTP"] = fetched[token]["ltp"]
                row[f"{opt_type}_OI"]  = fetched[token]["oi"]
            else:
                row[f"{opt_type}_LTP"] = 0
                row[f"{opt_type}_OI"]  = 0
        results.append(row)

    df_oi = pd.DataFrame(results)
    logger.info(f"✅ OI data fetched for {len(df_oi)} strikes")
    return df_oi


def calculate_pcr(df_oi: pd.DataFrame):
    """Put-Call Ratio based on total OI."""
    total_put_oi  = df_oi["PE_OI"].sum()
    total_call_oi = df_oi["CE_OI"].sum()

    if total_call_oi == 0:
        return 0

    pcr = round(total_put_oi / total_call_oi, 2)
    return pcr


def find_max_pain(df_oi: pd.DataFrame):
    """
    Max Pain = strike where total losses for option buyers is maximum.
    (i.e. where option sellers profit most)
    """
    strikes = df_oi["strike"].tolist()
    pain    = []

    for s in strikes:
        ce_pain = df_oi.apply(
            lambda r: max(0, s - r["strike"]) * r["CE_OI"], axis=1
        ).sum()
        pe_pain = df_oi.apply(
            lambda r: max(0, r["strike"] - s) * r["PE_OI"], axis=1
        ).sum()
        pain.append({"strike": s, "total_pain": ce_pain + pe_pain})

    pain_df    = pd.DataFrame(pain)
    max_pain   = pain_df.loc[pain_df["total_pain"].idxmin(), "strike"]
    return max_pain


def find_support_resistance(df_oi: pd.DataFrame):
    """
    Resistance = strike with highest Call OI (big money sold calls here)
    Support    = strike with highest Put OI  (big money sold puts here)
    """
    resistance = df_oi.loc[df_oi["CE_OI"].idxmax(), "strike"]
    support    = df_oi.loc[df_oi["PE_OI"].idxmax(), "strike"]
    return support, resistance


def summarise_options_chain(df_oi: pd.DataFrame, nifty_spot: float):
    """Return a clean summary dict of all key options metrics."""
    pcr              = calculate_pcr(df_oi)
    max_pain         = find_max_pain(df_oi)
    support, resistance = find_support_resistance(df_oi)
    atm_strike       = round(nifty_spot / 50) * 50

    # ATM IV proxy (LTP based — real IV needs Black-Scholes, Week 2)
    atm_ce = df_oi[df_oi["strike"] == atm_strike]["CE_LTP"].values
    atm_pe = df_oi[df_oi["strike"] == atm_strike]["PE_LTP"].values
    atm_ce_ltp = atm_ce[0] if len(atm_ce) else 0
    atm_pe_ltp = atm_pe[0] if len(atm_pe) else 0

    sentiment = "Bullish" if pcr > 1.2 else "Bearish" if pcr < 0.8 else "Neutral"

    return {
        "nifty_spot":  nifty_spot,
        "atm_strike":  atm_strike,
        "pcr":         pcr,
        "sentiment":   sentiment,
        "max_pain":    max_pain,
        "support":     support,
        "resistance":  resistance,
        "atm_ce_ltp":  atm_ce_ltp,
        "atm_pe_ltp":  atm_pe_ltp,
    }
