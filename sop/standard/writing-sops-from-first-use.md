# Writing SOPs from first use (stubs, and filling them at time of action)

**Status:** Adopted 2026-10-07 by Orchestrator (HO) at the operator's direction (Telegram 11309-11316: "we do want these things to be sops, but we don't really have an automable way to make sure those sops are made at time of action, except maybe to have stub or explicitly 'placeholder' text"; "Sop stuff sounds right. This in itself, could be an sop"). First applied the same night, to `repository-extraction.md` and `assembly-update.md`. The resolve-time forcing hook (step 3) is enforced by `ticket.py` since T-4680.

## When to use this

- You are about to write an SOP for an operation **nobody has done yet**. Write a stub, not a procedure.
- You are about to **do an operation whose SOP is a stub**. You are its first run, so you write the procedure.

Detailed steps written before anyone has done the thing are guesses that look like procedure. The first real run usually contradicts them, and readers trust them anyway.

## Procedure

1. **Write the stub.** Keep it short:
   - a status line, `STUB, not yet run`, with the reason and date;
   - **When to use this** (and when not);
   - **Invariants known now**: the 3-6 rules that are true regardless of how the steps turn out (for example, "the lock is generated, never hand-edited");
   - an empty **First run: record the actual procedure here** section;
   - cross-references to the authority (the ADR) and to neighbouring SOPs.
   If a detailed draft already exists, don't keep it in the SOP; point to it in git history as notes.
2. **Label the work.** A ticket that performs a stubbed operation carries the label `sop:<sop-file-stem>` (for example `sop:repository-extraction`).
3. **Fill it before closing.** Before resolving that ticket, the person who did the work replaces the First-run section with the steps actually taken, the commands that worked, and what surprised them. The status then flips from STUB, and the operator ratifies at surface time (ORG-010). If the run taught nothing new, say so explicitly in the resolve note.
   *Enforced (T-4680):* `ticket.py resolve` refuses a `sop:<x>` ticket while `sop/standard/<x>.md` is still a STUB, unless the SOP changed in the ticket's arc: a landed commit that touches it and names the ticket id, or `--sop-updated <sha>` citing the landed commit (after a squash merge, the squash sha). If the run taught nothing new, pass `--sop-no-change <why>`; the reason is recorded in the resolve note. A `sop:` label that names a missing SOP is refused too.
4. **Keep stubs visible.** The status line starts with `STUB` so the SOP index and any rendered skill show it. An agent who picks one up knows to take notes.

## Common failure modes

- **Over-specifying the stub.** It turns back into a guessed procedure. Keep it to invariants.
- **Filling it from memory a week later.** The value is capturing it while it's fresh, in the same ticket.
- **Stubbing an operation that is already routine.** If it has been done several times, write the real SOP from those runs instead.

## Boundary

How to stage, write and fill not-yet-practised SOPs. It does not cover the SOP file shape in general (`sop/standard/README.md`), or skill rendering (`tools/persona_orientation/`).

## Cross-references

- `architecture/adr/ORG-010-three-tier-policies-extension.md`: surface-time ratification.
- `sop/standard/repository-extraction.md`, `sop/standard/assembly-update.md`: the first two stubs.
- T-4680: the resolve-time forcing hook.
