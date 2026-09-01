import { useEffect, useState } from 'react'
import { listOrders } from '../api'

const PAGE_SIZE = 25

function ActionBadge({ action }) {
  return <span className={`badge ${action}`}>{action}</span>
}

function FinalAction({ row }) {
  // When a human has overruled the system, show both. Hiding the original
  // recommendation would make the queue disagree with the audit trail.
  if (!row.was_overridden) return <ActionBadge action={row.final_action} />
  return (
    <span>
      <span className="strike">{row.recommended_action}</span>
      <span className="arrow">&rarr;</span>
      <ActionBadge action={row.final_action} />
    </span>
  )
}

function Outcome({ value }) {
  if (value === null) return <span className="badge muted">pending</span>
  return (
    <span className={`badge ${value ? 'rto' : 'delivered'}`}>
      {value ? 'came back' : 'delivered'}
    </span>
  )
}

export default function OrderQueue({ selectedId, onSelect, refreshToken }) {
  const [filters, setFilters] = useState({
    action: '', tier: '', outcome: '', overridden: '', sort: 'score',
  })
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    listOrders({ ...filters, limit: PAGE_SIZE, offset })
      .then((data) => { if (!cancelled) { setPage(data); setError(null) } })
      .catch((err) => { if (!cancelled) setError(err.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [filters, offset, refreshToken])

  function update(key, value) {
    setOffset(0)
    setFilters((current) => ({ ...current, [key]: value }))
  }

  const total = page ? page.total : 0
  const shown = page ? page.items.length : 0

  return (
    <div className="panel">
      <h2>Order queue</h2>
      <p className="note">
        One row per order, showing where it stands now. Click any row for the
        full reasoning and its audit trail.
      </p>

      <div className="filters">
        <label>
          Action
          <select value={filters.action} onChange={(e) => update('action', e.target.value)}>
            <option value="">any</option>
            <option value="ship">ship</option>
            <option value="confirm">confirm</option>
            <option value="nudge">nudge</option>
            <option value="review">review</option>
          </select>
        </label>
        <label>
          Risk tier
          <select value={filters.tier} onChange={(e) => update('tier', e.target.value)}>
            <option value="">any</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
            <option value="very_high">very high</option>
          </select>
        </label>
        <label>
          Outcome
          <select value={filters.outcome} onChange={(e) => update('outcome', e.target.value)}>
            <option value="">any</option>
            <option value="pending">pending</option>
            <option value="rto">came back</option>
            <option value="delivered">delivered</option>
          </select>
        </label>
        <label>
          Overridden
          <select value={filters.overridden} onChange={(e) => update('overridden', e.target.value)}>
            <option value="">any</option>
            <option value="true">yes</option>
            <option value="false">no</option>
          </select>
        </label>
        <label>
          Sort
          <select value={filters.sort} onChange={(e) => update('sort', e.target.value)}>
            <option value="score">highest score</option>
            <option value="recent">most recent</option>
          </select>
        </label>
      </div>

      {error && <p className="error">Could not load the queue: {error}</p>}
      {loading && !page && <p className="loading">Loading orders…</p>}

      {page && (
        <>
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Order</th>
                  <th className="num">Score</th>
                  <th className="num">Evidence</th>
                  <th>Tier</th>
                  <th>Action</th>
                  <th>Outcome</th>
                  <th>Decided</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((row) => (
                  <tr
                    key={row.order_id}
                    aria-selected={row.order_id === selectedId}
                    onClick={() => onSelect(row.order_id)}
                  >
                    <td>{row.order_id}</td>
                    <td className="num">{row.score}</td>
                    <td className="num">{row.evidence_score}</td>
                    <td>{row.policy_tier.replace('_', ' ')}</td>
                    <td><FinalAction row={row} /></td>
                    <td><Outcome value={row.outcome} /></td>
                    <td>{row.decided_at.replace('T', ' ').slice(0, 16)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {shown === 0 && <p className="empty">No orders match these filters.</p>}

          <div className="pager">
            <span>
              {total === 0 ? 'nothing to show'
                : `${offset + 1}–${offset + shown} of ${total.toLocaleString()}`}
            </span>
            <button
              className="secondary"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <button
              className="secondary"
              disabled={offset + shown >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  )
}
