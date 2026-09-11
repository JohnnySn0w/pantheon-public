"""Manual live-bus verification for `tools.persona_controller_health_dbus`
(T-2969 BT3 / F2, REBOUND finding).

NOT a pytest test module -- deliberately named without a `test_`/`_test`
prefix so pytest's default collection never picks it up (see
`tools/conftest.py`'s absence of a `python_files` override; default glob
is `test_*.py`/`*_test.py`, which this filename doesn't match). Run it
directly:

    python3 -m tools.manual.persona_controller_health_dbus_livebus_verify

Why this exists instead of a committed pytest test: the exact operations
below (private `dbus-daemon --session` spinup, `dbus.SessionBus()`
connect, `start_service()`, a real wire-level `GetAll()` round trip via a
second connection) complete cleanly in under two seconds run this way,
but reproducibly HUNG (60-90s+) when the identical calls ran inside a
`unittest.TestCase` under this environment's pytest -- narrowed to a
`setUp`/teardown + background-GLib-mainloop-thread interaction, not a
defect in the module itself (see `tools/test_persona_controller_health_
dbus.py`'s module docstring for the full writeup). A hanging committed
test would block the smoke loop indefinitely, which is worse than no
test; this script is the reproducible evidence trail instead, and
doubles as a live-fire recipe for a future test-bench that CAN host the
subprocess-driver shape `tools/test_host_control_health_dbus.py` uses.

This script is also the regression proof for the real bug T-2969 found:
`start_service()` originally discarded the `dbus.service.BusName(...)`
return value, so CPython garbage-collected it almost immediately
(`BusName.__del__` releases the claimed name on GC) -- a caller's very
next lookup saw `ServiceUnknown` instead of ever reaching the exported
object. Fixed by retaining the handle on `PersonaController1Object` as
`bus_name_handle`. `verify_busname_retained` below is that regression
check: a second connection requesting the SAME name with
`DO_NOT_QUEUE` must observe `REPLY_EXISTS`, not `REPLY_PRIMARY_OWNER`
(the latter would mean the first claim was already released).

Exit code 0 and "ALL CHECKS PASSED" on success; nonzero and a printed
failure on any check that doesn't hold.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_PERMISSIVE_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow own="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""


def _spawn_private_bus(work_dir: Path) -> tuple[subprocess.Popen, str]:
    socket = work_dir / "bus.sock"
    config_path = work_dir / "bus.conf"
    config_path.write_text(_PERMISSIVE_BUS_CONFIG.format(socket=socket), encoding="utf-8")
    daemon = subprocess.Popen(
        ["dbus-daemon", f"--config-file={config_path}", "--nofork"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not socket.exists():
        if time.monotonic() > deadline:
            raise SystemExit("FAIL: private dbus-daemon never listened")
        if daemon.poll() is not None:
            raise SystemExit("FAIL: private dbus-daemon exited early")
        time.sleep(0.05)
    return daemon, f"unix:path={socket}"


def verify_busname_retained(pchd, bus_address: str) -> None:
    """Regression check for the BusName-GC bug (see module docstring)."""
    import dbus
    import dbus.bus

    snapshot = {name: ("" if sig == "s" else []) for name, sig in pchd.PERSONA_CONTROLLER_PROPERTY_SIGNATURES.items()}
    service = pchd.start_service("livebusverifyclaim", lambda: snapshot)
    assert service is not None, "FAIL: start_service returned None"

    second_connection = dbus.bus.BusConnection(bus_address)
    DBUS_NAME_FLAG_DO_NOT_QUEUE = 0x4
    DBUS_REQUEST_NAME_REPLY_EXISTS = 3
    result = second_connection.request_name(
        pchd.bus_name_for("livebusverifyclaim"), DBUS_NAME_FLAG_DO_NOT_QUEUE
    )
    assert result == DBUS_REQUEST_NAME_REPLY_EXISTS, (
        f"FAIL: expected REPLY_EXISTS (3) -- name was NOT retained, got {result!r}. "
        "This is the BusName-GC regression: start_service()'s claimed name was "
        "released before this check ran."
    )
    print("PASS: start_service() retains its claimed bus name (BusName-GC regression check)")


def verify_wire_level_getall(pchd, bus_address: str) -> None:
    """Full wire round trip: a SECOND connection resolves the exported
    object by name and calls GetAll over the bus (not a direct Python
    method call) -- the scenario that hung under pytest but completes
    cleanly here."""
    import dbus
    import dbus.bus

    snapshot = {
        "ActiveRuntime": "claude/anthropic-code",
        "ViabilityStatus": "viable",
        "LastSelectionTs": "2026-08-19T00:00:00Z",
        "LastFailureClass": "",
        "NextCooldownExpiryTs": "",
        "Holds": [],
    }
    pchd.start_service("livebusverifywire", lambda: snapshot)

    client = dbus.bus.BusConnection(bus_address)
    obj = client.get_object(pchd.bus_name_for("livebusverifywire"), pchd.object_path_for("livebusverifywire"))
    props = dbus.Interface(obj, dbus_interface="org.freedesktop.DBus.Properties")
    result = props.GetAll(pchd.PERSONA_CONTROLLER_INTERFACE, timeout=10.0)

    assert str(result["ActiveRuntime"]) == "claude/anthropic-code", f"FAIL: {dict(result)!r}"
    assert str(result["ViabilityStatus"]) == "viable", f"FAIL: {dict(result)!r}"
    assert list(result["Holds"]) == [], f"FAIL: {dict(result)!r}"
    print("PASS: wire-level GetAll() round trip via a second connection returns the provider's snapshot")


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from tools import persona_controller_health_dbus as pchd

    if not pchd.DBUS_AVAILABLE:
        print("SKIP: dbus-python not available in this environment")
        return 0

    work = tempfile.mkdtemp(dir="/tmp")
    daemon, bus_address = _spawn_private_bus(Path(work))
    previous = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = bus_address
    try:
        verify_busname_retained(pchd, bus_address)
        verify_wire_level_getall(pchd, bus_address)
    finally:
        daemon.terminate()

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    # T-2969 finding (surfaced during this script's own construction):
    # once `ensure_glib_mainloop_running()` has started its background
    # daemon thread, a NORMAL Python interpreter shutdown in this
    # environment's dbus-python/GLib stack does not complete cleanly --
    # observed as a silent, no-traceback abort at shutdown standalone,
    # and as an indefinite hang under pytest (see
    # `tools/test_persona_controller_health_dbus.py`'s module docstring
    # for the pytest side of this). The checks above all complete and
    # report correctly BEFORE this point; only the ordinary cleanup path
    # afterward is unreliable. `os._exit()` skips atexit/thread-join/
    # finalizer machinery entirely, sidestepping it -- verified: with a
    # normal `sys.exit()` here this script's process exit crashed 100%
    # of the time despite every check already having printed PASS.
    #
    # Production relevance (flagged to HO/BT2, not fixed here -- out of
    # this script's scope): `controller_main`'s production process ALSO
    # starts this same background thread and then returns normally,
    # going through the identical interpreter-shutdown path. Under
    # `Restart=always`, an unclean/crashy exit is harmless (systemd
    # restarts on any exit code), but if this fault can also HANG rather
    # than crash (as observed under pytest) in the production process,
    # that would threaten the SS87 restart-fixable invariant -- BT2
    # should confirm the unit's stop/timeout behavior tolerates this, or
    # `controller_main`'s own exit path should adopt the same
    # `os._exit()` pattern this script uses.
    try:
        exit_code = main()
    except AssertionError as exc:
        print(str(exc))
        exit_code = 1
    except SystemExit as exc:
        print(exc)
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
