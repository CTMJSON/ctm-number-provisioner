"""Console entry point: run the CTM number provisioner MCP server over stdio."""

from __future__ import annotations

import asyncio
import sys

from .auth import AuthError, load_token
from .client import CTMClient, CTMError
from .server import mcp


async def _startup_check() -> None:
    """Verify the token and print the resolved account to stderr (never stdout)."""
    try:
        token = load_token()
    except AuthError as exc:
        print(f"[ctm-number-provisioner] {exc}", file=sys.stderr)
        return
    client = CTMClient(token=token)
    try:
        aid = client.resolve_account_id()
        data = await client.get(f"/accounts/{aid}")
        account = data.get("account") if isinstance(data, dict) and "account" in data else data
        name = (account or {}).get("name") if isinstance(account, dict) else None
        print(
            f"[ctm-number-provisioner] account {aid} ({name}) via {token.source}",
            file=sys.stderr,
        )
    except CTMError as exc:
        print(f"[ctm-number-provisioner] startup check failed: {exc}", file=sys.stderr)
    finally:
        await client.aclose()


def main() -> None:
    """Run the MCP server on stdio, with a best-effort startup account check."""
    try:
        asyncio.run(_startup_check())
    except Exception:  # never block startup on the informational check
        pass
    mcp.run()


if __name__ == "__main__":
    main()