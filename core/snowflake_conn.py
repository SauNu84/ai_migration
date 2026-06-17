"""Live Snowflake connection factory (SAU-308).

Phase 0 reconciled the medallion against a DuckDB execution proxy (no live account).
Phase 1 runs the *same* generated Snowflake SQL against a real account, so the loop
needs one well-defined, secret-safe way to open a connection.

Design mirrors the SAU-303 / SAU-305 libpq convention: **credentials come from the
environment, never the repo.** No secret is ever read from, written to, or logged by
this module. The standard variables are:

    SNOWFLAKE_ACCOUNT     account identifier, e.g. ab12345.eu-central-1  (required)
    SNOWFLAKE_USER        login name                                     (required)
    SNOWFLAKE_PASSWORD    password                       (required unless key auth)
    SNOWFLAKE_PRIVATE_KEY_PATH  path to an unencrypted/encrypted PEM   (key-pair auth)
    SNOWFLAKE_PRIVATE_KEY_PASSPHRASE  passphrase for the PEM, if encrypted
    SNOWFLAKE_ROLE        role to assume                                 (optional)
    SNOWFLAKE_WAREHOUSE   compute warehouse                              (optional)
    SNOWFLAKE_DATABASE    default database                               (optional)
    SNOWFLAKE_SCHEMA      default schema                                 (optional)

The `snowflake-connector-python` driver is an *optional* dependency: this module and
the rest of the loop import cleanly without it, and only `connect()` requires it. That
keeps Phase 0 stdlib-light while making the live path a one-line install away.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Variables that carry a secret value — never echo these.
_SECRET_VARS = ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")

# Canonical credential file: the repo-root `.env` (gitignored), overridable via
# SNOWFLAKE_ENV_FILE. One unambiguous place to land credentials so the run can read
# them without depending on adapter-specific env injection.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_FILE = _REPO_ROOT / ".env"


def load_dotenv(path: str | os.PathLike | None = None) -> list[str]:
    """Populate os.environ from a KEY=VALUE file for any keys not already set.

    Real-environment vars always win (a file never overrides an injected secret).
    Stdlib-only, tolerant parser: blank lines and `#` comments skipped, optional
    `export ` prefix and surrounding quotes stripped. Returns the SNOWFLAKE_* keys it
    set (names only — never values). Missing file is a no-op.
    """
    p = Path(path) if path else Path(os.environ.get("SNOWFLAKE_ENV_FILE", _DEFAULT_ENV_FILE))
    if not p.is_file():
        return []
    set_keys: list[str] = []
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            if key.startswith("SNOWFLAKE_"):
                set_keys.append(key)
    return set_keys


class SnowflakeConfigError(RuntimeError):
    """Raised when required connection settings are missing from the environment."""


@dataclass
class SnowflakeConfig:
    """Connection settings resolved from the environment. Holds no secret in repr."""

    account: str
    user: str
    password: str | None = field(default=None, repr=False)
    private_key_path: str | None = None
    private_key_passphrase: str | None = field(default=None, repr=False)
    role: str | None = None
    warehouse: str | None = None
    database: str | None = None
    schema: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SnowflakeConfig":
        """Build a config from os.environ (or a supplied mapping).

        Raises SnowflakeConfigError listing every missing required variable, so a
        misconfigured run fails loudly and actionably instead of half-connecting.

        When reading the real environment (env is None), a gitignored `.env`
        (or SNOWFLAKE_ENV_FILE) is loaded first for any keys not already set.
        """
        if env is None:
            load_dotenv()
        e = os.environ if env is None else env

        def val(name: str) -> str | None:
            v = e.get(name)
            v = v.strip() if isinstance(v, str) else v
            return v or None

        account = val("SNOWFLAKE_ACCOUNT")
        user = val("SNOWFLAKE_USER")
        password = val("SNOWFLAKE_PASSWORD")
        key_path = val("SNOWFLAKE_PRIVATE_KEY_PATH")

        missing = [n for n, v in (("SNOWFLAKE_ACCOUNT", account),
                                  ("SNOWFLAKE_USER", user)) if not v]
        if not password and not key_path:
            missing.append("SNOWFLAKE_PASSWORD (or SNOWFLAKE_PRIVATE_KEY_PATH)")
        if missing:
            raise SnowflakeConfigError(
                "Missing required Snowflake settings: " + ", ".join(missing)
                + ". Export them in the run environment / secret store (never the repo)."
            )

        return cls(
            account=account, user=user, password=password,
            private_key_path=key_path,
            private_key_passphrase=val("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
            role=val("SNOWFLAKE_ROLE"),
            warehouse=val("SNOWFLAKE_WAREHOUSE"),
            database=val("SNOWFLAKE_DATABASE"),
            schema=val("SNOWFLAKE_SCHEMA"),
        )

    def connect_kwargs(self) -> dict[str, Any]:
        """Keyword args for snowflake.connector.connect(), secrets included.

        Key-pair auth (if a PEM path is set) is preferred over password and loaded
        lazily so cryptography is only required on the key path.
        """
        kw: dict[str, Any] = {"account": self.account, "user": self.user}
        for opt in ("role", "warehouse", "database", "schema"):
            v = getattr(self, opt)
            if v:
                kw[opt] = v
        if self.private_key_path:
            kw["private_key"] = _load_private_key(
                self.private_key_path, self.private_key_passphrase
            )
        elif self.password:
            kw["password"] = self.password
        return kw

    def redacted(self) -> dict[str, str]:
        """Loggable view: identity + targets only, never a secret."""
        out = {"account": self.account, "user": self.user,
               "auth": "key_pair" if self.private_key_path else "password"}
        for opt in ("role", "warehouse", "database", "schema"):
            v = getattr(self, opt)
            if v:
                out[opt] = v
        return out


def _load_private_key(path: str, passphrase: str | None) -> bytes:
    """Load a PEM private key into the DER bytes the connector expects."""
    from cryptography.hazmat.primitives import serialization  # lazy: key path only

    with open(path, "rb") as fh:
        pkey = serialization.load_pem_private_key(
            fh.read(),
            password=passphrase.encode() if passphrase else None,
        )
    return pkey.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect(config: SnowflakeConfig | None = None):
    """Open a live Snowflake connection from env-resolved settings.

    Returns a snowflake.connector.SnowflakeConnection. Raises SnowflakeConfigError if
    settings are missing, or RuntimeError with install guidance if the optional driver
    is not present. The caller owns closing the connection.
    """
    cfg = config or SnowflakeConfig.from_env()
    try:
        import snowflake.connector as sf  # optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without the driver
        raise RuntimeError(
            "snowflake-connector-python is not installed. Install the live extra:\n"
            "  pip install 'snowflake-connector-python>=3.12'\n"
            "(see requirements.txt — it is optional so Phase 0 stays driver-free)."
        ) from exc
    return sf.connect(**cfg.connect_kwargs())
