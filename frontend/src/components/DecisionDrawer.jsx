import { Fragment, useEffect, useState } from 'react'
import { ShieldAlert, X } from 'lucide-react'
import { getAudit, postOutcome, postOverride } from '../api'
import AuditTimeline from './AuditTimeline'
import ActionBadge from './ActionBadge'
import { useToast } from './Toast'

const ACTIONS = ['ship', 'confirm', 'nudge', 'review']

const request_label = (isRto) => (isRto === 'true' ? 'came back (RTO)' : 'delivered')

const GROUP_LABELS = {
  payment: 'Payment exposure',
  history: 'Customer history',
  context: 'Order context',
  deliverability: 'Deliverability',
}

function RuleTable({ rules }) {
  if (!rules.length) {
    return <p className="note">No rules fired. Nothing about this order raised a flag.</p>
  }
  const byGroup = rules.reduce((acc, rule) => {
    ;(acc[rule.group] = acc[rule.group] || []).push(rule)
    return acc
  }, {})

  return (
    <div className="scroll-x">
      <table className="rules">
        <thead>
          <tr>
            <th>Rule</th>
            <th className="num">Points</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(byGroup).map(([group, groupRules]) => (
            <Fragment key={group}>
              <tr>
                <td colSpan={3} style={{ color: 'var(--muted)', fontSize: 12 }}>
                  {GROUP_LABELS[group] || group}
                </td>
              </tr>
              {groupRules.map((rule, i) => (
                <tr key={`${group}-${rule.id}-${i}`}>
                  <td>
                    {rule.label} <span style={{ color: 'var(--muted)' }}>({rule.id})</span>
                    {rule.evidence && <span className="evidence-tag">evidence</span>}
                  </td>
                  <td className={`num pts${rule.points < 0 ? ' neg' : ''}`}>
                    {rule.points > 0 ? `+${rule.points}` : rule.points}
                  </td>
                  <td style={{ color: 'var(--muted)' }}>{rule.detail}</td>
                </tr>
              ))}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function DecisionDrawer({ orderId, onClose, onChanged }) {
  const toast = useToast()
  const [trail, setTrail] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const [override, setOverride] = useState({ action: 'ship', reason: '', actor: '' })
  const [outcome, setOutcome] = useState({ is_rto: 'true', source: 'courier', note: '' })

  function load() {
    getAudit(orderId).then(setTrail).catch((err) => setError(err.message))
  }

  useEffect(() => {
    setTrail(null)
    setError(null)
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderId])

  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function submitOverride(e) {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      const result = await postOverride(orderId, override)
      setOverride({ ...override, reason: '' })
      toast(
        `${orderId}: overridden to ${result.final_action}. The ` +
        `${result.recommended_action} recommendation stays on the trail.`,
        'success',
      )
      load()
      onChanged()
    } catch (err) {
      toast(err.message, 'error')
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function submitOutcome(e) {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      const result = await postOutcome(orderId, {
        is_rto: outcome.is_rto === 'true',
        source: outcome.source,
        note: outcome.note || null,
      })
      setOutcome({ ...outcome, note: '' })
      toast(
        result.superseded_earlier_outcome
          ? `${orderId}: outcome corrected. The earlier one is still on the trail.`
          : `${orderId}: recorded as ${request_label(outcome.is_rto)}.`,
        'success',
      )
      load()
      onChanged()
    } catch (err) {
      toast(err.message, 'error')
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const decision = trail && trail.current_decision

  return (
    <div className="drawer-backdrop" onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="drawer" role="dialog" aria-label={`Decision detail for ${orderId}`}>
        <header>
          <div>
            <h2>{orderId}</h2>
            {decision && (
              <div className="scoreline">
                <span className="big">{decision.score}</span>
                <span className="sub">
                  risk score · {decision.policy_tier.replace('_', ' ')} tier ·
                  {' '}evidence {decision.evidence_score}
                </span>
                <ActionBadge action={trail.final_action} size={14} />
              </div>
            )}
          </div>
          <button className="close" onClick={onClose} aria-label="Close">
            <X size={16} strokeWidth={2.4} />
          </button>
        </header>

        {error && (
          <p className="error inline">
            <ShieldAlert size={15} strokeWidth={2.3} aria-hidden="true" />
            {error}
          </p>
        )}
        {!trail && !error && <p className="loading">Loading…</p>}

        {decision && (
          <>
            <section>
              <h3>Why this decision</h3>
              <ul className="reasons">
                {decision.reasons.map((reason, i) => <li key={i}>{reason}</li>)}
              </ul>
              {decision.raw_score !== decision.score && (
                <p className="note">
                  Raw score before group caps was {decision.raw_score}; caps brought it
                  to {decision.score}.
                </p>
              )}
              {decision.safeguards_applied.length > 0 && (
                <p className="note">
                  Safeguards applied: {decision.safeguards_applied.join(', ')} — the score
                  is left untouched, only the tier moves.
                </p>
              )}
            </section>

            <section>
              <h3>Rules that fired</h3>
              <RuleTable rules={decision.fired_rules} />
            </section>

            <section>
              <h3>Audit trail</h3>
              <AuditTimeline events={trail.timeline} />
            </section>

            <section>
              <h3>Override this decision</h3>
              <p className="note">
                A reason of at least 10 characters is required and is recorded
                permanently. The recommendation is never erased.
              </p>
              <form className="stack" onSubmit={submitOverride}>
                <div className="row">
                  <select
                    value={override.action}
                    onChange={(e) => setOverride({ ...override, action: e.target.value })}
                  >
                    {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                  </select>
                  <input
                    className="grow"
                    placeholder="your name"
                    value={override.actor}
                    onChange={(e) => setOverride({ ...override, actor: e.target.value })}
                  />
                </div>
                <textarea
                  placeholder="Why are you overriding this? e.g. regular wholesale buyer, verified by phone"
                  value={override.reason}
                  onChange={(e) => setOverride({ ...override, reason: e.target.value })}
                />
                <button
                  className="primary"
                  disabled={busy || override.reason.trim().length < 10 || !override.actor.trim()}
                >
                  Record override
                </button>
              </form>
            </section>

            <section>
              <h3>Record the delivery outcome</h3>
              <p className="note">
                Corrections append rather than overwrite — send a second outcome and
                both stay on the trail.
              </p>
              <form className="stack" onSubmit={submitOutcome}>
                <div className="row">
                  <select
                    value={outcome.is_rto}
                    onChange={(e) => setOutcome({ ...outcome, is_rto: e.target.value })}
                  >
                    <option value="false">delivered</option>
                    <option value="true">came back (RTO)</option>
                  </select>
                  <input
                    className="grow"
                    placeholder="source, e.g. courier"
                    value={outcome.source}
                    onChange={(e) => setOutcome({ ...outcome, source: e.target.value })}
                  />
                </div>
                <input
                  placeholder="optional note"
                  value={outcome.note}
                  onChange={(e) => setOutcome({ ...outcome, note: e.target.value })}
                />
                <button className="primary" disabled={busy || !outcome.source.trim()}>
                  Record outcome
                </button>
              </form>
            </section>
          </>
        )}
      </div>
    </div>
  )
}
