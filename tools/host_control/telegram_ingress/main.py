"""Entry point of the Telegram human-approval ingress (T-2919).

``python3 -m tools.host_control.telegram_ingress --config <path>``

argv carries ONLY the config path (gate 21: no token, nonce, digest, or
payload ever appears in argv). Startup, in order, each step fail-closed:

1. load the root-owned config (trust-checked);
2. refuse unless running as the configured dedicated ingress uid;
3. obtain the bot token through an ORG-051 host-file lease (fd delivery,
   no symlink follow, owner/mode validated), read it, close + revoke;
4. learn the bot's OWN user id via ``getMe`` (the Bot API does not
   document the token's numeric prefix as the bot id) and require
   ``is_bot`` true;
5. open the private state, connect the system bus, export ``Present``,
   recover pending intents, poll.

The token is never read from the environment.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable

from tools.credential_broker import (
    CredentialError,
    CredentialRef,
    HostFileConstraints,
    HostFileCredentialBroker,
    LeaseRequest,
)
from tools.host_control.telegram_ingress.bot_api import (
    TELEGRAM_API_BASE,
    BotApiClient,
    BotApiError,
)
from tools.host_control.telegram_ingress.config import (
    IngressConfig,
    IngressConfigError,
    load_ingress_config,
)
from tools.host_control.telegram_ingress.core import IngressCore, IngressLog

LEASE_ROLE = "telegram-ingress"
LEASE_SERVICE = "pantheon-host-approval-telegram-ingress"
LEASE_ACTION = "bot-api"
LEASE_INSTANCE = "host-approval"
LEASE_TTL_SECONDS = 60
MAX_TOKEN_BYTES = 256


class StartupRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def credential_ref(config: IngressConfig) -> CredentialRef:
    return CredentialRef(
        credential_id=config.credential_id,
        kind="bearer-token",
        provider="host-file",
        location=config.credential_location,
        owner_uid=config.ingress_uid,
        owner_gid=config.credential_owner_gid,
        mode=0o600,
        roles=(LEASE_ROLE,),
        instances=(LEASE_INSTANCE,),
        services=(LEASE_SERVICE,),
        actions=(LEASE_ACTION,),
        host_file=HostFileConstraints(
            parent_dir=config.credential_location.parent,
            parent_owner_uid=config.ingress_uid,
            parent_owner_gid=config.credential_owner_gid,
            parent_mode=0o700,
        ),
    )


def lease_bot_token(
    config: IngressConfig,
    *,
    caller_uid: Callable[[], int] = os.geteuid,
) -> bytes:
    """Read the bot token through one short ORG-051 lease."""

    broker = HostFileCredentialBroker(
        (credential_ref(config),),
        audit_path=config.state_dir / "credential-audit.jsonl",
        caller_uid=caller_uid,
        max_ttl_seconds=LEASE_TTL_SECONDS,
    )
    try:
        lease = broker.issue(
            LeaseRequest(
                credential_id=config.credential_id,
                caller_uid=caller_uid(),
                role=LEASE_ROLE,
                instance=LEASE_INSTANCE,
                service=LEASE_SERVICE,
                action=LEASE_ACTION,
                ttl_seconds=LEASE_TTL_SECONDS,
            )
        )
    except (CredentialError, OSError, ValueError):
        raise StartupRefused("credential_unavailable") from None
    try:
        chunks = []
        total = 0
        while True:
            chunk = os.read(lease.fd, MAX_TOKEN_BYTES + 1)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_TOKEN_BYTES:
                raise StartupRefused("credential_invalid")
        token = b"".join(chunks).strip()
    finally:
        broker.revoke(lease.lease_id)
    if not token:
        raise StartupRefused("credential_invalid")
    return token


def learn_bot_user_id(api: BotApiClient) -> int:
    try:
        me = api.get_me()
    except BotApiError:
        raise StartupRefused("get_me_failed") from None
    bot_id = me.get("id")
    if (
        not isinstance(bot_id, int)
        or isinstance(bot_id, bool)
        or me.get("is_bot") is not True
    ):
        raise StartupRefused("get_me_not_a_bot")
    return bot_id


def build_core(
    config: IngressConfig,
    *,
    decider: object,
    log: IngressLog,
    caller_uid: Callable[[], int] = os.geteuid,
    api_base: str = TELEGRAM_API_BASE,
) -> IngressCore:
    """Steps 2-4 + state. ``api_base`` exists for the loopback test
    driver only; ``main`` never passes it (the production base is the
    compiled constant)."""

    from tools.host_control.telegram_ingress.state import IngressState

    if caller_uid() != config.ingress_uid:
        raise StartupRefused("wrong_uid")
    token = lease_bot_token(config, caller_uid=caller_uid)
    api = BotApiClient(token, base_url=api_base)
    del token
    bot_user_id = learn_bot_user_id(api)
    state = IngressState.open(config.state_dir)
    return IngressCore(
        state=state,
        api=api,
        decider=decider,  # type: ignore[arg-type]
        bot_user_id=bot_user_id,
        allow_user_id=config.allow_user_id,
        allow_chat_id=config.allow_chat_id,
        card_ttl_max_seconds=config.card_ttl_max_seconds,
        log=log,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantheon-host-approval-telegram-ingress")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    log = IngressLog(sys.stderr)
    try:
        config = load_ingress_config(Path(args.config), expected_owner_uid=0)
    except IngressConfigError as exc:
        log.event("api_error", detail="config_refused")
        del exc
        return 78
    try:
        import dbus
        import dbus.mainloop.glib

        from tools.host_control.telegram_ingress.dbus_service import (
            DbusHumanDecider,
            run_ingress,
        )
    except ImportError:
        log.event("api_error", detail="dbus_unavailable")
        return 69
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    connection = dbus.SystemBus()
    decider = DbusHumanDecider(lambda: connection)
    try:
        core = build_core(config, decider=decider, log=log)
    except StartupRefused as exc:
        log.event("api_error", detail=exc.code)
        return 77
    return run_ingress(
        core,
        connection,
        approval_uid=config.approval_uid,
        poll_timeout_seconds=config.poll_timeout_seconds,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
