# Approver MCP adapter (ORG-052 Unit 4b, T-2916)

The privilege-free stdio MCP server that an **approver** seat (Claude Code or
Codex) launches to review and decide other seats' host plans. It reaches the
approval authority, `org.pantheon.HostApproval1`, and nothing else. It is a
separate binary from the host adapter (`tools/host_mcp_adapter/`, T-2915), as
ORG-052 §5 requires.

It exposes exactly four tools, each mapped one-to-one onto one fixed method of
`org.pantheon.HostApproval1.Reviewer` at `/org/pantheon/HostApproval1`.

The adapter has no host mutation, no durable state, no credentials or broker
token, no listener, no caller-identity argument, no generic passthrough, and
no path to the `Consumer`/`HumanIngress` interfaces, the broker or an
executor.

| File | Role |
|---|---|
| `contract.py` | Pure: tools, closed `Method` enum, 2020-12 schemas, validation, shaping, total error mapping over the 12 approval wire names, rate limiter, self-check report. |
| `bus_client.py` | The only D-Bus code (jeepney), fixed Reviewer destination, lazy self-healing connect. |
| `server.py` | lowlevel MCP Server over stdio (both eras), `main()`, `--self-check`. |

`tools/test_approval_mcp_adapter_driver.py` is test-only and never packaged.

## Tools

| Tool | Reviewer method | Arguments (all required) | structuredContent | Hints |
|---|---|---|---|---|
| `approval.review` | `Review` sss→ssss | `plan_handle`, `expected_digest`, `request_plan_handle` (64-hex or `""` = direct review) | `review_handle`, `state`, `plan_digest`, `summary` (object) | idempotent |
| `approval.review.get` | `ReviewGet` s→ss | `review_handle` | `state`, `summary` (object) | readOnly |
| `approval.decide` | `Decide` ssssss→ss | `review_handle`, `expected_digest`, `decision` (granted/denied), `reason_code` (approved/rejected), `audit_anchor` (printable ASCII 1-256), `idempotency_key` | `state`, `approval_handle` (64-hex if granted, `""` if denied) | destructive, idempotent |
| `approval.request_human` | `RequestHuman` sss→ss | `review_handle`, `expected_digest`, `idempotency_key` | `request_handle`, `state` | idempotent; the current service answers `unsupported` (T-2919) |

## Errors (PROVISIONAL-PENDING-RATIFICATION)

isError result with canonical JSON `{"error":{code,retryable,message[,detail]}}`.
Adapter-side: `invalid_arguments`, `rate_limited`, `authority_unavailable`
(retryable), `authority_no_reply` (retryable), `authority_contract_mismatch`,
`bus_access_denied` (system bus policy denied the call). Wire: `approver_denied`, `handoff_mismatch`,
`policy_denied`, `unknown_handle`, `idempotency_conflict`,
`invalid_transition`, `metadata_unavailable` (retryable), `registry_denied`,
`bind_denied`, `failed`, `unsupported`, `internal` (retryable).

## Authority, registration and revocation

- Registration is presentation, not authority: only a seat named by
  root-owned approver policy gets it (`tools/harness_adapter/host_mcp_registration.py`,
  same `ApproverResolver` predicate as the server's Reviewer gate).
- A manual launch by a non-approver lists the four tools but every call is
  `policy_denied`/`subject_denied`, as is any direct same-uid Reviewer call.
- Deleting a registration revokes nothing; removing the seat from approver
  policy does, even with stale config.
- **Staged revocation takes effect when HostApproval1 restarts with the new
  policy** (no hot reload). T-2920 must restart or reload the service on every
  approver-policy install.

## Runtime and packaging

One lock for both adapters (`tools/host_mcp_adapter/dist/requirements.lock`).
`build_runtime.py --adapter approval` / `verify_artifact.py --adapter approval`
build and verify `usr/local/lib/pantheon/approval-mcp-adapter/` with launcher
`usr/local/libexec/pantheon-approval-mcp-adapter` (PROVISIONAL, T-2920).

## Tests

`tools/test_approval_mcp_adapter.py`; full proven/unsupported list in
`SUPPORT_MATRIX` (`tools/harness_adapter/host_mcp_registration.py`).
