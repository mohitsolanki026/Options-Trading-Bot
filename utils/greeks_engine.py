import numpy as np
from scipy.stats import norm
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
#  BLACK-SCHOLES CORE
# ─────────────────────────────────────────

def black_scholes(S, K, T, r, sigma, option_type="CE"):
    """
    S     = Spot price (e.g. 24119)
    K     = Strike price (e.g. 24000)
    T     = Time to expiry in years (e.g. 7/365)
    r     = Risk-free rate (e.g. 0.065 for 6.5%)
    sigma = Implied volatility (e.g. 0.15 for 15%)
    option_type = 'CE' or 'PE'
    """
    if T <= 0 or sigma <= 0:
        return 0.0

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == "CE":
        price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    return round(price, 2)


def calculate_greeks(S, K, T, r, sigma, option_type="CE"):
    """
    Returns all Greeks for a given option.
    """
    if T <= 0 or sigma <= 0:
        return {}

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    # Delta
    if option_type == "CE":
        delta = norm.cdf(d1)
    else:
        delta = norm.cdf(d1) - 1

    # Gamma (same for CE and PE)
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))

    # Theta (per day)
    theta_CE = (
        -(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
        - r * K * np.exp(-r * T) * norm.cdf(d2)
    ) / 365

    theta_PE = (
        -(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
        + r * K * np.exp(-r * T) * norm.cdf(-d2)
    ) / 365

    theta = theta_CE if option_type == "CE" else theta_PE

    # Vega (per 1% change in IV)
    vega = S * norm.pdf(d1) * np.sqrt(T) / 100

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 2),
        "vega":  round(vega, 2),
    }


# ─────────────────────────────────────────
#  IMPLIED VOLATILITY (Newton-Raphson)
# ─────────────────────────────────────────

def calculate_iv(market_price, S, K, T, r, option_type="CE", iterations=100):
    """
    Back-solve for IV given the market price of an option.
    Uses Newton-Raphson method.
    """
    if T <= 0 or market_price <= 0:
        return 0.0

    sigma = 0.2  # initial guess: 20%

    for _ in range(iterations):
        price = black_scholes(S, K, T, r, sigma, option_type)
        diff  = market_price - price

        if abs(diff) < 0.001:
            break

        d1   = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        vega = S * norm.pdf(d1) * np.sqrt(T)

        if vega < 1e-10:
            break

        sigma += diff / vega
        sigma  = max(0.001, min(sigma, 10.0))  # clamp between 0.1% and 1000%

    return round(sigma * 100, 2)  # return as percentage


# ─────────────────────────────────────────
#  TIME TO EXPIRY
# ─────────────────────────────────────────

def time_to_expiry(expiry_str: str):
    """
    expiry_str: '05MAY2026'
    Returns T in years (float)
    """
    try:
        expiry_date = datetime.strptime(expiry_str, "%d%b%Y").date()
        today       = date.today()
        days_left   = (expiry_date - today).days
        T           = max(days_left, 0) / 365
        return T, days_left
    except Exception as e:
        logger.error(f"❌ Time to expiry error: {e}")
        return 0.0, 0


# ─────────────────────────────────────────
#  ATM ANALYSIS (main function called by bot)
# ─────────────────────────────────────────

def analyse_atm_greeks(summary: dict, expiry_str: str, risk_free_rate=0.065):
    """
    Takes the options summary from options_helper
    and returns full Greeks + IV analysis for ATM strike.
    """
    S          = summary["nifty_spot"]
    K          = summary["atm_strike"]
    ce_ltp     = summary["atm_ce_ltp"]
    pe_ltp     = summary["atm_pe_ltp"]
    T, days    = time_to_expiry(expiry_str)
    # Handle expiry day (T=0) gracefully
    if T <= 0 or days <= 0:
        logger.warning("⚠️ Expiry day — T=0, using default Greeks.")
        return {
            "spot":        S,
            "atm_strike":  K,
            "days_to_exp": 0,
            "ce_iv":       0.0,
            "pe_iv":       0.0,
            "avg_iv":      0.0,
            "ce_delta":    0.5,
            "pe_delta":    -0.5,
            "gamma":       0.0,
            "theta":       0.0,
            "vega":        0.0,
        }
    r          = risk_free_rate

    logger.info(f"⏳ Days to expiry: {days} | T={round(T*365,1)} days")

    # Calculate IV from market prices
    ce_iv = calculate_iv(ce_ltp, S, K, T, r, "CE")
    pe_iv = calculate_iv(pe_ltp, S, K, T, r, "PE")
    avg_iv = round((ce_iv + pe_iv) / 2, 2)

    # Use avg IV to calculate Greeks
    sigma = avg_iv / 100
    ce_greeks = calculate_greeks(S, K, T, r, sigma, "CE")
    pe_greeks = calculate_greeks(S, K, T, r, sigma, "PE")

    result = {
        "spot":        S,
        "atm_strike":  K,
        "days_to_exp": days,
        "ce_iv":       ce_iv,
        "pe_iv":       pe_iv,
        "avg_iv":      avg_iv,
        "ce_delta":    ce_greeks.get("delta"),
        "pe_delta":    pe_greeks.get("delta"),
        "gamma":       ce_greeks.get("gamma"),
        "theta":       ce_greeks.get("theta"),  # daily decay ₹ per lot
        "vega":        ce_greeks.get("vega"),
    }

    return result


# ─────────────────────────────────────────
#  IV RANK (0-100)
# ─────────────────────────────────────────

def calculate_iv_rank(current_iv: float, iv_history: list):
    """
    IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100
    iv_history: list of past IV values (at least 30 days)

    IV Rank > 50 → IV is HIGH → sell options
    IV Rank < 30 → IV is LOW  → buy options
    """
    if not iv_history or len(iv_history) < 2:
        return 50.0  # default neutral

    iv_low  = min(iv_history)
    iv_high = max(iv_history)

    if iv_high == iv_low:
        return 50.0

    rank = (current_iv - iv_low) / (iv_high - iv_low) * 100
    return round(rank, 1)
