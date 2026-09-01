import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'

const ToastContext = createContext(() => {})

const ICONS = { success: CheckCircle2, error: AlertCircle, info: Info }

// Errors stay up longer than confirmations - a failure is something you need to
// read, a success is something you already expected.
const LIFETIMES = { success: 4200, info: 4200, error: 7000 }

let nextId = 0

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const dismiss = useCallback((id) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback((message, tone = 'success') => {
    const id = ++nextId
    setToasts((current) => [...current, { id, message, tone }])
    return id
  }, [])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <Toast key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  )
}

function Toast({ toast, onDismiss }) {
  const Icon = ICONS[toast.tone] || Info

  useEffect(() => {
    const timer = setTimeout(onDismiss, LIFETIMES[toast.tone] || 4200)
    return () => clearTimeout(timer)
  }, [onDismiss, toast.tone])

  return (
    <div className={`toast ${toast.tone}`}>
      <Icon size={17} strokeWidth={2.25} className="toast-icon" aria-hidden="true" />
      <span className="toast-body">{toast.message}</span>
      <button className="toast-close" onClick={onDismiss} aria-label="Dismiss">
        <X size={14} strokeWidth={2.5} />
      </button>
    </div>
  )
}

export function useToast() {
  return useContext(ToastContext)
}
