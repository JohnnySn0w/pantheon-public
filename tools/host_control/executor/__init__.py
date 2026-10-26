"""Per-seat executor service (ORG-052 Unit 5, increment 5b; T-2917).

One executor runs per registered seat, AS that seat's Unix uid, selected by
the root-owned seat registry from ``getuid()`` alone (never a request
parameter, argv, environment or unit instance name). It serves
``org.pantheon.HostExecutor1.Executor`` under the bus name
``org.pantheon.HostExecutor1.U<uid>``, fetches each operation's
server-resolved spec from the broker's ``ExecutionSource``, performs that
seat's user-level host work, and returns ONLY typed mutation and evidence
records. It never imports or writes the broker journal (AST- and
import-tested).

Every name minted here is PROVISIONAL-PENDING-RATIFICATION (T-2917 ruling
i3); see the table in ``tools/host_control/README.md``.
"""
