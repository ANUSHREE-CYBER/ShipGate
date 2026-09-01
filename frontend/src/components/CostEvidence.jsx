import { useEffect, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { AlertTriangle, Ban, TrendingDown, TrendingUp } from 'lucide-react'
import { getEvaluation } from '../api'

// Recharts takes literal colours rather than CSS variables, so these mirror
// the palette in styles.css. If one changes, change both.
const C = {
  good: '#3fbf87',
  bad: '#e05563',
  accent: '#5b8cff',
  warn: '#d8a72c',
  muted: '#8d97a8',
  grid: '#2a3140',
  panel: '#1e232d',
  line: '#2a3140',
  text: '#e6e9ef',
}

const rupees = (n) =>
  `${n < 0 ? '−' : ''}₹${Math.abs(Math.round(n)).toLocaleString('en-IN')}`

const compact = (n) => {
  const abs = Math.abs(n)
  const sign = n < 0 ? '−' : ''
  if (abs >= 100000) return `${sign}₹${(abs / 100000).toFixed(2)}L`
  if (abs >= 1000) return `${sign}₹${(abs / 1000).toFixed(0)}k`
  return `${sign}₹${abs}`
}

const pct = (n) => (n === null || n === undefined ? '—' : `${(n * 100).toFixed(1)}%`)

function Money({ value, signed = false }) {
  const cls = value > 0 ? 'money pos' : value < 0 ? 'money neg' : 'money'
  return <span className={cls}>{signed && value > 0 ? '+' : ''}{rupees(value)}</span>
}

function ChartTooltip({ active, payload, label, formatter }) {
  if (!active || !payload || !payload.length) return null
  return (
    <div className="chart-tip">
      <div className="chart-tip-label">{label}</div>
      {payload.map((entry) => (
        <div key={entry.dataKey} className="chart-tip-row">
          <span className="swatch" style={{ background: entry.color || entry.fill }} />
          <span>{entry.name}</span>
          <strong>{formatter(entry.value)}</strong>
        </div>
      ))}
    </div>
  )
}

/** Short labels so the axis stays readable at chart width. */
function shortPolicy(policy) {
  if (policy.startsWith('ShipGate')) return 'ShipGate graduated'
  if (policy.startsWith('ship everything')) return 'Do nothing'
  if (policy.startsWith('confirm every')) return 'Confirm everything'
  if (policy.startsWith('hard block everything')) return 'Hard block (all above Low)'
  if (policy.startsWith('hard block Very High')) return 'Hard block (Very High only)'
  return policy
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

  const policyChart = data.graduated_policies
    .map((p) => ({ ...p, short: shortPolicy(p.policy) }))
    .sort((a, b) => b.vs_ship_all - a.vs_ship_all)

  const breakEvenChart = data.graduated_actions
    .filter((a) => a.break_even_p !== null && a.tier_rto_rate !== null)
    .map((a) => ({
      action: a.label.replace(' / OTP', '').replace(' queue', '').replace('prepaid incentive ', ''),
      breakEven: +(a.break_even_p * 100).toFixed(1),
      actual: +(a.tier_rto_rate * 100).toFixed(1),
    }))

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
          <div className="kpi headline">
            <div className="label">
              <TrendingUp size={14} strokeWidth={2.4} /> Same detection, gentler response
            </div>
            <div className="value money pos">{rupees(spread)}</div>
            <div className="foot">graduated actions vs blunt blocking, identical orders</div>
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
          Net rupee value against doing nothing, on the test slice. The bottom bar
          applies a hard block to <em>exactly the orders ShipGate intervenes on</em> —
          same detection, harsher response.
        </p>

        <div className="chart" style={{ height: 260 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={policyChart} layout="vertical"
                      margin={{ top: 8, right: 56, bottom: 8, left: 8 }}>
              <CartesianGrid stroke={C.grid} horizontal={false} />
              <XAxis type="number" tickFormatter={compact} stroke={C.muted}
                     tick={{ fill: C.muted, fontSize: 12 }} />
              <YAxis type="category" dataKey="short" width={190} stroke={C.muted}
                     tick={{ fill: C.text, fontSize: 12.5 }} />
              <ReferenceLine x={0} stroke={C.muted} strokeWidth={1.5} />
              <Tooltip cursor={{ fill: 'rgba(255,255,255,.04)' }}
                       content={<ChartTooltip formatter={rupees} />} />
              <Bar dataKey="vs_ship_all" name="vs doing nothing" radius={[0, 4, 4, 0]}>
                {policyChart.map((row) => (
                  <Cell key={row.policy}
                        fill={row.vs_ship_all >= 0 ? C.good : C.bad}
                        fillOpacity={row.policy.startsWith('ShipGate') ? 1 : 0.62} />
                ))}
                <LabelList dataKey="vs_ship_all" position="right"
                           formatter={compact}
                           style={{ fill: C.text, fontSize: 12, fontWeight: 600 }} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="spread-callout">
          <TrendingUp size={18} strokeWidth={2.3} className="up" />
          <span>
            <strong>{rupees(spread)}</strong> separates the two. Same rules, same orders
            flagged — the entire gap is choosing the least-disruptive action instead of
            the harshest one.
          </span>
          <TrendingDown size={18} strokeWidth={2.3} className="down" />
        </div>

        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>Policy</th>
                <th className="num">Net value</th>
                <th className="num">vs doing nothing</th>
              </tr>
            </thead>
            <tbody>
              {data.graduated_policies.map((row) => (
                <tr key={row.policy} className={row.policy.startsWith('ShipGate') ? 'highlight' : ''}>
                  <td>{row.policy}</td>
                  <td className="num"><Money value={row.net_value} /></td>
                  <td className="num"><Money value={row.vs_ship_all} signed /></td>
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
          measured failure rate of the tier that triggers it clears its own break-even —
          in other words, wherever the blue bar clears the grey one.
        </p>

        <div className="chart" style={{ height: 260 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={breakEvenChart}
                      margin={{ top: 18, right: 12, bottom: 4, left: 4 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="action" stroke={C.muted}
                     tick={{ fill: C.text, fontSize: 12.5 }} />
              <YAxis unit="%" stroke={C.muted} tick={{ fill: C.muted, fontSize: 12 }} />
              <Tooltip cursor={{ fill: 'rgba(255,255,255,.04)' }}
                       content={<ChartTooltip formatter={(v) => `${v}%`} />} />
              <Legend wrapperStyle={{ fontSize: 12.5, color: C.muted, paddingTop: 6 }} />
              <Bar dataKey="breakEven" name="break-even" fill={C.muted}
                   fillOpacity={0.55} radius={[4, 4, 0, 0]} />
              <Bar dataKey="actual" name="tier's measured RTO rate" fill={C.accent}
                   radius={[4, 4, 0, 0]}>
                <LabelList dataKey="actual" position="top" formatter={(v) => `${v}%`}
                           style={{ fill: C.text, fontSize: 12, fontWeight: 600 }} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

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
                        : row.action === 'block'
                          ? <span className="badge muted"><Ban size={13} strokeWidth={2.3} /> not offered</span>
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
          <AlertTriangle size={17} strokeWidth={2.3} className="caveat-icon" />
          <div>
            <strong>These rates are assumptions, not measurements.</strong> Nothing in
            synthetic data can say how many real customers abandon after an OTP prompt —
            that is a fact about people, not software. Each action stops paying for
            itself above these abandonment rates:
            <ul>
              {data.assumption_sensitivity.map((row) => (
                <li key={row.action}>
                  {row.action}: assumed {pct(row.assumed_abandon_rate)}, stops paying above{' '}
                  <strong>{pct(row.stops_paying_above)}</strong>
                </li>
              ))}
            </ul>
          </div>
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
          <AlertTriangle size={17} strokeWidth={2.3} className="caveat-icon" />
          <div>
            <strong>The rules are near-useless on a first-time customer.</strong> A new
            customer scores barely above random ranking, because there is genuinely
            almost nothing to go on. Everything ShipGate is good at comes from customers
            with delivery history.
          </div>
        </div>
      </div>
    </>
  )
}
