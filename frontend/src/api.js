// Thin wrapper over the five endpoints. Kept deliberately dumb: no caching, no
// client-side state machine. The audit log is the source of truth, so after any
// write the UI re-reads rather than patching a local copy - a dashboard that
// disagrees with the audit trail would undermine the one thing this product
// claims.

async function request(path, options) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = body && body.detail
    throw new Error(
      typeof detail === 'string'
        ? detail
        : detail
          ? JSON.stringify(detail)
          : `Request failed (${response.status})`,
    )
  }
  return body
}

export function listOrders(filters = {}) {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) {
      params.set(key, value)
    }
  })
  return request(`/orders?${params.toString()}`)
}

export function getAudit(orderId) {
  return request(`/orders/${encodeURIComponent(orderId)}/audit`)
}

export function postOverride(orderId, payload) {
  return request(`/orders/${encodeURIComponent(orderId)}/override`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function postOutcome(orderId, payload) {
  return request(`/orders/${encodeURIComponent(orderId)}/outcome`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getEvaluation() {
  return request('/evaluation.json')
}
