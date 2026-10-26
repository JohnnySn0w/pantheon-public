# CI in this seed — opt-in, your call

This seed ships **no active CI** (no `.github/workflows/*.yml`). What it does
ship is a **byte-sequence discipline guard** in two halves; you already have one,
and the other is here as an opt-in template.

- **Local half (shipped, active once installed):** `.githooks/pre-commit`,
  activated by `tools/install-hooks.sh`. Catches the trigger at author time.
- **Server-side half (opt-in template):**
  `.github/workflows/hermes-audit.yml.template`. Rename it to
  `hermes-audit.yml` to enable — it then runs on GitHub-hosted CI on every push
  and PR, catching at origin even for a `--no-verify` or hookless push. Enabling
  it uses **your** account's Actions minutes.

Enabling the server-side half is **recommended but optional**. Rolling your own
CI that runs `tools/hermes_diff_scan.py` on the push diff is equally fine, and a
less-technical fork can just enable the template as-is. See the template file's
header for the exact steps and the rationale.
