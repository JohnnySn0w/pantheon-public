// T-4510: the producer half of the fleet usage gate.
//
// Run: claude plugin test tools/mods/fleet-usage
//
// Fixture provenance (D11): LIVE is the statusLine rate_limits object observed live on
// specialist 2026-10-04T18:2xZ (Orchestrator) -- integer used_percentage, resets_at in epoch seconds
// {five_hour:{resets_at:1791151200,used_percentage:13},seven_day:{resets_at:1791738000,
// used_percentage:3}} -- re-expressed in the shape session.measure carries it
// (SessionRateLimit: percentUsed, resetsAt as ISO 8601; types/claude-code.d.ts).
// contract/fleet-usage-record.ts (strict JSON after `export default`) is the file this mod
// must produce from it; the Python gate test (tools/test_fleet_usage_gate.py) reads that
// same committed fixture back.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import contract from '../contract/fleet-usage-record'
import { buildRecord, readingFor, register } from '../hooks/register'

const NOW_MS = 1791140000_000
const DIR = '/var/lib/pantheon/fleet-usage'

const LIVE = [
  { kind: 'five_hour', percentUsed: 13, resetsAt: '2026-10-04T22:00:00Z' },
  { kind: 'seven_day', percentUsed: 3, resetsAt: '2026-10-11T17:00:00Z' },
]

type Reading = { used_percentage: number; resets_at: number }

type World = {
  writes: { path: string; text: string }[]
  runs: string[][]
  statuses: (string | undefined)[]
  prompts: number
}

type Faults = { dirMissing?: boolean; writeDenied?: boolean; mvExit?: number; runDenied?: boolean }

const ok = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

function world(on: On, env: Record<string, string>, faults: Faults = {}): World {
  const w: World = { writes: [], runs: [], statuses: [], prompts: 0 }
  mock.env(on, env)
  mock.clock(on, { now: NOW_MS })
  on('fs.exists', () => ({ value: !faults.dirMissing }))
  on('fs.write', (_$, e) => {
    if (faults.writeDenied) return { deny: 'disk full' }
    w.writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  on('process.run', (_$, e) => {
    w.runs.push([...e.argv])
    if (faults.runDenied) return { deny: 'no process' }
    if (e.argv[0] === 'mv' && faults.mvExit) return { value: { ...ok, exitCode: faults.mvExit } }
    return { value: ok }
  })
  on('ui.status', (_$, e) => {
    w.statuses.push(e.text)
    return { value: undefined }
  })
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('prompt.compose', () => {
    w.prompts += 1
    return { sections: [{ id: 'bottom', text: 'bottom', scope: 'shared' as const }] }
  })
  return w
}

function measure(rateLimits: unknown[], changed: string[] = ['rateLimits']) {
  return { context: { window: 200000 }, rateLimits, changed } as never
}

const SEAT_ENV = { PANTHEON_FLEET_USAGE_SEAT: 'specialist' }

describe('the record the mod writes', () => {
  test('the live specialist shape produces exactly the committed contract file', async ($, on) => {
    const w = world(on, SEAT_ENV)
    const result = await $.session.measure(measure(LIVE))
    expect(result).toEqual({ changed: ['rateLimits'] })
    expect(w.writes.length).toBe(1)
    expect(JSON.parse(w.writes[0]!.text)).toEqual(contract)
  })

  test('atomic: temp file in the same dir, chmod 0644, then mv -f over the seat file', async ($, on) => {
    const w = world(on, SEAT_ENV)
    await $.session.measure(measure(LIVE))
    const tmp = w.writes[0]!.path
    expect(tmp.startsWith(`${DIR}/.specialist.`)).toBe(true)
    expect(tmp.endsWith('.json')).toBe(false)
    expect(w.runs).toEqual([
      ['chmod', '0644', tmp],
      ['mv', '-f', tmp, `${DIR}/specialist.json`],
    ])
  })

  test('a window that left is stored as null, the other kept', async ($, on) => {
    const w = world(on, SEAT_ENV)
    await $.session.measure(measure([LIVE[1]]))
    const rec = JSON.parse(w.writes[0]!.text)
    expect(rec.five_hour).toBeNull()
    expect(rec.seven_day).toEqual({ used_percentage: 3, resets_at: 1791738000 })
  })

  test('a float percentage and an ISO offset are converted faithfully', () => {
    const r = readingFor(
      [{ kind: 'seven_day', percentUsed: 95.5, resetsAt: '2026-10-11T12:00:00-05:00' }],
      'seven_day',
    ) as Reading
    expect(r).toEqual({ used_percentage: 95.5, resets_at: 1791738000 })
  })

  test('only windows with a usable percentage AND reset time count', () => {
    for (const bad of [
      { kind: 'seven_day', percentUsed: 50 },
      { kind: 'seven_day', percentUsed: 50, resetsAt: 'tomorrow' },
      { kind: 'seven_day', percentUsed: Number.NaN, resetsAt: '2026-10-11T17:00:00Z' },
      { kind: 'seven_day', percentUsed: -1, resetsAt: '2026-10-11T17:00:00Z' },
      { kind: 'spend_limit', percentUsed: 50, resetsAt: '2026-10-11T17:00:00Z' },
    ]) {
      expect(readingFor([bad as never], 'seven_day')).toBeNull()
    }
    expect(buildRecord([], 'specialist', NOW_MS)).toBeNull()
  })
})

describe('when the mod writes nothing', () => {
  test('rateLimits did not change', async ($, on) => {
    const w = world(on, SEAT_ENV)
    await $.session.measure(measure(LIVE, ['context']))
    expect(w.writes).toEqual([])
    expect(w.runs).toEqual([])
  })

  test('rateLimits is empty (off a subscription, or before the first reading)', async ($, on) => {
    const w = world(on, SEAT_ENV)
    const result = await $.session.measure(measure([]))
    expect(result).toEqual({ changed: ['rateLimits'] })
    expect(w.writes).toEqual([])
  })

  test('no usable window in a non-empty rateLimits', async ($, on) => {
    const w = world(on, SEAT_ENV)
    await $.session.measure(measure([{ kind: 'spend_limit', percentUsed: 5, resetsAt: '2026-10-11T17:00:00Z' }]))
    expect(w.writes).toEqual([])
  })

  test('the shared dir is not provisioned: never created, silent no-op', async ($, on) => {
    const w = world(on, SEAT_ENV, { dirMissing: true })
    const result = await $.session.measure(measure(LIVE))
    expect(result).toEqual({ changed: ['rateLimits'] })
    expect(w.writes).toEqual([])
    expect(w.runs).toEqual([])
  })

  test('a relative or empty dir override is refused', async ($, on) => {
    const w = world(on, { ...SEAT_ENV, PANTHEON_FLEET_USAGE_DIR: 'relative/dir' })
    await $.session.measure(measure(LIVE))
    expect(w.writes).toEqual([])
  })

  test('an unusable seat name', async ($, on) => {
    const w = world(on, { PANTHEON_FLEET_USAGE_SEAT: '../../escape', USER: 'also bad/name' })
    await $.session.measure(measure(LIVE))
    expect(w.writes).toEqual([])
  })
})

describe('configuration', () => {
  test('dir override from PANTHEON_FLEET_USAGE_DIR', async ($, on) => {
    const w = world(on, { ...SEAT_ENV, PANTHEON_FLEET_USAGE_DIR: '/x/fleet' })
    await $.session.measure(measure(LIVE))
    expect(w.runs[1]).toEqual(['mv', '-f', w.writes[0]!.path, '/x/fleet/specialist.json'])
  })

  test('seat: PANTHEON_FLEET_USAGE_SEAT wins, then USER, then LOGNAME', async ($, on) => {
    const a = world(on, { PANTHEON_FLEET_USAGE_SEAT: 'mason', USER: 'suborch1' })
    await $.session.measure(measure(LIVE))
    expect(JSON.parse(a.writes[0]!.text).seat).toBe('mason')
  })

  test('seat falls back to the unix user', async ($, on) => {
    const w = world(on, { USER: 'claude-fallback' })
    await $.session.measure(measure(LIVE))
    expect(JSON.parse(w.writes[0]!.text).seat).toBe('claude-fallback')
    expect(w.runs[1]![3]).toBe(`${DIR}/claude-fallback.json`)
  })

  test('seat falls back to LOGNAME when USER is unset', async ($, on) => {
    const w = world(on, { LOGNAME: 'suborch2' })
    await $.session.measure(measure(LIVE))
    expect(JSON.parse(w.writes[0]!.text).seat).toBe('suborch2')
  })
})

describe('never breaks the session', () => {
  test('every failure is a no-op that still passes the event on', async ($, on) => {
    const w = world(on, SEAT_ENV, { writeDenied: true })
    const result = await $.session.measure(measure(LIVE))
    expect(result).toEqual({ changed: ['rateLimits'] })
    expect(w.writes).toEqual([])
  })

  test('mv failing removes the temp file and does not throw', async ($, on) => {
    const w = world(on, SEAT_ENV, { mvExit: 1 })
    const result = await $.session.measure(measure(LIVE))
    expect(result).toEqual({ changed: ['rateLimits'] })
    expect(w.runs[w.runs.length - 1]).toEqual(['rm', '-f', w.writes[0]!.path])
  })

  test('process.run refused: no throw', async ($, on) => {
    world(on, SEAT_ENV, { runDenied: true })
    const result = await $.session.measure(measure(LIVE))
    expect(result).toEqual({ changed: ['rateLimits'] })
  })

  test('garbage in rateLimits: no throw', async ($, on) => {
    const w = world(on, SEAT_ENV)
    for (const junk of [null, 7, 'x', [null], [{}], [{ kind: 5 }], {}]) {
      const result = await $.session.measure(measure(junk as never))
      expect(result).toEqual({ changed: ['rateLimits'] })
    }
    expect(w.writes).toEqual([])
  })
})

describe('the status line', () => {
  test('default OFF: nothing is ever drawn', async ($, on) => {
    const w = world(on, SEAT_ENV)
    await $.session.measure(measure(LIVE))
    expect(w.statuses).toEqual([])
  })

  test('opt in with PANTHEON_FLEET_USAGE_STATUS=1', async ($, on) => {
    const w = world(on, { ...SEAT_ENV, PANTHEON_FLEET_USAGE_STATUS: '1' })
    await $.session.measure(measure(LIVE))
    expect(w.statuses).toEqual(['fleet wk 3%'])
  })

  test('opt in, no weekly window: the line is cleared', async ($, on) => {
    const w = world(on, { ...SEAT_ENV, PANTHEON_FLEET_USAGE_STATUS: '1' })
    await $.session.measure(measure([LIVE[0]]))
    expect(w.statuses).toEqual([undefined])
  })
})

describe('observes only', () => {
  test('registers a session.measure hook and nothing on the prompt (the operator TG 10725)', async ($, on) => {
    const w = world(on, SEAT_ENV)
    await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 99, resetsAt: '2026-10-11T17:00:00Z' }]))
    const composed = await $.prompt.compose({} as never)
    expect(composed).toEqual({ sections: [{ id: 'bottom', text: 'bottom', scope: 'shared' }] })
    expect(w.prompts).toBe(1)
  })

  test('register() subscribes to session.measure only', () => {
    const seen: string[] = []
    const spy = ((name: string) => {
      seen.push(name)
      return { catch: () => undefined }
    }) as unknown as On
    register(spy, {})
    expect(seen).toEqual(['session.measure'])
  })
})
