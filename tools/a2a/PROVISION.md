# Provisioning the specialist A2A seat server (T-1886, rung 1)

Stub-skill loopback server per `knowledge/episodic/2026-07-20-a2a-rung1-design/notes.md`
§2.1/§2.2. Runs on the specialist account as a user-mode systemd unit.

## 1. Install

Python deps are venv-scoped (the account's system python3 is
externally-managed / PEP 668 — no `--break-system-packages`).

```sh
python3 -m venv ~/.venv-a2a
~/.venv-a2a/bin/pip install --upgrade pip
~/.venv-a2a/bin/pip install 'a2a-sdk[http-server]==1.1.1' 'uvicorn==0.51.0'
```

Pinned/tested against: `a2a-sdk==1.1.1` (`[http-server]` extra — pulls
`starlette` + `sse-starlette`), `uvicorn==0.51.0` (not pulled by the
`[http-server]` extra, needed separately to actually serve — this is the
version pip resolved at implementation time; bump deliberately, not
silently, since the specialist account is a separate install from this
worktree's throwaway `.venv-a2a`).

## 2. Install the unit

```sh
mkdir -p ~/.config/systemd/user
cp tools/a2a/specialist-a2a.service ~/.config/systemd/user/specialist-a2a.service
# Edit the ExecStart path if the repo checkout or venv isn't at the
# defaults the template assumes — see comments in the unit file.
systemctl --user daemon-reload
systemctl --user enable --now specialist-a2a.service
```

## 3. Verify

```sh
systemctl --user status specialist-a2a.service
curl -s http://127.0.0.1:41100/.well-known/agent-card.json | python3 -m json.tool
# Loopback-only bind check — must show 127.0.0.1:41100, never 0.0.0.0 or ::
ss -tlnp | grep 41100
```

A minimal stub round-trip (any valid `SendMessage` drives
SUBMITTED→WORKING→COMPLETED with a canned artifact) is exercised by the
test suite; see `tools/test_a2a_specialist_seat_server.py` for a working client
example if you want to drive it by hand.

## SDK surface notes (a2a-sdk 1.1.1)

Derived from the installed package (D11), not from memory of older A2A
SDKs — the real API here diverges from what's commonly documented online.
Anyone consuming this server (BT-2 through BT-5 implementers, or Specialist as
provisioner) should read this before assuming an API shape.

**Two coexisting AgentCard schemas.** The SDK's native v1.0 core type
(`a2a.types.AgentCard`, protobuf-generated) has no `url` / `preferredTransport`
/ `protocolVersion` / `capabilities.stateTransitionHistory` fields — it
uses a `supported_interfaces` list instead. The SDK separately ships a
backward-compat pydantic model, `a2a.compat.v0_3.types.AgentCard`, whose
fields match this seat's Agent Card (`agent_card.json`) exactly. This
server serves `agent_card.json` as a literal static file at the well-known
path rather than through either of the SDK's card objects — see "the
card-serializer field-drop hazard" below for why.

**Native method names are PascalCase, not slash-style.** The SDK's own
JSON-RPC methods are `SendMessage`, `GetTask`, `CancelTask`,
`SendStreamingMessage` (matching its gRPC service method names) — not the
`message/send` / `tasks/get` / `tasks/cancel` names the design notes name.
The SDK's own client library (`a2a.client`) speaks the PascalCase names
natively and interoperates with this server with zero extra configuration.

**What `enable_v0_3_compat=True` actually does.** Passed to
`create_jsonrpc_routes(...)`, it additionally exposes the slash-style
method names on the *same* `/a2a` endpoint, alongside the native
PascalCase ones (both work simultaneously — it's additive, not a mode
switch). Under the hood it delegates to the identical `RequestHandler`
instance via an internal translation shim (`RequestHandler03`), so there
is exactly one code path for actual request handling regardless of which
method-name family a caller uses. This server turns it on so the design's
literal §2.1 method names are honestly supported for any non-SDK caller.

**The card-serializer field-drop hazard.** The SDK's
`create_agent_card_routes()` helper serializes the *native* proto
`AgentCard` object to JSON for the well-known endpoint. Since that native
schema lacks `url`/`preferredTransport`/`protocolVersion`/
`stateTransitionHistory`, routing this seat's card through it would
silently drop those fields (and risk `pushNotifications: false` being
dropped too, depending on proto3 field-presence/`exclude_none` semantics).
This server avoids the helper entirely and serves the literal JSON file
instead, guaranteeing byte-for-byte fidelity to the finalized §2.2 card.
Note this is *not* a problem for card **consumption**: the SDK's client-side
resolver (`a2a.client.card_resolver.parse_agent_card`) has built-in legacy
mapping that already handles this seat's exact card shape (`url` /
`preferredTransport` / `protocolVersion` get folded into
`supportedInterfaces` automatically) — so any SDK client can discover and
use this card with no shimming on the client's part either.

## Running the orchestrator-seat client (T-1887, BT-2)

`tools/a2a/orchestrator_client.py` is the orchestrator-side A2A client: fetches the
specialist Agent Card, sends a `research-request` Message, follows the task to
a terminal state, and prints the returned artifact. Same venv-scoped
install pattern as the server (an orchestrator-account venv, not the repo's
system python3):

```sh
python3 -m venv ~/.venv-a2a
~/.venv-a2a/bin/pip install --upgrade pip
~/.venv-a2a/bin/pip install 'a2a-sdk[http-server]==1.1.1' 'uvicorn==0.51.0'
```

(The `[http-server]` extra + uvicorn are only needed to run the in-process
test module below, which spins the server up locally; the client script
itself only needs bare `a2a-sdk`.)

Run from the repo root, against the live loopback server:

```sh
PYTHONPATH=. ~/.venv-a2a/bin/python -m tools.a2a.orchestrator_client
```

Target host/port is env-overridable (`SPECIALIST_A2A_URL`, default
`http://127.0.0.1:41100`) or via `--base-url`; `--question`/`--depth`/
`--domains-hint`/`--recency-days`/`--requester-context` shape the §2.3
research-request payload. Exit code is 0 on `COMPLETED`, 1 on any other
terminal/paused/unreachable outcome (INPUT_REQUIRED/AUTH_REQUIRED
pause-resume is BT-4 scope — this client reports a paused task cleanly
rather than hanging or attempting to resume it).

Test suite (in-process, ephemeral-port server, no systemd unit needed):

```sh
~/.venv-a2a/bin/python -m unittest tools.test_a2a_orchestrator_client -v
```

**Client-side note on the metadata DataPart's number fields** (e.g.
`"rung": 1.0`, `"lanes_run": 0.0` printed as floats): this is inherent to
`google.protobuf.Struct`/`Value`, whose `NumberValue` is always a double —
there is no way to preserve a JSON-integer's int-ness once it round-trips
through `Struct`, on either the server's encode side or the client's
`MessageToDict` decode side. Not a client bug; cosmetic only, and not
this ticket's surface to fix (the server built the metadata this way in
T-1886).

### Pause/resume/cancel (T-1889, BT-4)

An above-bar question (rung-1 stub detection — see `specialist_seat_server.py`'s
`ABOVE_BAR_KEYWORDS`, e.g. a question containing "credentials") pauses the
task at `INPUT_REQUIRED` instead of completing. By default the client just
reports the pause and exits (`--on-pause report`, same as BT-2/BT-3 — no
hang, no auto-resume). Two new flags drive the pause explicitly, since
rung 1's human is orchestrator driving this client (no Telegram wiring — design
notes §2.4):

```sh
# Resume: sends a follow-up Message on the same task, drives it to COMPLETED.
PYTHONPATH=. ~/.venv-a2a/bin/python -m tools.a2a.orchestrator_client \
    --question "What are the credentials for the specialist seat?" \
    --on-pause resume --resume-text "approved"

# Cancel: calls tasks/cancel on the paused task, drives it to CANCELED.
PYTHONPATH=. ~/.venv-a2a/bin/python -m tools.a2a.orchestrator_client \
    --question "What are the credentials for the specialist seat?" \
    --on-pause cancel
```

Exit code: 0 on `COMPLETED`; 0 on `CANCELED` when `--on-pause cancel` was
requested (cancel-as-requested succeeded); 1 otherwise. Programmatic
callers can reuse `resume_task(client, task, text=...)` and
`cancel_task(client, task)` directly instead of going through the CLI.

## Real research-request skill + result-poll loop (T-1890, BT-5)

`specialist_seat_server.py`'s `research-request` handler is no longer a canned
stub (BT-1..4 shape) — it stages a passing request to the specialist mailbox
inbox and leaves the task `WORKING`; a background asyncio poll loop
(started/stopped via the app's ASGI lifespan, no extra process or unit)
drains the dedicated `specialist-a2a` result mailbox inbox every
`RESULT_POLL_INTERVAL_SECONDS` (5s default) and applies `COMPLETED`/
`FAILED` onto the task once the specialist session (applying
`sop/standard/a2a-research-request-handling.md`) sends a result back.
**Reprovisioning this unit after a BT-5 deploy needs nothing new beyond
the existing `systemctl --user restart specialist-a2a.service`** — the request
inbox (`/home/post-office/mailbox/specialist`) and result inbox
(`/home/post-office/mailbox/specialist-a2a`) are both already provisioned on
the shared substrate and are hardcoded module defaults
(`DEFAULT_REQUEST_INBOX`/`DEFAULT_RESULT_INBOX` in
`specialist_seat_server.py`), not new env vars — no unit-file edit is required
unless you want to point the server at different mailboxes (both
`build_app()` params and the module constants are the override points for
that, no env var exists for them by design — see the module's docstring
for why).

A never-do-list violation (design notes §3, `NEVER_DO_TRIGGERS` in
`specialist_seat_server.py`) now rejects a request outright (`FAILED`, no
mailbox send) — this is checked *before* the existing above-bar
(`INPUT_REQUIRED`) gate, so an out-of-scope action request never even
reaches the pause-for-approval state.

## Follow-ups noted, not built here

- A one-line pointer for this dir belongs in `tools/README.md` (that file
  is a spine doc — HO's edit, not this ticket's, per D2).
