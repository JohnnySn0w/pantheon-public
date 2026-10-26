---
aliases: [ORG-052]
title: Harness-neutral host capability broker with MCP adapters over system D-Bus
status: "Ratified 2026-07-31 by the operator (Telegram message 450). Amended 2026-08-26 by [[ORG-082]] (operator-ratified via Telegram 8612 as the ORG-080 + ORG-081 + ORG-082 bundle): substrate op families (account/group/ACL/sudoers/root-unit + mailbox-dir + linger) move off the broker daemon's exposed surface (bootstrap in-process root only); gate 32a (same-registry-digest acceptance) added."
date: 2026-07-30
author: Codex (Consultant); canonicalized by Consultant as interim dispatcher under D2
relates: ORG-031, ORG-033, ORG-040, ORG-049, ORG-050, ORG-051
# elements: — ORG-065 machine index of the "Acceptance gates for implementation"
# section (T-2934 migration walk, pass 1). E<n> == gate n, 1:1; text is a
# compact label — the ADR body's gate text remains authoritative. Added by HO;
# no obligation content changed.
elements:
  - {id: E1, text: "gate 1: stable MCP SDK/Inspector validates discover/initialize fallback, tools/list, schemas, error mapping, stdio lifecycle; installed-client era/revision evidence"}
  - {id: E2, text: "gate 2: D-Bus introspection exposes exactly the ratified interfaces/signatures; no generic dict-input, command, transport, or passthrough method"}
  - {id: E3, text: "gate 3: real installed Codex client launches packaged stdio adapter from its sandbox, lists exact tools, read-only call + one broker-denied call"}
  - {id: E4, text: "gate 4: real installed Claude Code client same sequence, same broker; tools/list schemas semantically identical after canonical JSON normalization"}
  - {id: E5, text: "gate 5: approver seat lists exactly four approval tools; requester-only seat does not; manual launch or direct reviewer D-Bus call denied"}
  - {id: E6, text: "gate 6: adapter initializes with broker/authority absent, returns bounded unavailable error; succeeds after activation/repair without harness restart"}
  - {id: E7, text: "gate 7: packaged adapters import all runtime deps without per-seat installation; executable/runtime bytes not seat-writable"}
  - {id: E8, text: "gate 8: unproven harness/provider/startup behavior remains unsupported; documentation evidence not relabeled as deployment evidence"}
  - {id: E9, text: "gate 9: unregistered uid cannot call; identity matches bus uid + root registry under adversarial env/persona/pid/cgroup/metadata/groups; capabilities exposes only own intersection"}
  - {id: E10, text: "gate 10: bus policy denies unauthorized senders/members/name-ownership; direct D-Bus bypass gets same service authorization; neither MCP nor bus policy alone is the perimeter"}
  - {id: E11, text: "gate 11: same-uid second process is same trust domain — cannot approve that uid's plan or apply/rollback without independently issued exact grant"}
  - {id: E12, text: "gate 12: approval service checks bus-derived approver before returning plan effects; unauthorized reviewers learn neither effects nor topology; plan source returns no credentials/evidence/endpoint/roster"}
  - {id: E13, text: "gate 13: approval denied when approver equals/shares uid with owner, performer, request-plan owner, or handoff participant; reciprocal/circular/mismatched approvals attacked and denied"}
  - {id: E14, text: "gate 14: Telegram path accepts only provider-authenticated callback from allowlisted user/chat/message + single-use nonce; bot-sent/copied/forwarded/edited/stale/replayed denied; credential+cursor isolated"}
  - {id: E15, text: "gate 15: handles stolen across subjects denied; digest/mailbox/ticket/role/attestation/approval handle alone is insufficient authority"}
  - {id: E16, text: "gate 16: ValidateAndBind refuses every mismatched tuple field; identical retry returns the original binding"}
  - {id: E17, text: "gate 17: no caller can supply shell, argv, executable, arbitrary path/content, environment, sudo target, uid, group, unit/policy/sudoers text, helper operation, or credential value"}
  - {id: E18, text: "gate 18: adapters have no listener/credential/journal/executor/root/mailbox/arbitrary-D-Bus surface; broker cannot write approval state and vice versa"}
  - {id: E19, text: "gate 19: broker+approval unprivileged; per-seat executor runs as registry-selected target uid; root helper accepts only compiled operation enums, cannot write either journal"}
  - {id: E20, text: "gate 20: sudoers tests reject all malformed/overbroad shapes; apply requires visudo -cf, atomic replacement, sudo -l verification, receipt, exact rollback"}
  - {id: E21, text: "gate 21: credentials, grant payloads, approval contents, callbacks, unbounded evidence never appear in broadcasts, argv, units, logs, mailbox, tool output, downstream requests"}
  - {id: E22, text: "gate 22: delayed/duplicated/forged/replayed mailbox-ticket content can request review but cannot mint authority or duplicate an operation; authoritative state stays in owning journals"}
  - {id: E23, text: "gate 23: cross-persona traversal binds request-plan owner, performer, both plans/digests, shared effect digest, handoff anchor; human gate preserved for privileged families"}
  - {id: E24, text: "gate 24: duplicate identical idempotency keys return same plan/review/decision/operation; conflicting reuse refused"}
  - {id: E25, text: "gate 25: transitions exactly OPEN->GRANTED|DENIED|EXPIRED and ISSUED->BOUND|REVOKED|EXPIRED; terminal grants never reusable"}
  - {id: E26, text: "gate 26: fault injection over the five named stuck states; each resumes identical operation or terminates loud non-reusable manual-recovery"}
  - {id: E27, text: "gate 27: concurrent revoke/bind single serialized winner; revoke-before-bind prevents; bind-before-revoke commits and stays consumed"}
  - {id: E28, text: "gate 28: authority outage fails closed for new/unbound work; durable AUTHORIZED reconciles from broker journal; cached ISSUED authorizes nothing"}
  - {id: E29, text: "gate 29: adapter exit, disconnect/reconnect, unique-name change, disconnect-after-submit, restart, helper timeout, verification failure, provider retry each deterministic"}
  - {id: E30, text: "gate 30: staged receipt, rollback intent, DISPATCH_INTENT durably observable before first activation-class mutation; post-dispatch faults prove restore or loud refusal"}
  - {id: E31, text: "gate 31: client-wait cancellation does not cancel, revoke, reuse, or duplicate the approval or host operation"}
  - {id: E32, text: "gate 32: disposable host converges and rolls back the manifest-driven account/group/mailbox/ACL/linger/unit/bus-policy set exclusively from a registered manifest revision"}
  - {id: E33, text: "gate 33: disposable host applies/verifies/revokes/rolls back one exact human-approved sudoers fragment with full before/after evidence retained"}
  - {id: E34, text: "gate 34: account retirement disables seat and jobs without deleting home or mailbox; destructive deletion unsupported"}
  - {id: E35, text: "gate 35: supervised root bootstrap installs every account/executable/unit/policy/registry/journal-root/allowlist with complete receipt; identical rerun idempotent"}
  - {id: E36, text: "gate 36: plans attempting to modify broker/approval/helper bytes, units, bus rules, root registries, approver policy, allowlists, or operation enums are refused"}
  - {id: E37, text: "gate 37: one disposable seat clears MCP-to-D-Bus plan/review/approval/apply/verify/rollback through the real broker, authority, target-uid executor, user-systemd manager"}
  - {id: E38, text: "gate 38: one human-gated disposable operation clears the real Telegram card+callback; agent-forwarded approval fails red before the real human callback succeeds green"}
  - {id: E39, text: "gate 39: sandbox regression proves adapters reach system bus and executor reaches target user manager without the founding-incident private-socket path"}
  - {id: E40, text: "gate 40: exact T-2381 bringup path clears supervised live proof — unit fire, correlated mailbox send/receive, finite timer fire, attributable effect"}
  - {id: E41, text: "gate 41: every required guard records a red falsification and restored green run across the named attack classes"}
---

# ORG-052 — Harness-neutral host capability broker with MCP adapters over system D-Bus

**Status:** **Ratified 2026-07-31 by the operator via Telegram message 450. Amended 2026-08-26 by [[ORG-082]]** (operator-ratified via Telegram 8612 as the ORG-080 + ORG-081 + ORG-082 bundle): substrate op families (account/group/ACL/sudoers/root-unit + mailbox-dir + linger) move off the broker daemon's exposed surface (bootstrap in-process root only); gate 32a (same-registry-digest acceptance) added; the ratified seven-method D-Bus contract (`Capabilities`, `Plan`, `Apply`, `Verify`, `Rollback`, `OperationGet`, `ApprovalGet`) is unchanged. The frozen original review candidate had SHA-256 `ffbfbf7a336159e1104e3eefe8b3b6e95ce67bc73954def1cca82b45ba53d203`; this canonical file now also carries the [[ORG-082]] amendment. Implementation and production enablement remain separately gated.

## Intent

Give every supported harness the same narrow, auditable agent tools for
diagnosing and operating a Pantheon persona seat when the harness command
sandbox cannot reach the host surface. Keep MCP at the compatibility boundary
where harnesses support it natively, while making host authority independent of
MCP, model sessions, network listeners, and per-seat bearer secrets.

## Context

Pantheon's installer now has a typed `plan` / `apply` / `verify` / `rollback`
contract, content-addressed plans, staged receipts, and a `HostBackend`
interface. The live Codex session exposed the remaining boundary: command
tools run inside a user and PID namespace that strips supplementary groups and
causes `systemctl --user` to fail on the user manager's private control socket.
Restarting the harness does not remove that structural boundary. The same class
can recur in any harness whose local tools are sandboxed. The later live probe
showed that the session D-Bus itself remains reachable, so the CLI failure must
not be generalized into a bus-connectivity claim.

Running the privileged operation inside a local stdio MCP server does not solve
the problem. In the standard stdio transport, the harness starts the server as
its child, so it inherits the harness execution boundary. A stdio MCP process
*can*, however, remain a privilege-free adapter to an independently supervised
host component. That preserves the simplest native integration in both current
harnesses without making the adapter itself an escape hatch.

Live probes from the current Codex workspace-write sandbox established a
useful distinction:

- supplementary groups are stripped and `systemctl --user` fails while trying
  the user manager's private socket;
- pathname Unix sockets under `/run`, the system D-Bus, and the session D-Bus
  are reachable from the sandbox;
- both `busctl` and Python `dbus` authenticate successfully to both buses; and
- read-only `org.freedesktop.systemd1` calls on the session bus work from the
  same sandbox, including resolving an installed timer unit.

This does not prove the future broker, but it falsifies the premise that a
network transport is required merely to cross this sandbox. The system bus is
already the host's authenticated local RPC boundary.

ORG-031 already separates harness adapters from org-tier policy. ORG-049
defines the installer lifecycle. ORG-050 separates host, persona,
communication, credential, and authority identities. ORG-051 defines
credential references and brokered, short-lived access. This ADR composes
those decisions at the one missing seam: authenticated host actuation.

## Verified external surface, 2026-07-30

This decision targets the current MCP protocol revision, `2026-07-28`, and the
host's existing D-Bus/systemd substrate. The packaged adapter is dual-era: it
also supports the legacy initialization-based `2025-11-25` revision until both
installed harness clients are live-proven on the modern per-request protocol.
This follows MCP's explicit backward-compatibility model and does not depend on
optional Tasks, Skills, Apps, sampling, roots, or elicitation features:
<https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning>.

- MCP defines stdio as a client-launched subprocess. That is the desired
  lifecycle for the privilege-free harness adapter, not the host broker:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports>.
- MCP tools have JSON Schema inputs and optional structured outputs. Tool
  annotations are hints, not enforcement. Servers must validate inputs, enforce
  access control, rate-limit, sanitize output, and retain audit evidence:
  <https://modelcontextprotocol.io/specification/2026-07-28/server/tools>.
- MCP defines transport-specific cancellation, but adapter cancellation only
  abandons the client-side wait; it never cancels a broker operation after
  durable submission. Legacy protocol sessions and optional extension state
  are therefore not used as durable operation state:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports>
  and
  <https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices>.
- Codex currently documents stdio MCP servers, tool allowlists, and per-tool
  approval modes:
  <https://learn.chatgpt.com/docs/extend/mcp?surface=cli>.
- Claude Code currently documents stdio MCP servers and per-tool permission
  controls:
  <https://code.claude.com/docs/en/mcp>.
- D-Bus on Unix recommends the `EXTERNAL` authentication mechanism using
  out-of-band Unix credentials. The system bus provides a connection-scoped
  unique sender, service discovery, activation, and security policy:
  <https://dbus.freedesktop.org/doc/dbus-specification.html>.
- The system bus is default-deny for sending method calls and owning names.
  D-Bus guidance recommends small targeted services with explicit
  `send_destination` policy:
  <https://dbus.freedesktop.org/doc/dbus-daemon.1.html> and
  <https://dbus.freedesktop.org/doc/dbus-api-design.html>.
- D-Bus services may be systemd-activated and run as a dedicated unprivileged
  account with `Type=dbus` and `BusName=`:
  <https://www.freedesktop.org/software/systemd/man/systemd.service.html>.

Local readback confirms Codex CLI `0.145.0` and both harness configuration
models support stdio MCP. The Codex account currently has Python `dbus` bindings
but not the Python MCP SDK, which makes adapter dependency provisioning an
installer obligation rather than a per-seat manual `pip` step. Claude Code was
not executable from the Codex account; its behavior remains a real-client test
obligation before deployment.

## Decision

### 1. MCP is the agent boundary; system D-Bus is the host boundary

Each supported harness launches the same privilege-free stdio MCP adapter
through its native MCP configuration. The adapter exposes the common Pantheon
tool names and schemas, translates each call one-to-one to the host substrate,
and translates the result back to MCP. It has:

- no host mutation implementation;
- no durable plan, approval, operation, or receipt state;
- no credential value or network listener;
- no ability to assert caller identity or authority; and
- no generic command, file, D-Bus, or transport passthrough.

Inheriting the harness sandbox is therefore intentional. The adapter needs only
stdio and the system D-Bus socket, both of which must be proven in every
supported harness profile.

The primary use case is the intersection
`policy_authorized ∩ unix_or_harness_blocked`: Pantheon policy already permits
the persona to perform the exact operation, but its ordinary harness process
cannot reach the required Unix/systemd/root surface. The broker supplies typed
actuation, not policy authority. If policy does not authorize the caller, the
broker refuses. It never searches for, borrows, or transfers another persona's
authority.

Ordinary in-sandbox work—repository edits, research, tickets, mailbox
conversation, and unprivileged commands—does not use this system. Host tools
are an exceptional lifecycle/recovery surface, not an everyday agent control
plane.

The adapter initializes and lists its stable tools even when the host broker is
temporarily unavailable. Calls then return a bounded, actionable
`broker_unavailable` tool error. It reconnects to D-Bus on later calls so a
broker repair does not require restarting the agent merely to recover its tool
registry.

The independently supervised host component owns the well-known system-bus name
`org.pantheon.HostBroker1`, object path `/org/pantheon/HostBroker1`, and
versioned interface `org.pantheon.HostBroker1.Manager`. The system bus is the
only production substrate in the initial architecture. There is no local HTTP
listener, OAuth enrollment, bearer token, certificate, port, or MCP session in
the host authority path.

### 2. Identity comes from D-Bus credentials and the seat registry

The system bus authenticates the adapter's Unix identity using D-Bus
`EXTERNAL`. The broker resolves the unique message sender to its Unix uid using
bus-provided credentials, then constructs `ActorContext` from that uid and a
root-owned seat registry. Tool and D-Bus method arguments may name a target
`seat_id`, but may not assert caller uid, persona, role, instance, or
capabilities.

The D-Bus policy permits only explicitly registered seat uids to send to the
broker name and only the dedicated broker account to own that name. This policy
is coarse defense in depth; the broker still authorizes every method against
the resolved subject, target seat, operation family, and external approval.

A caller-supplied MCP session id, D-Bus unique-name string, operation handle,
plan id, receipt id, or plan digest is never sufficient authority. The broker
uses only the live message sender supplied by the bus, resolves its credentials,
and then applies policy. Every handle is random, expires, is bound to the
server-derived subject, and is re-authorized on every use.

The local trust boundary is explicit: any process already running as the same
seat uid can reach the same D-Bus surface. D-Bus proves the Unix seat, not a
specific model process. Mutation therefore still requires a separate
digest-bound approval grant. If process- or harness-instance identity becomes
necessary, it requires a separately reviewed credential profile rather than
being inferred from pid, cgroup, environment, or MCP metadata.

### 3. Protocol handling and privilege remain split

The system-bus broker runs as a dedicated unprivileged account. Root owns its
unit, D-Bus service activation file, bus policy, executable, immutable
configuration, operation allowlist, and seat registry. The broker never runs as
root and receives no general-purpose sudo grant.

Approved execution is delegated only to root-provisioned, typed per-seat
executors running as the target Unix account. Their unit identity and target uid
come from the root-owned seat registry, never from a request parameter. A
root-owned helper accepts only the privileged operation families ratified
below.

Internal execution also uses restricted system D-Bus interfaces, not mailbox
delivery or caller-built `busctl` commands. The broker selects the per-seat
executor bus name or root-helper bus name from the root registry and calls
`Execute(operation_handle, expected_operation_digest)` (`"ss" → "s"`).
`ExecutionGet(execution_handle)` (`"s" → "ss"`) returns state and bounded
evidence. The selected executor/helper obtains its server-resolved,
schema-versioned operation through the broker-only
`ExecutionSource.OperationSpec(operation_handle,
expected_operation_digest)` (`"ss" → "ssss"`), which returns schema version,
closed operation kind, recomputed digest, and bounded canonical specification.
The specification contains only registry/template identifiers and typed
preconditions; a privileged helper independently resolves root-owned template
bytes.

Member-level bus policy permits only the broker uid to call executor/helper
methods and only registered executor/helper uids to read their matching
operation. The service rechecks sender uid, operation kind, target, digest, and
registry-selected executor before acting. No component accepts shell text, an
argv array, a caller-chosen executable, an unclassified absolute path, raw file
content, arbitrary environment variables, a caller-chosen Unix uid, or a sudo
target.

If an operation needs an upstream credential, the executor requests a separate
purpose-scoped lease from the ORG-051 credential broker. Neither the MCP adapter
nor the public D-Bus request carries credential values.

### 4. The public contract is closed, handle-based, and one-to-one

The initial MCP adapter exposes seven stable tools. Each maps to exactly one
fixed D-Bus method:

| MCP tool | D-Bus method | In → out signature | Effect | Required binding |
|---|---|---|---|---|
| `host.capabilities` | `Capabilities()` | `"" → "ss"` | return contract version and bounded capabilities JSON | bus-derived subject |
| `host.plan` | `Plan(seat_id, manifest_revision, idempotency_key)` | `"sss" → "sss"` | return plan handle, digest, and bounded summary JSON | subject + seat scope |
| `host.apply` | `Apply(plan_handle, plan_digest, idempotency_key, approval_handle)` | `"ssss" → "s"` | return the durable operation handle for the exact stored plan | subject + plan + approval |
| `host.verify` | `Verify(seat_id, plan_handle, idempotency_key)` | `"sss" → "s"` | return an operation handle for bounded verification evidence | subject + seat or plan scope |
| `host.rollback` | `Rollback(receipt_handle, idempotency_key, approval_handle)` | `"sss" → "s"` | return the durable operation handle for receipt-bound restore | subject + receipt + approval |
| `host.operation.get` | `OperationGet(operation_handle)` | `"s" → "ss"` | return terminal/nonterminal state and bounded result JSON | handle bound to subject |
| `host.approval.get` | `ApprovalGet(approval_handle)` | `"s" → "ss"` | return approval state and bounded non-secret summary JSON | handle bound to subject |

`host.capabilities` is optional discovery, not a mandatory preflight or an
authority directory. It reports the contract/broker/policy revisions, health,
host-supported operation families, and the intersection this bus-derived
subject is already authorized to plan/apply, including target scope and
approval class. It never names another persona, approver, credential, uid, or
executor. A caller may invoke `host.plan` directly; capabilities is expected at
bringup, after `unsupported` / `policy_changed`, or when a new harness/seat
discovers the installed bridge—not on every task or turn.

There is deliberately no `host.approve` MCP tool and no approval method on the
public `HostBroker1.Manager` D-Bus interface. A subject that requests an
operation cannot mint that operation's approval. Approval is issued by the
separate, independently authorized `HostApproval1` service defined below. Its
reviewer interface is unavailable to requester seats unless they independently
hold approver policy for some *other* subject's plan; self-approval is still
denied server-side.

`host.plan` does not accept a host path or an operation list. It accepts a
logical seat id, an installed/allowlisted manifest revision, and a caller
idempotency key. The harness and any deployment override are resolved entirely
from the root-owned seat registry; neither is a public argument. The resolved
harness is included in the plan digest and every resulting receipt. The server
resolves the manifest and regenerates the `InstallPlan`. `host.apply` accepts
only a server-owned plan handle plus its digest; it never accepts an arbitrary
serialized plan as authority.

The bounded plan summary also includes an `effect_digest`: a canonical digest
of every execution-affecting field, including target, manifest/framework
revision, resolved harness/deployment override, ordered typed operation
specifications, template ids, and preconditions. It excludes only
owner/performer, request/handoff metadata, and idempotency/correlation fields.
The full plan digest remains actor-bound. The effect digest is not authority;
it exists so the approval service can prove that a recipient's independently
created plan matches the original request plan without treating mailbox
content as provenance.

All MCP tools publish JSON Schema 2020-12 input and output schemas and return
`structuredContent` plus a small text representation for older clients. The
D-Bus interface uses fixed scalar input signatures rather than `a{sv}`, raw
JSON input, or an extensible command dictionary. Bounded summaries and evidence
may be returned as canonical JSON strings with a declared schema version. Both
the adapter and broker validate inputs; the broker is authoritative.

Policy, precondition, validation, and execution failures use a closed,
versioned D-Bus error namespace which the adapter maps to MCP tool execution
errors. Malformed JSON-RPC or unknown MCP tools are protocol errors. The initial
D-Bus interface emits no broadcast signals; clients poll `OperationGet`, so
evidence cannot leak through a permissive broadcast channel.

`host.capabilities`, `host.operation.get`, and `host.approval.get` carry
`readOnlyHint=true`. `host.plan` writes durable plan state, and `host.verify`
may run active probes, so neither is advertised read-only. Apply and rollback
use conservative destructive annotations and idempotency hints. Server-side
policy remains the enforcement layer because annotations are not trustworthy
controls.

### 5. Approval authority is separate, exact, and crash-safe

#### Service and tool boundary

Approval is owned by a second unprivileged system service with well-known bus
name `org.pantheon.HostApproval1`, object
`/org/pantheon/HostApproval1`, and versioned reviewer interface
`org.pantheon.HostApproval1.Reviewer`. Root owns its unit, executable, bus
policy, immutable approver policy, and external-ingress allowlist. The
dedicated approval account owns only its durable review/grant journal. It does
not run executors, mutate seats, hold sudo, read credentials, or write the host
broker journal.

Authorized approver seats receive a distinct root-owned stdio MCP adapter.
Requester-only seats do not receive these tools. The adapter remains
privilege-free and maps four tools one-to-one to the reviewer interface:

| Approver MCP tool | D-Bus method | In → out signature | Effect |
|---|---|---|---|
| `approval.review` | `Review(plan_handle, expected_digest, request_plan_handle)` | `"sss" → "ssss"` | return review handle, state, recomputed digest, and bounded redacted plan summary |
| `approval.review.get` | `ReviewGet(review_handle)` | `"s" → "ss"` | return review state and bounded non-secret summary |
| `approval.decide` | `Decide(review_handle, expected_digest, decision, reason_code, audit_anchor, idempotency_key)` | `"ssssss" → "ss"` | grant or deny; return state and an opaque approval handle only when granted |
| `approval.request_human` | `RequestHuman(review_handle, expected_digest, idempotency_key)` | `"sss" → "ss"` | request a human approval card; return request handle and state |

`decision` and `reason_code` are closed enums. `audit_anchor` is bounded audit
context, not authority. For a handoff, requester identity comes only from the
server-owned `request_plan_handle`; a mailbox sender label or ticket anchor is
never identity evidence. The MCP caller cannot assert approver, requester,
performer, Unix uid, authority source, policy revision, or grant fields.

The approval service first obtains only minimal immutable metadata—plan owner,
performer, family, target, and digest—through the broker's separately versioned
internal `org.pantheon.HostBroker1.ApprovalSource` interface. It authorizes the
bus-derived approver against root-owned policy *before* requesting or
displaying detailed effects. Only then may it fetch a bounded redacted summary.
The internal interface is callable only by the approval-service uid, recomputes
the digest from the server-owned plan, and never returns credentials, raw
files, unbounded evidence, executor endpoints, or capability rosters. The
public seven-method `HostBroker1.Manager` contract remains unchanged.

The initial restricted internal contracts are also closed:

| Interface / authorized caller | Method | Signature | Purpose |
|---|---|---|---|
| `HostBroker1.ApprovalSource` / approval uid | `PlanMetadata(plan_handle)` | `"s" → "ssssssss"` | owner, performer, target, family, plan digest, effect digest, manifest revision, policy revision |
| `HostBroker1.ApprovalSource` / approval uid | `PlanSummary(plan_handle, expected_digest)` | `"ss" → "ss"` | recomputed digest and bounded redacted summary |
| `HostApproval1.Consumer` / broker uid | `ValidateAndBind(approval_handle, plan_handle, expected_digest, operation_handle)` | `"ssss" → "ss"` | atomically return bound state and authority-record id |
| `HostApproval1.Consumer` / broker uid | `ApprovalStatus(approval_handle)` | `"s" → "ss"` | bounded state and non-secret summary |
| `HostApproval1.HumanIngress` / registered ingress uid | `HumanDecision(review_handle, expected_digest, decision, source_event_handle, callback_nonce, idempotency_key)` | `"ssssss" → "ss"` | submit one provider-verified human decision |
| `HostApprovalTelegram1.Ingress` / approval uid | `Present(review_handle, expected_digest, summary_json, callback_nonce, expires_at)` | `"sssss" → "ss"` | deliver one bounded callback card |

These internal interfaces accept neither extensible dictionaries nor generic
commands. Member-level bus policy denies every other sender, and each service
rechecks the live bus-derived uid. Introspection visibility is not call
authority.

Bus policy is defense in depth: only registered approver uids may call
`Reviewer`; requester uids cannot. The approval service still authorizes every
method against the bus-derived subject. Registration of an approver MCP server
in a harness config is presentation, not authority; a direct D-Bus caller
receives the same checks.

#### Approver policy and separation of duty

Root-owned, versioned policy maps each approver subject or external human
source to exact operation families, target seat classes, maximum TTL, and
permitted decision source. It has no implicit wildcard or inheritance. The
initial policy denies an agent/orchestrator approval when the approver:

- is the plan owner, performing subject, or request-plan owner;
- shares the plan owner/performer's Unix uid trust domain; or
- is any participant in the same server-linked request-plan handoff.

Two personas therefore cannot approve one another inside the same request
chain. A future narrowly scoped reciprocal policy would require a distinct
ratified rule and an explicit audit flag; it is not present initially.
Human-gated operation families cannot be downgraded to agent approval by a
handoff, role name, ticket label, or harness configuration.

Read-only diagnostics and planning may be pre-authorized by role policy.
Reversible current-seat apply operations may use a distinct
orchestrator-issued grant when written policy permits it. Unix accounts,
group/ACL/linger changes, system-level units, sudoers fragments, credential
installation/rotation, cross-seat repair, and destructive rollback require the
human decision source in the initial policy.

#### Human authority ingress

Normal human-gated approval enters through a separately supervised Telegram
ingress account, not through agent-forwarded text. The approval service creates
a bounded card containing the exact digest, target, operation family, redacted
effects, expiry, and a single-use random callback nonce. The ingress obtains
its dedicated bot credential through an ORG-051 lease; that credential and
update cursor are never shared with an agent-facing Telegram MCP server or
persona. It sends the card and accepts only a Telegram callback whose
provider-authenticated user id, chat id, message id, review handle, and nonce
match the root-owned allowlist and pending record.

The ingress then calls a fixed private `HumanDecision` method available only to
its dedicated Unix uid. The approval service maps that uid to the configured
provider and human subject; model-supplied Telegram ids or prose cannot do so.
A bot-sent message, copied message id, forwarded “approved” text, stale button,
or replayed callback does not mint authority.

An optional local operator CLI may use the reviewer interface only from a
registered non-agent operator uid. A uid shared with any harness is never
treated as human merely because a CLI or `sudo` command was used. The root
administration surface may inspect, revoke, or disable approval issuance, but
normal production grants do not depend on a root shell ceremony. Root remains
an unavoidable host break-glass authority, and any use of that fact is audited
as break-glass rather than relabeled as ordinary approval.

#### Grant record and state machine

An issued grant is a durable server-side record containing:

- random opaque `approval_handle` and review handle;
- immutable plan handle, recomputed digest, target instance/seat, and operation
  family;
- plan owner and actual performing subject;
- request-plan handle/digest/owner, shared effect digest, and bounded handoff
  anchor, or an explicit `direct_no_handoff` marker;
- bus-derived approver or provider-authenticated human subject;
- authority source, source-event pointer, policy revision, decision, and
  bounded reason code;
- issuance and expiry times, single-use nonce, and idempotency key; and
- lifecycle state plus the operation handle if consumed.

The handle is allowed to travel through the mailbox, but it is not sufficient
bearer authority. The broker accepts it only when its calling subject, plan
handle, digest, performer, target, family, and operation handle match the
server-side record. Grant payloads, source credentials, callbacks, and signing
material never enter model context.

Review state is `OPEN → GRANTED | DENIED | EXPIRED`. A grant is
`ISSUED → BOUND`, `ISSUED → REVOKED`, or `ISSUED → EXPIRED`. `BOUND` is the
single-use authorization commit and never becomes reusable. Denial and
revocation require a new review for any later attempt.

For apply or independently requested rollback:

1. the broker creates and durably syncs a `PREPARED` operation with a random
   operation handle, exact plan/receipt binding, subject, and idempotency key;
2. the broker calls the approval service's private, broker-only
   `ValidateAndBind(approval_handle, plan_handle, expected_digest,
   operation_handle)` method;
3. the approval service revalidates policy, subject/performer/target/family,
   digest, expiry, revocation, and nonce, then atomically syncs `BOUND` to that
   one operation before replying;
4. the broker durably records `AUTHORIZED` with the returned authority record;
5. before dispatch, it durably records the staged receipt, rollback intents,
   and `DISPATCH_INTENT`; only then does it call an executor/helper; and
6. it appends typed execution evidence and terminal state.

`ValidateAndBind` repeated with the identical tuple returns the original
binding. Any different operation, subject, plan, digest, target, or family is
denied. Revocation and bind serialize in the approval journal: if revocation
wins, bind fails; if bind wins, the authorization commit stands and stopping
the operation requires the broker's cancellation/rollback policy. A bound
grant is never “unconsumed.”

Recovery is explicit:

- `PREPARED` with no binding retries validation or terminates refused if the
  grant is invalid;
- approval `BOUND` with broker `PREPARED` retries the identical tuple, records
  `AUTHORIZED`, and continues; if the broker's matching prepared record is
  missing or corrupt, it refuses loudly and leaves the grant consumed;
- `AUTHORIZED` without `DISPATCH_INTENT` resumes from the broker journal;
- `DISPATCH_INTENT` without a result reconciles live state using the staged
  receipt and rollback intents before it proceeds; and
- `DISPATCHED` without a result follows the same reconciliation path.

Approval-authority outage fails closed for new or not-yet-bound operations.
An operation whose broker journal already durably records `AUTHORIZED` may
continue deterministic recovery because the bind commit already occurred.
No cached `ISSUED` grant can be used while the authority is unavailable.

#### Cross-persona requests are new authorized work, not transferred identity

The initial broker has no impersonate, assume-role, forward-as, or
delegate-authority tool or D-Bus method. If policy denies a persona apply
authority but allows bounded planning, the broker simply refuses apply. Any
cross-persona continuation begins as a new dispatch outside the broker: the
requester creates a server-owned request plan and submits its handle, digest,
and bounded summary through Pantheon's dispatcher/ticket/mailbox plane. That
plane, not the requesting model or `host.capabilities`, selects an eligible
recipient from policy. The broker never names another persona's credential,
uid, executor endpoint, or capability roster. If the subject is not permitted
even to plan that target, the operation requires operator-originated
escalation; the system does not invent requester provenance.

The recipient persona evaluates the request under its own instructions and may
refuse it. If it accepts, it creates a new server-owned plan through its own
D-Bus-authenticated subject for the same target and manifest revision. The
approver passes the original request-plan handle as the third
`approval.review` argument. The approval service resolves both handles from the
broker, derives requester and performer from their respective plan owners, and
requires the target, manifest revision, operation families, and effect digest
to match before review. The initial handoff is one hop; longer chains remain
unsupported rather than inferred from prose.

An independently issued grant binds both plan handles and digests, their shared
effect digest, server-derived requesting and performing subjects, target,
operation family, expiry, and bounded dispatch/ticket anchor. Naming a persona,
seat, target uid, executor, mailbox message, ticket, or plan handle in an
MCP/D-Bus argument never transfers authority.

The resulting receipt records the actual performer, the broker-attested request
plan owner, both plans/effect digest, and the bounded handoff anchor. The
mailbox sender label remains delivery context and unverified caller prose is
never promoted to identity evidence. Human-gated capability families remain
human-gated after handoff. Cross-persona work therefore composes two
independently attributable plans plus a separate approval rather than
laundering one persona's access into another.

An apply failure may automatically roll back the mutations from that same
approved operation using its staged receipt. That compensating action does not
need a second grant because it is part of the originally approved transaction.
An independently requested rollback does.

### 6. Durable state owns retries, crashes, and disconnects

Durable state is split by ownership. Root owns immutable broker configuration,
the operation allowlist, the seat registry, approval policy, bus policy, and
external authority-adapter allowlists.
The unprivileged broker owns a separate operation journal containing plans,
operations, staged/complete/failed receipts, and redacted evidence. Harness and
persona processes cannot write either area. A privileged helper, if introduced,
may return typed helper-scoped receipts and evidence but cannot mutate arbitrary
broker state; the broker appends those records to its journal.

The unprivileged approval service owns a different durable journal containing
reviews, decisions, grant bindings, revocations, provider attestations, and
redacted approval evidence. The broker cannot mint or edit those records; the
approval service cannot edit plans, operations, or receipts. The Telegram
ingress keeps only the provider cursor, pending callback correlation, and
redacted delivery evidence needed to validate its source event. No model,
harness, mailbox process, executor, or helper can write either authority
journal.

Before instructing an executor to perform the first activation-class mutation,
the broker writes and durably syncs the staged receipt plus write-ahead rollback
intents required by ORG-049. The executor/helper never writes the broker
journal. It returns only typed mutation/evidence records over its restricted
internal D-Bus interface, which the broker appends. If the broker dies after
dispatch but before appending the result, the unresolved write-ahead intent
drives deterministic live-state reconciliation and rollback-or-loud-refusal on
recovery.

A repeated call with the same subject, idempotency key, operation family, and
target returns the original operation; reuse with different parameters is
refused.

A client timeout, MCP adapter exit, D-Bus disconnect, bus unique-name change, or
harness restart does not imply cancellation or rollback. The caller resumes by
querying `host.operation.get`. Operation state is explicit and does not depend
on an MCP transport session, D-Bus connection, or adapter process. Long-running
MCP Tasks or D-Bus signals may be added later but are not required for
correctness.

Receipts include server-derived actor identity, source manifest/framework
revision, plan digest, helper version, target account, mutations, evidence,
terminal state, and correlation ids. They never include credentials or
unbounded command output.

### 7. Harness adapters configure packaged role-specific stdio commands

The host-substrate installer installs root-owned, versioned host and approver
MCP adapters with one pinned runtime, at harness-readable paths such as
`/usr/local/libexec/pantheon-host-mcp-adapter` and
`/usr/local/libexec/pantheon-approval-mcp-adapter`. Seats do not run
`pip install` and do not own or modify the adapter bytes.

The shared persona installer generates one logical host-broker registration
from the manifest. Harness adapters translate it into:

- Codex `config.toml`: the packaged stdio command, startup/call timeouts, tool
  allowlist, required/optional startup posture, and per-tool approval
  presentation; or
- Claude MCP configuration: the same packaged stdio command, timeout, and
  harness-specific permission presentation.

These differences remain bridge/configuration surfaces under ORG-031. They do
not change tool names, schemas, server policy, actor identity, approval
requirements, or receipts.

Every registered seat receives the host adapter. Only a seat named by the
root-owned approver policy receives the approver adapter. Removing that
registration is not revocation by itself; D-Bus policy and the approval service
deny the seat after policy removal even if stale config or another same-uid
process launches the binary.

No seat config contains a broker token, D-Bus address override, Unix uid, bus
policy, or executor path. The adapter always uses the well-known system bus and
well-known broker name. Test-only bus overrides require an explicit test mode
that production packaging omits.

Client-side prompts are defense in depth. Pantheon's unattended harness modes
may suppress or pre-answer them, and vendor-specific approval metadata can
change. Therefore no privileged authorization depends solely on a Codex or
Claude permission dialog.

### 8. Capability envelope has a bootstrap slice and a useful privileged slice

The bootstrap slice exercises the already-designed persona installer operation
families:

- read-only host and user-manager diagnostics;
- installer-owned file materialization within a registered seat root;
- daemon reload, direct/unit fire, timer enable/observation, and bounded
  user-systemd inspection for that seat;
- mailbox send/receive round-trip probes against the `pantheon-mailbox`
  boundary; and
- verify and receipt-bound rollback.

That slice proves the protocol but is not sufficient to claim useful generic
bringup. The first production-useful envelope must also implement typed,
manifest-derived privileged actions for:

- Unix seat account ensure and non-destructive retirement;
- allowlisted group membership and mailbox group/directory creation;
- ownership, mode, and ACL convergence below registered installer-owned roots;
- systemd user-manager linger enable/disable for the registered seat, so its
  user services/timers may run without an interactive login;
- install/remove of root-owned, allowlisted systemd unit templates and D-Bus
  policy fragments;
- install/remove of an exact sudoers fragment selected by template id and
  manifest revision; and
- credential-reference and broker-policy binding without credential values.

The public API still accepts only the manifest revision and server-owned plan;
it never accepts a requested privileged action, username, group, path, unit
body, D-Bus policy body, sudoers text, command, or credential. The broker
resolves every value from root-owned registries/templates. The narrow root
helper receives a closed operation enum plus server-resolved identifiers.

`sudoers_fragment.ensure/remove` cannot install arbitrary `ALL` access. The
selected root-owned template enumerates the grantee, run-as identity, exact
executable and fixed argument policy, expiry/revocation metadata, and owning
ticket. The helper writes a root-owned mode-`0440` temporary drop-in, validates
the composed policy with `visudo -cf`, atomically replaces only its registered
fragment, verifies `sudo -l` evidence for the target seat, and stages the prior
bytes/hash for rollback. Unknown fragments, commands, paths, grantees, or
validation warnings fail closed.

Account retirement initially locks/disables the managed seat and its jobs; it
does not delete a home directory or mailbox. Package management,
firewall/network changes, storage, VM/container control, arbitrary system
files, policy documents, and git history remain outside the initial envelope.
Each future family requires a separately reviewed typed schema, helper
allowlist, approval class, receipt, and rollback evidence. There is never a
generic shell/sudo fallback.

The retired session-resurrection concept is out of scope. Mailbox access is a
communication substrate and is not coupled to session interrogation.

## Deployment structure

The logical components are:

```text
Requester harness
  `-- stdio Host MCP adapter
        `-- system D-Bus --> unprivileged HostBroker
                              | plan/operation/receipt journal
                              +--> per-seat executor (target uid)
                              `--> narrow root helper (typed families only)

plan handle + digest
  `-- mailbox/ticket --> approver harness
                          `-- stdio Approval MCP adapter
                                `-- system D-Bus --> HostApproval
                                                      | review/grant journal
Human Telegram callback --> dedicated ingress --------+

HostBroker -- private typed D-Bus ValidateAndBind --> HostApproval
```

MCP is harness-native presentation. System D-Bus is the authenticated
synchronous IPC substrate for adapters, broker, approval service, executors,
and helper; internal interfaces have distinct names and member-level bus
policy. The mailbox/ticket plane is asynchronous collaboration delivery: it
may carry a request, plan handle/digest, opaque approval handle, review
response, and completion notice, but it is neither the mutation transport nor
the authority store. Delayed, forged, duplicated, or replayed mailbox content
cannot change broker or approval state without independent D-Bus identity,
policy, exact binding, and idempotency checks.

The adapters, broker, approval authority, Telegram ingress, executors, and
helper use separate process roles. The broker, authority, ingress, and helper
use separate Unix identities, filesystem roots, D-Bus interfaces, and systemd
units. Root-owned unit/config files define bus policy, seat and approver
registries, executable paths, operation templates, and allowlists; the harness,
persona overlay, mailbox, and repository cannot edit them.

The system-bus service is Linux/systemd host substrate, not a cross-platform
harness feature. A non-systemd host fails the substrate capability gate and
requires a separate architecture adapter; it does not silently fall back to
shell, HTTP, or a user-owned socket.

### Bootstrap and self-amendment boundary

The broker cannot install the authority boundary that makes the broker safe.
Initial installation therefore remains a supervised root-run host-substrate
installer using the same plan/receipt discipline. It creates the dedicated
service accounts, installs pinned executables/runtimes, units, bus policies,
registries, root templates, journals, and initial approver/human-source policy,
then performs a red/green first fire. This one bootstrap action is explicit in
the receipt and is not relabeled as broker-mediated.

After bootstrap, typed operations may converge registered seat accounts,
seat-specific units, mailbox ACLs, and allowlisted policy fragments. They may
not modify the broker/approval/helper executables, their own systemd units or
bus ownership rules, the root operation/template registries, approver policy,
human allowlist, or the definition of their operation enums. Those
self-amendment surfaces require a new root-run reviewed installer plan. A
broker cannot authorize itself to recognize a wider operation.

## Acceptance gates for implementation

Implementation is not complete until all gates below are green.

### Protocol and client reality

1. A stable MCP SDK/Inspector validates modern `server/discover` and per-request
   version behavior, legacy `initialize` fallback, `tools/list`, every
   input/output schema, error mapping, and stdio lifecycle. Installed-client
   evidence records which era and exact revision each harness actually uses.
2. D-Bus introspection exposes exactly the public seven-method
   `HostBroker1.Manager`, four-method `HostApproval1.Reviewer`, and restricted
   internal interfaces/signatures enumerated in this ADR. No generic
   dictionary-input, command, transport, or passthrough method exists.
3. The real installed Codex client launches the packaged stdio adapter from its
   normal sandbox, lists exactly the expected tools, performs a read-only call,
   and exercises one broker-denied call.
4. The real installed Claude Code client performs the same sequence against
   the same broker. Server-observed `tools/list` schemas are
   semantically identical after canonical JSON normalization; client-side
   presentation differences are recorded but do not change the server
   contract.
5. A real configured approver seat launches the separate approver adapter and
   lists exactly four approval tools. A requester-only seat does not list them;
   launching the binary manually or calling reviewer D-Bus methods directly is
   denied.
6. With the broker or approval authority absent at harness startup, each
   corresponding adapter still initializes and returns its bounded unavailable
   error. After D-Bus activation or repair, the same live adapter succeeds
   without a harness restart.
7. Both packaged adapters import every runtime dependency without per-seat
   installation, and their executable/runtime bytes are not seat-writable.
8. Any harness, Telegram-provider, or startup/permission behavior not proven
   live remains unsupported; documentation evidence is not relabeled as
   deployment evidence.

### Security and authority

9. An unregistered Unix uid cannot call the broker or approval authority. For a
   registered caller, actor/seat/approver identity matches the bus-provided uid
   and root-owned registry even when environment, claimed persona, pid, cgroup,
   MCP metadata, and supplementary groups are adversarially changed.
   `host.capabilities` exposes only that subject's policy/host intersection and
   no other persona/approver/capability roster; direct `host.plan` remains
   valid without a capabilities call.
10. D-Bus policy denies unauthorized senders, members, and name ownership. A
    direct D-Bus call bypassing MCP receives the same service authorization;
    neither MCP registration nor bus policy alone is treated as the security
    perimeter.
11. A second process under the same registered uid is deliberately the same
    Unix trust domain. It cannot approve that uid's plan or apply/rollback
    without an independently issued exact grant.
12. Before returning plan effects, the approval service checks the
    bus-derived approver against minimal family/target metadata. Unauthorized
    reviewers learn neither detailed effects nor operational topology; the
    internal plan source returns no credentials, unbounded evidence, executor
    endpoint, or capability roster.
13. Agent/orchestrator approval is denied when approver equals or shares a uid
    with plan owner, performer, request-plan owner, or any participant in the
    same server-linked handoff. Reciprocal/circular and recipient-mismatched
    approvals are attacked and denied.
14. The Telegram path accepts only a provider-authenticated callback from the
    allowlisted human user/chat/message and matching single-use nonce.
    Bot-sent messages, copied ids, forwarded approval prose, wrong users/chats,
    edited/stale cards, and callback replays are denied. Its bot credential and
    update cursor are not available to any persona or agent-facing Telegram
    integration.
15. Plan, review, grant, operation, and receipt handles stolen across subjects
    are denied. A plan digest, mailbox message, ticket, role/persona name,
    attestation handle, or approval handle alone is insufficient authority.
16. `ValidateAndBind` refuses mismatched subject, performer, target, family,
    plan, digest, policy, expiry, revocation, nonce, and operation. Identical
    retry returns the original binding; every different tuple is denied.
17. No MCP or D-Bus caller can supply shell, argv, executable, arbitrary
    path/content, environment, sudo target, uid, group, unit/policy/sudoers
    text, root-helper operation, or credential value.
18. Host and approval MCP adapters have no network listener, credential,
    journal, executor/helper access, root privilege, mailbox privilege, or
    arbitrary D-Bus call surface. The broker cannot write approval state; the
    approval service cannot write broker state.
19. The unprivileged broker and approval service have no root or mailbox-group
    privilege. Each per-seat executor runs as the registry-selected target uid.
    The root helper accepts only compiled/configured operation enums and cannot
    write either journal.
20. Sudoers tests reject unknown templates, grantees, run-as identities,
    commands, argument policies, paths, `ALL` grants, bad modes/owners,
    validation warnings, and untracked existing fragments. Apply requires
    `visudo -cf`, atomic replacement, target `sudo -l` verification, receipt,
    and exact rollback.
21. Credentials, grant payloads, approval contents, callbacks, and unbounded
    evidence never appear in broadcasts, argv, generated units, adapter logs,
    mailbox payloads, tool output, or downstream requests.
22. Delayed, duplicated, forged, or replayed mailbox/ticket content can request
    review but cannot mint authority or duplicate an operation. The authoritative
    request, plan, grant, and operation states remain in their owning journals.
23. A cross-persona test traverses the dispatcher/mailbox plane, requires the
    recipient to create a plan under its own bus-derived identity, binds the
    broker-attested request-plan owner, performer, both plans/digests, shared
    effect digest, and handoff anchor, and preserves the human gate for
    privileged families.

### Lifecycle and failure

24. Duplicate identical broker and approval idempotency keys return the same
    plan/review/decision/operation; conflicting reuse is refused.
25. Approval review and grant transitions implement exactly
    `OPEN → GRANTED|DENIED|EXPIRED` and
    `ISSUED → BOUND|REVOKED|EXPIRED`. Denied, revoked, expired, and bound grants
    never become reusable.
26. Fault injection covers broker `PREPARED` with no bind, authority `BOUND`
    with broker `PREPARED`, broker `AUTHORIZED` without dispatch intent,
    `DISPATCH_INTENT` without result, and executor mutation without result.
    Each state resumes the identical operation or terminates in a loud,
    non-reusable manual-recovery state as specified.
27. Concurrent revoke/bind proves a single serialized winner. Revocation before
    bind prevents authorization; bind before revocation is the commit and
    remains consumed. A different operation can never acquire it.
28. Approval-authority outage fails closed for new/unbound work. Already
    durable `AUTHORIZED` work can reconcile from the broker journal; cached
    `ISSUED` state cannot authorize anything.
29. MCP-adapter exit, D-Bus disconnect/reconnect, unique-name change,
    disconnect-after-submit, service restart, helper timeout, verification
    failure, and provider retry each have a deterministic query/recovery result.
30. The broker's staged receipt, rollback intent, and `DISPATCH_INTENT` are
    durably observable before the first activation-class mutation. Faults after
    dispatch, executor mutation, and before result append prove restore or loud
    refusal.
31. Cancellation of client waiting does not cancel, revoke, reuse, or duplicate
    the approval or host operation.

### Privileged bringup slice

32. A disposable host/VM converges and rolls back account, allowlisted group,
    mailbox directory/ACL, owner/mode/ACL, linger, system-unit, and D-Bus-policy
    actions exclusively from a registered manifest revision.
33. The same disposable host applies, verifies, revokes, and rolls back one
    exact human-approved sudoers fragment. Before/after `visudo -cf`, target
    `sudo -l`, owner/mode/hash, and negative-command evidence are retained.
34. Account retirement disables the seat and jobs without deleting its home or
    mailbox; destructive deletion remains unsupported.
35. The supervised root bootstrap installs every service account, executable,
    unit, bus policy, registry, journal root, approver policy, and human
    allowlist with a complete receipt; an identical rerun is idempotent.
36. Broker plans attempting to modify broker/approval/helper bytes or units,
    bus ownership rules, root registries/templates, approver policy, human
    allowlists, or operation-enum definitions are refused.

### Real path

37. One disposable seat clears MCP-to-D-Bus plan/review/agent-approval/apply/
    verify/rollback through the real broker, approval authority, target-uid
    executor, and user-systemd manager.
38. One human-gated disposable operation clears the real Telegram card and
    callback path. Agent-forwarded approval text fails red before the real
    allowlisted human callback succeeds green.
39. A sandbox regression proves both adapters reach the system bus and the
    executor reaches the target user manager without relying on the
    `systemctl --user` private-socket path that failed in the founding incident.
40. The exact T-2381 Recovery/Codex bringup path clears a supervised live proof:
    unit fire succeeds, mailbox send/receive is correlated, a finite timer fire
    is observed, and the mailbox effect is attributable to that invocation.
41. Every required guard records a red falsification and restored green run,
    including bus-name takeover, direct-D-Bus bypass, review-data probing,
    self/circular approval, provider spoof/replay, handle theft, digest replay,
    alternate-path input, helper-enum abuse, and disconnect/retry races.

Production enablement remains a separate per-capability
supervised-first-fire decision after these gates. Passing simulator tests alone
is insufficient.

## Pre-mortem

- **The broker becomes remote sudo.** Failure: a generic command/path field
  slips into a “convenience” tool. Mitigation: closed operation enums,
  server-side plan regeneration, privilege split, and explicit forbidden-field
  tests.
- **A prompt self-approves its own plan.** Failure: the server trusts a digest
  echoed by the same caller or mistakes a role-scoped tool for authority.
  Mitigation: separate approval service and journal; bus-derived approver;
  server-side owner/performer/requester/chain exclusion; exact digest binding.
- **Approval commits but the broker crashes.** Failure: a single-use grant is
  lost, reused, or dispatched without a durable operation. Mitigation:
  `PREPARED → BOUND → AUTHORIZED → DISPATCH_INTENT`, identical-tuple recovery,
  serialized revoke/bind, and no transition out of `BOUND`.
- **Forwarded Telegram prose becomes human authority.** Failure: an agent
  copies “approved” or a message id into a request. Mitigation: dedicated
  ingress verifies provider user/chat/message plus a pending single-use
  callback nonce; agent text is never an authority source.
- **Mailbox delivery is mistaken for authorization.** Failure: a forged or
  replayed mailbox file carries a plan or approval handle. Mitigation: mailbox
  is asynchronous request delivery only; D-Bus identity, server-owned records,
  exact binding, and idempotency remain authoritative.
- **Bus policy is mistaken for full authorization.** Failure: an allowed uid
  calls a method against another seat or reuses a stolen handle. Mitigation:
  bus-derived identity plus broker-side target, operation, handle, and approval
  checks on every call.
- **Unix uid is mistaken for process identity.** Failure: design prose claims a
  specific harness or model is authenticated when D-Bus proves only the seat
  account. Mitigation: state the same-uid trust boundary explicitly and require
  external approval for mutation; add a distinct credential profile only if
  process identity becomes a real requirement.
- **A disconnect repeats a mutation.** Failure: the harness retries after a
  timeout while the first call still runs. Mitigation: durable operation state
  and strict idempotency keys; adapter cancellation never cancels an accepted
  broker operation.
- **The unprivileged broker is cosmetic.** Failure: it can still invoke a root
  helper with arbitrary parameters. Mitigation: helper-owned allowlist and
  schema, separate peer authorization, and negative tests against internal IPC.
- **A sudoers template becomes remote root.** Failure: a caller supplies text,
  `ALL`, a path, or a new command through a “template” field. Mitigation:
  root-owned registered template ids, fixed command/argument policy,
  `visudo -cf`, atomic registered-fragment replacement, negative-command
  proof, and exact rollback.
- **The adapter disappears at session start.** Failure: a missing dependency or
  temporarily absent broker leaves a live agent with no tools until restart.
  Mitigation: root-owned packaged runtime, adapter-first health smoke, stable
  tool listing while the broker is absent, D-Bus activation, and in-process
  reconnect.
- **Harness parity is claimed from config generation.** Failure: one client
  ignores schema, stdio lifecycle, reconnection, or permission semantics.
  Mitigation: same live-server sequence on both real clients before enablement.
- **A green simulator hides the original bus/group problem.** Failure: the
  production user manager remains unreachable. Mitigation: real target-uid
  executor, direct user-manager D-Bus calls, and supervised live T-2381
  evidence.

## Alternatives considered

### Lift or bypass every harness sandbox

Rejected. It broadens every tool rather than granting one narrow capability,
erases an existing defense boundary, and still leaves harness-specific
bringup.

### A privileged stdio MCP server per harness

Rejected. Stdio is launched as a harness child and inherits its sandbox. Giving
that child sudo recreates a generic escalation path and duplicates config per
harness.

### Streamable HTTP MCP end to end

Rejected for the initial same-host substrate. It is harness-native but adds a
listener, TLS/certificate bootstrap, OAuth or bearer lifecycle, network-origin
controls, and a weaker identity signal than the already-reachable system bus.
A future remote-harness adapter may use Streamable HTTP, but its authentication
and exposure model requires a separate architecture decision. It would still
call the same host broker rather than replacing its policy or durable state.

### A generic shell or sudo MCP tool

Rejected unconditionally. There is no safe input schema for arbitrary host
commands exposed to a prompt-driven caller.

### Custom Unix RPC or Varlink

Viable but deferred. A pathname Unix socket with peer credentials and a closed
schema, or a Varlink interface, can satisfy the local transport requirement.
Both require Pantheon to own more discovery, activation, policy, framing,
client-runtime, and name-takeover behavior than the existing system bus. The
current host already supplies D-Bus, Python bindings, policy, introspection, and
systemd activation, and the live sandbox reaches it.

ORG-026 rejected Unix sockets for the *asynchronous durable mailbox* because
both ends must be live and the socket has no store-and-forward semantics. This
ADR uses synchronous RPC only to submit or query operations; the broker's
durable journal, not D-Bus, owns retry and recovery. The two decisions do not
conflict.

### gRPC over a Unix socket

Rejected for the initial host. It offers code-generated schemas and Unix-socket
transport, but adds an absent per-seat/runtime dependency and HTTP/2 stack.
Current local-credential APIs are not a stronger, already-proven identity and
policy surface than the system bus. The expected Pantheon call volume does not
justify that operational weight.

### Direct D-Bus commands from the agent

Rejected as the agent surface. D-Bus is appropriate host IPC, but asking a
model to construct `busctl` calls would be harness-specific, non-discoverable,
and easy to misuse. The MCP adapter gives agents fixed names, schemas, bounded
errors, and native tool invocation while keeping D-Bus invisible.

### Continue with operator-run scripts only

Safe but rejected as the target architecture. It preserves fragmented bringup,
prevents harness-neutral recovery, and cannot produce a common typed receipt
surface. Operator scripts remain the escape hatch while the broker is absent
or unhealthy.

## Consequences

Positive:

- a sandboxed harness can operate the same typed host lifecycle without gaining
  general host access;
- persona bringup and recovery use one agent contract and evidence shape;
- authentication, approval commitments, and receipts are enforced outside
  requester model context;
- approver personas and the operator can inspect exact plans and approve through
  independently authenticated surfaces without sharing requester credentials;
- the mailbox remains useful for asynchronous collaboration without becoming
  a privilege channel;
- typed account/group/ACL/linger/unit/sudoers operations make the system useful
  for real bringup rather than diagnostics alone;
- local seat identity comes from the OS and root-owned registry, without a new
  per-seat bearer secret;
- a new MCP-capable harness needs only stdio registration, not new privileged
  scripts;
- the original sandbox remains useful even after host actuation exists.

Costs and retained tensions:

- the broker, approval authority, Telegram ingress, and narrow root helper
  become security-critical and need independent supervision, D-Bus policy,
  separate durable state, and adversarial tests;
- D-Bus proves the Unix seat, not the individual harness process. That matches
  the current one-seat-per-account posture but is intentionally not advertised
  as stronger process identity;
- the host substrate is Linux/systemd-specific. Harness neutrality does not
  silently imply host-OS neutrality;
- the packaged MCP adapters become shared executable/runtime infrastructure and
  must survive service outages without disappearing from a live session;
- a narrow helper intentionally leaves some recovery actions manual. Coverage
  grows only through separately reviewed operation types, not generic escape
  hatches.

## Ratification surface

This ratified decision accepts these architecture calls:

1. native stdio MCP is the uniform harness-facing presentation for the host and
   approver tool sets, while system D-Bus is the independently supervised local
   host-control substrate;
2. both MCP adapters are privilege-free, stateless, packaged once by the host
   installer, role-configured, and reconnect without an agent restart;
3. the host broker and approval authority are separate unprivileged services
   with separate journals; execution is split into typed per-seat and narrow
   root helpers;
4. D-Bus credentials plus the root-owned seat registry derive Unix-seat
   identity; same-uid processes share that trust domain, while exact external
   approval, subject-bound handles, durable idempotency, and staged receipts
   remain mandatory; the broker bridges already-granted policy authority across
   a Unix/harness boundary and never creates policy authority;
5. the public surface is the closed seven-tool host contract plus the distinct
   four-tool approver contract; all broker/authority/ingress internal methods
   are the fixed restricted interfaces enumerated above;
6. approval grants use the exact durable review/issue/bind/revoke state machine,
   self/circular approval is denied, and human authority comes from a
   provider-authenticated Telegram callback rather than forwarded agent text;
7. the capability envelope includes both the existing installer operations and
   typed manifest-derived account/group/ACL/linger/system-unit/D-Bus-policy/
   sudoers/credential-reference bringup families;
8. no generic shell/sudo/path/content/dictionary passthrough surface or
   caller-supplied privileged value will be added;
9. the mailbox/ticket plane carries asynchronous work and opaque handles but is
   not the mutation transport, authority source, or durable operation store;
10. the initial host substrate requires Linux, systemd, and the system bus;
11. a supervised root installer bootstraps and upgrades the authority boundary;
    the broker cannot modify its own executables, units, bus rules, registries,
    templates, approver policy, human allowlist, or operation definitions;
12. ratification authorizes planning and implementation work, not production
   enablement or any specific new account/access grant; and
13. cross-persona work links a broker-owned request plan to a matching new plan
    under the recipient's own identity and policy; mailbox delivery never
    transfers credentials, identity, or capability.

### Provisional wire names (pending ratification)

Ratification covers the closed, versioned namespace and the internal
signatures above, but not the concrete strings implementation has minted
inside them. These are PROVISIONAL until ratified together. The canonical
table, with the minting ticket for each name, is "Provisional names pending
ratification" in `tools/host_control/README.md`.

- the `org.pantheon.HostBroker1.Error.*` broker error names (T-2912 OQ4),
  plus `…Error.RateLimited` (T-2917 5a);
- the host MCP adapter error codes (T-2915), including `broker_rate_limited`;
- `org.pantheon.HostBroker1.ExecutionSource` and the
  `pantheon-host-execution-spec/v1` schema;
- the per-seat executor bus-name scheme `org.pantheon.HostExecutor1.U<uid>`,
  the `org.pantheon.HostExecutor1.Executor` interface, the
  `org.pantheon.HostExecutor1.Error.*` names, and the
  `pantheon-host-execution-result/v1` and `pantheon-host-executor-config/v1`
  schemas, including the `mailbox_hub` key and its `@MAILBOX_HUB@` unit
  substitution (the config path and entrypoint are owned by T-2920);
- the T-2917 5b-5d execution vocabulary:
  - verify sentinels and seat-local residue ids;
  - execution states (`refused`) and kinds (`reconcile`);
  - journal events (`spec_rearmed`, `execute_retry`, `redispatch_intent`,
    `execute_voided`, `reconcile_probe`);
  - reconciler detail codes and the rollback restore intent;
- the `operation_driver` leg of the `pantheon-health/v1` broker roster
  (T-2917 5d.4).
- the automatic apply-compensation vocabulary (T-4964): the
  `broker:apply-compensation` approval sentinel, the reserved
  `broker compensation <handle>` idempotency namespace, the
  `compensation_authorized` / `compensation_refused` journal events,
  `compensation_error`, and the OperationGet
  `compensation_operation_handle` field.

## DECISIONS

- **Keep the harness sandbox and move only typed host actuation outside it** —
  justifies-via P1 and ORG-031. The boundary becomes architectural instead of a
  per-harness exception.
- **Bridge Unix execution capability only after policy authority already
  exists** — justifies-via P1, P5, and ORG-050. Host actuation cannot become a
  path for discovering, borrowing, or manufacturing another persona's
  authority.
- **Use native stdio MCP only as a privilege-free agent adapter** —
  justifies-via D11, P1, and ORG-031. This maximizes harness-native discovery
  without asking the sandboxed child to perform host operations.
- **Use the existing system D-Bus as the initial host substrate** —
  justifies-via D11 and P6. Its reachability from the real sandbox was
  live-proven; its Unix authentication, default-deny policy, activation, and
  introspection avoid a bespoke protocol and per-seat network credential.
- **Split unprivileged protocol handling from privileged execution** —
  justifies-via P1 and ORG-050. A parser exposed to model-driven input does not
  share root authority.
- **Derive identity and authority server-side** — justifies-via P4, P5,
  ORG-040, ORG-050, and ORG-051. Caller assertions and model context cannot
  manufacture provenance or permission.
- **Name the same-uid trust boundary instead of claiming process identity** —
  justifies-via P4, P6, and ORG-050. D-Bus proves the Unix account; stronger
  identity requires a separate credential decision.
- **Bind approvals to server-owned plans on a separate approver-only service** —
  justifies-via P1, P3, P5, ORG-040, and ORG-050. Requesting and granting
  authority remain different identities, tools, journals, and policies.
- **Treat bind as the durable single-use authorization commit** —
  justifies-via P1, P4, and ORG-049. Crash recovery cannot silently reuse or
  lose authority between the approval and operation journals.
- **Use provider-authenticated Telegram callbacks for normal human-gated
  decisions** — justifies-via P1, P4, and ORG-040. A model can relay prose but
  cannot manufacture the operator's provider identity and matching callback nonce.
- **Model cross-persona work as a provenance-preserving handoff, not
  impersonation** — justifies-via P1, P4, P5, ORG-040, and ORG-050. The
  recipient remains independently accountable and no model-controlled field
  manufactures another persona's authority.
- **Keep mailbox/tickets as asynchronous delivery rather than authority or
  actuation** — justifies-via P1, P4, ORG-026, and ORG-050. Communication
  context remains useful without turning shared files into privileged RPC.
- **Make durable operation handles and idempotency independent of MCP
  sessions and D-Bus connections** — justifies-via P4, ORG-049, and the
  deliberate cancellation boundary.
- **Include typed privileged bringup families, including exact sudoers
  fragments, in the first production-useful envelope** — justifies-via P1, P5,
  and D7. The system is useful for actual bringup while every privileged value
  remains server-resolved, human-gated, receipt-bound, and reversible.
- **Keep bootstrap and authority-boundary upgrades outside the broker** —
  justifies-via P1, P4, and P5. A service cannot widen the registries, policies,
  templates, or operation definitions that constrain its own authority.
- **Require real-client, real-user-manager, and red/green falsification gates**
  — justifies-via D5, D11, and the testing-depth SOP. Configuration parity and
  simulator green are necessary but not sufficient evidence.
- **Preserve the Unix-seat-versus-process-identity,
  host-portability-versus-native-policy, and automation-versus-approval
  tensions** — justifies-via P6. The decision names the current balance and the
  evidence required to rebalance it.
