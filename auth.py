"""
ERPNext cookie-based authentication.
"""
import httpx
import config


def login(username: str, password: str) -> tuple[dict, str]:
    """
    Login to ERPNext. Returns (cookies_dict, csrf_token) or raises on failure.
    """
    r = httpx.post(
        f"{config.ERPNEXT_URL}/api/method/login",
        json={"usr": username, "pwd": password},
        timeout=15,
    )
    if r.status_code != 200:
        raise ValueError(f"Login failed: {r.json().get('message', r.text[:100])}")

    cookies = dict(r.cookies)
    csrf = cookies.get("csrftoken", "")
    return cookies, csrf


def get_logged_user(cookies: dict) -> str:
    """Return the logged-in ERPNext username."""
    r = httpx.get(
        f"{config.ERPNEXT_URL}/api/method/frappe.auth.get_logged_user",
        cookies=cookies,
        timeout=10,
    )
    return r.json().get("message", "")


def get_user_roles(username: str) -> list[str]:
    """Return role names for a user. Uses API key so any user can see their own roles."""
    r = httpx.get(
        f"{config.ERPNEXT_URL}/api/method/frappe.client.get",
        headers={"Authorization": f"token {config.ERPNEXT_API_KEY}:{config.ERPNEXT_SECRET}"},
        params={"doctype": "User", "name": username},
        timeout=10,
    )
    if not r.is_success:
        return []
    roles_raw = r.json().get("message", {}).get("roles", [])
    return [row["role"] for row in roles_raw if row.get("role")]
