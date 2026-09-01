import { useEffect, useState } from 'react'
import { getEvaluation } from '../api'

const rupees = (n) =>
  `${n < 0 ? '−' : ''}₹${Math.abs(Math.round(n)).toLocaleString('en-IN')}`

const pct = (n) => (n === null || n === undefined ? '—' : `${(n * 100).toFixed(1)}%`)

function Money({ value, signed = false }) {
  const cls = value > 0 ? 'money pos' : value < 0 ? 'money neg' : 'money'
  return <span className={cls}>{signed && value > 0 ? '+' : ''}{rupees(value)}</span>
}

export default function CostEvidence() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    getEvaluation().then(setData).catch((err) => setError(err.message))
  }, [])

  if (error) {
    return (
      <div className="panel">
        <h2>Cost evidence</h2>
        <p className="error">
          Could not load evaluation.json: {error}. Run{' '}
          <code>python -m app.evaluation --json</code> and rebuild the dashboard.
        </p>
      </div>
    )
  }
  if (!data) return <div className="panel"><p className="loading">Loading evaluation…</p></div>

  const cod = data.ranking.cod_only
  const shipgate = data.graduated_policies.find((p) => p.policy.startsWith('ShipGate'))
  const block = data.graduated_policies.find((p) => p.policy.startsWith('hard block everything'))
  const spread = shipgate.vs_ship_all - block.vs_ship_all
  const worst = Math.max(...data.graduated_policies.map((p) => Math.abs(p.vs_ship_all)))

  return (
    <>
      <div className="panel">
        <h2>Does the policy pay for itself?</h2>
        <p className="note">
          Measured on the held-out later {data.split.test_orders.toLocaleString()} orders
          ({data.split.test_from} to {data.split.test_to}), which the thresholds were
          never tuned against. Training used the earlier{' '}
          {data.split.train_orders.toLocaleString()}.
        </p>
        <div className="kpis">
          <div className="kpi">
            <div className="label">Same detection, gentler response</div>
            <div className="value money pos">{rupees(spread)}</div>
            <div className="foot">graduated actions vs blunt blocking, same orders</div>
          </div>
          <div className="kpi">
            <div className="label">ShipGate vs doing nothing</div>
            <div className="value money pos">+{rupees(shipgate.vs_ship_all)}</div>
            <div className="foot">across {data.split.test_orders.toLocaleString()} orders</div>
          </div>
          <div className="kpi">
            <div className="label">PR-AUC, COD orders</div>
            <div className="value">{cod.pr_auc.toFixed(3)}</div>
            <div className="foot">vs {cod.baseline.toFixed(3)} baseline ({(cod.pr_auc / cod.baseline).toFixed(2)}×)</div>
          </div>
          <div className="kpi">
            <div className="label">Orders touched</div>
            <div className="value">
              {(data.operational_load.confirm || 0) + (data.operational_load.nudge || 0)
                + (data.operational_load.review || 0)}
            </div>
            <div className="foot">
              {data.operational_load.confirm || 0} confirm · {data.operational_load.nudge || 0} nudge ·{' '}
              {data.operational_load.review || 0} review
            </div>
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>Graduated actions vs blunt blocking</h2>
        <p className="note">
          Net rupee value on the test slice. The bottom row applies a hard block to
          exactly the orders ShipGate intervenes on — same detection, harsher response.
        </p>
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>Policy</th>
                <th className="num">Net value</th>
                <th className="num">vs doing nothing</th>
                <th style={{ width: 160 }}></th>
              </tr>
            </thead>
            <tbody>
              {data.graduated_policies.map((row) => (
                <tr key={row.policy} className={row.policy.startsWith('ShipGate') ? 'highlight' : ''}>
                  <td>{row.policy}</td>
                  <td className="num"><Money value={row.net_value} /></td>
                  <td className="num"><Money value={row.vs_ship_all} signed /></td>
                  <td>
                    <div className="bar">
                      <span
                        className={row.vs_ship_all >= 0 ? 'pos' : 'neg'}
                        style={{ width: `${(Math.abs(row.vs_ship_all) / worst) * 100}%` }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <h2>Why each action is justified</h2>
        <p className="note">
          Every action has its own break-even, because each does a different amount of
          good and a different amount of harm. An action earns its place where the
          measured failure rate of the tier that triggers it clears its own break-even.
        </p>
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>Action</th>
                <th className="num">Prevents</th>
                <th className="num">Abandons</th>
                <th className="num">Cost</th>
                <th className="num">Break-even</th>
                <th className="num">Tier's real rate</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {data.graduated_actions.map((row) => {
                const justified = row.tier_rto_rate !== null && row.break_even_p !== null
                  && row.tier_rto_rate > row.break_even_p
                return (
                  <tr key={row.action}>
                    <td>{row.label}</td>
                    <td className="num">{pct(row.prevent_rate)}</td>
                    <td className="num">{pct(row.abandon_rate)}</td>
                    <td className="num">₹{row.op_cost}</td>
                    <td className="num">{pct(row.break_even_p)}</td>
                    <td className="num">{pct(row.tier_rto_rate)}</td>
                    <td>
                      {row.action === 'ship' ? <span className="badge muted">baseline</span>
                        : row.action === 'block' ? <span className="badge muted">not offered</span>
                          : justified ? <span className="badge delivered">justified</span>
                            : <span className="badge rto">not justified</span>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        <div className="caveat">
          <strong>These rates are assumptions, not measurements.</strong> Nothing in
          synthetic data can say how many real customers abandon after an OTP prompt —
          that is a fact about people, not software. Each action stops paying for
          itself above these abandonment rates:
          <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
            {data.assumption_sensitivity.map((row) => (
              <li key={row.action}>
                {row.action}: assumed {pct(row.assumed_abandon_rate)}, stops paying above{' '}
                <strong>{pct(row.stops_paying_above)}</strong>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="panel">
        <h2>The brief's original cost table</h2>
        <p className="note">
          {data.blunt_cost_table.note} Under it, flagging only pays above a{' '}
          {pct(data.blunt_cost_table.break_even_p)} failure probability — so "do nothing"
          is nearly optimal. That is what motivated pricing each action separately above.
        </p>
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>Policy</th>
                <th className="num">Flagged</th>
                <th className="num">Precision</th>
                <th className="num">Recall</th>
                <th className="num">Net value</th>
              </tr>
            </thead>
            <tbody>
              {data.blunt_cost_table.policies.map((row) => (
                <tr key={row.policy}>
                  <td>{row.policy}</td>
                  <td className="num">{row.flagged.toLocaleString()}</td>
                  <td className="num">{row.precision.toFixed(3)}</td>
                  <td className="num">{row.recall.toFixed(3)}</td>
                  <td className="num"><Money value={row.net_value} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <h2>Where the rules work, and where they do not</h2>
        <p className="note">
          PR-AUC by segment, COD orders only. Prepaid is excluded because separating
          prepaid from COD is not a prediction — payment method is known at checkout.
        </p>
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>Segment</th>
                <th className="num">Orders</th>
                <th className="num">RTO rate</th>
                <th className="num">PR-AUC</th>
                <th className="num">vs baseline</th>
              </tr>
            </thead>
            <tbody>
              {data.segments.map((row) => (
                <tr key={row.segment}>
                  <td>{row.segment}</td>
                  <td className="num">{row.n.toLocaleString()}</td>
                  <td className="num">{pct(row.rto_rate)}</td>
                  <td className="num">{row.pr_auc.toFixed(3)}</td>
                  <td className="num">{(row.pr_auc / row.rto_rate).toFixed(2)}×</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="caveat">
          <strong>The rules are near-useless on a first-time customer.</strong> A new
          customer scores barely above random ranking, because there is genuinely
          almost nothing to go on. Everything ShipGate is good at comes from customers
          with delivery history.
        </div>
      </div>
    </>
  )
}
