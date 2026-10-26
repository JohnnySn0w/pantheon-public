"""Disposable-host fixture for the host-substrate root helper (T-2918, ORG-052 Unit 6b).

TEST TIER ONLY. Nothing here is importable from a live path (a test asserts it).

The "disposable host" is an unprivileged user-namespace sandbox built from this
host's own binaries, with no new dependency and no container image:

* ``podman unshare`` (via ``sg <primary-group>`` when the session gid differs
  from the passwd gid) enters the account's rootless user namespace with the
  full subuid map, so uid 0 inside can chown to the fixture uids (3101, 3201...);
* ``unshare --mount`` gives a private mount namespace, where a tmpfs carries a
  private copy of ``/etc`` (synthesized shadow, gshadow and sudoers), plus
  empty ``/home``, ``/srv`` and ``/var/lib/systemd``, each bind-mounted over the
  real path; the tree is rbound at ``/mnt`` (staging, a read-only repo view at
  ``/mnt/repo``) and ``/tmp``, ``/var/tmp`` and ``/dev/shm`` are fresh private
  tmpfs too. Nothing
  with a subuid owner ever lands on disk: the tmpfs vanishes with the namespace;
* fake ``loginctl`` / ``systemctl`` are bind-mounted over ``/usr/bin`` INSIDE the
  namespace only (systemd does not run in a userns): linger and unit-enable
  evidence at this tier is "fake-port tier" (ORG-052 gate 8), real activation is
  T-2924's;
* ``sudo -l -U`` is NEVER executed (T-2918 ruling A1, the T-1345 worker guard):
  ``cvtsudoers`` (a non-setuid policy parser) supplies "parser-derived" evidence;
  real ``sudo -l`` is routed to T-2924.

Footprint (T-2918 ruling A5, accepted): rootless podman keeps one ``catatonit -P``
pause process alive for the account after the first ``podman unshare``.

Every command runs as a fixed argv list through the in-sandbox agent, which
refuses unless the per-run marker exists in the PRIVATE ``/etc`` and the binary
is allowlisted. The real host is fingerprinted before and after every test
session (conftest.py), and an audit hook blocks any real-host write.
"""
