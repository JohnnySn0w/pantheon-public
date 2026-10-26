"""T-3421 regression: the retired persona `the-ancestor` must not reappear in the live
registry or the code enumerations derived from it (ORG-087 sec. 4 step 4).

Historical mentions (comments, tickets, ledger, deferred-tool fixtures) are fine; this guards
the LIVE surfaces: instance.yaml parsed structure, agent_role_map's maps, and the mailbox MCP
recipient sets."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import yaml

from tools import agent_role_map as arm

_HAS_MCP = importlib.util.find_spec("mcp") is not None
if _HAS_MCP:
    from tools import mailbox_mcp

RETIRED = "the-ancestor"
ROOT = Path(__file__).resolve().parent.parent


def _walk(node, path=""):
    """Yield (path, kind, text) for every key and scalar string in a parsed YAML tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield (f"{path}/{k}", "key", str(k))
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield (path, "value", node)


class TheAncestorRetired(unittest.TestCase):
    def test_instance_yaml_has_no_live_reference(self):
        data = yaml.safe_load((ROOT / "instance.yaml").read_text())
        hits = [(p, t) for p, _k, t in _walk(data) if RETIRED in t]
        self.assertEqual(hits, [], "instance.yaml parsed content still names the-ancestor")

    def test_wake_timer_personas_excludes_it(self):
        data = yaml.safe_load((ROOT / "instance.yaml").read_text())
        wake = data["persona_installer"]["mailbox"]["wake_timer_personas"]
        self.assertNotIn(RETIRED, wake)

    def test_agent_role_map_live_maps_exclude_it(self):
        self.assertNotIn(RETIRED, arm.AGENT_ROLE_MAP)
        self.assertNotIn(RETIRED, arm.AGENT_ROLE_MAP.values())
        self.assertNotIn(RETIRED, arm._FALLBACK_PERSONA_TO_INSTANCE_ROLE)
        self.assertNotIn("peer-daemon-the-ancestor", arm._FALLBACK_PERSONA_TO_INSTANCE_ROLE.values())
        self.assertNotIn(RETIRED, arm._FALLBACK_PERSONA_TO_BINDING)
        self.assertNotIn(RETIRED, arm.account_persona_map())
        self.assertNotIn(RETIRED, arm.fleet_accounts())
        self.assertNotIn(RETIRED, arm.valid_target_values())

    @unittest.skipUnless(_HAS_MCP, "mcp SDK not installed; mailbox_mcp cannot be imported")
    def test_mailbox_mcp_recipients_exclude_it(self):
        self.assertNotIn(RETIRED, mailbox_mcp._KNOWN_RECIPIENTS)
        self.assertNotIn(RETIRED, mailbox_mcp._NO_MCP_RECIPIENT)
        self.assertNotIn(RETIRED, mailbox_mcp._USER_TO_PERSONA.values())


if __name__ == "__main__":
    unittest.main()
