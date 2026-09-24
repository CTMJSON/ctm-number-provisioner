"""Token resolution for the CTM number provisioner.

Resolution order:

1. ``CTM_BASIC_AUTH`` env var (raw base64 ``access:secret`` token).
2. ``CTM_ENV_FILE`` (default ``~/.ctm/env``) line named by ``CTM_TOKEN_NAME``,
   parsed as ``name:token``.
3. Otherwise raise :class:`AuthError` with actionable guidance.

The token is never printed, logged, or returned by any public function here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = "~/.ctm/env"


class AuthError(RuntimeError):
    """Raised when a CTM API token cannot be resolved."""


@dataclass(frozen=True)
class Token:
    """A resolved CTM basic-auth token and where it came from."""

    value: str
    source: str  # "env" or "env.txt:<name>"


def default_env_file() -> Path:
    return Path(os.environ.get("CTM_ENV_FILE") or DEFAULT_ENV_FILE).expanduser()


def parse_env_file(path: Path, name: str) -> str:
    """Return the token for ``name`` from a ``name:token`` style file.

    Blank lines and lines starting with ``#`` are ignored. Only the first ``:``
    splits the line, so tokens containing ``:`` are preserved intact.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthError(f"Could not read CTM env file {path}: {exc}") from exc

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key.strip() == name:
            token = value.strip()
            if not token:
                raise AuthError(f"Token '{name}' in {path} is empty.")
            return token
    raise AuthError(
        f"Token '{name}' not found in {path}. Set CTM_TOKEN_NAME or CTM_BASIC_AUTH."
    )


def load_token(token_name: str | None = None, env_file: str | Path | None = None) -> Token:
    """Resolve a CTM token, preferring the environment over the env file.

    Set ``CTM_BASIC_AUTH`` for a single account, or ``CTM_ENV_FILE`` +
    ``CTM_TOKEN_NAME`` to select a named line from a shared credentials file.
    """
    env_token = os.environ.get("CTM_BASIC_AUTH")
    if env_token and env_token.strip():
        return Token(value=env_token.strip(), source="env")

    name = token_name or os.environ.get("CTM_TOKEN_NAME")
    if not name:
        raise AuthError(
            "No CTM credentials configured. Set CTM_BASIC_AUTH to your base64 "
            "access:secret token, or set CTM_TOKEN_NAME (and optionally "
            "CTM_ENV_FILE) to select a named line from a credentials file."
        )
    path = Path(env_file).expanduser() if env_file else default_env_file()
    return Token(value=parse_env_file(path, name), source=f"env.txt:{name}")