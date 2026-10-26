# Obsidian vault conventions — human-friendly repo traversal

**Status:** Adopted 2026-07-07 (Recovery, T-1597; the operator's direction TG 3693/3695: read-only navigation, desktop Obsidian primary). **Scope:** pantheon-config now; the convention is written so any sibling repo can adopt it by reference. **Design capture:** `notes/2026-07-07-repo-traversal-human-friendly.md`.

## What this is

pantheon-config doubles as an **Obsidian vault**: open the repo folder (or its parent — see multi-repo below) as a vault and every `[[wikilink]]`, README index, and ticket frontmatter field becomes navigable/queryable. Read-only: the vault is a *view* over the git checkout; edits still flow through the normal git/ticket discipline.

## Setup (one-time, desktop)

1. Clone (or reuse) a checkout of `pantheon-config`.
2. Obsidian → **Open folder as vault** → pick either:
   - the repo itself (single-repo vault), or
   - a **parent folder** containing several pantheon repos (multi-repo vault) — Obsidian resolves basename wikilinks across all of them, which is how cross-repo refs like echo's `[[ADR-212]]` light up once that repo adopts aliases too.
3. The committed `.obsidian/app.json` gives sane read defaults (preview mode, readable line length, visible frontmatter). Everything else under `.obsidian/` is per-user and gitignored — your workspace layout, themes, and plugins stay yours.
4. Optional but recommended community plugin: **Dataview** (for the live views below).

## Link conventions (what makes it traverse)

| Link form | Resolves because | Rule |
|---|---|---|
| `[[T-1497]]` | tickets are named `T-NNNN.md` | nothing to do — works everywhere |
| `[[ORG-020]]` | every `architecture/adr/ORG-NNN-slug.md` carries `aliases: [ORG-NNN]` frontmatter | new ADRs MUST include the alias line |
| `[[notes/2026-05-10-pantheon-as-contracting-firm.md]]` | vault-relative paths resolve | fine, but bare basenames are shorter |
| `[[feedback_*]] [[reference_*]] [[project_*]] [[user_*]] …` | they DON'T — these name **agent-private memory files** outside the repo | intentional; read them as "an agent has a memory pin by this name" |
| `[[ADR-212]]`, `[[Supplementary Material/…]]` | echo-repo namespace | resolves only in a multi-repo vault once echo adopts aliases; until then a known cross-repo pointer |
| `[[1]]`, `[[single-source: …]]` | historical watch-report citation/annotation idiom | frozen history; **new reports use `[^1]` footnotes and `(single-source: …)` parens** |

## The enforcement gate

`python3 tools/vault_link_check.py` — classifies every wikilink outside code fences and **fails on dead links in curated dirs** (architecture/, rulebook/, sop/, notes/, lessons/, live tickets); historical record dirs (tickets/done/, watch reports, incidents/, ledger/) warn only, because history is never rewritten. Run it after adding cross-referenced docs; suite: `python3 -m unittest tools.test_vault_link_check`.

## Ready-to-paste Dataview views

Create a note (per-user, or under notes/ if it earns it) and paste:

**Ticket board by state**
~~~
```dataview
TABLE state, target, labels, not_before FROM "tickets"
WHERE id
SORT state ASC, file.name DESC
```
~~~

**Open tickets for one agent**
~~~
```dataview
TABLE state, labels FROM "tickets"
WHERE target = "recovery" AND (state = "inbox" OR state = "active")
```
~~~

**ADR index with status**
~~~
```dataview
TABLE status, date FROM "adr"
SORT date DESC
```
~~~

(Ticket/ADR frontmatter already carries these fields; no file changes were needed to make them queryable.)

## Adopting in another repo

1. Name addressable docs by their citable id (`T-NNNN.md`) or add `aliases: [ID]` frontmatter.
2. Commit a minimal `.obsidian/app.json` + the two-line gitignore stanza.
3. Copy or reference `tools/vault_link_check.py` (it only needs a repo root).
4. Keep code-identifier prose in backticks so it never reads as a link.
