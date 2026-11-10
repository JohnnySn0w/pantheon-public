// T-4536: the per-turn usage ledger half of the fleet-usage mod (turn.complete).
//
// Run: claude plugin test tools/mods/fleet-usage
//
// Fixture provenance (D11): TURN_EVENT follows Claude Code 2.1.316's declared turn.complete
// input (TurnCompleteFields + TurnUsage, plugin-authoring types); values are synthetic.
// contract/fleet-usage-turn-row.ts (strict JSON after `export default`) is the row this mod
// must produce from it; tools/test_fleet_usage_ledger.py reads the same committed fixture.
//
// Recovery review (T-4536): the ledger write must NEVER delay turn completion. The `recovery` block
// below proves it with a tee that never answers, a tee that rejects, and the in-flight bound.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { MockClock } from 'claude-code/testing'
import type { On } from 'claude-code'

import contract from '../contract/fleet-usage-turn-row'
import {
  LEDGER_RUN_TIMEOUT_MS,
  LEDGER_TASK_DEADLINE_MS,
  MAX_DAY_FILE_BYTES,
  MAX_LEDGER_IN_FLIGHT,
  MAX_ROW_BYTES,
  TURN_ROW_KEYS,
  buildTurnRow,
  serializeRow,
  utcDay,
} from '../hooks/register'

const NOW_MS = 1791140000_000 // 2026-10-04T18:53:20Z
const DIR = '/var/lib/pantheon/fleet-usage'
const SEAT_DIR = `${DIR}/turns/specialist`
const DAY_FILE = `${SEAT_DIR}/2026-10-04.jsonl`
const SESSION = '5f0c2a8e-1d4b-4c1e-9a7f-2b6d3e8f9a01'
const ANSWER = { text: 'THE ANSWER' }
const SENTINEL = 'SENTINEL-T4536-CONTENT-MUST-NOT-LEAK'

const TURN_EVENT = {
  answer: 'a model answer the ledger must never see',
  durationMs: 14250,
  isAborted: false,
  turnId: 'turn_01HZX9Q2V7K3M8N4P5R6S7T8U9',
  reason: 'answer',
  usage: {
    input_tokens: 1200,
    output_tokens: 850,
    cache_read_input_tokens: 48000,
    cache_creation_input_tokens: 3100,
    model: 'claude-opus-5-5',
  },
}

type Run = { argv: string[]; stdin?: string; timeoutMs?: number }
type Stat = { kind: 'file' | 'dir' | 'other'; size: number; isLink: boolean; mtimeMs: number }
type Mode = 'ok' | 'stuck' | 'reject'

type World = {
  runs: Run[]
  existing: Set<string>
  stats: Map<string, Stat>
  release: () => void
  pending: () => number
  releaseHung: () => void
  clock: MockClock
  agentListedBeforeFs: boolean[]
}

type Opts = {
  existing?: string[]
  stats?: Record<string, Stat>
  tee?: Mode
  mkdirMakes?: boolean
  sessionId?: 'ok' | 'reject'
  agents?: unknown[] | 'reject' | 'hang' | 'not-an-array'
  now?: number
}

const ok = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

function world(on: On, env: Record<string, string>, o: Opts = {}): World {
  const pending: (() => void)[] = []
  const hung: (() => void)[] = []
  const w: World = {
    runs: [],
    existing: new Set(o.existing ?? [DIR, `${DIR}/turns`, SEAT_DIR]),
    stats: new Map(Object.entries(o.stats ?? {})),
    release: () => {
      for (const r of pending.splice(0)) r()
    },
    pending: () => pending.length,
    releaseHung: () => {
      for (const r of hung.splice(0)) r()
    },
    clock: undefined as unknown as MockClock,
    agentListedBeforeFs: [],
  }
  let fsCalls = 0
  mock.env(on, env)
  w.clock = mock.clock(on, { now: o.now ?? NOW_MS })
  on('fs.exists', (_$, e) => {
    fsCalls += 1
    return { value: w.existing.has(e.path) }
  })
  on('fs.stat', (_$, e) => {
    fsCalls += 1
    const s = w.stats.get(e.path)
    if (s) return { value: { ...s } }
    // an existing path with no explicit stat is a real directory
    if (w.existing.has(e.path)) return { value: { kind: 'dir', size: 0, isLink: false, mtimeMs: 0 } }
    return { deny: 'ENOENT' }
  })
  on('session.id', () => (o.sessionId === 'reject' ? { deny: 'no session' } : { value: SESSION }))
  on('agent.list', () => {
    w.agentListedBeforeFs.push(fsCalls === 0)
    if (o.agents === 'reject') return { deny: 'no agents' }
    if (o.agents === 'hang') {
      return new Promise(resolve => hung.push(() => resolve({ value: [] as never })))
    }
    if (o.agents === 'not-an-array') return { value: { id: 'x' } as never }
    return { value: (o.agents ?? []) as never }
  })
  on('process.run', (_$, e) => {
    const run: Run = { argv: [...e.argv], stdin: e.init?.stdin, timeoutMs: e.init?.timeoutMs }
    w.runs.push(run)
    if (e.argv[0] === 'mkdir') {
      if (o.mkdirMakes !== false) w.existing.add(e.argv[e.argv.length - 1]!)
      return { value: ok }
    }
    if (o.tee === 'reject') return { deny: 'disk full' }
    if (o.tee === 'stuck') {
      return new Promise(resolve => pending.push(() => resolve({ value: ok })))
    }
    return { value: ok }
  })
  on('turn.complete', () => ANSWER)
  return w
}

const SEAT_ENV = { PANTHEON_FLEET_USAGE_SEAT: 'specialist' }

function turn(extra: Record<string, unknown> = {}) {
  return { ...TURN_EVENT, ...extra } as never
}

// Let the unawaited ledger tasks run to completion (or to a stuck child).
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) await new Promise(r => setTimeout(r, 1))
}

function tees(w: World): Run[] {
  return w.runs.filter(r => r.argv[0] === 'tee')
}

describe('the row the mod appends', () => {
  test('a main-loop turn produces exactly the committed contract row', async ($, on) => {
    const w = world(on, SEAT_ENV)
    const result = await $.turn.complete(turn())
    expect(result).toEqual(ANSWER)
    await settle()
    const t = tees(w)
    expect(t.length).toBe(1)
    expect(t[0]!.argv).toEqual(['tee', '-a', DAY_FILE])
    expect(t[0]!.stdin!.endsWith('\n')).toBe(true)
    expect(JSON.parse(t[0]!.stdin!)).toEqual(contract)
  })

  test('its keys are exactly the allowlist, in order', async ($, on) => {
    const w = world(on, SEAT_ENV)
    await $.turn.complete(turn())
    await settle()
    expect(Object.keys(JSON.parse(tees(w)[0]!.stdin!))).toEqual([...TURN_ROW_KEYS])
    expect(Object.keys(contract)).toEqual([...TURN_ROW_KEYS])
  })

  test('a subagent turn carries its id, type and parent (from $.agent.list)', async ($, on) => {
    const agents = [
      { id: 'a1b2c3', type: 'Explore', parentId: 'p9', description: 'x', status: 'running' },
    ]
    const w = world(on, SEAT_ENV, { agents })
    await $.turn.complete(turn({ agentId: 'a1b2c3' }))
    await settle()
    const row = JSON.parse(tees(w)[0]!.stdin!)
    expect(row.agent_id).toBe('a1b2c3')
    expect(row.agent_type).toBe('Explore')
    expect(row.parent_agent_id).toBe('p9')
  })

  test('a subagent the list no longer holds: id kept, type and parent null', async ($, on) => {
    const w = world(on, SEAT_ENV, { agents: [] })
    await $.turn.complete(turn({ agentId: 'gone1' }))
    await settle()
    const row = JSON.parse(tees(w)[0]!.stdin!)
    expect([row.agent_id, row.agent_type, row.parent_agent_id]).toEqual(['gone1', null, null])
  })

  test('an agent type outside the safe pattern is recorded as "other"', () => {
    const row = buildTurnRow(turn({ agentId: 'a1' }), 'specialist', SESSION, NOW_MS, {
      type: 'has spaces / and words',
    })
    expect(row!.agent_type).toBe('other')
  })

  test('the main loop never reads agent facts', () => {
    const row = buildTurnRow(turn(), 'specialist', SESSION, NOW_MS, { type: 'Explore', parentId: 'p' })
    expect([row!.agent_id, row!.agent_type, row!.parent_agent_id]).toEqual([null, null, null])
  })

  test('hostile or odd values are coerced, never copied', () => {
    const row = buildTurnRow(
      turn({
        turnId: 'has spaces',
        agentId: '../../x',
        durationMs: -5,
        reason: 'weird',
        usage: {
          input_tokens: -1,
          output_tokens: Number.NaN,
          cache_read_input_tokens: 'many',
          cache_creation_input_tokens: 12.7,
          model: 'model with "quotes"\n',
        },
      }),
      'specialist',
      { not: 'a string' },
      NOW_MS,
      null,
    )!
    expect(row.turn_id).toBeNull()
    expect(row.agent_id).toBeNull()
    expect(row.session_id).toBeNull()
    expect(row.duration_ms).toBeNull()
    expect(row.reason).toBe('other')
    expect(row.model).toBe('unknown')
    expect([
      row.input_tokens,
      row.output_tokens,
      row.cache_read_input_tokens,
      row.cache_creation_input_tokens,
    ]).toEqual([0, 0, 0, 12])
  })

  test('the day file is the UTC day of the row: rotation at UTC midnight', async () => {
    expect(utcDay(Date.parse('2026-10-04T23:59:59Z') / 1000)).toBe('2026-10-04')
    expect(utcDay(Date.parse('2026-10-05T00:00:00Z') / 1000)).toBe('2026-10-05')
  })

  test('a turn just after UTC midnight goes to the next day file', async ($, on) => {
    const w = world(on, SEAT_ENV, { now: Date.parse('2026-10-05T00:00:01Z') })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)[0]!.argv[2]).toBe(`${SEAT_DIR}/2026-10-05.jsonl`)
  })

  test('a row over 2048 bytes (or not plain ASCII) is not serialized', () => {
    const row = buildTurnRow(turn(), 'specialist', SESSION, NOW_MS, null)!
    expect(serializeRow(row)).not.toBeNull()
    expect(serializeRow({ ...row, seat: 'x'.repeat(MAX_ROW_BYTES) })).toBeNull()
    expect(serializeRow({ ...row, seat: 'café' })).toBeNull()
  })
})

describe('no content, ever', () => {
  test('sentinels in the answer, refusal, extra fields and agent text never reach a write', async (
    $,
    on,
  ) => {
    const agents = [
      {
        id: 'sub1',
        type: 'general-purpose',
        parentId: 'p1',
        description: SENTINEL,
        name: SENTINEL,
        status: 'running',
      },
    ]
    const w = world(on, SEAT_ENV, { agents })
    await $.turn.complete(
      turn({
        answer: SENTINEL,
        reason: 'refusal',
        refusal: { explanation: SENTINEL, category: SENTINEL },
        agentId: 'sub1',
        prompt: SENTINEL,
        messages: [{ role: 'user', content: SENTINEL }],
        usage: { ...TURN_EVENT.usage, extra: SENTINEL },
      }),
    )
    await settle()
    expect(tees(w).length).toBe(1)
    expect(JSON.stringify(w.runs)).not.toContain(SENTINEL)
    const row = JSON.parse(tees(w)[0]!.stdin!)
    expect(Object.keys(row)).toEqual([...TURN_ROW_KEYS])
    expect(row.reason).toBe('refusal')
  })
})

describe('dirs and bounds', () => {
  test('creates turns/ 3775 (setgid+sticky) and turns/<seat> 2755 when missing, never the top dir', async (
    $,
    on,
  ) => {
    const w = world(on, SEAT_ENV, { existing: [DIR] })
    await $.turn.complete(turn())
    await settle()
    expect(w.runs.map(r => r.argv)).toEqual([
      ['mkdir', '-m', '3775', `${DIR}/turns`],
      ['mkdir', '-m', '2755', SEAT_DIR],
      ['tee', '-a', DAY_FILE],
    ])
  })

  test('the top-level dir is not provisioned: nothing runs at all', async ($, on) => {
    const w = world(on, SEAT_ENV, { existing: [] })
    const result = await $.turn.complete(turn())
    expect(result).toEqual(ANSWER)
    await settle()
    expect(w.runs).toEqual([])
  })

  test('a mkdir that leaves the dir absent: no tee', async ($, on) => {
    const w = world(on, SEAT_ENV, { existing: [DIR], mkdirMakes: false })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)).toEqual([])
  })

  test('the day file at the 16 MiB cap: no tee', async ($, on) => {
    const full: Stat = { kind: 'file', size: MAX_DAY_FILE_BYTES - 10, isLink: false, mtimeMs: 0 }
    const w = world(on, SEAT_ENV, { stats: { [DAY_FILE]: full } })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)).toEqual([])
  })

  test('a day file under the cap is appended to', async ($, on) => {
    const some: Stat = { kind: 'file', size: 4096, isLink: false, mtimeMs: 0 }
    const w = world(on, SEAT_ENV, { stats: { [DAY_FILE]: some } })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w).length).toBe(1)
  })

  test('a symlink at the day path: no tee', async ($, on) => {
    const link: Stat = { kind: 'file', size: 0, isLink: true, mtimeMs: 0 }
    const w = world(on, SEAT_ENV, { stats: { [DAY_FILE]: link } })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)).toEqual([])
  })

  test('a directory at the day path: no tee', async ($, on) => {
    const dir: Stat = { kind: 'dir', size: 0, isLink: false, mtimeMs: 0 }
    const w = world(on, SEAT_ENV, { stats: { [DAY_FILE]: dir } })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)).toEqual([])
  })

  test('no usage (an interrupt or API error before anything counted): nothing runs', async (
    $,
    on,
  ) => {
    const w = world(on, SEAT_ENV)
    const result = await $.turn.complete(turn({ usage: undefined, reason: 'aborted' }))
    expect(result).toEqual(ANSWER)
    await settle()
    expect(w.runs).toEqual([])
  })

  test('an unusable seat name: nothing runs', async ($, on) => {
    const w = world(on, { PANTHEON_FLEET_USAGE_SEAT: '../x', USER: 'bad/name' })
    await $.turn.complete(turn())
    await settle()
    expect(w.runs).toEqual([])
  })

  test('no PANTHEON_FLEET_USAGE_SEAT (T-4643): nothing runs, even with USER set', async ($, on) => {
    const w = world(on, { USER: 'claude-fallback' })
    await $.turn.complete(turn())
    await settle()
    expect(w.runs).toEqual([])
  })

  test('a seat of ".." (review F1): nothing runs, never the top dir', async ($, on) => {
    const w = world(on, { PANTHEON_FLEET_USAGE_SEAT: '..', USER: '.' }, { existing: [DIR, `${DIR}/turns`, `${DIR}/turns/..`] })
    await $.turn.complete(turn())
    await settle()
    expect(w.runs).toEqual([])
  })

  test('a seat of "." (review F1): nothing runs', async ($, on) => {
    const w = world(on, { PANTHEON_FLEET_USAGE_SEAT: '.' })
    await $.turn.complete(turn())
    await settle()
    expect(w.runs).toEqual([])
  })

  test('a symlinked turns/<seat> (review F2): no tee', async ($, on) => {
    const link: Stat = { kind: 'dir', size: 0, isLink: true, mtimeMs: 0 }
    const w = world(on, SEAT_ENV, { stats: { [SEAT_DIR]: link } })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)).toEqual([])
  })

  test('a symlinked turns/ (review F2): no tee', async ($, on) => {
    const link: Stat = { kind: 'dir', size: 0, isLink: true, mtimeMs: 0 }
    const w = world(on, SEAT_ENV, { stats: { [`${DIR}/turns`]: link } })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)).toEqual([])
  })

  test('a file where turns/<seat> should be: no tee', async ($, on) => {
    const file: Stat = { kind: 'file', size: 0, isLink: false, mtimeMs: 0 }
    const w = world(on, SEAT_ENV, { stats: { [SEAT_DIR]: file } })
    await $.turn.complete(turn())
    await settle()
    expect(tees(w)).toEqual([])
  })

  test('the mod deletes nothing: no rm in any path', async ($, on) => {
    const w = world(on, SEAT_ENV, { existing: [DIR] })
    await $.turn.complete(turn())
    await $.turn.complete(turn({ agentId: 'z1' }))
    await settle()
    expect(w.runs.some(r => r.argv[0] === 'rm')).toBe(false)
  })
})

describe('recovery: never delay turn completion', () => {
  test('a tee that never answers: the hook resolves at once with next(e)', async ($, on) => {
    const w = world(on, SEAT_ENV, { tee: 'stuck' })
    const t0 = Date.now()
    const result = await $.turn.complete(turn())
    const took = Date.now() - t0
    expect(result).toEqual(ANSWER)
    // Structural proof: the hook has resolved while the append is still outstanding (it is
    // released only below). An awaited write would never get here: the mutation that awaits
    // it runs into the 5 s test timeout. The wall-clock bound is a sanity check only, set
    // well under the 2 s child timeout so test-engine dispatch jitter under load (54 ms seen
    // with a parallel pytest run) cannot flake it.
    await settle()
    expect(w.pending()).toBe(1)
    expect(took).toBeLessThan(250)
    // the child is bounded by $.process.run's own kill-on-expiry
    expect(tees(w)[0]!.timeoutMs).toBe(LEDGER_RUN_TIMEOUT_MS)
    expect(LEDGER_RUN_TIMEOUT_MS).toBeLessThanOrEqual(2000)
    for (const r of w.runs) expect(r.timeoutMs).toBeLessThanOrEqual(2000)
    w.release()
    await settle()
  })

  test('a tee, session.id and agent.list that all reject: no throw, slots freed', async (
    $,
    on,
  ) => {
    const w = world(on, SEAT_ENV, { tee: 'reject', sessionId: 'reject', agents: 'reject' })
    // More turns than the in-flight bound: every one still reaches tee, which proves each
    // rejected task released its slot (the rejection arm ran; nothing was left unhandled).
    for (let i = 0; i < MAX_LEDGER_IN_FLIGHT * 2; i++) {
      const result = await $.turn.complete(turn({ agentId: 'sub1', turnId: `r${i}` }))
      expect(result).toEqual(ANSWER)
      await settle()
    }
    expect(tees(w).length).toBe(MAX_LEDGER_IN_FLIGHT * 2)
  })

  test('the agent list is asked for before any other I/O (review F4)', async ($, on) => {
    const agents = [{ id: 'sub9', type: 'Explore', parentId: 'p', description: 'x', status: 'completed' }]
    const w = world(on, SEAT_ENV, { agents })
    await $.turn.complete(turn({ agentId: 'sub9' }))
    await settle()
    expect(w.agentListedBeforeFs).toEqual([true])
    expect(JSON.parse(tees(w)[0]!.stdin!).agent_type).toBe('Explore')
  })

  test('a hung $ call never leaks a slot: the deadline frees it and drops the row (review F3)', async (
    $,
    on,
  ) => {
    const w = world(on, SEAT_ENV, { agents: 'hang', existing: [DIR] })
    // fill every slot with a subagent turn whose agent.list never answers
    for (let i = 0; i < MAX_LEDGER_IN_FLIGHT; i++) {
      expect(await $.turn.complete(turn({ agentId: `h${i}`, turnId: `h${i}` }))).toEqual(ANSWER)
    }
    await settle()
    // all slots held: a main-loop turn (which needs no agent list) is dropped
    await $.turn.complete(turn({ turnId: 'blocked' }))
    await settle()
    expect(tees(w)).toEqual([])
    // just short of the deadline nothing has changed
    await w.clock.advance(LEDGER_TASK_DEADLINE_MS - 1)
    await settle()
    await $.turn.complete(turn({ turnId: 'still-blocked' }))
    await settle()
    expect(tees(w)).toEqual([])
    // past the deadline: the hung calls answer late, and the late-resuming tasks make no
    // change at all (no mkdir, no tee: review N5)
    await w.clock.advance(2)
    await settle()
    w.releaseHung()
    await settle()
    expect(w.runs).toEqual([])
    // and the slots are free again: the next turn is written
    await $.turn.complete(turn({ turnId: 'free' }))
    await settle()
    const written = tees(w).map(t => JSON.parse(t.stdin!).turn_id)
    expect(written).toEqual(['free'])
  })

  test('a child still running at the deadline keeps its slot: never more than 4 children (review N1)', async (
    $,
    on,
  ) => {
    const w = world(on, SEAT_ENV, { tee: 'stuck' })
    for (let i = 0; i < MAX_LEDGER_IN_FLIGHT; i++) {
      await $.turn.complete(turn({ turnId: `s${i}` }))
    }
    await settle()
    expect(w.pending()).toBe(MAX_LEDGER_IN_FLIGHT)
    // well past the task deadline, with every tee still running
    await w.clock.advance(LEDGER_TASK_DEADLINE_MS + 1000)
    await settle()
    // the slots are NOT freed early: a new turn is dropped, no fifth child starts
    await $.turn.complete(turn({ turnId: 'over' }))
    await settle()
    expect(tees(w).length).toBe(MAX_LEDGER_IN_FLIGHT)
    expect(w.pending()).toBe(MAX_LEDGER_IN_FLIGHT)
    // the children end (their own timeout, here a release): the slots free, writing resumes
    w.release()
    await settle()
    await $.turn.complete(turn({ turnId: 'after' }))
    await settle()
    expect(tees(w).map(t => JSON.parse(t.stdin!).turn_id).slice(-1)).toEqual(['after'])
    w.release()
    await settle()
  })

  test('a non-array agent list is a null agent, never a rejection (review N3)', async ($, on) => {
    const w = world(on, SEAT_ENV, { agents: 'not-an-array' })
    expect(await $.turn.complete(turn({ agentId: 'sub1' }))).toEqual(ANSWER)
    await settle()
    const row = JSON.parse(tees(w)[0]!.stdin!)
    expect([row.agent_id, row.agent_type, row.parent_agent_id]).toEqual(['sub1', null, null])
  })

  test(`at most ${MAX_LEDGER_IN_FLIGHT} appends in flight; the rest are dropped`, async (
    $,
    on,
  ) => {
    const w = world(on, SEAT_ENV, { tee: 'stuck' })
    for (let i = 0; i < MAX_LEDGER_IN_FLIGHT + 3; i++) {
      const result = await $.turn.complete(turn({ turnId: `t${i}` }))
      expect(result).toEqual(ANSWER)
    }
    await settle()
    // (the engine runs its own copy of the module, so the bound is read off what reached tee)
    expect(tees(w).length).toBe(MAX_LEDGER_IN_FLIGHT)
    expect(w.pending()).toBe(MAX_LEDGER_IN_FLIGHT)
    w.release()
    await settle()
    // the slots are free again: the next turn is written
    await $.turn.complete(turn({ turnId: 'after' }))
    await settle()
    expect(tees(w).length).toBe(MAX_LEDGER_IN_FLIGHT + 1)
  })
})
