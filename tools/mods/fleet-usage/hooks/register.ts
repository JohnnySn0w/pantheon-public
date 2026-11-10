// T-4510 / the operator TG 10709, TG 10723: the PRODUCER half of the fleet usage gate.
//
// Claude Code pushes `session.measure` after each main-thread turn and whenever a rate-limit
// window moves a whole point; its input carries rateLimits: {kind, percentUsed, resetsAt?}[]
// (types/claude-code.d.ts, 2.1.289). On a rateLimits change this mod records the reading to
// <fleet-usage-dir>/<seat>.json in the schema tools/fleet_usage_gate.py reads
// (contract/fleet-usage-record.ts pins both sides). The Max window is shared across the fleet,
// so any seat's reading is the fleet's reading.
//
// It OBSERVES ONLY (the operator TG 10725): one hook, on session.measure, always passing the event
// on; it touches no prompt and no transcript. Every failure is a silent no-op: the gate fails
// open, which is the behaviour before this mod existed.
//
// T-4536 (the operator TG 10732, per-seat usage ledger): a second observe-only hook, on
// turn.complete. It returns `next(e)`'s result unchanged the moment it settles and starts an
// UNAWAITED background task (at most 4 in flight, else the row is dropped; each child killed
// after 2 s; the whole task dropped and its slot freed after 10 s) that appends ONE row per turn, main loop and subagents alike (e.agentId), to
// <fleet-usage-dir>/turns/<seat>/<YYYY-MM-DD>.jsonl (UTC day of the row's ts; the daily file
// is the rotation). Row schema pantheon-fleet-usage-turn/v1, pinned by
// contract/fleet-usage-turn-row.ts and read by tools/fleet_usage_ledger.py.
//   - NO CONTENT, BY CONSTRUCTION: buildTurnRow() builds a literal object from a fixed key
//     list and reads only e.usage (four counts + model), e.turnId, e.agentId, e.durationMs and
//     e.reason; ids and the model must match ASCII patterns or become null/"unknown". It never
//     reads e.answer, e.refusal, or an agent's description/name ($.agent.list() supplies only
//     `type` and `parentId`). The test feeds sentinels into all of those.
//   - Append: `tee -a <file>` with the row on stdin (no shell). A row is capped at 2048 bytes,
//     under PIPE_BUF (4096), so it is one write(2) to an O_APPEND file: atomic against the
//     main loop, subagents and other processes. The reader still skips a torn line.
//   - Bounds: a day file stops growing at 16 MiB (checked by $.fs.stat; a symlink or non-file
//     at the path means no write). The mod DELETES NOTHING; retention is
//     `tools/fleet_usage_ledger.py prune` (dry-run unless --commit), scheduled separately.
//   - Dirs: the top-level dir is still never created (provisioned 2775 pantheon-shared).
//     Below it the mod may create turns/ (mkdir -m 3775: any group member can add its seat
//     dir; sticky, so none can rename or remove another's) and turns/<seat> (mkdir -m 2755,
//     only the seat writes it); the group comes from the setgid bit. A lost mkdir race
//     (EEXIST) is ignored. turns/ and turns/<seat> must be real directories, never symlinks,
//     or the row is dropped. A seat name of `.` or `..` is refused. The gate and every other reader of
//     the dir look only at top-level *.json, so nothing here is visible to them.
//   - Not built (heavier documented alternative): Claude Code's OpenTelemetry export
//     (CLAUDE_CODE_ENABLE_TELEMETRY plus an exporter and a collector).
//
// Configuration (process env, which CLAUDE_CODE_PLUGIN_DIRS' own settings.json env block can
// carry; the same variable names the Python gate uses):
//   PANTHEON_FLEET_USAGE_DIR     shared dir, absolute; default /var/lib/pantheon/fleet-usage
//   PANTHEON_FLEET_USAGE_SEAT    seat name; REQUIRED (T-4643 step 0): unset or unusable means the
//                                mod writes nothing. No USER/LOGNAME fallback: a managed
//                                enabledPlugins enables the mod for every host account, and the
//                                gate reads the freshest seat file, so only declared seats may write.
//   PANTHEON_FLEET_USAGE_STATUS  1/true/yes/on draws "fleet wk N%" as this plugin's status
//                                line; default OFF (the footer sentinels the idle detectors
//                                read must not move)
import type { EngineInterface, Register } from 'claude-code'

export const SCHEMA = 'pantheon-fleet-usage/v1'

const DEFAULT_DIR = '/var/lib/pantheon/fleet-usage'
// T-4536 review F1: `.` and `..` are refused (a seat name is a path component under turns/).
const SEAT_RE = /^(?!\.\.?$)[A-Za-z0-9_.-]{1,64}$/
const RUN_TIMEOUT_MS = 5000

export type Reading = { used_percentage: number; resets_at: number }

export type FleetUsageRecord = {
  schema: string
  ts: number
  seat: string
  seven_day: Reading | null
  five_hour: Reading | null
}

type RateLimit = { kind: string; percentUsed: number; resetsAt?: string }

type Engine = EngineInterface

// One window as the gate's file holds it, or null when the window is absent or unusable.
// A window without a parseable reset time cannot be validated by the gate, so it is null.
export function readingFor(rateLimits: readonly RateLimit[], kind: string): Reading | null {
  if (!Array.isArray(rateLimits)) return null
  for (const r of rateLimits) {
    if (!r || r.kind !== kind) continue
    const pct = r.percentUsed
    if (typeof pct !== 'number' || !Number.isFinite(pct) || pct < 0) continue
    const ms = typeof r.resetsAt === 'string' ? Date.parse(r.resetsAt) : Number.NaN
    if (!Number.isFinite(ms)) continue
    return { used_percentage: pct, resets_at: Math.floor(ms / 1000) }
  }
  return null
}

// The record to persist, or null when no window is usable: an empty rateLimits is no
// observation, and must not clobber a good reading.
export function buildRecord(
  rateLimits: readonly RateLimit[],
  seat: string,
  nowMs: number,
): FleetUsageRecord | null {
  const seven = readingFor(rateLimits, 'seven_day')
  const five = readingFor(rateLimits, 'five_hour')
  if (seven === null && five === null) return null
  return { schema: SCHEMA, ts: Math.floor(nowMs / 1000), seat, seven_day: seven, five_hour: five }
}

async function resolveSeat($: Engine): Promise<string | null> {
  const name = await $.env.get('PANTHEON_FLEET_USAGE_SEAT')
  return typeof name === 'string' && SEAT_RE.test(name) ? name : null
}

async function resolveDir($: Engine): Promise<string | null> {
  const configured = await $.env.get('PANTHEON_FLEET_USAGE_DIR')
  const dir = configured && configured.length > 0 ? configured : DEFAULT_DIR
  return dir.startsWith('/') ? dir.replace(/\/+$/, '') : null
}

async function run($: Engine, argv: string[]): Promise<void> {
  const done = await $.process.run(argv, { timeoutMs: RUN_TIMEOUT_MS })
  if (done.exitCode !== 0) throw new Error(`${argv[0]} exited ${done.exitCode}`)
}

// Temp file in the SAME dir, chmod, then mv -f: rename(2) is atomic, so the gate never reads a
// torn file. Never creates the shared dir (it is provisioned 2775 pantheon-shared; a dir made
// here would get the wrong mode). Any failure removes the temp file and is swallowed.
async function writeRecord($: Engine, dir: string, record: FleetUsageRecord): Promise<void> {
  if (!(await $.fs.exists(dir))) return
  const nonce = Math.random().toString(36).slice(2, 10)
  const tmp = `${dir}/.${record.seat}.${record.ts}.${nonce}.tmp`
  try {
    await $.fs.write(tmp, JSON.stringify(record) + '\n')
    await run($, ['chmod', '0644', tmp])
    await run($, ['mv', '-f', tmp, `${dir}/${record.seat}.json`])
  } catch {
    try {
      await $.process.run(['rm', '-f', tmp], { timeoutMs: RUN_TIMEOUT_MS })
    } catch {
      // nothing more to do
    }
  }
}

async function observe($: Engine, rateLimits: readonly RateLimit[]): Promise<void> {
  const flag = ((await $.env.get('PANTHEON_FLEET_USAGE_STATUS')) ?? '').toLowerCase()
  if (['1', 'true', 'yes', 'on'].includes(flag)) {
    const seven = readingFor(rateLimits, 'seven_day')
    $.ui.status(seven === null ? undefined : `fleet wk ${seven.used_percentage}%`)
  }
  const seat = await resolveSeat($)
  const dir = await resolveDir($)
  if (seat === null || dir === null) return
  const record = buildRecord(rateLimits, seat, await $.clock.now())
  if (record !== null) await writeRecord($, dir, record)
}

// ---------------------------------------------------------------------------------------
// T-4536: the per-turn ledger.

export const TURN_SCHEMA = 'pantheon-fleet-usage-turn/v1'

// The ONLY keys a Claude row may carry, in order. tools/fleet_usage_ledger.py holds the same
// list (its test asserts it against the contract fixture).
export const TURN_ROW_KEYS = [
  'schema',
  'ts',
  'harness',
  'seat',
  'session_id',
  'turn_id',
  'agent_id',
  'agent_type',
  'parent_agent_id',
  'model',
  'model_source',
  'input_tokens',
  'output_tokens',
  'cache_read_input_tokens',
  'cache_creation_input_tokens',
  'duration_ms',
  'reason',
] as const

export const MAX_ROW_BYTES = 2048
export const MAX_DAY_FILE_BYTES = 16 * 1024 * 1024

const ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/
const TYPE_RE = /^[A-Za-z0-9_.:-]{1,64}$/
const MODEL_RE = /^[A-Za-z0-9._:/@\[\]-]{1,128}$/
const ASCII_LINE_RE = /^[\x20-\x7e]*\n$/
const REASONS = ['answer', 'aborted', 'refusal', 'error'] as const

export type TurnRow = {
  schema: string
  ts: number
  harness: 'claude'
  seat: string
  session_id: string | null
  turn_id: string | null
  agent_id: string | null
  agent_type: string | null
  parent_agent_id: string | null
  model: string
  model_source: 'usage'
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  duration_ms: number | null
  reason: string
}

// What the mod learns about a subagent from $.agent.list(): its type and parent, never its
// description or name (both can be task text).
export type AgentFacts = { type?: unknown; parentId?: unknown } | null

function idOrNull(v: unknown): string | null {
  return typeof v === 'string' && ID_RE.test(v) ? v : null
}

function count(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0
}

function field(o: unknown, k: string): unknown {
  return o !== null && typeof o === 'object' ? (o as Record<string, unknown>)[k] : undefined
}

function agentTypeOf(agentId: string | null, agent: AgentFacts): string | null {
  if (agentId === null || agent === null) return null
  const t = field(agent, 'type')
  return typeof t === 'string' && TYPE_RE.test(t) ? t : 'other'
}

// The row for one turn, or null when the turn reported no usage (an interrupt or API error
// before anything counted). Every value is taken field by field from an explicit source and
// coerced; nothing of `e` is spread or copied wholesale.
export function buildTurnRow(
  e: unknown,
  seat: string,
  sessionId: unknown,
  nowMs: number,
  agent: AgentFacts,
): TurnRow | null {
  const usage = field(e, 'usage')
  if (usage === null || typeof usage !== 'object') return null
  const agentId = idOrNull(field(e, 'agentId'))
  const model = field(usage, 'model')
  const reason = field(e, 'reason')
  const duration = field(e, 'durationMs')
  return {
    schema: TURN_SCHEMA,
    ts: Math.floor(nowMs / 1000),
    harness: 'claude',
    seat,
    session_id: idOrNull(sessionId),
    turn_id: idOrNull(field(e, 'turnId')),
    agent_id: agentId,
    agent_type: agentTypeOf(agentId, agent),
    parent_agent_id: agentId === null ? null : idOrNull(field(agent, 'parentId')),
    model: typeof model === 'string' && MODEL_RE.test(model) ? model : 'unknown',
    model_source: 'usage',
    input_tokens: count(field(usage, 'input_tokens')),
    output_tokens: count(field(usage, 'output_tokens')),
    cache_read_input_tokens: count(field(usage, 'cache_read_input_tokens')),
    cache_creation_input_tokens: count(field(usage, 'cache_creation_input_tokens')),
    duration_ms:
      typeof duration === 'number' && Number.isFinite(duration) && duration >= 0
        ? Math.floor(duration)
        : null,
    reason: (REASONS as readonly unknown[]).includes(reason) ? (reason as string) : 'other',
  }
}

// One JSONL line, or null when it is not plain ASCII or exceeds MAX_ROW_BYTES (ASCII, so its
// length is its byte count).
export function serializeRow(row: TurnRow): string | null {
  const line = JSON.stringify(row) + '\n'
  if (line.length > MAX_ROW_BYTES || !ASCII_LINE_RE.test(line)) return null
  return line
}

export function utcDay(tsSeconds: number): string {
  return new Date(tsSeconds * 1000).toISOString().slice(0, 10)
}

// T-4536 review F4: called SYNCHRONOUSLY from startTurnRecord (not awaited there), so the
// list is asked for while a subagent that just ended is still in it. Never rejects: a
// non-array answer is null, and a terminal catch covers a throw anywhere in the chain
// (review N3), since an early-returning task never awaits this promise.
function agentFactsPromise($: Engine, agentId: string | null): Promise<AgentFacts> {
  if (agentId === null) return Promise.resolve(null)
  try {
    return $.agent
      .list()
      .then(agents => {
        if (!Array.isArray(agents)) return null
        const found = agents.find(a => field(a, 'id') === agentId)
        return found ? { type: found.type, parentId: found.parentId } : null
      })
      .catch(() => null)
  } catch {
    return Promise.resolve(null)
  }
}

async function sessionIdOf($: Engine): Promise<string | null> {
  try {
    return await $.session.id()
  } catch {
    return null
  }
}

type TaskState = { expired: boolean; inChild: number }

// Every child the ledger spawns goes through here (review N1): none starts once the task's
// deadline has passed, and state.inChild tells the deadline a child is still running, so the
// slot is then held until the child's own timeout ends it (a hard bound of
// MAX_LEDGER_IN_FLIGHT children). False when nothing was spawned.
async function runChild(
  $: Engine,
  state: TaskState,
  argv: string[],
  stdin?: string,
): Promise<boolean> {
  if (state.expired) return false
  state.inChild += 1
  try {
    const init =
      stdin === undefined
        ? { timeoutMs: LEDGER_RUN_TIMEOUT_MS }
        : { stdin, timeoutMs: LEDGER_RUN_TIMEOUT_MS }
    await $.process.run(argv, init)
    return true
  } finally {
    state.inChild -= 1
  }
}

async function ensureDir($: Engine, state: TaskState, path: string, mode: string): Promise<boolean> {
  if (await $.fs.exists(path)) return true
  try {
    if (!(await runChild($, state, ['mkdir', '-m', mode, path]))) return false
  } catch {
    // no process: re-checked below (EEXIST from a racing seat is a non-zero exit, also fine)
  }
  return $.fs.exists(path)
}

// True when the day file may take `bytes` more: absent, or a plain file under the cap.
async function roomFor($: Engine, path: string, bytes: number): Promise<boolean> {
  let stat
  try {
    stat = await $.fs.stat(path)
  } catch {
    return true // absent: tee creates it
  }
  if (stat.isLink || stat.kind !== 'file') return false
  return stat.size + bytes <= MAX_DAY_FILE_BYTES
}

// T-4536 review F2: a real directory, never a symlink (nor anything else).
async function isRealDir($: Engine, path: string): Promise<boolean> {
  try {
    const s = await $.fs.stat(path)
    return !s.isLink && s.kind === 'dir'
  } catch {
    return false
  }
}

// The background half: every I/O the row needs (env, session id, clock, stat, mkdir, tee)
// happens here, never on the turn's path. Once the task's deadline has passed (state.expired)
// it makes no further change: the row is dropped.
async function recordTurn(
  $: Engine,
  e: unknown,
  agentFacts: Promise<AgentFacts>,
  state: TaskState,
): Promise<void> {
  const seat = await resolveSeat($)
  const dir = await resolveDir($)
  if (seat === null || dir === null) return
  if (!(await $.fs.exists(dir))) return
  const agent = await agentFacts
  const row = buildTurnRow(e, seat, await sessionIdOf($), await $.clock.now(), agent)
  if (row === null) return
  const line = serializeRow(row)
  if (line === null) return
  const turns = `${dir}/turns`
  // 3775: setgid (group inherits) + sticky, so group members cannot rename or remove each
  // other's seat dirs (review F2).
  if (state.expired || !(await ensureDir($, state, turns, '3775'))) return
  if (!(await isRealDir($, turns))) return
  const seatDir = `${turns}/${seat}`
  if (state.expired || !(await ensureDir($, state, seatDir, '2755'))) return
  if (!(await isRealDir($, seatDir))) return
  const file = `${seatDir}/${utcDay(row.ts)}.jsonl`
  if (!(await roomFor($, file, line.length))) return
  await runChild($, state, ['tee', '-a', file], line)
}

// Recovery review (T-4536): the ledger write must NEVER delay turn completion. The hook returns
// next(e)'s result as soon as it settles; the row is written by an UNAWAITED task. At most
// MAX_LEDGER_IN_FLIGHT tasks run at once: past that the row is dropped, so a stuck FS cannot
// pile up children. Each child is killed by $.process.run's own timeoutMs (the call rejects
// then). Review F3 (never leak): a hung $.fs/$.env/$.session/$.agent call has no timeout of
// its own, so the whole task also races LEDGER_TASK_DEADLINE_MS on $.clock.after: when the
// deadline fires the task makes no further change (the row is dropped) and the slot is freed,
// unless a child is still running (review N1): then the slot is held until that child ends,
// within its own LEDGER_RUN_TIMEOUT_MS, so children never exceed MAX_LEDGER_IN_FLIGHT.
export const MAX_LEDGER_IN_FLIGHT = 4
export const LEDGER_RUN_TIMEOUT_MS = 2000
export const LEDGER_TASK_DEADLINE_MS = 10_000
let ledgerInFlight = 0

// Synchronous: decides and starts, never awaits. True when a task was started.
export function startTurnRecord($: Engine, e: unknown): boolean {
  const usage = field(e, 'usage')
  if (usage === null || typeof usage !== 'object') return false
  if (ledgerInFlight >= MAX_LEDGER_IN_FLIGHT) return false
  ledgerInFlight += 1
  let released = false
  const release = () => {
    if (!released) {
      released = true
      ledgerInFlight -= 1
    }
  }
  const state: TaskState = { expired: false, inChild: 0 }
  // F4: asked for now (not awaited here), awaited inside the task.
  const agents = agentFactsPromise($, idOrNull(field(e, 'agentId')))
  let timer: { cancel: () => void } | null = null
  try {
    timer = $.clock.after(LEDGER_TASK_DEADLINE_MS, () => {
      state.expired = true
      if (state.inChild === 0) release() // else done() releases when the child ends
    })
  } catch {
    timer = null
  }
  const done = () => {
    try {
      timer?.cancel()
    } catch {
      // the timer is gone already
    }
    release()
  }
  let task: Promise<void>
  try {
    task = recordTurn($, e, agents, state)
  } catch {
    done()
    return false
  }
  task.then(done, done)
  return true
}

export const register: Register = on => {
  on('session.measure', async ($, e, next) => {
    try {
      if (e.changed.includes('rateLimits')) await observe($, e.rateLimits)
    } catch {
      // an observer never breaks the session
    }
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    try {
      startTurnRecord($, e) // NOT awaited: nothing on the turn's path waits on I/O
    } catch {
      // an observer never breaks the session
    }
    return result
  })
}
