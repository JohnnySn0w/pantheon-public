---
symptom: "In Car Thing CrKey chromium, browser ws:// or fetch() to a non-loopback host fail (opaque ws onerror / 'Failed to fetch') even though shell wget/nc to the same IP:port succeed on the device"
fix: "Don't use direct browser networking off-loopback; route via client.net.wsOpen / client.net.fetch to destinations reachable from the connected phone's network stack (device-loopback ws is fine)"
domain: "carthing / chromium / crkey / networking / bridgething"
---

# Car Thing CrKey chromium blocks non-loopback networking outside `client.net`

## Symptom

A bridgething webapp opens `new WebSocket('ws://<lan-ip>:port')` or `fetch('http://<lan-ip>:port/…')` directly from JS and both fail — `onerror` fires with an opaque Event on the WS, `fetch` throws `TypeError: Failed to fetch`. The exact same IP:port is reachable from shell tools on the same device (`wget`, `nc` — a plain TCP path exists), and the destination *is* listening. There is no CSP header on the page and no CSP meta tag; DevTools console (via CDP) shows no explicit block message.

Verified 2026-09-14 in the T-3712 mic-publisher B5 arc, kiosk chromium version Chrome/147.0.0.0 Safari/537.36 **CrKey/1.56.500000** on Spotify Car Thing:

```
{"csp_meta":null,"fetch_err":"Failed to fetch","ws_error":"err","ws_errored":true}
```

Chromium runs in the same net namespace as pid 1 on the device (`ls -l /proc/<chromium-pid>/ns/net` matches `/proc/1/ns/net`), and the routing table shows the destination is on-link (`ip route` → `10.42.1.144/29 dev uncm0`). Shell wget from the same device to the same IP:port returned `HTTP/1.1 426 Upgrade Required` from the target websocket server, proving the TCP path is open. Chromium still refuses.

## Root cause

The CrKey chromium build is Chromecast-derived and its network stack refuses outbound connections outside a small allowlist (loopback + the client.net tunnel it exposes via the bridgething daemon). The `@bridgething/client` SDK documents `client.net` as tunnelling "HTTP, WS, and SOCKS through the phone" for a reason: it's the *only* path the CrKey network policy permits. There is no browser-visible error surface for the refusal — the request just errors out through the normal WS/fetch failure paths.

## Fix

Do not use direct browser networking APIs from a bridgething webapp for anything off-loopback. Route WebSockets through `client.net.wsOpen`, HTTP through `client.net.fetch`, and pick destinations that are **reachable from the connected phone's network stack**, not from the device. For a VPS destination, that means the phone must have a working route to it (tailnet on the phone, or a public URL over the phone's internet path). Layering a local proxy on the connected laptop's USB-NCM address does not work: the phone can't see the kiosk's USB link.

If the webapp legitimately needs device-loopback WS (talking to the daemon at `ws://127.0.0.1:...`), that works fine — the block is specifically non-loopback.

## Verification

From a running bridgething webapp on device, via `chrome://inspect` or CDP:

```js
new WebSocket('ws://<any-non-loopback-lan-ip>:<open-port>').onerror = e =>
    console.log('ws blocked:', e);
fetch('http://<any-non-loopback-lan-ip>:<open-port>/').catch(e =>
    console.log('fetch blocked:', e.message));
```

Both log the block within ~1 s despite the destination being reachable at the TCP layer from the device shell.
