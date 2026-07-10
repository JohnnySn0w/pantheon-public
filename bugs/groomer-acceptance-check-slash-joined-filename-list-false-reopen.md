---
symptom: "ticket_grooming acceptance-check auto-reopens a legitimately-done ticket, claiming a file is 'missing' that actually exists"
fix: "in ticket-resolution / Context prose, separate a list of filenames with commas or '+', NEVER '/'; a slash-joined list like 'repos.yaml/instance.yaml/pinned-rules' is extracted as ONE bogus path"
domain: pantheon, ticket-grooming, content-authoring
---

## Symptom

`tools/ticket_grooming.py`'s closed-ticket acceptance-check (`check_acceptance` → `extract_artifacts`, T-0693) auto-reopened T-0882 (done → inbox) with reason *"acceptance-check found repos.yaml/instance.yaml missing"* — even though both `repos.yaml` and `instance.yaml` exist on main. The ticket's Phase-0 work had genuinely landed (instance.yaml @ commit 5d4f0abc, repos.yaml present + later role-keyed).

## Root cause

`extract_artifacts` pulls "repo-relative-looking paths" (a token containing `/` plus a known extension) out of the resolution prose and checks existence. The resolution listed the ORG-030 manifest-class files as a **slash-joined list**: `repos.yaml` + `instance` + `.yaml` + `/pinned-rules` (written with `/` separators). The extractor read the contiguous `repos.yaml/instance.yaml` as a **single path**, which of course does not exist on disk → flagged missing → reopen. Bare filenames (no `/`) are explicitly NOT extracted (ticket_grooming.py ~line 758), so comma-separated lists are safe; slash-separated lists are the trap.

Self-perpetuating risk: quoting the groomer's own *"repos.yaml/instance.yaml missing"* reason verbatim in the re-close resolution re-introduces the bad token → re-flag. The infinite loop is only prevented by the `acceptance-unmet` label idempotency-skip (`_has_acceptance_unmet_label`), which leaves a semantically-wrong "unmet" label on a met ticket.

## Fix

1. **Authoring caution (the cheap, durable fix):** when naming multiple files in resolution/Context prose, separate with commas or `+` (`repos.yaml + instance.yaml + pinned-rules`), never `/`. Applies to HO and every worker writing a resolution.
2. **Recovering a falsely-reopened ticket:** neutralize the slash-joined token in the prose, REMOVE the `acceptance-unmet` label (so the skip-marker doesn't outlive the false flag), and verify clean before re-closing:
   `python3 -c "import sys; sys.path.insert(0,'.'); from tools.ticket_grooming import check_acceptance; import pathlib; print(check_acceptance(pathlib.Path('tickets/done/T-NNNN.md').read_text(), pathlib.Path('.').resolve()))"` → must print `None`.

## Follow-up — RESOLVED (structural fix landed)

The structural hardening was implemented: `extract_artifacts` now splits a slash-joined filename list (`a.yaml/b.yaml`) at each `.<knownext>/` boundary into separate path candidates (`_split_joined_filename_list` / `_EXT_SLASH_RE`), so the authoring caution above is now a backstop rather than the only defense. Landed in **T-0973** (commit `907aa8e2`, 2026-06-08); the critical `.d/`-drop-in edge case (`settings.d/config.yaml`, `mosquitto.service.d/x.conf` must NOT be mis-split) is handled correctly and regression-tested in **T-1056** (commit `43434375`, 2026-06-14). The authoring caution + recovery steps above remain valid as defense-in-depth.
