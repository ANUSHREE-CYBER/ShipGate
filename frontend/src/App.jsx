import { useEffect, useState } from 'react'
import { ArrowUp, FlaskConical, IndianRupee, ListChecks, ShieldCheck } from 'lucide-react'
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
  // "not asked yet" and "asked and failed" are different things, and rendering
  // both as "queue unavailable" made an in-flight request look like an outage.
  const [state, setState] = useState({ status: 'loading', total: null })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading', total: null })
    listOrders({ limit: 1 })
      .then((page) => { if (!cancelled) setState({ status: 'ok', total: page.total }) })
      .catch(() => { if (!cancelled) setState({ status: 'failed', total: null }) })
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
          {state.status === 'ok' ? (
            <span className="topbar-count">
              <strong>{state.total.toLocaleString()}</strong> orders in queue
            </span>
          ) : (
            <span className="topbar-count muted">
              {state.status === 'loading' ? 'loading queue…' : 'queue unavailable'}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * Back-to-top, shown once the page has scrolled far enough to be worth it.
 *
 * Sits at z-index 45: above the page, below the drawer backdrop (50) and the
 * toasts (100). It is not rendered while the drawer is open, because the drawer
 * scrolls in its own container - a window scroll would do nothing there, and a
 * control that visibly does nothing is worse than no control.
 */
function ScrollToTop({ hidden }) {
  const [show, setShow] = useState(false)

  useEffect(() => {
    const onScroll = () => setShow(window.scrollY > 400)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  if (hidden || !show) return null

  const toTop = () => {
    // Honour the OS "reduce motion" setting - a long smooth scroll is exactly
    // the kind of movement that setting exists to switch off.
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' })
  }

  return (
    <button className="scroll-top" onClick={toTop} aria-label="Back to top" title="Back to top">
      <ArrowUp size={17} strokeWidth={2.5} aria-hidden="true" />
    </button>
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

      <ScrollToTop hidden={Boolean(selectedId)} />
    </ToastProvider>
  )
}
