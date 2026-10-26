# Host MCP adapter (ORG-052 Unit 4a, T-2915)

The privilege-free stdio MCP server every harness seat (Claude Code, Codex)
launches to reach the Action Broker's narrow, audited host capabilities
without holding host power itself (ORG-052 §1, ORG-063 layer 3).

It exposes exactly seven tools. Each tool maps one-to-one onto one fixed method
of the landed `org.pantheon.HostBroker1.Manager` service at
`/org/pantheon/HostBroker1` on the system bus (`tools/host_control/dbus_service.py`).

The adapter has none of the following:

- host mutation;
- durable state;
- credentials;
- a listener (it is stdio only and owns no bus name);
- a caller-identity argument (the broker resolves the caller from the bus);
- generic command, file, D-Bus or transport passthrough.

`tools/host_control/` is deliberately not imported. That package's `__init__`
loads the broker journal, planner, policy and registry. The few wire constants
the adapter needs are mirrored in `contract.py` and pinned to the landed
modules by `LandedParityTests`.

| File | Role |
|---|---|
| `contract.py` | Pure: tool table, the closed `Method` enum, JSON Schema 2020-12 schemas, input validation, result shaping, the error mapping, the rate limiter and the self-check report. It imports neither the MCP SDK nor any D-Bus library. |
| `bus_client.py` | The ONLY D-Bus code (jeepney). It accepts only `contract.Method` members, has a fixed address, and connects lazily and self-heals. |
| `server.py` | Low-level `mcp.server.lowlevel.Server` over `stdio_server()` (dual-era loop), plus `main()` and `--self-check`. |
| `dist/` | `requirements.in`, the hash-locked `requirements.lock`, `lock.py`, `build_runtime.py` and `verify_artifact.py`. |

`tools/test_host_mcp_adapter_driver.py` is the only way to point the adapter at
a non-system bus. It is test-only and never packaged (ORG-052 §7).

## Tools

| Tool | D-Bus method (in → out) | Arguments (all required) | `structuredContent` | Hints |
|---|---|---|---|---|
| `host.capabilities` | `Capabilities` `""→"ss"` | none | `contract_version`, `capabilities` (object) | readOnly |
| `host.plan` | `Plan` `"sss"→"sss"` | `seat_id`, `manifest_revision`, `idempotency_key` | `plan_handle`, `plan_digest`, `summary` | not readOnly, not destructive, idempotent |
| `host.apply` | `Apply` `"ssss"→"s"` | `plan_handle`, `plan_digest`, `idempotency_key`, `approval_handle` | `operation_handle` | destructive, idempotent |
| `host.verify` | `Verify` `"sss"→"s"` | `seat_id`, `plan_handle`, `idempotency_key` | `operation_handle` | not readOnly, not destructive, idempotent |
| `host.rollback` | `Rollback` `"sss"→"s"` | `receipt_handle`, `idempotency_key`, `approval_handle` | `operation_handle` | destructive, idempotent |
| `host.operation.get` | `OperationGet` `"s"→"ss"` | `operation_handle` | `state`, `result` | readOnly |
| `host.approval.get` | `ApprovalGet` `"s"→"ss"` | `approval_handle` | `state`, `summary` | readOnly |

All tools set `openWorldHint: false`. The hints are presentation only: the
broker's server-side policy is the enforcement layer.

**Schemas.** Every input and output schema declares
`"$schema": "https://json-schema.org/draft/2020-12/schema"`, is a closed object
(`additionalProperties: false`) and lists every field as required.

**Value bounds.** These mirror the landed broker validators:

| Kind | Rule | Pattern |
|---|---|---|
| Scalars | printable ASCII, 1–128 characters | `^[!-~]{1,128}$` |
| Handles and digests, including `approval_handle` (T-2917 ruling f2) | 64 lowercase hex characters | `^[0-9a-f]{64}$` |
| Nested broker JSON | at most 65536 bytes; must be an object; no duplicate keys or NaN | — |

The broker owns the shape of the nested payload (`summary`, `result`,
`capabilities`).

**Successful results** carry `structuredContent` plus one TextContent block
with the same data as canonical JSON, for older clients.

**Current behaviour of the landed broker:**

- `host.verify` returns `unsupported` until T-2917 5b wires Verify. Until
  then, `plan_handle` and `seat_id` are both required and non-empty.
- `host.approval.get` uses the broker's HostApproval1.Consumer client (T-2917
  5a). That client answers only for an approval handle that one of the
  caller's own operations carries; any other handle is `unknown_handle`. A
  broker wired without an approval authority keeps the fail-closed
  `UnavailableApprovalStatus` (`approval_unavailable`).

## Errors (PROVISIONAL-PENDING-RATIFICATION)

**Format.** Every failure is an `isError: true` result whose only content is a
TextContent block holding canonical JSON:
`{"error":{"code","retryable","message"[,"detail"]}}`. It carries no
`structuredContent`, because the output schema describes success and a strict
client must not be able to reject the error itself.

**Why errors are built, not raised.** Since mcp 2.1.0, an exception raised from
a tool handler reaches clients only as a generic "Error executing tool". The
adapter therefore never raises for a tool failure; it builds the result.

**Protocol errors.** An unknown tool name or malformed JSON-RPC produces a
JSON-RPC error (`-32602`), not a tool result.

**Detail field.** `detail` is present only for the broker's closed wire names.
It holds the broker's own bounded body, re-sanitized to printable ASCII of at
most 200 characters. Messages from the bus daemon, from libraries, or under
unknown names are never forwarded.

Both vocabularies below are provisional: the T-2912 OQ4 wire names and this
adapter's MCP codes.

| MCP code | Source | Retryable |
|---|---|---|
| `invalid_arguments` | adapter input validation; nothing sent | no |
| `rate_limited` | adapter token bucket (burst 20, 5/s) | yes |
| `broker_unavailable` | connect failure, `ServiceUnknown`, `NameHasNoOwner`, `Spawn.*`; request not delivered | yes |
| `broker_no_reply` | timeout, router closed, write fault, `NoReply`/`Timeout`/`TimedOut`/`Disconnected`; outcome unknown, retry with the SAME `idempotency_key` or poll `host.operation.get` | yes |
| `broker_contract_mismatch` | wrong reply signature, invalid handle or JSON, oversize reply, unknown error name | no |
| `bus_access_denied` | `org.freedesktop.DBus.Error.AccessDenied` (bus policy) | no |
| `policy_denied` | `org.pantheon.HostBroker1.Error.PolicyDenied` | no |
| `idempotency_conflict` | `…Error.IdempotencyConflict` | no |
| `unknown_handle` | `…Error.UnknownHandle` | no |
| `subject_mismatch` | `…Error.SubjectMismatch` | no |
| `invalid_transition` | `…Error.InvalidTransition` | no |
| `approval_unavailable` | `…Error.ApprovalUnavailable` | yes |
| `recovery_refused` | `…Error.RecoveryRefused` | no |
| `registry_denied` | `…Error.RegistryDenied` | no |
| `migration_required` | `…Error.MigrationRequired` | yes |
| `failed` | `…Error.Failed` | no |
| `unsupported` | `…Error.Unsupported` | no |
| `internal` | `…Error.Internal` | yes |
| `broker_rate_limited` | `…Error.RateLimited`: the broker's per-uid budget (T-2917 5a) | yes |

## Bounds, reconnect and statelessness

**Wait bounds.**

| Wait | Limit |
|---|---|
| Connect | 2 s |
| Call, read-only tools | 10 s |
| Call, plan/apply/verify/rollback | 30 s |
| In flight | one broker call per process |

**Reconnect.**

- MCP initialize and `tools/list` never touch the bus. The adapter starts and
  lists its stable tools with the broker absent (gate 6).
- Each call connects lazily if there is no live link.
- Any transport fault drops the link, so the next call reconnects. Broker
  restarts, unique-name changes and bus restarts all heal without restarting
  the harness.
- The adapter never retries a call and never generates an idempotency key.
  Exactly-once comes from the broker's idempotency.
- Client cancellation abandons only the wait (gate 31).

**Rate limiting.** The limiter is per-process and in-memory, and it is NOT a
security control: any process running as the same uid can call the broker over
D-Bus directly. The authoritative limit is the broker's per-uid budget
(T-2917 5a), keyed on the bus-derived uid and checked after identity; it
surfaces here as `broker_rate_limited`.

**ORG-068 E10 invariants.**

- **C2, restart-fixable:** the process holds no state. The harness respawns it
  on exit, and a broker repair heals on the next call.
- **C3, stateless:** nothing is written to disk. Process memory holds only the
  connection object and the limiter tokens. Handles and keys belong to the
  caller and the broker.
- **C1, externally legible:** recorded as `legs.health: partial_by_design`
  (accepted by HO ruling on T-2915). The waiver acknowledges E10.C1 and E9.C3.
  ORG-052 forbids the adapter a listener, so it cannot own a bus name or export
  `org.pantheon.Health1`. Its legibility path has three parts:
  - `pantheon-host-mcp-adapter --self-check [--no-bus]` prints one bounded
    `pantheon-health/v1`-shaped line covering `runtime`, `system_bus` and
    `broker`. It exits 1 only when the status is `unhealthy`.
  - The bounded `broker_unavailable` tool error.
  - The broker's own Health1 status, relayed through `host.capabilities`
    (its `health` field).

## Runtime pin and lock

**`mcp==2.3.0`.** The arc plan pinned `mcp==2.0.0` (2026-07-28). On
2026-10-10, the live index showed 2.3.0 (2026-10-02) as the latest stable.
Every 2.x release serves protocol `2026-07-28` (per-request, `server/discover`)
and the handshake era up to `2025-11-25`. The transitive requirements are the
same across 2.0.0–2.3.0. Two later releases change behaviour this adapter
depends on:

- 2.1.0: raised handler exceptions become generic errors (see Errors).
- 2.3.0: empty `_meta` is no longer sent on `2025-11-25` connections.

The HO approved the deviation on T-2915.

**`jeepney==0.9.0`.** A pure-Python D-Bus client with zero runtime
dependencies. `dbus-python` ships only as an sdist and cannot be locked by
wheel hash. Approved by HO ruling on T-2915. jeepney is confined to
`bus_client.py` behind `contract.Method`.

**The lock.** `dist/requirements.lock` holds 29 wheels for CPython 3.12
manylinux x86_64, each pinned and sha256-hashed. Its header records the
sha256 of `requirements.in`. The lock is generated without installing
anything:

```
python3 tools/host_mcp_adapter/dist/lock.py          # regenerate (pip --dry-run --report)
python3 tools/host_mcp_adapter/dist/lock.py --check  # exit 1 if stale
```

The seat MCP servers keep their own fleet pin, `mcp==1.27.1` (T-1934). The
adapter's runtime is a separate venv and does not touch that pin.

## Packaging (fixture root only; install is T-2920's)

```
pip download --require-hashes --no-deps --only-binary=:all: \
    --python-version 3.12 --implementation cp --abi cp312 \
    --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
    -d WHEELHOUSE -r tools/host_mcp_adapter/dist/requirements.lock
python3 tools/host_mcp_adapter/dist/build_runtime.py --root FIXTURE --wheelhouse WHEELHOUSE
python3 tools/host_mcp_adapter/dist/verify_artifact.py --root FIXTURE [--expect-owner 0]
```

**What `build_runtime.py` installs.**

- It installs only with `--require-hashes --no-deps --only-binary=:all:
  --no-index`. A wheel that doesn't match the lock aborts the build.
- It removes the console scripts that wheels drop into `venv/bin`.
- It precompiles the app.
- It strips group and other write bits.
- It writes `BUILD-MANIFEST.json`, a sha256 for every file, for T-2920's
  receipt.

**The launcher.** It runs `venv/bin/python3 -I`, which ignores the user site,
PYTHON* variables and the script's directory. It sets
`sys.dont_write_bytecode`, puts only the packaged app directory on `sys.path`,
and calls `server.main()`. `main()` accepts `--self-check` and `--no-bus`, and
nothing else: no bus address, no config file, no environment override.

**What `verify_artifact.py` checks (gate 7, fixture form):**

1. The installed distribution set equals the lock.
2. The launcher's self-check passes in a hostile environment (a poisoned
   PYTHONPATH and a user site each carrying a fake `mcp`).
3. After that run, the manifest still re-hashes exactly, which proves the
   runtime wrote nothing into its own tree.
4. No path is group- or other-writable.
5. With `--expect-owner`, every path is owned by the given uid. Root ownership
   can only be proven at install.

## Tests

`tools/test_host_mcp_adapter.py` (unittest). Every skip carries an explicit
reason.

| Tier | Needs | Where it runs |
|---|---|---|
| Pure, parity, structural, lock, skip guard | system python3; dbus-python for signature parity | CI and locally |
| SDK stdio (both eras) | the pinned runtime | fixture venv only; CI skips with a reason |
| Private bus (real landed broker driver plus jeepney fault fakes) | the pinned runtime, `dbus-daemon`, system python3 with dbus-python, PyGObject and PyYAML | fixture venv only |
| Packaging | `T2915_WHEELHOUSE` | locally |

CI pins mcp 1.x, so the runtime tiers skip there. A hash-locked CI job is a
follow-up ticket. `SkipGuardTests` pins that the skip fires only when the
pinned runtime is absent.

```
timeout 900 taskset -c 3 FIXTURE/usr/local/lib/pantheon/host-mcp-adapter/venv/bin/python3 \
    -m unittest -v tools.test_host_mcp_adapter      # runtime + private bus + packaging
timeout 300 taskset -c 3 python3 -m unittest -v tools.test_host_mcp_adapter   # pure + parity
```

**Acceptance gates.**

| Gate | Here | Deferred |
|---|---|---|
| E1 | The SDK-client half: both eras, exact `tools/list`, every schema, error mapping, stdio lifecycle. | The official Inspector and installed-client era evidence go to T-2924. |
| E6 | The host-adapter half, on a private bus with the real landed broker. | Real D-Bus activation goes to T-2924; the approver half is T-2916. |
| E7 | Fixture form. | Root ownership goes to T-2920 and T-2924. |
| E3, E4 | — | T-2924 (real Codex and Claude clients). |

## Known risks and routing

- **Install paths are PROVISIONAL.** ORG-052 §7 names only the libexec
  launcher; T-2920 owns `/usr/local/lib/pantheon/host-mcp-adapter/`.
- **Dotted tool names** (`host.operation.get`) are legal under MCP SEP-986.
  Whether the real Claude Code and Codex clients handle them is unproven: a
  T-2924 risk. The names are ADR-ratified and unchanged.
- **The lock is platform-specific** (cp312 manylinux x86_64). A change of
  interpreter minor version needs a new lock; `--self-check` reports the
  mismatch.
- **Harness registration** (the Codex and Claude config) for this adapter is
  out of scope here. It is routed to T-2916 together with the approver adapter,
  and live registration is an enable leg.
