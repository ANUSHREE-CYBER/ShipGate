import { useState } from 'react'
import OrderQueue from './components/OrderQueue'
import DecisionDrawer from './components/DecisionDrawer'
import CostEvidence from './components/CostEvidence'

export default function App() {
  const [tab, setTab] = useState('queue')
  const [selectedId, setSelectedId] = useState(null)
  // Bumped after any write so the queue re-reads from the audit log rather than
  // patching a local copy. The log is the source of truth; a dashboard that
  // disagreed with it would undermine the one thing this product claims.
  const [refreshToken, setRefreshToken] = useState(0)

  return (
    <div className="app">
      <div className="masthead">
        <h1>ShipGate</h1>
        <span className="tagline">
          merchant-configurable decision policy for COD return risk
        </span>
      </div>

      <p className="disclaimer">
        <strong>Synthetic simulation.</strong> Every order, outcome and rupee figure
        here is generated. It validates policy logic, safeguards and the evaluation
        pipeline — it does not establish real-world RTO prediction accuracy.
      </p>

      <div className="tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === 'queue'}
          onClick={() => setTab('queue')}
        >
          Order queue
        </button>
        <button
          role="tab"
          aria-selected={tab === 'cost'}
          onClick={() => setTab('cost')}
        >
          Cost evidence
        </button>
      </div>

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
  )
}
