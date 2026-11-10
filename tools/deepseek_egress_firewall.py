"""Dependency-free rendering contract for the DeepSeek UID firewall blocks."""

from __future__ import annotations

from tools.deepseek_egress_proxy import ProxyPolicy


BEGIN = "# P-DEEPSEEK-EGRESS-BEGIN"
END = "# P-DEEPSEEK-EGRESS-END"


def render_firewall(policy: ProxyPolicy, *, seat_uid: int, proxy_uid: int) -> dict[str, str]:
    port = policy.local_port
    v4 = [
        BEGIN,
        f"-A ufw-before-output -m owner --uid-owner {seat_uid} -o lo -p tcp -d 127.0.0.1/32 --dport {port} -j ACCEPT",
        f"-A ufw-before-output -o lo -p tcp -d 127.0.0.1/32 --dport {port} -j REJECT",
        f"-A ufw-before-output -m owner --uid-owner {seat_uid} -j REJECT",
        f"-A ufw-before-output -m owner --uid-owner {proxy_uid} -o lo -p udp -d 127.0.0.53/32 --dport 53 -j ACCEPT",
        f"-A ufw-before-output -m owner --uid-owner {proxy_uid} -o lo -p tcp -d 127.0.0.53/32 --dport 53 -j ACCEPT",
        f"-A ufw-before-output -m owner --uid-owner {proxy_uid} -p tcp --dport 443 -j ACCEPT",
        f"-A ufw-before-output -m owner --uid-owner {proxy_uid} -j REJECT",
        END,
    ]
    v6 = [
        BEGIN,
        f"-A ufw6-before-output -m owner --uid-owner {seat_uid} -o lo -p tcp -d ::1/128 --dport {port} -j ACCEPT",
        f"-A ufw6-before-output -o lo -p tcp -d ::1/128 --dport {port} -j REJECT",
        f"-A ufw6-before-output -m owner --uid-owner {seat_uid} -j REJECT",
        f"-A ufw6-before-output -m owner --uid-owner {proxy_uid} -p tcp --dport 443 -j ACCEPT",
        f"-A ufw6-before-output -m owner --uid-owner {proxy_uid} -j REJECT",
        END,
    ]
    return {"v4": "\n".join(v4) + "\n", "v6": "\n".join(v6) + "\n"}
