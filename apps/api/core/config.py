"""Runtime configuration.

Every value that differs between a laptop, a staging box, and production is read from the
environment here and nowhere else. Nothing sensitive has a usable default: a deployment
that forgets to inject a signing key or a database password fails at start-up instead of
quietly running on a well-known secret.

Names describe what they configure, not where they came from, so a container environment
variable reads the same as the setting it feeds.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything the API needs to boot."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        description="PostgreSQL connection string for the account database.",
    )
    redis_url: str = Field(
        description="Redis connection string holding verification codes and live sessions.",
    )

    # Signing material is asymmetric: the API signs with the private half and only ever
    # needs the public half to check a token it did not issue. Keys are rotated by
    # swapping the injected pair, never by editing code.
    signing_private_key: SecretStr = Field(
        description="PEM-encoded private key for signing tokens."
    )
    signing_public_key: SecretStr = Field(
        description="PEM-encoded public key for verifying tokens."
    )
    signing_key_id: str = Field(
        default="primary",
        description="Identifier stamped into issued tokens so a rotated key can be told apart.",
    )

    access_token_lifetime_seconds: int = Field(
        default=15 * 60,
        description="How long a freshly issued access token stays usable.",
    )
    refresh_token_lifetime_seconds: int = Field(
        default=30 * 24 * 60 * 60,
        description="How long a refresh token stays usable before it must be rotated.",
    )

    # Verification codes
    otp_code_digits: int = Field(default=6, description="Digits in a verification code.")
    otp_ttl_seconds: int = Field(
        default=300,
        description="How long a verification code remains usable after it is sent.",
    )
    otp_max_attempts: int = Field(
        default=5,
        description="Wrong guesses a code tolerates before it is destroyed.",
    )
    otp_resend_cooldown_seconds: int = Field(
        default=60,
        description="Minimum gap between two codes sent to the same number.",
    )
    otp_daily_send_limit_per_phone: int = Field(
        default=5,
        description="Codes one number may receive in a day.",
    )

    # Abuse controls, keyed by caller's address rather than by account because the caller
    # has no account yet at the moment these matter most.
    auth_endpoint_limit_per_minute_per_ip: int = Field(
        default=10,
        description="Sign-in and code requests one address may make per minute.",
    )
    otp_daily_send_limit_per_ip: int = Field(
        default=20,
        description="Codes one address may trigger in a day, across all numbers.",
    )

    # Which SMS backend to build. Only the recording backend exists so far; the real
    # gateway arrives with the provider credentials.
    sms_provider: str = Field(
        default="recording",
        description="Name of the SMS backend the API builds at start-up.",
    )

    terms_version: str = Field(
        description="Version stamped on every terms acceptance recorded from now on.",
    )

    # Off unless something asks for it. FastAPI serves an interactive browser at /docs, a
    # second at /redoc, and a machine-readable description of every route at /openapi.json -
    # all three mounted unconditionally, all three reachable by anyone who can reach the
    # port. That last one is the reason for the flag: it is a complete map of the API for a
    # caller who has no account, including which endpoints take which secret. Nothing in the
    # specification asks for it, so it is not on by default.
    #
    # The security scan in CI is the one caller that sets it, and it has to: ZAP's API scan
    # reads the OpenAPI description to learn the endpoints at all. Scanning an API whose
    # description is hidden would be scanning the absence of a schema.
    expose_docs: bool = Field(
        default=False,
        description="Serve /docs, /redoc and /openapi.json. Off unless asked for.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read configuration once per process.

    Every required setting is read from the environment here, which is what keeps secrets
    out of the source tree: there is no default to fall back on if one is missing.
    """
    return Settings.model_validate({})
