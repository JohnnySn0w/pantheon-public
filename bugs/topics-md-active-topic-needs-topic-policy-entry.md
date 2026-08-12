---
symptom: "test_watch_runner.py::UnregisteredTopicPolicyTest::test_every_active_topic_in_the_real_topics_file_is_registered goes RED after adding a non-paused topic to knowledge/watch/topics.md ('active topics missing a TOPIC_POLICY entry: [<slug>]'). knowledge/watch/topics.md (topic definitions + cadence table) and tools/watch_runner.py TOPIC_POLICY (per-topic delivery policy {target, labels, interactive_required}) are a COUPLED change: every active (non-paused) topic MUST have a TOPIC_POLICY entry or its findings fall through to _DEFAULT_POLICY (the T-2433 silent-misroute the test exists to prevent)."
fix: "Add the {target, labels, interactive_required} entry to TOPIC_POLICY in tools/watch_runner.py in the SAME change as the topics.md edit. A topics.md edit is NOT a pure doc edit — it is code-coupled; run the full `python3 -m pytest tools/ -q` (the CI command), never a curated subset, after editing topics.md. Same trap for other structurally-linted trees (bugs/ frontmatter, tickets). Bit T-2808 (grand-goal-competitor-watch added to topics.md without the companion entry, red on main until hotfix 7bbb41976f)."
domain: pantheon, watch, topics, watch_runner, coupling, smoke-discipline, structural-lint
---

# Adding an active topic to topics.md requires a companion TOPIC_POLICY entry

## Symptom
`tools/test_watch_runner.py::UnregisteredTopicPolicyTest::test_every_active_topic_in_the_real_topics_file_is_registered` goes RED after adding a non-`paused` topic to `knowledge/watch/topics.md`:
```
AssertionError: active topics missing a TOPIC_POLICY entry: ['<your-new-slug>']
```

## Cause
`knowledge/watch/topics.md` (topic definitions + cadence table) and `tools/watch_runner.py` `TOPIC_POLICY` (per-topic delivery policy: `{target, labels, interactive_required}`) are a **coupled** change. Every active (non-`paused`) topic MUST have a `TOPIC_POLICY` entry — otherwise its findings fall through to `_DEFAULT_POLICY` (the T-2433 silent-misroute this test was added to prevent). The test enforces the coupling structurally.

## Fix
Add the entry to `TOPIC_POLICY` in `tools/watch_runner.py` in the same change as the topics.md edit:
```python
"<your-new-slug>": {
    "target": "orchestrator",   # or "recovery" for security/billing topics
    "labels": ["watch-finding", ...],
    "interactive_required": False,    # False = deliver to the target's queue for triage
},
```

## Prevention / lesson
A topics.md edit is NOT a pure doc edit — it is code-coupled. Run the **full** `python3 -m pytest tools/ -q` (the CI command), never a curated subset, after editing topics.md. This is the [[feedback_smoke_must_include_ci_command_not_curated_subset]] trap; it bit T-2808 (`grand-goal-competitor-watch` added to topics.md without the companion entry, red on main until the 2026-08-11 hotfix `7bbb41976f`). The general rule: pantheon-config has structural lints on several "doc" trees (bugs/ frontmatter, topics.md↔TOPIC_POLICY, ticket lint) — after editing any of them, run the relevant lint module (or the full suite), because they fail the CI even though the change "looks like a doc."
