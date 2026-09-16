"""Read-only native bridge cookie metadata; never print cookie/session values."""
import asyncio
import json
from http.cookies import SimpleCookie

import aiohttp

from hermes.shell_server.ads_bridge import _bridge_cookie_value


async def main():
    async with aiohttp.ClientSession(
        cookies={"ads_bridge": _bridge_cookie_value()},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        for path in ("/ads/cockpit", "/ads/api/v1/auth/me"):
            async with session.get("http://127.0.0.1:7517" + path, allow_redirects=False) as response:
                if response.status != 200:
                    payload = await response.json(content_type=None)
                    print(json.dumps({"path": path, "status": response.status,
                                      "error_code": payload.get("error", {}).get("code")}))
                    continue
                cookies = SimpleCookie()
                for header in response.headers.getall("Set-Cookie", []):
                    cookies.load(header)
                output = {"path": path, "status": response.status,
                          "session_cookie_forwarded": "ads_session" in cookies,
                          "csrf_cookie_present": "ads_csrf" in cookies}
                if "ads_csrf" in cookies:
                    cookie = cookies["ads_csrf"]
                    output["csrf_attributes"] = {
                        "secure": bool(cookie["secure"]), "httponly": bool(cookie["httponly"]),
                        "path": cookie["path"], "samesite": cookie["samesite"]}
                if path.endswith("auth/me"):
                    payload = await response.json()
                    output["business_count"] = len(payload["businesses"])
                print(json.dumps(output, sort_keys=True))


asyncio.run(main())
