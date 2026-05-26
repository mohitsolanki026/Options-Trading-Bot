import json
import logging
import requests
import time
from datetime import datetime
from config.settings import (
    LLM_PROVIDER, LLM_MODEL,
    ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMENI_API_KEY,
    GROQ_API_KEY, OLLAMA_BASE_URL, OLLAMA_MODEL
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
#  PROVIDER ADAPTERS
# ─────────────────────────────────────────

def _call_anthropic(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model      = LLM_MODEL,
        max_tokens = 3000,
        messages   = [{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


def _call_openai(prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model    = LLM_MODEL,
        messages = [{"role": "user", "content": prompt}],
        max_completion_tokens = 1000,
    )
    return resp.choices[0].message.content.strip()


def _call_groq(prompt: str) -> str:
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model      = LLM_MODEL,
        messages   = [
            {
                "role":    "system",
                "content": "You are an expert Indian options trader. Always respond with valid JSON only. No explanation outside the JSON object."
            },
            {"role": "user", "content": prompt}
        ],
        max_tokens  = 3000,
        temperature = 0.1,   # low temperature = more deterministic JSON
    )
    raw = resp.choices[0].message.content
    if not raw or not raw.strip():
        raise ValueError("Groq returned empty response")
    return raw.strip()

def _call_ollama(prompt: str) -> str:
    """Ollama — runs locally, completely free."""
    url  = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model":  OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()

def _call_gemini(prompt: str) -> str:
    """Google Gemini """
    from gemini import GeminiClient
    client = GeminiClient(api_key=GEMENI_API_KEY)
    resp = client.chat.completions.create(
        model      = LLM_MODEL,
        messages   = [{"role": "user", "content": prompt}],
        max_tokens = 1000,
    )
    return resp.choices[0].message.content.strip()


# ─────────────────────────────────────────
#  PROVIDER ROUTER
# ─────────────────────────────────────────

PROVIDERS = {
    "anthropic": _call_anthropic,
    "openai":    _call_openai,
    "groq":      _call_groq,
    "ollama":    _call_ollama,
    "gemini":    _call_gemini,
}


def call_llm(prompt: str, retries: int = 3) -> str:
    """Route to the configured LLM provider with retry logic."""
    provider = LLM_PROVIDER.lower()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")

    for attempt in range(1, retries + 1):
        try:
            logger.info(f"🤖 Calling LLM: provider={provider} model={LLM_MODEL} (attempt {attempt})")
            result = PROVIDERS[provider](prompt)
            if result and result.strip():
                return result
            raise ValueError("Empty response from LLM")
        except Exception as e:
            logger.warning(f"⚠️ LLM attempt {attempt} failed: {e}")
            if attempt == retries:
                raise
            time.sleep(2)

# ─────────────────────────────────────────
#  PROMPT BUILDER (unchanged)
# ─────────────────────────────────────────

def build_market_prompt(
    summary:     dict,
    greeks:      dict,
    regime:      dict,
    confluence:  dict,
    risk_status: dict,
    position:    dict = None,
    ta:         dict = None,
) -> str:
    now = datetime.now().strftime("%d %b %Y %H:%M")

    position_block = "No open position currently." if not position else f"""
    Current Position:
    Symbol     : {position.get('symbol')}
    Direction  : {position.get('direction')}
    Entry Price: ₹{position.get('entry_price')}
    Lots       : {position.get('lots')}
    Stop Loss  : ₹{position.get('stop_loss')}
    """

    ta_block = ""
    if ta and ta.get("signals"):
        ta_lines = "\n".join([f"  - {s['name']}: {s['note']}"
                              for s in ta["signals"]])
        ta_block = f"""
=== TECHNICAL ANALYSIS ===
Overall    : {ta.get('overall')}
RSI        : {ta.get('rsi')}
MACD Bias  : {ta.get('macd', {}).get('bias')}
EMA20 Bias : {ta.get('ema20', {}).get('bias')}
Supertrend : {ta.get('supertrend', {}).get('bias')}
VWAP Bias  : {ta.get('vwap', {}).get('bias')}
Signals:
{ta_lines}
"""

    prompt = f"""
You are an expert Indian options trader AI. Analyse the following real-time market data and make a precise trading decision.

=== MARKET SNAPSHOT [{now}] ===
Nifty Spot   : ₹{summary['nifty_spot']}
ATM Strike   : {summary['atm_strike']}
India VIX    : {greeks.get('vix', 'N/A')}
Days to Exp  : {greeks['days_to_exp']}

=== OPTIONS DATA ===
PCR          : {summary['pcr']} ({summary['sentiment']})
Max Pain     : {summary['max_pain']}
Support      : {summary['support']}
Resistance   : {summary['resistance']}
ATM CE LTP   : ₹{summary['atm_ce_ltp']}
ATM PE LTP   : ₹{summary['atm_pe_ltp']}

=== GREEKS ===
Avg IV       : {greeks['avg_iv']}%
CE IV        : {greeks['ce_iv']}%
PE IV        : {greeks['pe_iv']}%
Delta CE     : {greeks['ce_delta']}
Delta PE     : {greeks['pe_delta']}
Theta        : ₹{greeks['theta']}/day
Vega         : {greeks['vega']}

=== TA SIGNALS ===
{ta_block if ta_block else "No technical analysis data available."}

=== REGIME & SIGNALS ===
Market Regime   : {regime['regime_label']}
Regime Strategy : {regime['strategy']}
Signal Score    : {confluence['score']}/{confluence['max_score']}
Overall Bias    : {confluence['overall_bias']}
Trade Decision  : {confluence['decision']}

Signal Breakdown:
{chr(10).join([f"  - {s['label']}: {s['value']}" for s in confluence['signals']])}

=== RISK STATUS ===
Daily P&L       : ₹{risk_status['daily_pnl']}
Daily Limit     : ₹{risk_status['daily_loss_limit']}
Headroom Left   : ₹{risk_status['headroom']}
Open Positions  : {risk_status['open_positions']}
Trading Halted  : {risk_status['trading_halted']}

=== CURRENT POSITION ===
{position_block}

=== YOUR TASK ===
Based on all the above data, respond with ONLY a valid JSON object in this exact format:

{{
  "action": "ENTER" | "HOLD" | "ADJUST" | "EXIT" | "SKIP",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "strategy": "e.g. Short Straddle 24050",
  "entry": {{
    "ce_strike": 24050,
    "pe_strike": 24050,
    "ce_action": "SELL" | "BUY" | null,
    "pe_action": "SELL" | "BUY" | null
  }},
  "stop_loss": "e.g. Exit if combined premium rises 40%",
  "target":    "e.g. Exit at 50% premium decay",
  "reasoning": "2-3 sentences explaining your decision",
  "risk_warning": "any specific risk to watch for today"
}}

Rules:
- If signal score < 4, action must be SKIP
- If trading_halted is True, action must be SKIP
- If days_to_exp <= 1, prefer short premium strategies
- Never suggest naked options without hedge
- Respond ONLY with the JSON object, no explanation outside it
"""
    
    logger.info(f"📋 Built LLM prompt with current market data. {prompt}")
    return prompt.strip()


# ─────────────────────────────────────────
#  PARSE RESPONSE
# ─────────────────────────────────────────

def parse_response(raw: str) -> dict:
    """Parse LLM response into a clean dict."""
    try:
        # Strip markdown fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON parse error: {e} | Raw: {raw[:200]}")
        return {
            "action":       "SKIP",
            "confidence":   "LOW",
            "strategy":     "Parse error",
            "reasoning":    "Could not parse LLM response — skipping for safety",
            "risk_warning": "Check LLM response format",
        }


# ─────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────

def get_trade_decision(
    summary:     dict,
    greeks:      dict,
    regime:      dict,
    confluence:  dict,
    risk_status: dict,
    vix:         float,
    position:    dict = None,
    ta:         dict = None,
) -> dict:
    greeks_with_vix = {**greeks, "vix": vix}
    prompt   = build_market_prompt(
        summary, greeks_with_vix, regime,
        confluence, risk_status, position
    , ta
    )
    try:
        raw      = call_llm(prompt)
        decision = parse_response(raw)
        logger.info(f"✅ LLM decision: {decision.get('action')} ({decision.get('confidence')})")
        return decision
    except Exception as e:
        logger.error(f"❌ LLM call failed: {e}")
        return {
            "action":       "SKIP",
            "confidence":   "LOW",
            "strategy":     "LLM unavailable",
            "reasoning":    f"LLM error: {e}",
            "risk_warning": "Trading paused — LLM unreachable",
        }