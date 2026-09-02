import { useEffect, useState } from 'react'
import { Layers, PenLine, Undo2, Wallet } from 'lucide-react'
import { listOrders } from '../api'

const TIERS = [
  { id: 'low', label: 'Low', cls: 'ship' },
  { id: 'medium', label: 'Medium', cls: 'confirm' },
  { id: 'high', label: 'High', cls: 'nudge' },
  { id: 'very_high', label: 'Very high', cls: 'review' },
]

/**
 * Counts for the header cards and the tier pills.
 *
 * Each figure is a separate `limit=1` call whose only interesting field is
 * `total`, so the browser never pulls 10,000 rows to count them. They run in
 * parallel and the whole set is discarded if any one fails - a header showing
 * three right numbers and one stale one is worse than a header showing none.
 */
function useQueueStats(refreshToken) {
  const [stats, setStats] = useState(null)

  useEffect(() => {
    let cancelled = false
    const count = (filters) => listOrders({ ...filters, limit: 1 }).then((p) => p.total)

    Promise.all([
      count({}),
      count({ action: 'ship' }),
      count({ overridden: 'true' }),
      count({ outcome: 'rto' }),
      count({ outcome: 'pending' }),
      ...TIERS.map((tier) => count({ tier: tier.id })),
    ])
      .then(([total, ship, overridden, rto, pending, ...tiers]) => {
        if (cancelled) return
        setStats({
          total,
          intervened: total - ship,
          overridden,
          rto,
          resolved: total - pending,
          tiers: Object.fromEntries(TIERS.map((tier, i) => [tier.id, tiers[i]])),
        })
      })
      .catch(() => { if (!cancelled) setStats(null) })

    return () => { cancelled = true }
  }, [refreshToken])

  return stats
}

function Card({ Icon, label, value, foot }) {
  return (
    <div className="kpi">
      <div className="label">
        {Icon && <Icon size={14} strokeWidth={2.3} aria-hidden="true" />}
        {label}
      </div>
      <div className="value">{value}</div>
      <div className="foot">{foot}</div>
    </div>
  )
}

export default function QueueStats({ refreshToken }) {
  const stats = useQueueStats(refreshToken)

  if (!stats) {
    return (
      <div className="queue-stats">
        <p className="note stats-placeholder">Loading queue summary…</p>
      </div>
    )
  }

  const share = (n) => (stats.total ? `${((n / stats.total) * 100).toFixed(1)}% of all orders` : '—')
  // RTO rate is over orders whose fate is actually known. Dividing by every
  // order would quietly count "not delivered yet" as "did not come back".
  const rtoRate = stats.resolved ? (stats.rto / stats.resolved) * 100 : null
  const maxTier = Math.max(1, ...TIERS.map((t) => stats.tiers[t.id] || 0))

  return (
    <div className="queue-stats">
      <div className="kpis">
        <Card
          Icon={Layers}
          label="Orders scored"
          value={stats.total.toLocaleString()}
          foot="every order in the audit log"
        />
        <Card
          Icon={Undo2}
          label="RTO rate"
          value={rtoRate === null ? '—' : `${rtoRate.toFixed(1)}%`}
          foot={`${stats.rto.toLocaleString()} of ${stats.resolved.toLocaleString()} resolved`}
        />
        <Card
          Icon={Wallet}
          label="Intervened on"
          value={stats.intervened.toLocaleString()}
          foot={share(stats.intervened)}
        />
        <Card
          Icon={PenLine}
          label="Overridden by a human"
          value={stats.overridden.toLocaleString()}
          foot={stats.overridden === 0 ? 'none yet' : share(stats.overridden)}
        />
      </div>

      <div className="tier-strip">
        <span className="tier-strip-label">Risk spread</span>
        <div className="tier-pills">
          {TIERS.map((tier) => {
            const n = stats.tiers[tier.id] || 0
            return (
              <span key={tier.id} className={`tier-pill ${tier.cls}`}>
                <span className="tier-pill-dot" aria-hidden="true" />
                {tier.label}
                <strong>{n.toLocaleString()}</strong>
                <span
                  className="tier-pill-bar"
                  style={{ width: `${(n / maxTier) * 46 + 4}px` }}
                  aria-hidden="true"
                />
              </span>
            )
          })}
        </div>
      </div>
    </div>
  )
}
