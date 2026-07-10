---
symptom: "wiremock-mocked tests against `rmcp` (the Rust MCP SDK) hang for ~60s before failing — the test scaffolding's response IDs don't match `rmcp`'s sequential `AtomicU32RequestIdProvider` allocation, so the client's response-future is never resolved"
fix: "match the wiremock response `id` field to rmcp's actual sequential allocation: initialize=0 (consumed by `serve_client`), then sequential per `Peer` call (list_all_tools=1, call_tool=2, …); use a `body_partial_json` matcher to route by JSON-RPC method name and a per-method response that hard-codes the correct id"
domain: rust, rmcp, mcp, wiremock, jsonrpc
---

## Symptom

Test scaffolding for an MCP-client adapter using `rmcp` v1.5/1.6 with wiremock returns 10s-60s hangs on assertions, eventually failing with timeout. Logs show the wiremock server received the request and returned the response, but the client's await on `client.list_all_tools()` or `client.call_tool(...)` never resolves.

Common patterns that trigger:

```rust
// Wiremock setup — looks correct
Mock::given(method("POST"))
    .and(path("/api/mcp"))
    .and(body_partial_json(json!({"method": "tools/list"})))
    .respond_with(ResponseTemplate::new(200).set_body_json(json!({
        "jsonrpc": "2.0",
        "id": 1,                      // ← will not match rmcp's allocator state
        "result": {"tools": [...]}
    })))
    .mount(&mock_server)
    .await;

// Test
let transport = StreamableHttpClientTransport::from_config(config);
let client = serve_client((), transport).await?;
let tools = client.list_all_tools(None).await?;  // ← hangs ~60s, eventually times out
```

The hang is silent — no error message during the wait; just a stalled future.

## Cause

`rmcp` uses a single `AtomicU32RequestIdProvider` shared across the entire client session. The provider hands out IDs sequentially, starting at 0:

- `serve_client(handler, transport).await` consumes id=**0** for the MCP `initialize` handshake.
- The first `client.peer().list_all_tools(...)` call consumes id=**1**.
- The next `client.peer().call_tool(...)` call consumes id=**2**.
- And so on, sequentially.

The client matches incoming responses by id field. If wiremock returns `id=1` for the initialize handshake's response and `id=1` again for the list_all_tools response (or any other mismatch), the client either picks the wrong response or has no matching pending-future, and the actual list_all_tools future never resolves. rmcp doesn't error on the mismatch — it just drops unmatched responses, and the client awaits indefinitely until its internal timeout fires.

The 60s hang is rmcp's default request timeout. Tests using `tokio::time::timeout(...)` shorter than that hide the issue but still fail.

## Fix

Match wiremock response `id` fields to rmcp's actual sequential allocation. Pattern:

```rust
// Mock 1 — initialize (id=0)
Mock::given(method("POST"))
    .and(path("/api/mcp"))
    .and(body_partial_json(json!({"method": "initialize"})))
    .respond_with(ResponseTemplate::new(200).set_body_json(json!({
        "jsonrpc": "2.0",
        "id": 0,                                          // ← initialize is id=0
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-mock", "version": "0.0.0"}
        }
    })))
    .mount(&mock_server)
    .await;

// Mock 2 — tools/list (id=1, first Peer call)
Mock::given(method("POST"))
    .and(path("/api/mcp"))
    .and(body_partial_json(json!({"method": "tools/list"})))
    .respond_with(ResponseTemplate::new(200).set_body_json(json!({
        "jsonrpc": "2.0",
        "id": 1,                                          // ← first Peer call after init
        "result": {"tools": [{"name": "HassTurnOn", "inputSchema": {...}}]}
    })))
    .mount(&mock_server)
    .await;

// Mock 3 — tools/call (id=2, second Peer call)
Mock::given(method("POST"))
    .and(path("/api/mcp"))
    .and(body_partial_json(json!({"method": "tools/call"})))
    .respond_with(ResponseTemplate::new(200).set_body_json(json!({
        "jsonrpc": "2.0",
        "id": 2,                                          // ← second Peer call
        "result": {"content": [{"type": "text", "text": "..."}], "isError": false}
    })))
    .mount(&mock_server)
    .await;
```

For tests that exercise multiple Peer calls in a row, increment id sequentially per call.

The `body_partial_json(...)` matcher routes by method name; the hard-coded `id` field in each response matches rmcp's sequential allocator state for that test's call sequence. Tests that re-create the rmcp client (e.g., new `serve_client(...)` per test) reset the allocator to 0, so init=0 is correct again per test.

## Discovered

- Echo `agent/echo-adapter-ha-mcp` cycle 2026-05-08 (T-0137 implementer dispatch). Implementer's initial test scaffolding had `id=1` for the initialize-handshake response; client hung 60s on every test before failing. Diagnosed by tracing rmcp's request-id allocation through `serve_client` source — flagged as DECISIONS entry #9 in close report.
- echo-prismatic commit b6cad4c — `crates/echo-adapter-ha-mcp/tests/adapter_tests.rs` uses the per-method-routed wiremock pattern with hard-coded sequential ids.

## Pairs with

- `bugs/rust-blanket-from-for-boxerror-conflicts-with-explicit-impl.md` — adjacent Rust mechanics gotcha (BoxError conversions); same flavor of "library detail not surface-documented."
- `bugs/serde-untagged-enum-without-message-discriminant.md` — adjacent Rust mechanics gotcha (serde discriminator quirk); both are "vendor-protocol-shape pitfalls" worth pre-loading into briefs.

## Brief-quality lesson

When dispatching an MCP-adapter (or any rmcp-based) implementer cycle, the brief should call out:

> rmcp uses a single `AtomicU32RequestIdProvider` per client session, allocating ids sequentially from 0. `initialize` is id=0; subsequent `Peer` calls are id=1, id=2, etc. Wiremock test response `id` fields must match this allocation OR the client's response-future never resolves and tests hang ~60s. Match per-method via `body_partial_json` + hard-code the correct sequential id per response.

This single sentence in the brief would have saved the 60-second hang on initial test scaffolding and avoided the diagnostic round-trip during the implementer cycle.

## Keywords

rust, rmcp, model context protocol, MCP, wiremock, JSON-RPC, request id, AtomicU32RequestIdProvider, sequential id allocation, initialize handshake, Peer call, list_all_tools, call_tool, response matching, hang, timeout, body_partial_json, response routing, vendor protocol mock, serve_client
