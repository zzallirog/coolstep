"""coolstep MCP server — exposes coolstep dashboard API as Claude tools."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from coolstep import __version__

BASE_URL = os.environ.get("COOLSTEP_URL", "http://localhost:18889")

server = Server("coolstep")


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BASE_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str) -> Any:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{BASE_URL}{path}")
        r.raise_for_status()
        return r.json()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="status",
            description=(
                "Daemon health + latest thermal snapshot: temps, fan RPM, power. "
                "Use first to orient before any thermal decision."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_mode",
            description="Current operating mode: cool / quiet / off.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="set_mode",
            description="Switch coolstep mode. mode: cool | quiet | off.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["cool", "quiet", "off"],
                        "description": "Target mode",
                    }
                },
                "required": ["mode"],
            },
        ),
        types.Tool(
            name="incidents",
            description=(
                "Recent thermal incidents (throttle events, anomalies). "
                "Returns list sorted newest-first."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="telemetry",
            description=(
                "Time-series telemetry. since: duration string e.g. '5m','1h','7d' (default 15m). "
                "limit: max rows (default 200)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {"type": "string", "default": "15m"},
                    "limit": {"type": "integer", "default": 200},
                },
            },
        ),
        types.Tool(
            name="ml_state",
            description=(
                "ML predictor state: throttle probability, KNN neighbours, "
                "trajectory signal, last update age."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="efficiency",
            description=(
                "Efficiency report: RPM/temp bins, sweet spot. "
                "since: '1d','7d','30d' (default 7d)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {"type": "string", "default": "7d"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result: Any
        match name:
            case "status":
                health = await _get("/api/health")
                latest = await _get("/api/telemetry/latest")
                result = {"health": health, "latest": latest}
            case "get_mode":
                result = await _get("/api/mode")
            case "set_mode":
                mode = arguments["mode"]
                result = await _post(f"/api/mode/{mode}")
            case "incidents":
                result = await _get("/api/incidents")
            case "telemetry":
                result = await _get(
                    "/api/telemetry/range",
                    params={
                        "since": arguments.get("since", "15m"),
                        "limit": arguments.get("limit", 200),
                    },
                )
            case "ml_state":
                result = await _get("/api/ml-state")
            case "efficiency":
                result = await _get(
                    "/api/efficiency",
                    params={"since": arguments.get("since", "7d")},
                )
            case _:
                result = {"error": f"unknown tool: {name}"}
    except httpx.HTTPError as exc:
        result = {"error": str(exc)}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


def main() -> None:
    if any(arg in ("--version", "-V") for arg in sys.argv[1:]):
        print(f"coolstep-mcp, version {__version__}")
        return

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
