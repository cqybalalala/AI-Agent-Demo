"""
Forex exchange rate lookup via api.frankfurter.app (free, no API key).
Supports both latest and historical rates.
"""

import httpx

_BASE = "https://api.frankfurter.dev/v1"


def get_rate(from_currency: str, to_currency: str, date: str | None = None) -> dict:
    """
    Fetch exchange rate from_currency → to_currency.
    date: YYYY-MM-DD for historical rate, None for latest.
    Returns: { from, to, date, rate, converted_amount (if amount given) }
    """
    from_c = from_currency.upper()
    to_c = to_currency.upper()

    if from_c == to_c:
        return {"from": from_c, "to": to_c, "rate": 1.0, "date": date or "latest"}

    endpoint = f"{_BASE}/{date}" if date else f"{_BASE}/latest"
    params = {"from": from_c, "to": to_c}

    try:
        r = httpx.get(endpoint, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        return {"success": False, "error": str(e)}

    rate = data["rates"].get(to_c)
    if rate is None:
        return {"success": False, "error": f"Currency '{to_c}' not found. Supported: {list(data['rates'].keys())}"}

    return {
        "success": True,
        "from": from_c,
        "to": to_c,
        "rate": rate,
        "date": data.get("date", date or "latest"),
    }


def convert(amount: float, from_currency: str, to_currency: str, date: str | None = None) -> dict:
    """Convert an amount using get_rate, returning the converted value."""
    result = get_rate(from_currency, to_currency, date)
    if not result.get("success"):
        return result
    converted = round(amount * result["rate"], 2)
    return {**result, "original_amount": amount, "converted_amount": converted}
