"""Read-only local readiness checks; emit no credentials or cookie values."""
import asyncio
import json
from urllib.parse import urlencode

import aiohttp

from hermes.shell_server.ads_bridge import _bridge_cookie_value


async def main():
    async with aiohttp.ClientSession(
        cookies={"ads_bridge": _bridge_cookie_value()},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        async def get(path):
            async with session.get("http://127.0.0.1:7517" + path, allow_redirects=False) as response:
                assert response.status == 200, (path, response.status)
                return await response.json()

        me = await get("/ads/api/v1/auth/me")
        assert len(me["businesses"]) == 1, "Expected exactly one business"
        business = me["businesses"][0]
        assert business["name"] == "Friendog Center"
        settings = await get("/ads/api/v1/settings?" + urlencode({"business_id": business["business_id"]}))
        assert settings["business_id"] == business["business_id"]
        assert settings["timezone"] == "Europe/Madrid"
        assert settings["currency"] == "EUR"
        apps = await get("/ads/api/v1/platform-apps")
        print(json.dumps({
            "business_count": 1,
            "business": {key: business[key] for key in ("business_id", "name")},
            "settings": {key: settings[key] for key in ("timezone", "currency")},
            "platform_apps": [
                {key: item.get(key) for key in ("platform", "configured", "client_type", "redirect_uri")}
                for item in apps["items"]
            ],
        }, sort_keys=True))


asyncio.run(main())
