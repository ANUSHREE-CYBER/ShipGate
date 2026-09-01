import { useEffect, useState } from 'react'
import { FlaskConical, IndianRupee, ListChecks, ShieldCheck } from 'lucide-react'
import OrderQueue from './components/OrderQueue'
import DecisionDrawer from './components/DecisionDrawer'
import CostEvidence from './components/CostEvidence'
import { ToastProvider } from './components/Toast'
import { listOrders } from './api'

const TABS = [
  { id: 'queue', label: 'Order queue', Icon: ListChecks },
  { id: 'cost', label: 'Cost evidence', Icon: IndianRupee },
]

/**
 * Slim sticky bar. The count is read live from the queue endpoint rather than
 * lifted out of the table, so it stays correct on the cost tab too and updates
 * after an override without the table needing to be on screen.
 */
function TopBar({ refreshToken }) {
  const [total, setTotal] = useState(null)

  useEffect(() => {
    let cancelled = false
    listOrders({ limit: 1 })
      .then((page) => { if (!cancelled) setTotal(page.total) })
      .catch(() => { if (!cancelled) setTotal(null) })
    return () => { cancelled = true }
  }, [refreshToken])

  return (
    <div className="topbar">
      <div className="topbar-inner">
        <div className="topbar-brand">
          <span className="topbar-mark" aria-hidden="true">
            <ShieldCheck size={16} strokeWidth={2.4} />
          </span>
          <span className="topbar-name">
            Ship<span>Gate</span>
          </span>
        </div>
        <div className="topbar-meta">
          {total === null ? (
            <span className="topbar-count muted">queue unavailable</span>
          ) : (
            <span className="topbar-count">
              <strong>{total.toLocaleString()}</strong> orders in queue
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

function Hero() {
  return (
    <header className="hero">
      <div className="hero-inner">
        <div className="hero-mark" aria-hidden="true">
          <ShieldCheck size={26} strokeWidth={2.1} />
        </div>
        <div className="hero-text">
          <h1>
            Ship<span>Gate</span>
          </h1>
          <p className="hero-pitch">
            A merchant-configurable decision-policy layer for COD return risk. It turns
            a risk signal into the <strong>least-disruptive action that is
            economically justified</strong> — and records every score, rule, override
            and delivery outcome in an audit trail that cannot be edited.
          </p>
        </div>
      </div>

      <div className="hero-note">
        <FlaskConical size={16} strokeWidth={2.2} aria-hidden="true" />
        <span>
          <strong>Synthetic simulation.</strong> Every order, outcome and rupee figure
          here is generated. It validates policy logic, safeguards and the evaluation
          pipeline — it does not establish real-world RTO prediction accuracy.
        </span>
      </div>
    </header>
  )
}

export default function App() {
  const [tab, setTab] = useState('queue')
  const [selectedId, setSelectedId] = useState(null)
  // Bumped after any write so the queue re-reads from the audit log rather than
  // patching a local copy. The log is the source of truth; a dashboard that
  // disagreed with it would undermine the one thing this product claims.
  const [refreshToken, setRefreshToken] = useState(0)

  return (
    <ToastProvider>
      <TopBar refreshToken={refreshToken} />

      <div className="app">
        <Hero />

        <nav className="tabs" role="tablist">
          {TABS.map(({ id, label, Icon }) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              onClick={() => setTab(id)}
            >
              <Icon size={16} strokeWidth={2.2} aria-hidden="true" />
              {label}
            </button>
          ))}
        </nav>

        {tab === 'queue' ? (
          <OrderQueue
            selectedId={selectedId}
            onSelect={setSelectedId}
            refreshToken={refreshToken}
          />
        ) : (
          <CostEvidence />
        )}

        {selectedId && (
          <DecisionDrawer
            orderId={selectedId}
            onClose={() => setSelectedId(null)}
            onChanged={() => setRefreshToken((n) => n + 1)}
          />
        )}
      </div>
    </ToastProvider>
  )
}
