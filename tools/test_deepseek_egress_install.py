"""T-6192: deterministic inert install/update/remove/rollback artifacts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools import deepseek_egress_install as installer_mod
from tools.deepseek_egress_firewall import render_firewall
from tools.deepseek_egress_install import InstallError, Installer, _cert_source, main
from tools.deepseek_egress_proxy import ProxyPolicy


def _policy() -> ProxyPolicy:
    return ProxyPolicy.from_mapping(
        {
            "schema": "pantheon-deepseek-egress/v1",
            "local_authority": "deepseek-proxy.invalid:8448",
            "upstream_hostname": "api.deepseek.com",
            "upstream_port": 443,
            "routes": [
                {"id": "chat", "method": "POST", "local_path": "/v1/chat/completions", "upstream_path": "/v1/chat/completions", "json": True},
                {"id": "balance", "method": "GET", "local_path": "/v1/user/balance", "upstream_path": "/user/balance", "json": False},
            ],
            "limits": {"request_body_bytes": 65536, "response_body_bytes": 1048576, "header_bytes": 16384},
            "timeouts": {"connect_seconds": 3, "read_seconds": 15, "total_seconds": 60},
            "accounts": {"seat": "deepseek-fallback", "proxy": "pantheon-deepseek-proxy"},
        }
    )


def _uids(name: str) -> int:
    return {"deepseek-fallback": 2101, "pantheon-deepseek-proxy": 2102}[name]


def _new_certs(directory: Path, hostname: str = "deepseek-proxy.invalid", *, ca_name: str = "test-ca") -> dict[str, bytes]:
    directory.mkdir(parents=True, exist_ok=True)
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.pem"
    leaf_key = directory / "leaf.key"
    leaf_csr = directory / "leaf.csr"
    leaf_cert = directory / "leaf.pem"
    leaf_ext = directory / "leaf.ext"
    leaf_ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        f"subjectAltName=DNS:{hostname}\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n",
        encoding="ascii",
    )
    subprocess.run(
        [
            "/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", f"/CN={ca_name}",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash",
            "-keyout", ca_key, "-out", ca_cert,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "/usr/bin/openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-subj", f"/CN={hostname}", "-keyout", leaf_key, "-out", leaf_csr,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "/usr/bin/openssl", "x509", "-req", "-days", "1", "-sha256", "-in", leaf_csr,
            "-CA", ca_cert, "-CAkey", ca_key, "-CAcreateserial", "-extfile", leaf_ext, "-out", leaf_cert,
        ],
        check=True,
        capture_output=True,
    )
    return {
        "ca.pem": ca_cert.read_bytes(),
        "proxy-cert.pem": leaf_cert.read_bytes(),
        "proxy-key.pem": leaf_key.read_bytes(),
    }


_CERTS: dict[str, bytes] = {}


def _certs() -> dict[str, bytes]:
    assert _CERTS
    return dict(_CERTS)


UFW4 = b"""# minimal fixture from /usr/share/ufw/iptables/before.rules sha256=d63bc8bb2df0d20909267b7b40cb5cc187baba6e0baf95bbf71ac14f92df8e6c\n*filter\n:ufw-before-output - [0:0]\n-A ufw-before-output -o lo -j ACCEPT\n-A ufw-before-output -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\nCOMMIT\n"""
UFW6 = b"""# minimal fixture from /usr/share/ufw/iptables/before6.rules sha256=e31099b46c2ae40afa92e90dbe5d575c438327e1a4374355872f5eb37db3307b\n*filter\n:ufw6-before-output - [0:0]\n-A ufw6-before-output -o lo -j ACCEPT\n-A ufw6-before-output -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\nCOMMIT\n"""


def _seed_ufw(root: Path) -> None:
    directory = root / "etc/ufw"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "before.rules").write_bytes(UFW4)
    (directory / "before6.rules").write_bytes(UFW6)


@pytest.fixture(autouse=True)
def _captured_ufw_fixture(tmp_path: Path):
    _seed_ufw(tmp_path)


@pytest.fixture(scope="session", autouse=True)
def _real_certificate_bundle(tmp_path_factory: pytest.TempPathFactory):
    global _CERTS
    _CERTS = _new_certs(tmp_path_factory.mktemp("deepseek-egress-certs"))


def test_firewall_render_has_v4_v6_parity_and_only_proxy_seat_allowance():
    rendered = render_firewall(_policy(), seat_uid=2101, proxy_uid=2102)
    assert set(rendered) == {"v4", "v6"}
    for family, text in rendered.items():
        assert "--uid-owner 2101" in text
        assert "--uid-owner 2102" in text
        assert "--dport 8448" in text
        assert "REJECT" in text
        assert text.index("P-DEEPSEEK-EGRESS-BEGIN") < text.index("-o lo")
        assert text.count("--uid-owner 2101") >= 2
    assert "127.0.0.1/32" in rendered["v4"]
    assert "::1/128" in rendered["v6"]
    v4 = rendered["v4"].splitlines()
    v6 = rendered["v6"].splitlines()
    assert "-A ufw-before-output -m owner --uid-owner 2101 -o lo -p tcp -d 127.0.0.1/32 --dport 8448 -j ACCEPT" in v4
    assert "-A ufw-before-output -m owner --uid-owner 2101 -j REJECT" in v4
    assert "-A ufw6-before-output -m owner --uid-owner 2101 -o lo -p tcp -d ::1/128 --dport 8448 -j ACCEPT" in v6
    assert "-A ufw6-before-output -m owner --uid-owner 2101 -j REJECT" in v6
    seat_accepts = [line for line in v4 + v6 if "--uid-owner 2101" in line and "-j ACCEPT" in line]
    assert len(seat_accepts) == 2
    assert all("--dport 8448" in line and "-o lo" in line for line in seat_accepts)
    assert not any("--uid-owner 2101" in line and ("--dport 53" in line or "--dport 443" in line or "-p udp" in line) and "-j ACCEPT" in line for line in v4 + v6)
    v4_other_uid_reject = "-A ufw-before-output -o lo -p tcp -d 127.0.0.1/32 --dport 8448 -j REJECT"
    v6_other_uid_reject = "-A ufw6-before-output -o lo -p tcp -d ::1/128 --dport 8448 -j REJECT"
    assert v4.index(v4_other_uid_reject) > v4.index("-A ufw-before-output -m owner --uid-owner 2101 -o lo -p tcp -d 127.0.0.1/32 --dport 8448 -j ACCEPT")
    assert v6.index(v6_other_uid_reject) > v6.index("-A ufw6-before-output -m owner --uid-owner 2101 -o lo -p tcp -d ::1/128 --dport 8448 -j ACCEPT")


def test_installer_refuses_live_root_without_separate_flag():
    with pytest.raises(InstallError):
        Installer(Path("/"), _policy(), uid_lookup=_uids, certificate_factory=_certs)


def test_install_is_deterministic_idempotent_and_preserves_valid_certificates(tmp_path: Path):
    calls = []

    def certs():
        calls.append(1)
        return _certs()

    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=certs)
    first = installer.install()
    snapshot = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    second = installer.install()
    assert first.policy_digest == second.policy_digest == _policy().digest
    assert snapshot == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert calls == [1]


def test_update_changes_policy_bound_artifacts_but_preserves_certificates(tmp_path: Path):
    calls = []

    def certs():
        calls.append(1)
        return _certs()

    first_policy = _policy()
    Installer(tmp_path, first_policy, uid_lookup=_uids, certificate_factory=certs).install()
    cert_before = (tmp_path / "etc/pantheon/deepseek-egress/proxy-cert.pem").read_bytes()
    raw = first_policy.as_mapping()
    raw["limits"]["response_body_bytes"] = 2 * 1024 * 1024
    second_policy = ProxyPolicy.from_mapping(raw)
    receipt = Installer(tmp_path, second_policy, uid_lookup=_uids, certificate_factory=certs).install()
    assert receipt.policy_digest == second_policy.digest != first_policy.digest
    assert (tmp_path / "etc/pantheon/deepseek-egress/proxy-cert.pem").read_bytes() == cert_before
    assert calls == [1]


def test_install_rollback_restores_every_prior_byte_after_nth_write_failure(tmp_path: Path):
    hosts = tmp_path / "etc/hosts"
    hosts.parent.mkdir(parents=True, exist_ok=True)
    hosts.write_bytes(b"127.0.0.1 localhost\n")
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    before = hosts.read_bytes()
    with pytest.raises(InstallError):
        installer.install(fail_after_write=3)
    assert hosts.read_bytes() == before
    assert not (tmp_path / "etc/pantheon/deepseek-egress/policy.json").exists()


def test_nth_write_failure_rolls_back_each_transaction_boundary(tmp_path: Path):
    for failure in range(1, 12):
        root = tmp_path / str(failure)
        _seed_ufw(root)
        hosts = root / "etc/hosts"
        hosts.parent.mkdir(parents=True, exist_ok=True)
        hosts.write_bytes(b"127.0.0.1 localhost\n")
        unrelated = root / "etc/unrelated"
        unrelated.write_bytes(b"preserve-exactly\n")
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        installer = Installer(root, _policy(), uid_lookup=_uids, certificate_factory=_certs)
        with pytest.raises(InstallError):
            installer.install(fail_after_write=failure)
        after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert after == before


def test_remove_is_owned_only_and_repeated_remove_is_clean(tmp_path: Path):
    foreign = tmp_path / "etc/foreign.conf"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("keep\n", encoding="utf-8")
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    installer.install()
    assert installer.remove() is True
    assert installer.remove() is False
    assert foreign.read_text(encoding="utf-8") == "keep\n"


def test_symlink_or_foreign_marker_is_refused_without_mutation(tmp_path: Path):
    policy_path = tmp_path / "etc/pantheon/deepseek-egress/policy.json"
    policy_path.parent.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_text("untouched", encoding="utf-8")
    policy_path.symlink_to(target)
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    with pytest.raises(InstallError):
        installer.install()
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize(
    "relative",
    [
        "etc/pantheon/deepseek-egress/policy.json",
        "etc/pantheon/deepseek-egress/proxy-key.pem",
        "etc/pantheon/deepseek-egress/proxy-cert.pem",
        "etc/systemd/system/pantheon-deepseek-egress-proxy.service",
        "home/deepseek-fallback/.config/systemd/user/pantheon-runtime-controller@.service.d/egress.conf",
        "var/lib/pantheon-deepseek-egress/install-receipt.json",
    ],
)
@pytest.mark.parametrize("shape", ["symlink", "directory"])
def test_every_owned_target_refuses_symlink_or_nonregular(tmp_path: Path, relative: str, shape: str):
    target = tmp_path / "outside"
    target.write_bytes(b"outside\n")
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if shape == "symlink":
        path.symlink_to(target)
    else:
        path.mkdir()
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    with pytest.raises(InstallError):
        installer.install()
    assert target.read_bytes() == b"outside\n"


def test_foreign_marker_and_stale_receipt_refuse_without_overwrite(tmp_path: Path):
    hosts = tmp_path / "etc/hosts"
    hosts.parent.mkdir(parents=True, exist_ok=True)
    hosts.write_text("# P-DEEPSEEK-EGRESS-BEGIN foreign\n127.0.0.1 attacker.invalid\n# P-DEEPSEEK-EGRESS-END foreign\n", encoding="ascii")
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    before = hosts.read_bytes()
    with pytest.raises(InstallError):
        installer.install()
    assert hosts.read_bytes() == before

    hosts.write_text("127.0.0.1 localhost\n", encoding="ascii")
    receipt = installer.install()
    receipt_path = tmp_path / "var/lib/pantheon-deepseek-egress/install-receipt.json"
    raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    raw["policy_digest"] = "0" * 64
    receipt_path.chmod(0o600)
    receipt_path.write_text(json.dumps(raw), encoding="utf-8")
    receipt_path.chmod(0o400)
    managed = tmp_path / "etc/pantheon/deepseek-egress/policy.json"
    before_managed = managed.read_bytes()
    with pytest.raises(InstallError):
        installer.remove()
    assert managed.read_bytes() == before_managed


def test_receipt_is_value_free_and_hashes_random_certificate_material(tmp_path: Path):
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    receipt = installer.install()
    raw = (tmp_path / "var/lib/pantheon-deepseek-egress/install-receipt.json").read_text(encoding="utf-8")
    assert "TEST KEY" not in raw
    parsed = json.loads(raw)
    assert parsed["policy_digest"] == _policy().digest
    assert parsed["files"]["/etc/pantheon/deepseek-egress/proxy-key.pem"]["sha256"]
    assert stat_mode(tmp_path / "etc/pantheon/deepseek-egress/proxy-key.pem") == 0o400


def test_units_wire_loadcredential_and_shared_fail_closed_launch_gate(tmp_path: Path):
    Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs).install()
    unit = (tmp_path / "etc/systemd/system/pantheon-deepseek-egress-proxy.service").read_text()
    dropin = (tmp_path / "home/deepseek-fallback/.config/systemd/user/pantheon-runtime-controller@.service.d/egress.conf").read_text()
    assert "LoadCredential=provider-key:" in unit
    assert "Environment=DEEPSEEK_API_KEY=" not in unit
    assert "--credential ${CREDENTIALS_DIRECTORY}/provider-key" in unit
    assert "ExecStartPre=/usr/bin/python3 /opt/pantheon/tools/deepseek_egress_proxy.py gate" in dropin
    assert "--policy /etc/pantheon/deepseek-egress/policy.json" in dropin
    assert "--ca /etc/pantheon/deepseek-egress/ca.pem" in dropin
    assert "LoadCredential=proxy-cert:/etc/pantheon/deepseek-egress/proxy-cert.pem" in unit
    assert "LoadCredential=proxy-key:/etc/pantheon/deepseek-egress/proxy-key.pem" in unit
    assert "--cert ${CREDENTIALS_DIRECTORY}/proxy-cert" in unit
    assert "--cert-key ${CREDENTIALS_DIRECTORY}/proxy-key" in unit
    assert "Restart=on-failure" in unit and "UMask=0077" in unit
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in unit
    assert "--manifest /etc/pantheon/deepseek-egress/launch-gate.json" in dropin
    assert "Environment=SSL_CERT_FILE=/etc/pantheon/deepseek-egress/ca.pem" in dropin
    assert "Environment=DEEPSEEK_API_KEY=proxy-held-dummy" in dropin
    verifier = (tmp_path / "etc/systemd/system/pantheon-deepseek-egress-firewall-verify.service").read_text()
    timer = (tmp_path / "etc/systemd/system/pantheon-deepseek-egress-firewall-verify.timer").read_text()
    assert "/usr/sbin/iptables-save" not in verifier
    assert "deepseek_egress_firewall_verify.py --verify" in verifier
    assert "--output /run/pantheon-deepseek-egress/firewall.json" in verifier
    assert "RuntimeDirectory=" not in unit
    assert "RuntimeDirectory=pantheon-deepseek-egress" in verifier
    assert "RuntimeDirectoryMode=0755" in verifier
    assert "RuntimeDirectoryPreserve=yes" in verifier
    assert "OnUnitActiveSec=5s" in timer
    manifest = json.loads((tmp_path / "etc/pantheon/deepseek-egress/launch-gate.json").read_text())
    assert manifest["client_auth"] == "proxy-held-dummy"


def test_installed_units_are_byte_exact_committed_templates(tmp_path: Path):
    Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs).install()
    repo_templates = Path(__file__).parents[1] / "harness/hermes/systemd"
    for name in (
        "pantheon-deepseek-egress-proxy.service",
        "pantheon-deepseek-egress-firewall-verify.service",
        "pantheon-deepseek-egress-firewall-verify.timer",
    ):
        assert (tmp_path / "etc/systemd/system" / name).read_bytes() == (repo_templates / name).read_bytes()


def test_firewall_health_runtime_contract_is_traversable_by_unrelated_uid(tmp_path: Path):
    Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs).install()
    verifier = (tmp_path / "etc/systemd/system/pantheon-deepseek-egress-firewall-verify.service").read_text()
    assert "RuntimeDirectoryMode=0755" in verifier
    # A root-owned 0755 directory and 0444 state leave no owner/group-only
    # permission dependency for the distinct seat UID.
    directory_mode = 0o755
    state_mode = 0o444
    assert directory_mode & 0o001
    assert state_mode & 0o004


def test_installer_edits_actual_ufw_files_and_remove_preserves_foreign_bytes(tmp_path: Path):
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    installer.install()
    v4 = (tmp_path / "etc/ufw/before.rules").read_bytes()
    v6 = (tmp_path / "etc/ufw/before6.rules").read_bytes()
    assert v4.startswith(b"# minimal fixture") and v6.startswith(b"# minimal fixture")
    assert v4.index(b"P-DEEPSEEK-EGRESS-BEGIN") < v4.index(b"-o lo -j ACCEPT")
    assert v6.index(b"P-DEEPSEEK-EGRESS-BEGIN") < v6.index(b"-o lo -j ACCEPT")
    assert v4.index(b"P-DEEPSEEK-EGRESS-BEGIN") < v4.index(b"RELATED,ESTABLISHED")
    assert v6.index(b"P-DEEPSEEK-EGRESS-BEGIN") < v6.index(b"RELATED,ESTABLISHED")
    installer.remove()
    assert (tmp_path / "etc/ufw/before.rules").read_bytes() == UFW4
    assert (tmp_path / "etc/ufw/before6.rules").read_bytes() == UFW6


@pytest.mark.parametrize(
    "parent,managed",
    [
        ("etc/pantheon", "etc/pantheon/deepseek-egress/policy.json"),
        ("home/deepseek-fallback", "home/deepseek-fallback/.config/systemd/user/pantheon-runtime-controller@.service.d/egress.conf"),
        ("var/lib", "var/lib/pantheon-deepseek-egress/install-receipt.json"),
    ],
)
def test_intermediate_parent_symlink_refused_without_escape(tmp_path: Path, parent: str, managed: str):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / parent
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallError):
        Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs).install()
    assert not (outside / Path(managed).name).exists()


@pytest.mark.parametrize("uids", [{"deepseek-fallback": 0, "pantheon-deepseek-proxy": 2}, {"deepseek-fallback": 2, "pantheon-deepseek-proxy": 2}, {"deepseek-fallback": -1, "pantheon-deepseek-proxy": 3}])
def test_uid_pair_must_be_positive_nonroot_and_distinct(tmp_path: Path, uids: dict[str, int]):
    with pytest.raises(InstallError):
        Installer(tmp_path, _policy(), uid_lookup=uids.__getitem__, certificate_factory=_certs).install()


def test_unflagged_cli_refuses_without_writes(tmp_path: Path):
    assert main([]) == 64
    assert list(tmp_path.iterdir()) == [tmp_path / "etc"]


def test_post_install_unrelated_ufw_and_hosts_edits_survive_update_and_remove(tmp_path: Path):
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    installer.install()
    for relative, addition in (("etc/hosts", b"192.0.2.2 foreign.invalid\n"), ("etc/ufw/before.rules", b"# foreign-v4\n"), ("etc/ufw/before6.rules", b"# foreign-v6\n")):
        path = tmp_path / relative
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + addition)
        path.chmod(Installer._MODES[relative])
    installer.install()
    installer.remove()
    assert b"foreign.invalid" in (tmp_path / "etc/hosts").read_bytes()
    assert (tmp_path / "etc/ufw/before.rules").read_bytes().endswith(b"# foreign-v4\n")
    assert (tmp_path / "etc/ufw/before6.rules").read_bytes().endswith(b"# foreign-v6\n")


def test_remove_nth_failure_rolls_back_all_bytes(tmp_path: Path):
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    installer.install()
    before = {p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mode & 0o777) for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(InstallError):
        installer.remove(fail_after_write=3)
    after = {p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mode & 0o777) for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_cert_source_refuses_symlink_nonregular_and_wrong_mode(tmp_path: Path):
    for name in ("ca.pem", "proxy-cert.pem", "proxy-key.pem"):
        path = tmp_path / name
        path.write_bytes(b"x")
        path.chmod(0o400)
    target = tmp_path / "outside"
    target.write_bytes(b"secret")
    (tmp_path / "proxy-key.pem").unlink()
    (tmp_path / "proxy-key.pem").symlink_to(target)
    with pytest.raises(InstallError):
        _cert_source(tmp_path)()


def test_install_preflight_refuses_malformed_untrusted_wrong_host_and_mismatched_key(tmp_path: Path):
    good = _new_certs(tmp_path / "good")
    other_ca = _new_certs(tmp_path / "other-ca", ca_name="other-ca")
    wrong_host = _new_certs(tmp_path / "wrong-host-certs", "attacker.invalid")
    other_key = _new_certs(tmp_path / "other-key", ca_name="key-source")
    cases = {
        "bad-ca-pem": {**good, "ca.pem": b"not a certificate\n"},
        "bad-leaf-pem": {**good, "proxy-cert.pem": b"not a certificate\n"},
        "bad-key-pem": {**good, "proxy-key.pem": b"not a key\n"},
        "untrusted-ca": {**good, "ca.pem": other_ca["ca.pem"]},
        "wrong-host": wrong_host,
        "mismatched-key": {**good, "proxy-key.pem": other_key["proxy-key.pem"]},
    }
    for label, bundle in cases.items():
        root = tmp_path / label
        root.mkdir()
        _seed_ufw(root)
        with pytest.raises(InstallError):
            Installer(root, _policy(), uid_lookup=_uids, certificate_factory=lambda bundle=bundle: bundle).install()
        assert not (root / "var/lib/pantheon-deepseek-egress/install-receipt.json").exists()


def test_missing_openssl_fails_closed_at_installer_boundary(tmp_path: Path, monkeypatch):
    assert installer_mod._OPENSSL == "/usr/bin/openssl"
    assert "/usr/bin/openssl" in (installer_mod.__doc__ or "")

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("validator missing")

    monkeypatch.setattr(installer_mod.subprocess, "run", missing)
    with pytest.raises(InstallError, match="^certificate validator unavailable$"):
        Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs).install()
    assert not (tmp_path / "var/lib/pantheon-deepseek-egress/install-receipt.json").exists()


def test_live_root_flag_requires_root_euid():
    if os.geteuid() != 0:
        with pytest.raises(InstallError):
            Installer(Path("/"), _policy(), uid_lookup=_uids, certificate_factory=_certs, allow_live_root=True)


def test_parent_owner_policy_never_allows_seat_uid_below_etc_or_var(tmp_path: Path):
    installer = Installer(tmp_path, _policy(), uid_lookup=_uids, certificate_factory=_certs)
    assert installer._parent_owner_allowed("home/deepseek-fallback", 2101)
    assert not installer._parent_owner_allowed("etc/pantheon", 2101)
    assert not installer._parent_owner_allowed("var/lib", 2101)


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


def test_committed_policy_is_canonical_and_matches_installed_bytes(tmp_path: Path):
    source = Path(__file__).resolve().parents[1] / "harness/hermes/deepseek-egress-policy.json"
    policy = ProxyPolicy.from_mapping(json.loads(source.read_text()))
    assert policy.as_mapping() == _policy().as_mapping()
    Installer(tmp_path, policy, uid_lookup=_uids, certificate_factory=_certs).install()
    assert (tmp_path / "etc/pantheon/deepseek-egress/policy.json").read_bytes() == source.read_bytes()
