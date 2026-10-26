---
symptom: "one hostname behind Cloudflare is dead — TLS handshake_failure at the edge with ZERO certs presented ('no peer certificate available') — while its sibling hostnames on the SAME origin work; easy to misattribute to an origin/infra outage (a de-cluster, a container teardown, a dead tunnel) when the origin is actually 100% healthy"
fix: "compare the DOWN host's Cloudflare `proxied` flag against its working siblings via the CF API; the down one is almost always the lone proxied=True record with a broken/premature edge. Flip it to DNS-only (proxied=False) to match the working siblings — resolves straight to the origin's own cert. Reversible one-toggle. (Re-proxying properly is a separate CF-SSL task.)"
domain: "pantheon, cloudflare, dns, tls, forge, home-infra, misattribution"
---

## Symptom

A single public hostname (here `git.your-self-hosted-git.example`, the self-hosted Forgejo primary) is unreachable for ~20h:

- `curl https://host/` → `curl: (35) sslv3 alert handshake failure`, HTTP 000.
- `openssl s_client` → **0 certificates**, "no peer certificate available." TCP connects (edge is up) but TLS never completes.
- DNS resolves to Cloudflare anycast IPs (`2606:4700::…`, `104.21.x`/`172.67.x`) — i.e. the record is **proxied** (orange cloud).
- The outage correlated in time with a big infra change (a Proxmox de-cluster the night before), so the handoff attributed it to "the de-cluster tore down the CT/tunnel."

The trap: the symptom and the timing both *point at* an origin/infra failure, so the diagnosis inherits "origin is down" without testing it.

## Cause

The origin was never down. The failure is entirely at the **Cloudflare edge for that one hostname**:

- The container (Forgejo CT200) was running and serving the whole time.
- The reverse proxy (caddy) was healthy, had a valid Let's Encrypt cert for the host, reached the origin (`303`), and was **serving the host's SIBLINGS from live public internet traffic throughout the "outage."** No `cloudflared` tunnel existed anywhere — the front door is caddy behind a plain CF-proxied A record, not a tunnel.
- In Cloudflare DNS, **every** `*.your-domain.example` host pointed at the same origin IP and was **DNS-only (grey)** — *except* the dead one, which was the lone **proxied=True (orange)** record. Cloudflare's proxy edge was failing TLS for it (premature/half-configured proxying: the "properly proxy git.\*" work was still deferred, "needs caddy DNS-01 ACME first"). An origin-down record behind CF serves a CF **52x** page over CF's OWN working cert; getting handshake_failure with **no cert** means CF has no working edge routing/cert for that hostname.

The single falsifying check that would have skipped the whole infra red-herring: **`--resolve host:443:<origin-ip>` and hit the origin directly.** A `303 + valid cert` there proves the origin is fine and the fault is CF-edge-only.

## Fix

Compare the proxied flag of the down host against its working siblings, then flip the down one to DNS-only:

```bash
# List records + proxied flags (Recovery's DNS-scoped CF token, T-2014; reference by name, never echo):
TOK=$(cat ~/cloud.pass | tr -d '\n' | grep -oE '[A-Za-z0-9_-]{30,}' | head -1)
ZID=<zone-id>   # curl .../zones?name=<zone>
curl -sS -H "Authorization: Bearer $TOK" \
  "https://api.cloudflare.com/client/v4/zones/$ZID/dns_records?per_page=100" \
  | python3 -c "import sys,json;[print(r['name'],r['type'],r['content'],'proxied=',r['proxied']) for r in json.load(sys.stdin)['result'] if r['type'] in('A','AAAA','CNAME')]"

# Flip the lone proxied record to DNS-only (matches the working siblings):
RID=$(curl -sS -H "Authorization: Bearer $TOK" \
  "https://api.cloudflare.com/client/v4/zones/$ZID/dns_records?name=<host>&type=A" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['result'][0]['id'])")
curl -sS -X PATCH -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  "https://api.cloudflare.com/client/v4/zones/$ZID/dns_records/$RID" --data '{"proxied":false}'
```

Verify with a fresh public resolver (not the local box, which caches the stale CF IPs for the TTL):

```bash
FRESH=$(dig +short <host> @1.1.1.1 A | head -1)
curl -sS -o /dev/null -w "HTTP %{http_code} ssl_verify=%{ssl_verify_result}\n" \
  --resolve <host>:443:$FRESH https://<host>/          # want: 303 ssl_verify=0
```

Notes:
- **Reversible**: one PATCH back to `{"proxied":true}` restores the prior state.
- **Exposure-neutral** when the siblings already expose the same origin IP — flipping to grey reveals nothing new.
- The DNS-scoped token **cannot** read/fix CF edge SSL (Universal SSL / cert packs / SSL mode return `9109 Unauthorized`); re-proxying *properly* is a dashboard/broader-token task — fix the edge cert FIRST, then flip back to proxied, or the outage repeats.
- Draining the local stub-resolver cache is optional; external clients resolving fresh recover immediately.
