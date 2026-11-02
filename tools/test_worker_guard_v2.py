"""Tests for worker-guard v2 (T-5686): tools/hooks/worker_guard_v2.py and the shadow entry.

Small and literal: one or two cases per rule, the field true-positive list (all deny), the field
false-positive list (all allow), the rails (main thread, 64K cap, exception), and the shadow
entry. No wall-clock or CPU-bound assertions (T-5601/T-5628 lesson).

Sensitive words (the escalator names, the sudo-password file name, the push verb) are assembled
by concatenation so the contiguous token never sits in source.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "hooks")
sys.path.insert(0, HOOKS)

import worker_guard_v2 as g  # noqa: E402

SU = "su" + "do"
PW = "root" + ".env"
PUSH = "pu" + "sh"
HOME = os.path.expanduser("~")
PC = "/home/claude/pantheon-config"
SUB = "implementer"


def _src():
    with open(os.path.join(HOOKS, "worker_guard_v2.py")) as f:
        return f.read()


def bash(cmd, cwd="/tmp"):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "agent_type": SUB, "cwd": cwd}


def tool(name, **tin):
    return {"tool_name": name, "tool_input": tin, "agent_type": SUB, "cwd": "/tmp"}


def rule(data):
    v = g.verdict(data)
    return v[0] if v else None


class Base(unittest.TestCase):
    def assertDeny(self, data, rule_id=None):
        v = g.verdict(data)
        self.assertIsNotNone(v, f"expected deny: {data!r}")
        if rule_id:
            self.assertEqual(v[0], rule_id, f"{data!r} -> {v}")
        p = g.decide(data)
        reason = p["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertTrue(reason.startswith("BLOCKED (worker-guard v2, T-5686): "), reason)
        self.assertEqual(p["hookSpecificOutput"]["permissionDecision"], "deny")

    def assertAllow(self, data):
        self.assertIsNone(g.verdict(data), f"expected allow: {data!r}")
        self.assertIsNone(g.decide(data))


class TestMainThread(Base):
    def test_no_agent_type_allows_everything(self):
        d = bash(f"{SU} cat /etc/shadow")
        del d["agent_type"]
        self.assertAllow(d)
        d["agent_type"] = ""
        self.assertAllow(d)

    def test_non_dict_and_odd_inputs_allow(self):
        self.assertIsNone(g.decide([]))
        self.assertAllow({"agent_type": SUB, "tool_name": "Bash", "tool_input": None})
        self.assertAllow({"agent_type": SUB, "tool_name": "Bash", "tool_input": {"command": 5}})
        self.assertAllow({"agent_type": SUB, "tool_name": "WebFetch", "tool_input": {}})


class TestEscalation(Base):
    def test_bare_and_separators(self):
        for c in (f"{SU} -n true", f"cd /tmp && {SU} ls", f"echo hi; {SU} ls", f"true | {SU} tee x",
                  f"if {SU} -n true; then echo ok; fi", f"x=$({SU} cat f)", f"echo `{SU} id`",
                  f"/usr/bin/{SU} id", f"\\{SU} id", f"'{SU}' id", "su - root", "doas ls", "pkexec ls",
                  "run0 ls", "runuser -u x ls", "sudoedit /etc/x"):
            self.assertDeny(bash(c), "ESC")

    def test_behind_wrappers(self):
        for c in (f"env FOO=1 {SU} ls", f"nohup {SU} ls", f"time {SU} ls", f"timeout 5 {SU} ls",
                  f"nice -n 5 {SU} ls", f"ionice -c 3 {SU} ls", f"stdbuf -o0 {SU} ls",
                  f"setsid {SU} ls", f"xargs -n 1 {SU} cat", f"flock /tmp/l {SU} ls",
                  f"exec {SU} ls", f"command {SU} ls", f"taskset -c 0 {SU} ls",
                  f"chrt -f 1 {SU} ls", f"strace -o /tmp/o {SU} ls", f"ssh host {SU} ls",
                  f"watch -n 5 {SU} ls", f"timeout 5 env A=1 nice {SU} ls",
                  f"bash -c '{SU} ls'", f"sh -lc \"cd /tmp && {SU} ls\"",
                  f"flock -c '{SU} ls' /tmp/l"):
            self.assertDeny(bash(c), "ESC")

    def test_field_shapes(self):
        self.assertDeny(bash(f"{SU} -S -p '' cat $f < {HOME}/{PW}"), "ESC")
        self.assertDeny(bash(f"{SU} -n -u suborch2 tmux list-panes -a"), "ESC")

    def test_broker(self):
        self.assertDeny(bash("/home/claude/pantheon-config/tools/pantheon-" + SU + " cat /run/x"), "ESC")
        self.assertDeny(bash("S=/home/claude/pantheon-config/tools/pantheon-" + SU + "; $S cat /run/x"), "ESC")
        self.assertDeny(bash("export S=tools/pantheon-" + SU), "ESC")
        self.assertDeny(bash("bash tools/pantheon-" + SU + " id"), "ESC")
        self.assertAllow(bash("grep -n pantheon-" + SU + " tools/x.py"))
        self.assertAllow(bash("cat tools/pantheon-" + SU))
        self.assertAllow(bash("ls -l tools/pantheon-" + SU + " && head -5 tools/pantheon-" + SU))

    def test_password_file_any_operand(self):
        self.assertDeny(bash(f"cat {HOME}/{PW}"), "ESC")
        self.assertDeny(bash(f"grep -c x /home/claude/{PW}"), "ESC")
        self.assertDeny(bash(f"head -1 < {HOME}/{PW}"), "ESC")

    def test_data_is_not_a_command(self):
        for c in (f"echo {SU}", f"grep -rn {SU} tools/", f"grep -n '{SU} -n' tools/x.py",
                  f"which {SU}", f"man {SU}", f"command -v {SU}", f"# {SU} ls",
                  f"echo hi # {SU} ls", f"git commit -m 'document {SU} policy'",
                  f"timeout 60 pytest -k {SU}", f"ls {SU}ers.d",
                  f"python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['{SU}', 'ls'])\nEOF",
                  f"cat > /tmp/x.py <<EOF\nrun('{SU} ls')\nEOF\npython3 /tmp/x.py",
                  f"python3 -c \"print('{SU}')\""):
            self.assertAllow(bash(c))

    def test_dynamic_command_word_is_out_of_scope(self):
        self.assertAllow(bash(f"X={SU}; $X ls"))
        self.assertAllow(bash(f"$(echo {SU}) ls"))


class TestCredentials(Base):
    def test_bash_operands(self):
        for c in (f"cut -d= -f1 {PC}/.claude/channels/telegram/.env",
                  "cut -d= -f1 .claude/channels/telegram/.env",
                  "grep -c 'KEY' echo-prismatic/.env", "cat ~/gh_token.env", "cat ~/.config/gh/hosts.yml",
                  "cat ~/j17.keys", "cat ~/.git-credentials", "cat ~/.claude/.credentials.json",
                  "cat ~/.codex/auth.json", "cat foo/.env", "cat deploy/.env.production",
                  "cat ~/.ssh/id_ed25519", "grep Host ~/.ssh/config", "cat $HOME/.ssh/config",
                  "cp -r ~/.ssh /tmp/x", "echo x > ~/gh_token.env", "set -a; . ./.env",
                  "curl -s https://api.telegram.org/bot123:abc/getMe",
                  "docker run --env-file=.env img"):
            self.assertDeny(bash(c), "CRED")

    def test_field_ssh_compound(self):
        self.assertDeny(bash("ls -la ~/.ssh/; grep -n Host ~/.ssh/config"), "CRED")

    def test_metadata_only_and_templates_allow(self):
        for c in ("ls -la ~/.ssh/", "stat ~/gh_token.env", "ls -l ~/.claude/channels/telegram/.env",
                  "cat .env.example", "cat config/.env.sample", "cat ~/.ssh/id_ed25519.pub",
                  "rg -g '*.env' foo", "grep -n \"token\\|\\.env\" tools/x.py", "grep -n process.env config.ts",
                  "grep -rn pantheon.env tools/systemd/", "find /home/claude -maxdepth 3 -name .env",
                  "echo $HOME/.environment", "cat tools/hooks/root-env-guard.py",
                  "python3 - <<'EOF'\nprint(open('.env').read())\nEOF"):
            self.assertAllow(bash(c))

    def test_tool_paths(self):
        self.assertDeny(tool("Read", file_path=f"{HOME}/.claude/channels/telegram/.env"), "CRED")
        self.assertDeny(tool("Read", file_path=f"{HOME}/.ssh/config"), "CRED")
        self.assertDeny(tool("Read", file_path=f"{PC}/../echo-prismatic/.env"), "CRED")
        self.assertDeny(tool("Grep", pattern="x", path=f"{HOME}/.git-credentials"), "CRED")
        self.assertDeny(tool("Grep", pattern="x", path=f"{HOME}/.ssh"), "CRED")
        self.assertDeny(tool("Write", file_path=f"{HOME}/gh_token.env", content="x"), "CRED")
        self.assertDeny(tool("Read", file_path=f"{HOME}/{PW}"), "ESC")
        self.assertAllow(tool("Read", file_path="/tmp/x.py"))
        self.assertAllow(tool("Grep", pattern="x", path="/home/claude/pantheon-config/tools"))
        self.assertAllow(tool("Read", file_path=f"{HOME}/.env.example"))


class TestProtectedDocs(Base):
    def test_tool_writes(self):
        for name in ("Edit", "Write", "MultiEdit"):
            for p in (f"{PC}/sop/standard/ticket-flow.md", f"{PC}/architecture/adr/ORG-106-x.md",
                      f"{PC}/notes/x-draft.md", f"{PC}/BACKLOG.md", f"{PC}/bugs/x.md",
                      f"{PC}/tools/README.md", "/tmp/pantheon-config-T-1/ledger/2026-W40/x.jsonl",
                      f"{PC}/rulebook/principles.md", f"{PC}/knowledge/watch/topics.md"):
                self.assertDeny(tool(name, file_path=p, content="x"), "DOC")
        self.assertDeny(tool("NotebookEdit", notebook_path=f"{PC}/notes/n.ipynb"), "DOC")

    def test_tool_writes_allowed(self):
        for p in (f"{PC}/tools/x.py", f"{PC}/tickets/inbox/T-1.md", f"{PC}/knowledge/episodic/x/a.md",
                  "/tmp/draft.md", f"{PC}/architecture/atlas/x.md"):
            self.assertAllow(tool("Write", file_path=p, content="x"))

    def test_apply_patch(self):
        patch = f"*** Begin Patch\n*** Update File: {PC}/sop/standard/x.md\n@@\n-a\n+b\n*** End Patch"
        self.assertDeny(tool("apply_patch", command=patch), "DOC")
        ok = "*** Begin Patch\n*** Add File: /tmp/x.py\n+a\n*** End Patch"
        self.assertAllow(tool("apply_patch", command=ok))

    def test_isolation_worktree_root_relative(self):
        with tempfile.TemporaryDirectory() as d:
            wt = os.path.join(d, "agent-abc")
            os.makedirs(os.path.join(wt, "sop", "standard"))
            os.makedirs(os.path.join(wt, "tools"))
            with open(os.path.join(wt, ".git"), "w") as f:
                f.write("gitdir: /home/claude/pantheon-config/.git/worktrees/agent-abc\n")
            data = bash("echo x > sop/standard/a.md", cwd=wt)
            self.assertDeny(data, "DOC")
            self.assertDeny(tool("Write", file_path=os.path.join(wt, "notes", "n.md")), "DOC")
            self.assertDeny({**tool("Write", file_path="sop/standard/a.md"), "cwd": wt}, "DOC")
            self.assertDeny(bash("cd sop && cat > standard/a.md", cwd=wt), "DOC")
            self.assertAllow(bash("echo x > tools/a.py", cwd=wt))
            # a sibling repo's same-named dir is NOT protected
            with open(os.path.join(wt, ".git"), "w") as f:
                f.write("gitdir: /home/claude/echo-prismatic/.git/worktrees/x\n")
            self.assertAllow(bash("echo x > sop/standard/a.md", cwd=wt))

    def test_literal_paths_need_no_filesystem(self):
        # worktrees that were cleaned up since (the replay case): judged on the path text alone
        wt = PC + "/.claude/worktrees/agent-gone123"
        self.assertDeny(tool("Edit", file_path=wt + "/sop/standard/ticket-flow.md"), "DOC")
        self.assertDeny(tool("Write", file_path=wt + "/tools/README.md"), "DOC")
        self.assertAllow(tool("Write", file_path=wt + "/tools/x.py"))
        self.assertDeny(bash("cat > sop/standard/x.md <<'EOF'\nx\nEOF", cwd="/tmp/pantheon-config-T-9999"), "DOC")
        self.assertDeny(bash("cat > sop/standard/x.md <<'EOF'\nx\nEOF", cwd=wt), "DOC")
        self.assertAllow(bash("cat > tools/x.py <<'EOF'\nx\nEOF", cwd=wt))

    def test_computed_cd_makes_relative_paths_unplaceable(self):
        self.assertAllow(bash("cd $S/r1 && cp x ledger/a.jsonl", cwd="/tmp/pantheon-config-T-1"))
        self.assertAllow(bash("cd \"$(mktemp -d)\" && echo x > sop/a.md", cwd="/tmp/pantheon-config-T-1"))
        self.assertDeny(bash("cd /tmp/pantheon-config-T-1 && echo x > sop/a.md", cwd="/tmp"), "DOC")

    def test_bash_writes_deny(self):
        p = PC + "/sop/standard/applying-the-threat-model.md"
        for c in (f"cat > {p} <<EOF\nx\nEOF", f"echo x >> {p}", f"echo x &> {p}", f"echo x 2>{p}",
                  f"echo x | tee {p}", f"echo x | tee -a {p}", f"sed -i 's/a/b/' {p}",
                  f"sed -i.bak -e 's/a/b/' {p}", f"perl -pi -e 's/a/b/' {p}", f"cp /tmp/x {p}",
                  f"mv /tmp/x {p}", f"install -m 644 /tmp/x {p}", f"cp -t {PC}/sop/standard /tmp/x",
                  f"rm {p}", f"rm -f {p}", f"mv {p} /tmp/x", f"git add {p}",
                  f"git -C {PC} add sop/standard/x.md", f"git commit -m msg {p}",
                  f"timeout 5 tee {p}"):
            self.assertDeny(bash(c), "DOC")

    def test_reads_never_deny(self):
        p = PC + "/architecture/adr/ORG-080-x.md"
        for c in (f"cat {p}", f"grep -n foo {p}", f"sed -n 5p {p} | cut -c1-90",
                  f"head -20 {p}", f"git show HEAD:architecture/adr/ORG-080-x.md",
                  f"cd /tmp/pantheon-config-T-3309-review && sed -n 5p architecture/adr/ORG-080-x.md | cut -c1-90",
                  f"cp {p} /tmp/copy.md", f"ls {PC}/sop/standard", f"wc -l {PC}/notes/*.md",
                  f"diff {p} /tmp/x.md", f"git commit -m 'fix {PC}/sop/x.md and docs/README.md'",
                  f"git add -A", f"git commit -am 'x'", f"echo x > /tmp/out.md",
                  f"sed -n 1,5p {p} > /tmp/x"):
            self.assertAllow(bash(c))


    def test_bare_protected_dir_names(self):
        # T-5695: a bare protected directory (no trailing component) is a write target too
        for c in ("rm -rf notes", "rm -rf ./notes", f"rm -rf {PC}/notes", "mv x notes/", "mv x notes",
                  "cp -t notes x", f"cp x {PC}/rulebook", "rm -r sop"):
            self.assertDeny(bash(c, cwd=PC), "DOC")
        for c in ("rm -rf notes-scratch", "ls notes", "cat notes/x.md", f"mv x {PC}/tools"):
            self.assertAllow(bash(c, cwd=PC))


class TestPushAndGh(Base):
    def test_push_forms(self):
        for c in (f"git {PUSH}", f"git {PUSH} origin main", f"git -C /tmp/x {PUSH}",
                  f"git --no-pager {PUSH}", f"git -c a=b {PUSH} -u origin x", f"timeout 60 git {PUSH}",
                  f"cd x && git {PUSH} --force", f"/usr/bin/git {PUSH}", f"nohup git {PUSH} &",
                  f"bash -c 'git {PUSH}'", f"xargs -n1 git {PUSH}"):
            self.assertDeny(bash(c), "PUSH")

    def test_push_help_allowed(self):
        # T-5695: reading git push's help is not a push
        for c in (f"git {PUSH} --help", f"git {PUSH} -h", f"git -C /tmp/x {PUSH} --help"):
            self.assertAllow(bash(c))
        for c in (f"git {PUSH} origin --help-not", f"git {PUSH} -o -h origin main",
                  f"git {PUSH} origin main -o -h", f"git {PUSH} origin main -h"):
            self.assertDeny(bash(c), "PUSH")

    def test_push_text_is_data(self):
        for c in (f"grep -n \"_MAX_FILE_RETRIES\\|git {PUSH} failed\" tools/ticket.py",
                  f"echo git {PUSH}", f"git commit -m 'never git {PUSH} from a worker'",
                  f"python3 - <<'EOF'\nprint('git {PUSH}')\nEOF", f"git log --grep='git {PUSH}'",
                  f"git status", f"git remote -v", f"git fetch origin"):
            self.assertAllow(bash(c))

    def test_gh_denies(self):
        for c in ("gh pr create --title x", "gh pr merge 5", "gh pr comment 5 -b x", "gh issue create",
                  "gh repo delete o/r", "gh repo edit o/r --visibility public", "gh repo archive o/r",
                  "gh workflow run x.yml", "gh release create v1", "gh api -X POST repos/o/r/issues",
                  "gh api --method=DELETE repos/o/r", "gh api repos/o/r/issues -f title=x",
                  "gh api graphql -f query='mutation { x }'", "gh frobnicate", "gh auth login",
                  "gh auth status --show-token", "timeout 9 gh pr merge 5",
                  "gh pr checkout 5"):
            if c == "gh pr checkout 5":
                self.assertAllow(bash(c))  # allowed outside a shared main checkout
                self.assertDeny(bash(c, cwd="/home/claude/pantheon-config"), "GH")
            else:
                self.assertDeny(bash(c), "GH")

    def test_gh_reads_allow(self):
        for c in ("gh pr view 5", "gh pr list --state open", "gh pr diff 5", "gh pr checks 5",
                  "gh run view 1", "gh issue list", "gh repo view o/r", "gh api repos/o/r/pulls",
                  "gh api -X GET repos/o/r", "gh api repos/o/r/rulesets --jq '.[0].id'",
                  "gh api graphql -f query='query { viewer { login } }'", "gh auth status",
                  "gh search issues x", "gh --version", "gh pr merge --help",
                  "gh api repos/o/r/rulesets/$(gh api repos/o/r/rulesets --jq '.[0].id')"):
            self.assertAllow(bash(c))

    def test_gh_text_is_data(self):
        self.assertAllow(bash("grep -rn 'gh pr merge' sop/"))
        self.assertAllow(bash("git commit -m 'gh pr create is reserved'"))


class TestTicketSecurityGate(Base):
    def test_denies(self):
        for c in ("python3 tools/ticket.py security-accept T-1", "./tools/ticket.py security-reject T-1 --note x",
                  f"{PC}/tools/ticket.py security-accept T-1", "python3 /x/tools/ticket.py -v security-accept T-1",
                  "env A=1 python3 tools/ticket.py security-reject T-2", "cd tools && ./ticket.py security-accept T-1",
                  "xargs -n1 python3 tools/ticket.py security-accept"):
            self.assertDeny(bash(c), "TICKET")

    def test_allows(self):
        for c in ("python3 tools/ticket.py claim T-1", "python3 tools/ticket.py annotate T-1 --note 'security-accept later'",
                  "grep -n security-accept tools/ticket.py", "python3 tools/ticket.py show T-1"):
            if "annotate" in c:
                continue  # a quoted note naming the verb: see test_note_word
            self.assertAllow(bash(c))

    def test_note_word(self):
        # the verb must be an argument of ticket.py; a note that merely says it is still an
        # argument word, but only an EXACT word match denies
        self.assertAllow(bash("python3 tools/ticket.py annotate T-1 --note 'security-accept later'"))


class TestAuditFalsePositives(Base):
    """Legitimate commands the old guard denied (field audit 2026-10-27). All must allow."""

    CASES = (
        "cd /tmp/pantheon-config-T-3309-review && sed -n 5p architecture/adr/ORG-080-x.md | cut -c1-90",
        "grep -n \"_MAX_FILE_RETRIES\\|git " + PUSH + " failed\" tools/ticket.py",
        "timeout 300 bash tools/test_launch_gates.sh",
        "export PATH=\"$HOME/.cargo/bin:$PATH\" && cargo --version",
        "python3 -m site --user-base",
        "python3 - <<'EOF'\nimport re\nsrc = open('tools/codex_telegram_bridge.py').read()\n"
        "print('" + SU + "' in src)\nEOF",
        "git config --get user.email",
        "git init /tmp/scratch && git -C /tmp/scratch config user.email t@t",
        "gh api repos/o/r/rulesets/$(gh api repos/o/r/rulesets --jq '.[0].id')",
        "sed -n 3560,3600p tools/persona_installer.py; sed -n 10,20p tools/ticket.py",
        "sleep 23; grep -c foo tools/x.py | tail -1",
        "nohup python3 -m pytest tools/test_x.py > $L 2>&1 &",
        "grep -rnoiE \"(suppress|_hold|retire)\" tools/",
        "printf \"Total %d of $n\\n\" 3",
        "ls .githooks; grep -l hermes .github/workflows/*; ls tools",
        "source venv/bin/activate && python3 -c 'print(1)'",
        "python3 tools/ticket.py annotate T-1 --note \"$(cat /tmp/note.txt)\"",
        "trap 'rm -f /tmp/x' EXIT; echo ok",
        "eval \"$(ssh-agent -s)\"",
        # HO addition: read-only `git patch-id` (reviewers use it) must pass
        "git patch-id --stable < /tmp/x.diff",
        "git diff A..B | git patch-id --stable",
        "git show HEAD | git patch-id --stable",
    )

    def test_all_allow(self):
        for c in self.CASES:
            self.assertAllow(bash(c))


# Field true positives (audit 2026-10-27 + the replay's by-eye list). Sensitive words assembled.
FIELD_TPS = (
    ("Bash", f"{SU} -S -p '' cat $f < {HOME}/{PW}", "ESC"),
    ("Bash", f"{SU} -n -u suborch2 tmux list-panes -a -F x", "ESC"),
    ("Bash", "S=/home/claude/pantheon-config/tools/pantheon-" + SU + "; $S cat /run/pantheon-health/x", "ESC"),
    ("Bash", "cut -d= -f1 .claude/channels/telegram/.env", "CRED"),
    ("Bash", "ls -la ~/.ssh/; grep -n Host ~/.ssh/config", "CRED"),
    ("Bash", "grep -c 'HOME_ASSISTANT_API_KEY' echo-prismatic/.env", "CRED"),
    ("Bash", "cat > sop/standard/applying-the-threat-model.md <<'EOF'\nx\nEOF", "DOC"),
    ("Edit", PC + "/sop/standard/ticket-flow.md", "DOC"),
    ("Edit", PC + "/architecture/adr/ORG-106-x.md", "DOC"),
    ("Write", PC + "/notes/some-draft.md", "DOC"),
    # by-eye list from the 2026-10-27 replay (tool_use ids in the validation report); reshaped
    ("Bash", "cd /tmp/x; grep -n foo tools/systemd/README.md | head; " + SU + " -n -u claude-fallback bash -c 'systemctl --user status x'", "ESC"),
    ("Bash", "for u in a b c; do echo == $u; " + SU + " -n true 2>/dev/null && echo ok; done", "ESC"),
    ("Bash", "cd /tmp; for f in /home/$u/.claude/settings.json; do echo \"=== $f\"; " + SU + " -S -p '' cat $f < /home/claude/" + PW + "; done", "ESC"),
    ("Bash", "grep -n a instance.yaml | head; " + SU + " -n true 2>&1 | head -1; git status --short", "ESC"),
    ("Bash", "for s in \"\" ; do " + SU + " -n -u suborch2 tmux list-windows -a; done", "ESC"),
    ("Bash", "cd /tmp/x && " + SU + " -n true && ls", "ESC"),
    ("Bash", "cd /tmp/pantheon-config-T-5380; /home/claude/pantheon-config/tools/pantheon-" + SU + " journalctl _UID=$(id -u claude-fallback) --since x", "ESC"),
    ("Bash", "cd /home/claude; S=/home/claude/pantheon-config/tools/pantheon-" + SU + "; for a in x y; do $S id -u $a; done", "ESC"),
    ("Bash", "grep -c '^[[:space:]]*\\(export \\)\\?HOME_ASSISTANT_API_KEY_ECHO_ONLY=' /home/claude/echo-prismatic/.env; cd /home/claude/echo-prismatic", "CRED"),
    ("Bash", "cd /home/claude && for f in .claude/channels/telegram/.env channels/telegram/.env; do [ -f $f ] && echo $f && cut -d= -f1 $f; done", "CRED"),
    ("Bash", "ls -la ~/.ssh/ | awk '{print $1}'; grep -iE '^Host' ~/.ssh/config 2>/dev/null | head -60; ls -la ~/gh_token.env | awk '{print $1}'", "CRED"),
    ("Write", "/tmp/pantheon-config-T-3650/notes/2026-09-28-draft.md", "DOC"),
    ("Edit", PC + "/.claude/worktrees/agent-ac823aacce4120c5e/rulebook/persona-orientation/README.md", "DOC"),
    ("Edit", PC + "/.claude/worktrees/agent-a9feb639209df9400/architecture/adr/ORG-088-x.md", "DOC"),
    ("Write", "/tmp/pantheon-config-T-5438/architecture/adr/ORG-106-seat-pause.md", "DOC"),
)


class TestFieldTruePositives(Base):
    def test_every_tp_denies(self):
        for tool_name, arg, rid in FIELD_TPS:
            if tool_name == "Bash":
                d = bash(arg, cwd=PC)
            else:
                d = tool(tool_name, file_path=arg, content="x")
            self.assertDeny(d, rid)


class TestRails(Base):
    def test_64k_cap(self):
        self.assertDeny(bash("echo " + "a" * 70000), "CAP")
        self.assertAllow(bash("echo " + "a" * 60000))
        d = tool("Write", file_path="/tmp/big.txt", content="a" * 200000)
        self.assertAllow(d)

    def test_unreadable_allows(self):
        self.assertAllow(bash("echo 'unterminated"))
        self.assertAllow(bash('echo "unterminated $('))
        self.assertAllow(bash("$(" * 20 + ")" * 20))

    def test_tokenizer_edges(self):
        self.assertAllow(bash("echo a\\\n b"))
        self.assertAllow(bash("f() { echo hi; }; f"))
        self.assertDeny(bash("f() { " + SU + " ls; }; f"), "ESC")
        self.assertDeny(bash("(( x = 1 << 3 )); " + SU + " ls"), "ESC")
        self.assertDeny(bash("cat <<EOF >/dev/null\nbody\nEOF\n" + SU + " ls"), "ESC")
        self.assertDeny(bash("echo ${HOME}; $(" + SU + " id)"), "ESC")
        self.assertDeny(bash("x=\"$(" + SU + " id)\""), "ESC")
        self.assertDeny(bash("diff <(" + SU + " cat f) g"), "ESC")

    def test_main_exception_is_deny(self):
        import io
        from unittest import mock

        raw = json.dumps(bash("echo hi"))
        out = io.StringIO()
        with mock.patch.object(g, "decide", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                g.main(raw)
        self.assertIn("BLOCKED (worker-guard v2, T-5686)", out.getvalue())

    def test_main_main_thread_and_bad_json_silent(self):
        import io
        from unittest import mock

        for raw in ('{"tool_name":"Bash"}', "not json", ""):
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                with self.assertRaises(SystemExit) as cm:
                    g.main(raw)
            self.assertEqual(cm.exception.code, 0)
            self.assertEqual(out.getvalue(), "")

    def test_main_prints_one_deny_line(self):
        import io
        from unittest import mock

        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            with self.assertRaises(SystemExit):
                g.main(json.dumps(bash(f"{SU} ls")))
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_no_nested_quantifier_regex_in_source(self):
        # structural linearity guard: no `(...+)+`/`(...*)*` shapes in the module's patterns
        import re

        src = _src()
        self.assertIsNone(re.search(r"\([^()]*[+*]\)[+*]", src))

    def test_source_has_no_contiguous_sensitive_tokens(self):
        src = _src()
        self.assertNotIn(PW, src)


class TestEntryShim(unittest.TestCase):
    """T-5695 step 2: tools/hooks/worker-guard-v2.py, the zero-import entry the swap points at."""

    SHIM = os.path.join(HOOKS, "worker-guard-v2.py")

    def _run(self, payload, *flags):
        return subprocess.run([sys.executable, *flags, self.SHIM], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60)

    def test_main_thread_is_silent_and_imports_nothing(self):
        p = {"tool_name": "Bash", "tool_input": {"command": f"git {PUSH}"}, "cwd": "/tmp"}
        r = self._run(p, "-X", "importtime")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        imported = {ln.rsplit("|", 1)[-1].strip() for ln in r.stderr.splitlines() if "|" in ln}
        # `os` is loaded by site at interpreter startup, so only the guard's own imports are checked
        self.assertFalse(imported & {"worker_guard_v2", "json", "re"}, imported)

    def test_subagent_push_denies(self):
        r = self._run(bash(f"git {PUSH} origin main"))
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_escaped_agent_type_key_reaches_v2(self):
        # a \\u-escaped key spelling must not take the main-thread fast path
        raw = json.dumps(bash(f"git {PUSH} origin main")).replace('"agent_type"', '"\\u0061gent_type"')
        r = subprocess.run([sys.executable, self.SHIM], input=raw, capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_subagent_harmless_allows(self):
        r = self._run(bash("git status"))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


class TestPathListsMatchCurrentGuard(unittest.TestCase):
    """v2 copies the canary-pinned protected-doc lists and the credential path list. Until the
    swap phase retires worker_guard_impl, the copies must not drift. Delete this class with v1."""

    def test_copies_agree(self):
        import worker_guard_impl as v1

        # T-5695: v2 also matches a bare protected directory ("(?:/|$)" where v1 requires "/");
        # the directory list itself must still agree with v1's.
        bare = "(?:/|$)"
        self.assertEqual(g._PROTECTED_DOC.pattern.replace(bare, "/", 1), v1._PROTECTED_DOC.pattern)
        self.assertEqual(g._PROTECTED_ROOTREL.pattern.replace(bare, "/", 1), v1._PROTECTED_ROOTREL.pattern)
        self.assertEqual(g._CRED_FIXED.pattern, v1._CRED_FIXED_PATHS.pattern)
        self.assertEqual(g._GH_READ_ALLOWLIST, v1._GH_READ_ALLOWLIST)

    def test_every_protected_dir_has_a_real_file(self):
        repo = os.path.dirname(HERE)
        for d in ("rulebook", "ledger", "notes", "bugs", "incidents", "lessons", "sop",
                  "architecture/adr", "architecture/engineering-practices"):
            with self.subTest(dir=d):
                self.assertTrue(os.path.isdir(os.path.join(repo, d)), f"{d} moved? update the v2 anchors")


class TestShadow(unittest.TestCase):
    SHADOW = os.path.join(HOOKS, "worker-guard-shadow.py")

    def run_shadow(self, payload, logdir):
        env = {**os.environ, "WORKER_GUARD_SHADOW_DIR": logdir}
        return subprocess.run([sys.executable, self.SHADOW], input=json.dumps(payload), text=True,
                              capture_output=True, env=env, timeout=60)

    def records(self, logdir):
        out = []
        for fn in sorted(os.listdir(logdir)):
            with open(os.path.join(logdir, fn)) as f:
                out += [json.loads(line) for line in f if line.strip()]
        return out

    def test_main_thread_prints_nothing_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_shadow({"tool_name": "Bash", "tool_input": {"command": "ls"}}, d)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            self.assertEqual(os.listdir(d), [])

    def test_agreement_and_disagreement_records(self):
        with tempfile.TemporaryDirectory() as d:
            ok = bash("ls /tmp")
            r = self.run_shadow(ok, d)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            recs = self.records(d)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            for k in ("ts", "tool_name", "agent_type", "v1", "v2", "v1_ms", "v2_ms", "sha256"):
                self.assertIn(k, rec)
            self.assertEqual(rec["v1"]["verdict"], "allow")
            self.assertEqual(rec["v2"]["verdict"], "allow")
            self.assertNotIn("command", rec)  # verdicts agree: no command text
            # a deny both guards agree on: still no command text
            r = self.run_shadow(bash(f"{SU} ls"), d)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            recs = self.records(d)
            self.assertEqual(len(recs), 2)
            self.assertEqual(recs[1]["v2"]["verdict"], "deny")
            self.assertEqual(recs[1]["v2"]["rule"], "ESC")
            self.assertNotIn("command", recs[1])
            # disagreement: a protected-doc READ the old guard denied (or any v1/v2 split)
            for c in ("grep -rn foo /home/claude/pantheon-config/architecture/adr/ORG-080-x.md",
                      "timeout 300 bash tools/test_launch_gates.sh"):
                self.run_shadow(bash(c), d)
            recs = self.records(d)
            dis = [x for x in recs if x["v1"]["verdict"] != x["v2"]["verdict"]]
            for x in dis:
                self.assertIn("command", x)
            for fn in os.listdir(d):
                self.assertEqual(os.stat(os.path.join(d, fn)).st_mode & 0o777, 0o600)

    def test_garbage_stdin_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "WORKER_GUARD_SHADOW_DIR": d}
            r = subprocess.run([sys.executable, self.SHADOW], input="not json agent_type", text=True,
                               capture_output=True, env=env, timeout=60)
            self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
