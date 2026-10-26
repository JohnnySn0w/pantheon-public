"""confined_load_burner.py - one CPU burner for confined_load.sh (T-4349).

Usage: python3 confined_load_burner.py <seconds> <parent pid> <tag>

Busy-loops until <seconds> have passed OR its parent is no longer <parent pid>,
i.e. confined_load.sh has died, even by SIGKILL, and the burner has been
reparented. The cap and the orphan check live in the burner itself: an external
`timeout` wrapper leaked uncapped orphans when cleanup TERMed it mid-fork (2 in
40 fast-exit runs), so there is deliberately no wrapper. <tag> is unused by the
code; it is there so pgrep can find exactly one run's burners.
"""
import os
import sys
import time

deadline = time.monotonic() + float(sys.argv[1])
parent = int(sys.argv[2])
while time.monotonic() < deadline and os.getppid() == parent:
    pass
