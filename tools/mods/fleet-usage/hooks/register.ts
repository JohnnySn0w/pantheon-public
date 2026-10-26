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
// Configuration (process env, which CLAUDE_CODE_PLUGIN_DIRS' own settings.json env block can
// carry; the same variable names the Python gate uses):
//   PANTHEON_FLEET_USAGE_DIR     shared dir, absolute; default /var/lib/pantheon/fleet-usage
//   PANTHEON_FLEET_USAGE_SEAT    seat name; default the unix user (USER, then LOGNAME)
//   PANTHEON_FLEET_USAGE_STATUS  1/true/yes/on draws "fleet wk N%" as this plugin's status
//                                line; default OFF (the footer sentinels the idle detectors
//                                read must not move)
import type { EngineInterface, Register } from 'claude-code'

export const SCHEMA = 'pantheon-fleet-usage/v1'

const DEFAULT_DIR = '/var/lib/pantheon/fleet-usage'
const SEAT_RE = /^[A-Za-z0-9_.-]{1,64}$/
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
  const candidates = [
    await $.env.get('PANTHEON_FLEET_USAGE_SEAT'),
    await $.env.get('USER'),
    await $.env.get('LOGNAME'),
  ]
  for (const name of candidates) {
    if (typeof name === 'string' && SEAT_RE.test(name)) return name
  }
  return null
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

export const register: Register = on => {
  on('session.measure', async ($, e, next) => {
    try {
      if (e.changed.includes('rateLimits')) await observe($, e.rateLimits)
    } catch {
      // an observer never breaks the session
    }
    return next(e)
  })
}
