"""Tests for tools/a2a/did_web.py — ORG-047 passport did:web resolution (T-2832).

Pure-local: no network/SDK, so runs under system python3 in the whole-suite smoke.
"""

from __future__ import annotations

import unittest

from tools.a2a.did_web import (
    DidResolutionError,
    ParsedDidDocument,
    parse_did_document,
    resolve_did_web_url,
    select_authentication_key,
)

DID = "did:web:your-domain.example"


def _jwk_vm(vid, alg="ES256", crv="P-256"):
    return {
        "id": vid, "type": "JsonWebKey2020", "controller": DID,
        "publicKeyJwk": {"kty": "EC", "crv": crv, "alg": alg, "x": "a", "y": "b"},
    }


class TestResolveDidWebUrl(unittest.TestCase):
    def test_bare_domain(self):
        self.assertEqual(resolve_did_web_url("did:web:your-domain.example"),
                         "https://your-domain.example/.well-known/did.json")

    def test_with_path(self):
        self.assertEqual(resolve_did_web_url("did:web:your-domain.example:agents:recovery"),
                         "https://your-domain.example/agents/recovery/did.json")

    def test_with_percent_encoded_port(self):
        self.assertEqual(resolve_did_web_url("did:web:localhost%3A3000"),
                         "https://localhost:3000/.well-known/did.json")

    def test_rejects_non_did_web(self):
        for bad in ("did:key:z6Mk", "https://your-domain.example", "", "did:web:"):
            with self.assertRaises(DidResolutionError):
                resolve_did_web_url(bad)

    def test_rejects_bad_host(self):
        with self.assertRaises(DidResolutionError):
            resolve_did_web_url("did:web:has spaces")

    def test_rejects_empty_path_segment(self):
        with self.assertRaises(DidResolutionError):
            resolve_did_web_url("did:web:your-domain.example::recovery")

    def test_rejects_dot_dot_path_segment(self):
        # T-2867 DIDWEB-3: a ".." path segment is refused.
        with self.assertRaises(DidResolutionError):
            resolve_did_web_url("did:web:your-domain.example:..")

    def test_rejects_encoded_slash_path_segment(self):
        # T-2867 DIDWEB-3: a segment that percent-decodes to carry a slash is refused.
        with self.assertRaises(DidResolutionError):
            resolve_did_web_url("did:web:your-domain.example:a%2Fb")


class TestParseDidDocument(unittest.TestCase):
    def _doc(self, auth):
        return {
            "id": DID,
            "verificationMethod": [_jwk_vm(f"{DID}#k1")],
            "authentication": auth,
        }

    def test_authentication_by_reference(self):
        parsed = parse_did_document(self._doc([f"{DID}#k1"]), DID)
        self.assertIsInstance(parsed, ParsedDidDocument)
        self.assertEqual(len(parsed.authentication), 1)
        self.assertEqual(parsed.authentication[0].id, f"{DID}#k1")
        self.assertEqual(parsed.authentication[0].algorithm, "ES256")

    def test_authentication_embedded(self):
        doc = {"id": DID, "verificationMethod": [],
               "authentication": [_jwk_vm(f"{DID}#embedded")]}
        parsed = parse_did_document(doc, DID)
        self.assertEqual(parsed.authentication[0].id, f"{DID}#embedded")

    def test_id_mismatch_rejected(self):
        with self.assertRaises(DidResolutionError):
            parse_did_document(self._doc([f"{DID}#k1"]), "did:web:evil.com")

    def test_unresolvable_auth_reference_rejected(self):
        with self.assertRaises(DidResolutionError):
            parse_did_document(self._doc([f"{DID}#missing"]), DID)

    def test_empty_authentication_rejected(self):
        with self.assertRaises(DidResolutionError):
            parse_did_document(self._doc([]), DID)

    def test_duplicate_kid_rejected(self):
        doc = {"id": DID,
               "verificationMethod": [_jwk_vm(f"{DID}#k1"), _jwk_vm(f"{DID}#k1", alg="RS256")],
               "authentication": [f"{DID}#k1"]}
        with self.assertRaises(DidResolutionError):
            parse_did_document(doc, DID)

    def test_embedded_authentication_duplicate_kid_rejected(self):
        # T-2867 DIDWEB-1: the distinct-kid (D2) invariant must span BOTH passes —
        # an embedded authentication method reusing a top-level verificationMethod
        # kid (with different key material) must fail closed, else a first-match-wins
        # consumer silently trusts a shadowed/stale key.
        doc = {"id": DID,
               "verificationMethod": [_jwk_vm(f"{DID}#k1")],
               "authentication": [_jwk_vm(f"{DID}#k1", alg="RS256")]}
        with self.assertRaises(DidResolutionError):
            parse_did_document(doc, DID)

    def test_two_embedded_authentication_same_kid_rejected(self):
        # T-2867 DIDWEB-1: two embedded authentication methods sharing a kid must
        # also fail closed (the shared `seen` set catches the second).
        doc = {"id": DID, "verificationMethod": [],
               "authentication": [_jwk_vm(f"{DID}#e1"), _jwk_vm(f"{DID}#e1", alg="RS256")]}
        with self.assertRaises(DidResolutionError):
            parse_did_document(doc, DID)

    def test_key_without_material_rejected(self):
        doc = {"id": DID,
               "verificationMethod": [{"id": f"{DID}#k1", "type": "X", "controller": DID}],
               "authentication": [f"{DID}#k1"]}
        with self.assertRaises(DidResolutionError):
            parse_did_document(doc, DID)

    def test_non_object_document_rejected(self):
        with self.assertRaises(DidResolutionError):
            parse_did_document(["not", "a", "doc"], DID)  # type: ignore[arg-type]

    def test_multibase_key_and_type_algorithm(self):
        doc = {"id": DID,
               "verificationMethod": [{"id": f"{DID}#mb", "type": "Multikey",
                                       "controller": DID, "publicKeyMultibase": "z6Mkxyz"}],
               "authentication": [f"{DID}#mb"]}
        parsed = parse_did_document(doc, DID)
        vm = parsed.authentication[0]
        self.assertEqual(vm.public_key_multibase, "z6Mkxyz")
        self.assertEqual(vm.algorithm, "Multikey")  # falls back to type


class TestSelectAuthenticationKey(unittest.TestCase):
    def _parsed(self, *vms):
        doc = {"id": DID, "verificationMethod": list(vms),
               "authentication": [vm["id"] for vm in vms]}
        return parse_did_document(doc, DID)

    def test_selects_matching_algorithm(self):
        parsed = self._parsed(_jwk_vm(f"{DID}#es", "ES256"), _jwk_vm(f"{DID}#rs", "RS256"))
        vm = select_authentication_key(parsed, required_algorithm="ES256")
        self.assertEqual(vm.id, f"{DID}#es")

    def test_no_matching_algorithm_rejected(self):
        parsed = self._parsed(_jwk_vm(f"{DID}#es", "ES256"))
        with self.assertRaises(DidResolutionError):
            select_authentication_key(parsed, required_algorithm="RS256")

    def test_ambiguous_algorithm_rejected(self):
        parsed = self._parsed(_jwk_vm(f"{DID}#a", "ES256"), _jwk_vm(f"{DID}#b", "ES256"))
        with self.assertRaises(DidResolutionError):
            select_authentication_key(parsed, required_algorithm="ES256")

    def test_duplicate_reference_is_not_ambiguous(self):
        # T-2867 DIDWEB-2: the same key referenced twice in `authentication` is ONE
        # key, not an ambiguous pair — it must resolve, not fail closed as ambiguous.
        doc = {"id": DID, "verificationMethod": [_jwk_vm(f"{DID}#k1", "ES256")],
               "authentication": [f"{DID}#k1", f"{DID}#k1"]}
        parsed = parse_did_document(doc, DID)
        vm = select_authentication_key(parsed, required_algorithm="ES256")
        self.assertEqual(vm.id, f"{DID}#k1")


if __name__ == "__main__":
    unittest.main()
