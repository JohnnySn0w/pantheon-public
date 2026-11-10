"""Tests for tools.tool_schema_budget.

Run:
    python3 -m unittest tools.test_tool_schema_budget -v
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools import tool_schema_budget as tsb


class ToolSchemaBudgetTests(unittest.TestCase):
    def test_tier_thresholds_are_strictly_greater_than_named_cutoffs(self) -> None:
        self.assertEqual(tsb.tier_for(25_000), "<=25K")
        self.assertEqual(tsb.tier_for(25_001), ">25K")
        self.assertEqual(tsb.tier_for(50_001), ">50K")
        self.assertEqual(tsb.tier_for(100_001), ">100K")

    def test_collects_claude_profile_servers_from_manifest(self) -> None:
        manifest = {
            "persona_installer": {
                "claude_mcp_servers": {
                    "personas": {
                        "specialist": {
                            "servers": {
                                "mailbox": {
                                    "command": "python3",
                                    "args": ["tools/mailbox_mcp.py"],
                                    "env": {},
                                }
                            }
                        }
                    }
                }
            },
            "harness_runtime": {
                "harnesses": {
                    "claude": {"default_profile": "interactive"},
                },
                "roles": {
                    "specialist": {
                        "claude": {
                            "model": "claude-opus-5-5",
                            "native": {"channels": "plugin:telegram@claude-plugins-official"},
                        }
                    }
                },
            },
            "roles": {"specialist": {"persona": "specialist", "account": "claude-research"}},
        }
        [(profile, specs)] = list(tsb.iter_profile_specs(manifest))
        self.assertEqual(profile.role, "specialist")
        self.assertEqual(profile.persona, "specialist")
        self.assertEqual(profile.harness, "claude")
        self.assertEqual(profile.profile, "interactive")
        self.assertEqual(profile.model, "claude-opus-5-5")
        self.assertEqual([spec.name for spec in specs], ["mailbox"])
        self.assertEqual(profile.unmeasured_surfaces, ("claude native channels: plugin:telegram@claude-plugins-official",))

    def test_probe_stdio_tools_and_measure_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "fake_mcp.py"
            server.write_text(
                textwrap.dedent(
                    """
                    import json, sys

                    for line in sys.stdin:
                        msg = json.loads(line)
                        method = msg.get("method")
                        if method == "initialize":
                            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}}), flush=True)
                        elif method == "notifications/initialized":
                            continue
                        elif method == "tools/list":
                            tool = {
                                "name": "fake_tool",
                                "description": "A fake tool for schema budget tests.",
                                "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
                            }
                            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [tool]}}), flush=True)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            manifest = {
                "persona_installer": {
                    "claude_mcp_servers": {
                        "personas": {
                            "specialist": {
                                "servers": {
                                    "fake": {
                                        "command": "python3",
                                        "args": [str(server)],
                                        "env": {},
                                    }
                                }
                            }
                        }
                    }
                },
                "harness_runtime": {
                    "harnesses": {"claude": {"default_profile": "interactive"}},
                    "roles": {"specialist": {"claude": {"profile": "interactive", "model": "m"}}},
                },
                "roles": {"specialist": {"persona": "specialist", "account": "claude-research"}},
            }
            [profile] = tsb.measure_profiles(manifest, probe=True, cwd=root, timeout=5)
            self.assertEqual(profile.tool_count, 1)
            self.assertGreater(profile.estimated_schema_tokens, 0)
            self.assertEqual(profile.tier, "<=25K")
            self.assertEqual(profile.servers[0].status, "probed")
            self.assertEqual(profile.servers[0].error, "")

    def test_jsonable_payload_keeps_server_errors(self) -> None:
        profile = tsb.ProfileMeasurement(
            role="r",
            persona="p",
            account="a",
            harness="claude",
            profile="interactive",
            model=None,
            tier="<=25K",
            tool_count=0,
            schema_chars=0,
            estimated_schema_tokens=0,
            config_chars=10,
            estimated_config_tokens=3,
            servers=(
                tsb.ServerMeasurement(
                    name="bad",
                    source="test",
                    status="probe_failed",
                    tool_count=0,
                    schema_chars=0,
                    estimated_schema_tokens=0,
                    config_chars=10,
                    estimated_config_tokens=3,
                    error="boom",
                ),
            ),
        )
        payload = tsb.as_jsonable(profile)
        self.assertEqual(payload["servers"][0]["error"], "boom")
        json.dumps(payload)


class HighFdCeilingTests(unittest.TestCase):
    """T-5872: _ask_jsonrpc must work when the server pipe fd is >= 1024."""

    def test_ask_jsonrpc_with_pipe_fd_above_1024(self) -> None:
        import subprocess
        import sys

        from tools._test_fixtures import fds_above_select_ceiling
        code = (
            "import sys,json\n"
            "m=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'ok':1}}),flush=True)\n"
        )
        with fds_above_select_ceiling():
            proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                self.assertGreaterEqual(proc.stdout.fileno(), 1024)
                reply = tsb._ask_jsonrpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "x"}, timeout=10)
                self.assertEqual(reply["result"], {"ok": 1})
            finally:
                proc.stdin.close()
                proc.stdout.close()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
