"""Read-only MCP handshake against the installed companion; no secret output."""
import asyncio
import json
import ssl

import aiohttp

from hermes.shell_server.companion_net import FixedIpResolver
from hermes.shell_server.companions import get_companion, read_companion_bearer


async def main():
    endpoint = get_companion("safent-ads")
    assert endpoint is not None
    bearer = read_companion_bearer(endpoint)
    assert bearer
    connector = aiohttp.TCPConnector(
        resolver=FixedIpResolver(hostname=endpoint.host, ip=endpoint.ip),
        ssl=ssl.create_default_context(cafile=endpoint.ca_path),
    )
    headers = {"Authorization": "Bearer " + bearer,
               "Accept": "application/json, text/event-stream"}
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                    timeout=aiohttp.ClientTimeout(total=30)) as client:
        async def request(method, params, request_id):
            async with client.post(endpoint.url, headers=headers,
                                   json={"jsonrpc": "2.0", "method": method,
                                         "params": params, "id": request_id},
                                   allow_redirects=False) as response:
                print(method, "http_status", response.status)
                assert response.status == 200
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    headers["Mcp-Session-Id"] = session_id
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    async for line in response.content:
                        if line.startswith(b"data:"):
                            return json.loads(line[5:].strip())
                return await response.json()
        init = await request("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "safent-release-readonly-check", "version": "0.9.14"},
        }, 1)
        assert "result" in init
        headers["MCP-Protocol-Version"] = init["result"]["protocolVersion"]
        async with client.post(endpoint.url, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"
        }) as response:
            assert response.status in (200, 202, 204)
        reply = await request("tools/list", {}, 2)
        tools = reply["result"]["tools"]
        assert tools
        assert init["result"]["serverInfo"]["version"] == "0.2.7"
        names = {tool["name"] for tool in tools}
        required = {"propose_campaign", "propose_budget_change", "get_insights",
                    "get_native_ads_tools", "get_native_ads_report"}
        assert required <= names
        print(json.dumps({"server": init["result"]["serverInfo"],
                          "tools_count": len(tools),
                          "required_tools_present": sorted(required)}, sort_keys=True))


asyncio.run(main())
