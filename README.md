# Pantheon

Pantheon runs a small team of AI coding agents on one Linux server and keeps them
working as an organization. They have roles, a chain of command, a shared ticket
queue, written rules, and recovery when something breaks.

This repository is the **framework**: the rules, the procedures and the tooling.
It does not include the history of the live instance it was extracted from.
Fork it to run your own.

## How it works

- **A few long-lived agents, each with a job.** The default roles are:
  - an `orchestrator`, which plans the work and hands it out;
  - a `specialist`, which does research and watches for news;
  - a `recovery` agent, which notices when another agent is stuck or down and
    brings it back.

  Each one runs as its own Unix account on the host, under systemd.
- **Work moves through tickets.** Agents file, claim and close tickets with
  `tools/ticket.py`. Decisions are recorded in an append-only ledger. Your fork
  starts both empty.
- **Rules are written down.** `rulebook/` holds the principles and standing
  directives. Agents cite them when they justify a decision, so you can audit why
  something was done.
- **Code changes go through review.** Worker agents write code on a branch. A
  separate reviewer agent checks it, and only the orchestrator merges.
- **Agents talk to each other by mailbox.** That's a small file-based message hub.
  Optionally, an agent can talk to you over Telegram.

## What's in here

| Path | What it is |
|---|---|
| `rulebook/` | Principles, directives, policies and the goals registry. Start here to see how the agents are meant to behave. |
| `architecture/adr/` | Design decisions (`ORG-NNN`). Each one records what was decided and why. |
| `sop/` | Step-by-step procedures. `sop/standard/` covers how to do things; `sop/remediation/` covers what to do when something breaks. |
| `tools/` | The scripts the agents and the host run: tickets, mailbox, monitors, the runtime controller and the installer. |
| `skills/` | Agent skills that load the right procedure at the right moment. |
| `bugs/`, `lessons/` | Gotchas and lessons learned, kept generic so they apply to any instance. |
| `knowledge/` | The glossary and infrastructure notes. |
| `instance.yaml`, `repos.yaml` | The live instance's configuration, shipped as a worked example. **Replace these with your own.** |
| `pinned-rules/` | Example per-role briefs. Write your own. |
| `.githooks/` | Commit hooks. Install them with `tools/install_hooks.sh`. |

## Getting started

1. **Read `sop/standard/pantheon-bringup.md`.** It takes a fresh Linux host to a
   working fleet: accounts, agent installs, services, mailbox, monitors and
   verification. It has been tested by people following the doc alone.
2. **Replace `instance.yaml` and `repos.yaml`** with your own roles, accounts and
   repositories. The shipped files are labelled as examples.
3. **Name your agents if you want.** The role names (`orchestrator`, `specialist`,
   `recovery`) are what the tooling keys on. Persona names are optional labels you
   layer on top; the bring-up guide shows where each name plugs in.
4. **Install the commit hooks:** `tools/install_hooks.sh`.
5. **Optional CI.** No workflows run by default. See `.github/CI.md` for the
   ready-made template and how to enable it.

You need a Linux host with systemd, and a Claude Code (or compatible) account for
each agent.

## Known rough edges

- The recovery tools (`tools/restart_drain.py`, `tools/context_fill_monitor.py`)
  come with the live instance's service names filled in. If yours differ, they
  currently fail **silently**. Section 6 of the bring-up guide explains what to
  change.
- Many documents mention `ORG-NNN` decisions and ticket numbers (`T-NNNN`). The
  decisions are all in `architecture/adr/`. The ticket numbers point into the live
  instance's history, which this repo leaves out on purpose.

## Updates

This repo is refreshed from the live instance as the framework improves. The
framework paths here and your fork's own history (tickets, ledger, notes) never
overlap, so you can pull framework updates without touching your history. Don't
push changes back upstream. The full model is in
`architecture/adr/ORG-029-pantheon-distribution-model.md`.

## How this copy is made

The repo is generated, not hand-edited. A script extracts only the framework
paths from the live instance's repository, renames the live agents to the generic
roles, scrubs personal and host-specific details, and starts the git history
fresh. Every refresh is checked for leaks before it is published.
