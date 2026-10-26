import fcntl, os, re

try:  # T-4096: the hub path comes from instance.yaml, never a literal.
    from tools.agent_role_map import mailbox_hub
except ImportError:  # script run: tools/ is on sys.path, the repo root is not
    from agent_role_map import mailbox_hub

# Parse by TOKEN SHAPE, never by column index: /proc/locks lines vary in field
# count (blocked waiters render as "-> " continuations, POSIX vs FLOCK differ).
# Specialist's own rule, T-2296: parse by the FLOCK token, and note the dev halves
# render HEX while the inode renders DECIMAL.
_DEVINO = re.compile(r"^([0-9a-f]+):([0-9a-f]+):(\d+)$")

def locks():
    out = []
    for line in open("/proc/locks"):
        fields = line.split()
        if "FLOCK" not in fields:
            continue
        pid = None
        for i, f in enumerate(fields):
            m = _DEVINO.match(f)
            if m:
                pid = fields[i - 1] if i else "?"
                out.append((int(m.group(1), 16), int(m.group(2), 16), int(m.group(3)), pid))
                break
    return out

f = open("/tmp/t2409-probe2.lock", "w")
fcntl.flock(f, fcntl.LOCK_EX)
st = os.stat("/tmp/t2409-probe2.lock")
hit = [x for x in locks() if x[2] == st.st_ino]
print("KNOWN-POSITIVE:", "FOUND — instrument works" if hit else "STILL BLIND — do not trust any zero below")
if hit:
    print("   ", hit[0])
f.close()
os.unlink("/tmp/t2409-probe2.lock")

print("\nall live FLOCKs (maj, min, ino, pid):")
for x in locks():
    print("   ", x)

print("\nthe real question (dev,ino)-keyed, never ino alone:")
for p in ("mason", "artisan", "orchestrator", "specialist", "codex", "archivist"):
    path = f"{mailbox_hub()}/{p}/.consumer.lock"
    if not os.path.exists(path):
        print(f"  {p}: no lockfile")
        continue
    st = os.stat(path)
    maj, mnr = os.major(st.st_dev), os.minor(st.st_dev)
    m = [x for x in locks() if (x[0], x[1], x[2]) == (maj, mnr, st.st_ino)]
    print(f"  {p}: dev={maj}:{mnr} ino={st.st_ino} -> "
          f"{'HELD by pid ' + m[0][3] if m else 'NOT HELD'}")
