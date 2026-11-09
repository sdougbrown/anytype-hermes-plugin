"""register(ctx) -- registers the Anytype platform adapter and its tools.

Confirmed against Hermes's real plugin API (docs/design.md Section 12): ctx exposes
register_platform() and register_tool() as documented in hermes_cli/plugins.py
and demonstrated by the shipped Mattermost/IRC plugins in NousResearch/
hermes-agent's plugins/platforms/.
"""

from __future__ import annotations

import os
from typing import Any

from .anytype_client import AnytypeClient, AnytypeConfig
from .tools import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, make_tool_handlers

__all__ = ["register"]

__version__ = "0.1.0"


def register(ctx: Any) -> None:
    # Deferred: adapter.py imports gateway.config / gateway.platforms.base,
    # which only exist inside a real Hermes install (see adapter.py's module
    # docstring). Importing it lazily here -- rather than at package import
    # time -- means `import hermes_anytype` (and its submodules) still works
    # standalone, e.g. for this repo's own test suite.
    from .adapter import register as _register_platform

    _register_platform(ctx)

    # Tools share one AnytypeClient with the platform adapter would be nicer,
    # but the adapter isn't constructed until register_platform's
    # adapter_factory runs later (possibly never, if the platform isn't
    # enabled) -- so tools get their own client, built straight from env vars
    # the same way the adapter does. Both are just aiohttp sessions opened
    # lazily on first request; there's no shared state to duplicate.
    client = AnytypeClient(
        AnytypeConfig(
            api_key=os.getenv("ANYTYPE_API_KEY", ""),
            base_url=os.getenv("ANYTYPE_API_BASE_URL", ""),
            space_id=os.getenv("ANYTYPE_SPACE_ID", ""),
        )
    )
    handlers = make_tool_handlers(client)
    for name, schema in TOOL_SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="anytype",
            schema=schema,
            handler=handlers[name],
            is_async=True,
            description=TOOL_DESCRIPTIONS.get(name, ""),
        )
