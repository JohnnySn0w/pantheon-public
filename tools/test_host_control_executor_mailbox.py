"""T-2917 5d.3: the executor's ``mailbox.probe`` (rulings T-2917:D19, D20,
D22): the confined round-trip and writable-root probes, the root-configured
mailbox hub, the engine wiring (apply / verify / reconcile, the pre-pass
gate, timer-observe correlation) and the capability flip over every real
persona plan.

Fixture evidence: a disposable hub under a temp dir (never the live
post-office hub); the bus-tier end-to-end class runs the real broker and
executor roles on a private bus (``skipUnless`` dbus; CI skips it, T-4931).
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import _host_control_executor_fixtures as fx
from tools.host_control.contracts import RegistryError
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    IMPLEMENTED_EXECUTION_CLASSES,
    OUT_OF_ENVELOPE_KINDS,
    OutOfEnvelopeKinds,
    require_executor_envelope,
    unimplemented_kinds,
)
from tools.host_control.executor import mailbox_probe as mp
from tools.host_control.executor.config import (
    EXECUTOR_CONFIG_SCHEMA,
    parse_executor_config,
    require_hub_outside_seat_root,
)
from tools.host_control.executor.mailbox_probe import (
    ROUND_TRIP_PREFIX,
    WRITABLE_ROOT_PREFIX,
    MailboxProbe,
    MailboxProbeRefused,
    probe_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MAILBOX_PROBE_SOURCE = REPO_ROOT / "tools" / "host_control" / "executor" / "mailbox_probe.py"


class _HubCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = fx.build_executor_fixture(Path(temp.name) / "x")
        self.addCleanup(self.fixture.close)
        self.hub = self.fixture.root / "mailbox"
        for persona in ("seat", "peer"):
            for sub in ("tmp", "new", "cur"):
                (self.hub / persona / sub).mkdir(parents=True)
        self.now = [1000.0]
        self.probe = MailboxProbe(hub=self.hub, uid=os.getuid(), clock=lambda: self.now[0])

    # -- operations ------------------------------------------------------

    def round_trip_op(self, *, target=None, receive=None, send=None, **extra):
        desired = {
            "receive_tmp": str(receive or self.hub / "seat" / "tmp"),
            "send_tmp": str(send or self.hub / "peer" / "tmp"),
            "receive": "required",
            "send": "required",
            "durable_round_trip": "required",
            **extra,
        }
        return fx.op("connective.mailbox-probe", "mailbox-probe", str(target or self.hub / "seat"), desired)

    def root_op(self, persona: str = "seat", **extra):
        desired = {
            "harness": "codex",
            "ref_kind": "bound-mailbox",
            "ref_persona": persona,
            "mailbox_hub": str(self.hub),
            "maildir_subdirs": "tmp,new,cur",
            "transient_probe": "o-excl-create-unlink",
            **extra,
        }
        return fx.op("mailbox-writable-root.codex.0", "mailbox-writable-root-check", str(self.hub / persona), desired)

    def engine(self, **overrides):
        values = {"mailbox_probe": self.probe}
        values.update(overrides)
        return self.fixture.engine(**values)

    def run_ops(self, *operations, kind="apply", engine=None):
        return (engine or self.engine()).run(fx.parsed(self.fixture, tuple(operations), execution_kind=kind))

    def hub_files(self) -> list[str]:
        return sorted(str(p.relative_to(self.hub)) for p in self.hub.rglob("*") if not p.is_dir())

    def unlinks(self):
        return mock.patch.object(mp.os, "unlink", wraps=os.unlink)


# ---------------------------------------------------------------------------
# the probes
# ---------------------------------------------------------------------------


class RoundTripTests(_HubCase):
    def test_round_trip_writes_reads_back_and_unlinks_in_both_tmp_dirs(self) -> None:
        operation = self.round_trip_op()
        with self.unlinks() as unlink:
            values = self.probe.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)
        names = [call.args[0] for call in unlink.call_args_list]
        self.assertEqual(
            names,
            [
                probe_name(ROUND_TRIP_PREFIX, fx.OPERATION_HANDLE, operation.operation_id, "receive"),
                probe_name(ROUND_TRIP_PREFIX, fx.OPERATION_HANDLE, operation.operation_id, "send"),
            ],
        )
        self.assertEqual(
            {key: values[key] for key in ("receive", "send", "durable_round_trip", "mailbox_hub_source")},
            {"receive": "true", "send": "true", "durable_round_trip": "true", "mailbox_hub_source": "executor-config"},
        )
        self.assertRegex(values["sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(self.hub_files(), [])  # nothing left behind

    def test_payload_sha_feeds_timer_observe_correlation(self) -> None:
        from tools.test_host_control_executor_activation import (
            INVOCATION,
            TIMER,
            _UnitFireBase,
            _service_state,
            _timer_state,
        )

        case = self

        class Correlated(_UnitFireBase):
            def runTest(self) -> None:  # noqa: N802 - a one-off harness
                self.script(
                    capture=_service_state(active="activating", sub="start", invocation=INVOCATION),
                    completion=_service_state(invocation=INVOCATION),
                )
                self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=99)
                hub = self.fixture.root / "mailbox"
                for persona in ("seat", "peer"):
                    (hub / persona / "tmp").mkdir(parents=True)
                probe = MailboxProbe(hub=hub, uid=os.getuid(), clock=self.clock)
                mailbox = fx.op(
                    "connective.mailbox-probe", "mailbox-probe", str(hub / "seat"),
                    {"receive_tmp": str(hub / "seat" / "tmp"), "send_tmp": str(hub / "peer" / "tmp")},
                )
                result = self.run_ops(
                    self.unit_fire_op(), mailbox, self.timer_observe_op(), engine=self.engine(mailbox_probe=probe)
                )
                case.assertTrue(result.ok, result.evidence)
                _fire, probed, observed = result.evidence
                case.assertEqual(observed.evidence["mailbox_effect_correlated"], "true")
                case.assertEqual(observed.evidence["mailbox_probe_sha256"], probed.evidence["sha256"])

        outcome = unittest.TestResult()
        Correlated().run(outcome)
        self.assertEqual((outcome.errors, outcome.failures), ([], []))

    def test_target_outside_the_configured_hub_is_refused_before_any_write(self) -> None:
        elsewhere = self.fixture.root / "elsewhere"
        (elsewhere / "seat" / "tmp").mkdir(parents=True)
        for operation in (
            self.round_trip_op(receive=elsewhere / "seat" / "tmp"),
            self.round_trip_op(send=elsewhere / "seat" / "tmp"),
            self.round_trip_op(target=elsewhere / "seat"),
            fx.op("mailbox-writable-root.codex.0", "mailbox-writable-root-check", str(elsewhere / "seat"), {}),
        ):
            with self.subTest(operation=operation.desired), self.unlinks() as unlink:
                directory = self.fixture.seat_root / "made-first"
                first = fx.op("dir", "mkdir", str(directory), {"mode": "0755"})
                result = self.run_ops(first, operation)
                self.assertFalse(result.ok)
                self.assertEqual(result.mutations, ())
                self.assertIn("pre-pass", result.detail)
                self.assertIn("outside the configured mailbox hub", result.evidence[0].detail)
                self.assertFalse(directory.exists())  # the pre-pass ran before ANY change
                self.assertEqual(unlink.call_args_list, [])
                self.assertEqual(sorted(p.name for p in elsewhere.rglob("*")), ["seat", "tmp"])

    def test_symlinked_hub_component_is_refused(self) -> None:
        # a symlinked hub itself
        link = self.fixture.root / "hub-link"
        os.symlink(self.hub, link)
        linked = MailboxProbe(hub=link, uid=os.getuid(), clock=lambda: 0.0)
        operation = fx.op(
            "mbox", "mailbox-probe", str(link / "seat"),
            {"receive_tmp": str(link / "seat" / "tmp"), "send_tmp": str(link / "peer" / "tmp")},
        )
        with self.assertRaises(MailboxProbeRefused):
            linked.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=10.0)
        # a symlinked persona tmp below a real hub
        decoy = self.fixture.root / "decoy"
        decoy.mkdir()
        (self.hub / "peer" / "tmp").rmdir()
        os.symlink(decoy, self.hub / "peer" / "tmp")
        with self.assertRaises(MailboxProbeRefused):
            self.probe.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=self.round_trip_op(), deadline=2000.0)
        self.assertEqual(list(decoy.iterdir()), [])
        self.assertEqual(sorted(p.name for p in (self.hub / "seat" / "tmp").iterdir()), [])  # receive side cleaned

    def test_non_tmp_or_nested_target_is_refused(self) -> None:
        hub = self.hub
        cases = {
            "new not tmp": self.round_trip_op(receive=hub / "seat" / "new"),
            "nested": self.round_trip_op(send=hub / "peer" / "tmp" / "deeper"),
            "hub itself": self.round_trip_op(send=hub),
            "persona dir": self.round_trip_op(send=hub / "peer"),
            "receive not target": self.round_trip_op(receive=hub / "peer" / "tmp"),
            "target nested": self.round_trip_op(target=hub / "seat" / "tmp"),
            "unnormalized": self.round_trip_op(send=Path(str(hub) + "/peer/../seat/tmp")),
            "dot persona": self.round_trip_op(send=hub / ".hidden" / "tmp"),
            "relative": fx.op("m", "mailbox-probe", "seat", {"receive_tmp": "seat/tmp", "send_tmp": "peer/tmp"}),
            "plan names another hub": self.round_trip_op(mailbox_hub="/elsewhere"),
        }
        for label, operation in cases.items():
            with self.subTest(label=label), self.assertRaises(MailboxProbeRefused):
                self.probe.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)
        for label, operation in {
            "root nested": fx.op("r", "mailbox-writable-root-check", str(hub / "seat" / "tmp"), {}),
            "root is hub": fx.op("r", "mailbox-writable-root-check", str(hub), {}),
            "subdirs": self.root_op(maildir_subdirs="tmp,new"),
        }.items():
            with self.subTest(label=label), self.assertRaises(MailboxProbeRefused):
                self.probe.writable_root(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)
        self.assertEqual(self.hub_files(), [])

    def test_deterministic_probe_name_and_own_residue_is_swept_once(self) -> None:
        operation = self.round_trip_op()
        name = probe_name(ROUND_TRIP_PREFIX, fx.OPERATION_HANDLE, operation.operation_id, "receive")
        self.assertRegex(name, r"\A\.installer-probe-[0-9a-f]{32}\Z")
        self.assertEqual(name, probe_name(ROUND_TRIP_PREFIX, fx.OPERATION_HANDLE, operation.operation_id, "receive"))
        self.assertNotEqual(name, probe_name(ROUND_TRIP_PREFIX, "ef" * 32, operation.operation_id, "receive"))
        rw = probe_name(WRITABLE_ROOT_PREFIX, fx.OPERATION_HANDLE, "op", "tmp")
        self.assertRegex(rw, r"\A\.pantheon-rw-probe-[0-9a-f]{32}\Z")
        residue = self.hub / "seat" / "tmp" / name
        residue.write_bytes(b"left by a killed run\n")  # own uid, regular, small
        values = self.probe.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)
        self.assertEqual(values["durable_round_trip"], "true")
        self.assertEqual(self.hub_files(), [])
        # only ONE sweep: a name re-taken after the sweep is refused
        real_open = os.open
        taken = {"n": 0}

        def reappearing_open(path, flags, *args, **kwargs):
            if path == name and flags & os.O_EXCL:
                taken["n"] += 1
                raise FileExistsError(path)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(mp.os, "open", side_effect=reappearing_open):
            with self.assertRaises(MailboxProbeRefused) as caught:
                self.probe.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)
        self.assertEqual(taken["n"], 2)
        self.assertIn("after one own-residue sweep", str(caught.exception))

    def test_foreign_file_at_the_probe_name_is_refused_not_unlinked(self) -> None:
        operation = self.round_trip_op()
        # planted on the FIRST (receive) side, so the sweep decision is
        # reached before this probe writes anything of its own
        name = probe_name(ROUND_TRIP_PREFIX, fx.OPERATION_HANDLE, operation.operation_id, "receive")
        target = self.hub / "seat" / "tmp" / name
        decoy = self.fixture.root / "decoy-target"
        decoy.write_text("not ours\n", encoding="utf-8")
        planted = {
            "other uid": lambda: target.write_text("someone else's\n", encoding="utf-8"),
            "symlink": lambda: os.symlink(decoy, target),
            "directory": lambda: target.mkdir(),
            "oversized": lambda: target.write_bytes(b"x" * (mp.RESIDUE_MAX_BYTES + 1)),
        }
        for label, plant in planted.items():
            with self.subTest(label=label):
                plant()
                probe = (
                    MailboxProbe(hub=self.hub, uid=os.getuid() + 1, clock=lambda: 0.0)
                    if label == "other uid"
                    else self.probe
                )
                with self.assertRaises(MailboxProbeRefused) as caught:
                    probe.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)
                self.assertIn("never unlinked", str(caught.exception))
                self.assertTrue(os.path.lexists(target))  # never unlinked
                self.assertEqual(sorted(p.name for p in (self.hub / "peer" / "tmp").iterdir()), [])
                if target.is_symlink() or target.is_file():
                    target.unlink()
                else:
                    target.rmdir()
        self.assertEqual(decoy.read_text(encoding="utf-8"), "not ours\n")

    def test_writable_root_probes_root_tmp_new_cur_and_unlinks(self) -> None:
        operation = self.root_op()
        with self.unlinks() as unlink:
            values = self.probe.writable_root(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)
        self.assertEqual(
            [call.args[0] for call in unlink.call_args_list],
            [
                probe_name(WRITABLE_ROOT_PREFIX, fx.OPERATION_HANDLE, operation.operation_id, label)
                for label in ("root", "tmp", "new", "cur")
            ],
        )
        self.assertEqual(
            values,
            {
                "root": str(self.hub / "seat"),
                "maildir_subdirs": "tmp,new,cur",
                "transient_write_probe": "true",
                "harness": "codex",
                "ref_kind": "bound-mailbox",
                "ref_persona": "seat",
                "mailbox_hub_source": "executor-config",
            },
        )
        self.assertEqual(self.hub_files(), [])
        (self.hub / "seat" / "cur").rmdir()
        with self.assertRaises(MailboxProbeRefused):
            self.probe.writable_root(operation_handle=fx.OPERATION_HANDLE, operation=operation, deadline=2000.0)

    def test_probe_honours_the_execution_deadline(self) -> None:
        with self.unlinks() as unlink:
            for call in (
                lambda: self.probe.round_trip(operation_handle=fx.OPERATION_HANDLE, operation=self.round_trip_op(), deadline=1000.0),
                lambda: self.probe.writable_root(operation_handle=fx.OPERATION_HANDLE, operation=self.root_op(), deadline=999.0),
            ):
                with self.subTest(), self.assertRaises(MailboxProbeRefused) as caught:
                    call()
                self.assertIn("deadline", str(caught.exception))
        self.assertEqual(unlink.call_args_list, [])
        self.assertEqual(self.hub_files(), [])
        # through the engine: a run whose budget is spent fails the probe
        # operation without writing
        result = self.run_ops(self.round_trip_op(), engine=self.engine(run_budget=0, clock=lambda: self.now[0]))
        self.assertFalse(result.ok)
        self.assertEqual(self.hub_files(), [])


# ---------------------------------------------------------------------------
# the engine wiring
# ---------------------------------------------------------------------------


class EngineMailboxTests(_HubCase):
    def test_apply_records_a_no_change_mutation_and_installer_parity_evidence(self) -> None:
        result = self.run_ops(self.round_trip_op(), self.root_op())
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual([m.changed for m in result.mutations], [False, False])  # transient: never restorable
        trip, root = result.evidence
        self.assertTrue({"receive", "send", "durable_round_trip", "sha256", "mailbox_hub_source"} <= set(trip.evidence))
        self.assertTrue(
            {"root", "maildir_subdirs", "transient_write_probe", "harness", "ref_kind", "ref_persona", "mailbox_hub_source"}
            <= set(root.evidence)
        )
        self.assertEqual(self.hub_files(), [])

    def test_verify_reruns_the_live_probe_and_returns_no_mutation(self) -> None:
        with self.unlinks() as unlink:
            result = self.run_ops(self.round_trip_op(), self.root_op(), kind="verify")
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.mutations, ())
        self.assertEqual(len(unlink.call_args_list), 2 + 4)  # the live probe actually ran
        (self.hub / "peer" / "tmp").rmdir()
        failed = self.run_ops(self.round_trip_op(), kind="verify")
        self.assertFalse(failed.ok)
        self.assertEqual(failed.mutations, ())

    def test_reconcile_observes_probe_residue_read_only(self) -> None:
        operation = self.round_trip_op()
        observed = self.run_ops(operation, kind="reconcile")
        self.assertTrue(observed.ok, observed.evidence)
        self.assertEqual(observed.mutations, ())
        self.assertEqual(observed.evidence[0].evidence["residue"], "absent")
        name = probe_name(ROUND_TRIP_PREFIX, fx.OPERATION_HANDLE, operation.operation_id, "send")
        own = self.hub / "peer" / "tmp" / name
        own.write_bytes(b"residue\n")
        with self.unlinks() as unlink:
            observed = self.run_ops(operation, kind="reconcile")
        self.assertEqual(observed.evidence[0].evidence["residue"], "own")
        self.assertEqual(unlink.call_args_list, [])
        self.assertTrue(own.exists())  # read-only: never swept here
        own.unlink()
        os.symlink(self.fixture.root, own)
        self.assertEqual(self.run_ops(operation, kind="reconcile").evidence[0].evidence["residue"], "foreign")

    def test_mailbox_hub_comes_from_root_config_never_env_or_plan_alone(self) -> None:
        # no configured hub: every mailbox operation fails closed, whatever
        # the plan or the environment names
        with mock.patch.dict(os.environ, {"PANTHEON_MAILBOX_HUB": str(self.hub), "MAILBOX_HUB": str(self.hub)}):
            for kind in ("apply", "verify"):
                with self.subTest(kind=kind):
                    result = self.run_ops(self.round_trip_op(), kind=kind, engine=self.fixture.engine())
                    self.assertFalse(result.ok)
                    self.assertIn("mailbox hub is not configured", result.evidence[0].detail)
        self.assertEqual(self.hub_files(), [])
        # a plan value naming another hub is refused
        with self.assertRaises(MailboxProbeRefused):
            self.probe.writable_root(
                operation_handle=fx.OPERATION_HANDLE, operation=self.root_op(mailbox_hub="/srv/other"), deadline=2000.0
            )
        # the module never reads the environment and never spawns a child
        tree = ast.parse(MAILBOX_PROBE_SOURCE.read_text(encoding="utf-8"))
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(names & {"environ", "environb", "getenv", "getenvb", "system", "popen", "spawnv", "execv"})
        self.assertNotIn("subprocess", imported)
        # the entrypoint builds the probe from the config's hub only
        main = (REPO_ROOT / "tools" / "host_control" / "executor" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("MailboxProbe(hub=config.mailbox_hub", main)
        self.assertIn("require_hub_outside_seat_root(config, identity.seat_root)", main)

    def test_d32_hub_overlapping_roots_or_system_trees_is_refused(self) -> None:
        good = {
            "schema": EXECUTOR_CONFIG_SCHEMA,
            "registry_path": "/opt/pantheon/trusted/registry.json",
            "trusted_roots": ["/opt/pantheon/trusted", "/opt/pantheon/framework"],
            "broker_uid": 991,
            "framework_root": "/opt/pantheon/framework/copy",
            "mailbox_hub": "/srv/mailbox",
        }
        self.assertEqual(parse_executor_config(good).mailbox_hub, Path("/srv/mailbox"))
        refused = {
            # trusted roots: equal, contained, containing
            "trusted root equal": "/opt/pantheon/trusted",
            "inside a trusted root": "/opt/pantheon/trusted/mailbox",
            "contains trusted roots": "/opt/pantheon",
            # the framework root (inside the second trusted root too)
            "framework root equal": "/opt/pantheon/framework/copy",
            "inside the framework root": "/opt/pantheon/framework/copy/hub",
            # the registry path
            "registry path equal": "/opt/pantheon/trusted/registry.json",
        }
        for tree in ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/var/lib/systemd",
                     "/proc", "/sys", "/dev", "/run/systemd"):
            refused[f"system tree {tree}"] = tree
            refused[f"inside system tree {tree}"] = f"{tree}/mailbox"
        refused["contains a system tree"] = "/var/lib"
        refused["the filesystem root"] = "/"
        for label, hub in refused.items():
            with self.subTest(label=label), self.assertRaises(RegistryError):
                parse_executor_config(dict(good, mailbox_hub=hub))
        # path-component comparison, never string prefix: siblings are fine
        for sibling in ("/etc2", "/usr-mailbox", "/opt/pantheon/trusted2", "/opt/pantheon/framework-hub",
                        "/var/lib/systemd2", "/run/systemd-mailbox", "/devices/mailbox"):
            with self.subTest(sibling=sibling):
                self.assertEqual(parse_executor_config(dict(good, mailbox_hub=sibling)).mailbox_hub, Path(sibling))
        # the registry path's own parent dir being the hub's parent is fine
        self.assertEqual(
            parse_executor_config(dict(good, mailbox_hub="/opt/pantheon/mailbox")).mailbox_hub,
            Path("/opt/pantheon/mailbox"),
        )

    def test_d32_entrypoint_refuses_an_overlapping_hub_before_any_connection(self) -> None:
        import contextlib
        import io

        from tools.host_control.executor import __main__ as entry
        from tools.host_control.trusted_files import RootOwnedFileLoader

        etc = self.fixture.root / "etc"
        etc.mkdir()
        etc.chmod(0o755)
        trusted = self.fixture.root / "trusted"
        config_path = etc / "config.json"
        for hub, expected in ((str(trusted / "mailbox"), "RegistryError"), ("/usr/mailbox", "RegistryError")):
            with self.subTest(hub=hub):
                config_path.write_text(
                    json.dumps(
                        {
                            "schema": EXECUTOR_CONFIG_SCHEMA,
                            "registry_path": str(trusted / "registry.json"),
                            "trusted_roots": [str(trusted), str(self.fixture.framework_root)],
                            "broker_uid": os.getuid(),
                            "framework_root": str(self.fixture.framework_root),
                            "mailbox_hub": hub,
                        }
                    ),
                    encoding="utf-8",
                )
                config_path.chmod(0o644)
                stderr = io.StringIO()

                def never_connect():
                    raise AssertionError("must not connect before the config is accepted")

                with contextlib.redirect_stderr(stderr):
                    code = entry.run(
                        config_path=config_path,
                        open_loader=lambda roots: RootOwnedFileLoader._open_for_test(
                            tuple(roots), expected_owner_uid=os.getuid()
                        ),
                        getuid=os.getuid,
                        connection_factory=never_connect,
                    )
                self.assertEqual(code, entry.EXIT_START_REFUSED)
                self.assertIn(f"start refused: {expected}", stderr.getvalue())

    def _good_config(self) -> dict:
        return {
            "schema": EXECUTOR_CONFIG_SCHEMA,
            "registry_path": "/opt/pantheon/trusted/registry.json",
            "trusted_roots": ["/opt/pantheon/trusted", "/opt/pantheon/framework"],
            "broker_uid": 991,
            "framework_root": "/opt/pantheon/framework/copy",
            "mailbox_hub": "/srv/mailbox",
        }

    def test_d32_double_slash_prefixed_paths_are_refused_everywhere(self) -> None:
        import os.path as posixpath

        good = self._good_config()
        # POSIX normpath keeps exactly two leading slashes; three or more collapse
        self.assertEqual(posixpath.normpath("//etc"), "//etc")
        self.assertEqual(posixpath.normpath("///etc"), "/etc")
        cases = {
            "hub //": dict(good, mailbox_hub="//"),
            "hub //etc": dict(good, mailbox_hub="//etc"),
            "hub ///etc (collapses: unnormalized)": dict(good, mailbox_hub="///etc"),
            "hub //usr/local/hub": dict(good, mailbox_hub="//usr/local/hub"),
            "hub //proc": dict(good, mailbox_hub="//proc"),
            "hub //opt/pantheon/trusted": dict(good, mailbox_hub="//opt/pantheon/trusted"),
            "trusted root": dict(good, trusted_roots=["//opt/pantheon/trusted", "/opt/pantheon/framework"]),
            "registry path": dict(good, registry_path="//opt/pantheon/trusted/registry.json"),
            "framework root": dict(good, framework_root="//opt/pantheon/framework/copy"),
        }
        for label, value in cases.items():
            with self.subTest(label=label), self.assertRaises(RegistryError):
                parse_executor_config(value)
        for hub in ("//", "//etc", "///etc"):
            with self.subTest(probe_hub=hub), self.assertRaises(MailboxProbeRefused):
                # the RAW string (pathlib itself collapses "///etc" to "/etc")
                MailboxProbe(hub=hub, uid=os.getuid(), clock=lambda: 0.0)  # type: ignore[arg-type]
        # a plan/spec target with a // prefix is refused by the probe's confinement
        with self.assertRaises(MailboxProbeRefused):
            self.probe.round_trip(
                operation_handle=fx.OPERATION_HANDLE,
                operation=self.round_trip_op(receive=Path("/" + str(self.hub / "seat" / "tmp"))),
                deadline=2000.0,
            )
        # the other normpath-equality sites in host_control refuse it too
        from tools.host_control.executor.seat_files import SeatFiles, SeatPathRefused

        with self.assertRaises(SeatPathRefused):
            SeatFiles(seat_root="//home/seat", owner_uid=os.getuid())  # type: ignore[arg-type]
        self.assertFalse(SeatFiles(seat_root=Path("/home/seat"), owner_uid=os.getuid()).contains("//home/seat/x"))
        from tools.host_control import registry as registry_module, trusted_files

        with self.assertRaises(RegistryError):
            registry_module._absolute_normalized("//home/seat", "install root")
        with self.assertRaises(RegistryError):
            trusted_files._normalized_absolute("//etc")  # type: ignore[arg-type]
        engine = self.engine()
        with self.assertRaises(Exception):
            engine._framework_path("/" + str(self.fixture.framework_root) + "/tools/persona_installer.py")

    def _entrypoint(self, hub: str, *, getuid=os.getuid) -> tuple[int, str]:
        import contextlib
        import io

        from tools.host_control.executor import __main__ as entry
        from tools.host_control.trusted_files import RootOwnedFileLoader

        etc = self.fixture.root / "etc-entry"
        etc.mkdir(exist_ok=True)
        etc.chmod(0o755)
        trusted = self.fixture.root / "trusted"
        config_path = etc / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema": EXECUTOR_CONFIG_SCHEMA,
                    "registry_path": str(trusted / "registry.json"),
                    "trusted_roots": [str(trusted), str(self.fixture.framework_root)],
                    "broker_uid": os.getuid(),
                    "framework_root": str(self.fixture.framework_root),
                    "mailbox_hub": hub,
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o644)
        stderr = io.StringIO()
        connected: list[str] = []

        def connect_marker():
            connected.append("connected")
            raise RuntimeError("stop after the pre-connection checks")

        with contextlib.redirect_stderr(stderr):
            try:
                code = entry.run(
                    config_path=config_path,
                    open_loader=lambda roots: RootOwnedFileLoader._open_for_test(tuple(roots), expected_owner_uid=os.getuid()),
                    getuid=getuid,
                    connection_factory=connect_marker,
                )
            except RuntimeError:
                code = -1
        return code, stderr.getvalue() + "|".join(connected)

    def test_d32_entrypoint_refuses_a_double_slash_hub_before_any_connection(self) -> None:
        from tools.host_control.executor import __main__ as entry

        code, output = self._entrypoint("//etc")
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertIn("start refused: RegistryError", output)
        self.assertNotIn("connected", output)

    def test_d33_symlinked_hub_or_parent_is_refused_at_the_entrypoint(self) -> None:
        from tools.host_control.executor import __main__ as entry
        from tools.host_control.executor.config import require_hub_without_symlinks

        real = self.fixture.root / "real-parent"
        (real / "hub").mkdir(parents=True)
        os.symlink(real / "hub", self.fixture.root / "hub-link")
        os.symlink(real, self.fixture.root / "parent-link")
        for label, hub in (
            ("symlinked hub", self.fixture.root / "hub-link"),
            ("symlinked parent", self.fixture.root / "parent-link" / "hub"),
            ("symlinked parent, hub not yet created", self.fixture.root / "parent-link" / "missing"),
        ):
            with self.subTest(label=label):
                with self.assertRaises(RegistryError):
                    require_hub_without_symlinks(hub)
                code, output = self._entrypoint(str(hub))
                self.assertEqual(code, entry.EXIT_START_REFUSED)
                self.assertIn("start refused: RegistryError", output)
                self.assertNotIn("connected", output)
        # a real directory (and a not-yet-created hub under one) passes
        require_hub_without_symlinks(real / "hub")
        require_hub_without_symlinks(real / "not-yet")

    def test_config_requires_an_absolute_hub_disjoint_from_the_seat_root(self) -> None:
        good = {
            "schema": EXECUTOR_CONFIG_SCHEMA,
            "registry_path": "/etc/pantheon/registry.json",
            "trusted_roots": ["/etc/pantheon"],
            "broker_uid": 991,
            "framework_root": "/etc/pantheon/framework",
            "mailbox_hub": "/srv/mailbox",
        }
        self.assertEqual(parse_executor_config(good).mailbox_hub, Path("/srv/mailbox"))
        missing = dict(good)
        del missing["mailbox_hub"]
        for label, value in (
            ("missing", missing),
            ("relative", dict(good, mailbox_hub="mailbox")),
            ("unnormalized", dict(good, mailbox_hub="/srv/../mailbox")),
            ("root", dict(good, mailbox_hub="/")),
            ("not a string", dict(good, mailbox_hub=7)),
        ):
            with self.subTest(label=label), self.assertRaises(RegistryError):
                parse_executor_config(value)
        config = parse_executor_config(good)
        require_hub_outside_seat_root(config, Path("/home/seat"))
        for seat_root in (Path("/srv/mailbox"), Path("/srv/mailbox/seat"), Path("/srv")):
            with self.subTest(seat_root=seat_root), self.assertRaises(RegistryError):
                require_hub_outside_seat_root(config, seat_root)


# ---------------------------------------------------------------------------
# the capability flip over every real persona plan
# ---------------------------------------------------------------------------


class RealPlanTests(unittest.TestCase):
    def test_every_real_persona_plan_is_admitted_for_apply_verify_and_rollback(self) -> None:
        from tools.persona_installer import SEAT_LOCAL_RESIDUE_KINDS, Manifest, PersonaInstaller

        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        installer = PersonaInstaller(REPO_ROOT)
        personas = sorted(
            {raw["persona"] for raw in manifest.roles.values() if isinstance(raw, dict) and raw.get("persona")}
        )
        self.assertTrue(personas)
        admitted: list[tuple[str, str]] = []
        denied: list[tuple[str, str]] = []
        mailbox_kinds: set[str] = set()
        for persona in personas:
            for harness in ("claude", "codex"):
                with self.subTest(persona=persona, harness=harness):
                    plan = installer.plan(manifest, persona, harness_override=harness, root=Path("/projection"))
                    # T-5447: the seat-core periodic kinds are seat-local residue on
                    # EVERY plan; the D31 pin below classifies by the remaining kinds.
                    kinds = tuple(
                        op.kind for op in plan.operations if op.kind not in SEAT_LOCAL_RESIDUE_KINDS
                    )
                    try:
                        require_executor_envelope(kinds)
                    except OutOfEnvelopeKinds as exc:
                        # refused at plan time with family_denied (ruling i4,
                        # T-4861 §8b): only the NAMED out-of-envelope kinds
                        self.assertTrue(set(exc.refused_kinds) <= OUT_OF_ENVELOPE_KINDS, exc.refused_kinds)
                        denied.append((persona, harness))
                        inside = tuple(kind for kind in kinds if kind in EXECUTOR_KIND_CLASSES)
                    else:
                        admitted.append((persona, harness))
                        inside = kinds
                    # no in-envelope kind is Unsupported for any execution kind
                    for execution in ("apply", "verify", "rollback", "reconcile"):
                        self.assertEqual(unimplemented_kinds(execution, inside), (), execution)
                    mailbox_kinds |= {kind for kind in inside if EXECUTOR_KIND_CLASSES[kind] == "mailbox.probe"}
        # ruling D31, pinned exactly (catches drift either way): only these
        # real plans are fully in-envelope; the rest stay family_denied.
        # watchman added by T-4884 B6 (codex-fallback recovery seat): its plan has
        # none of the out-of-envelope kinds. Descriptive of the plan, not a grant
        # (orchestrator ruling on #937; mason cc'd as the D31 owner).
        harnesses = ("claude", "codex")
        self.assertEqual(
            sorted(admitted),
            sorted((p, h) for p in ("codex", "watchman") for h in harnesses),
        )
        self.assertEqual(
            sorted(denied),
            sorted(
                (p, h)
                for p in ("specialist", "artisan", "mason", "archivist", "orchestrator", "recovery")
                for h in harnesses
            ),
        )
        self.assertEqual(mailbox_kinds, {"mailbox-probe", "mailbox-writable-root-check"})
        for execution in ("apply", "verify", "rollback", "reconcile"):
            self.assertIn("mailbox.probe", IMPLEMENTED_EXECUTION_CLASSES[execution])


# ---------------------------------------------------------------------------
# bus tier: a real persona plan end to end (fixture, private bus)
# ---------------------------------------------------------------------------

from tools import test_host_control_executor_dbus as _e2e  # noqa: E402 - the shared private-bus harness


class RealPlanEndToEndTests(_e2e.BrokerExecutorEndToEndTests):
    """The real broker and executor roles on a private bus (the
    ``test_host_control_executor_dbus`` harness, its own tree and
    processes): the real persona plan, narrowed to the classes this fixture
    exercises without a user manager (diagnostics, seat-root files and,
    since 5d.3, mailbox.probe against a disposable hub), applied, verified
    and rolled back. Activation runs in the 5c user-bus tier. Fixture
    evidence, never relabelled as live. (Skipped without dbus, inherited.)"""

    # only the cell below runs here; the base class's own tests run once,
    # in their own module
    test_1_executor_owns_its_uid_derived_name = None
    test_2_verify_apply_verify_end_to_end = None
    test_3_execute_cannot_act_without_the_brokers_spec = None

    def test_4_real_persona_plan_apply_verify_rollback_end_to_end(self) -> None:
        manager = self.manager
        plan_handle, plan_digest, _summary = manager("Plan", "sss", ("recovery", "manifest-r1", "e2e-real-plan"))
        hub = Path(self.tree["mailbox_hub"])
        seat_root = Path(self.tree["seat_root"])
        applied = str(manager("Apply", "ssss", (plan_handle, plan_digest, "e2e-real-apply", "ab" * 32)))
        record = self.wait_terminal(applied)
        receipt = self.receipt(applied)
        self.assertEqual(record["state"], "completed", receipt.get("evidence"))
        by_kind = {item["kind"]: item for item in receipt["evidence"]}
        self.assertEqual(by_kind["mailbox-probe"]["evidence"]["durable_round_trip"], "true")
        self.assertEqual(by_kind["mailbox-probe"]["evidence"]["mailbox_hub_source"], "executor-config")
        self.assertEqual([p for p in hub.rglob("*") if p.is_file()], [])  # transient probes only
        self.assertTrue((seat_root / ".config" / "systemd" / "user").is_dir())

        verified = str(manager("Verify", "sss", ("recovery", plan_handle, "e2e-real-verify")))
        self.assertEqual(self.wait_terminal(verified)["state"], "completed", self.receipt(verified))
        self.assertEqual(self.receipt(verified)["mutations"], [])

        rolled = str(manager("Rollback", "sss", (receipt["receipt_handle"], "e2e-real-rollback", "cd" * 32)))
        record = self.wait_terminal(rolled)
        self.assertEqual(record["state"], "completed", self.receipt(rolled))
        self.assertEqual(sorted(seat_root.iterdir()), [])  # every created file and directory restored away
        print(
            json.dumps(
                {"gate": "E29", "cell": "real-plan-apply-verify-rollback", "fault": "none",
                 "outcome": "completed", "evidence_label": "fixture", "transport": "private-bus"}
            )
        )


if __name__ == "__main__":  # must stay LAST
    unittest.main()
