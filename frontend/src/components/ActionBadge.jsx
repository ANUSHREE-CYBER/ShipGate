import { BadgePercent, MessageSquare, Truck, UserSearch } from 'lucide-react'

// One definition of what each action looks like, shared by the queue, the
// drawer and the timeline. Previously each rendered its own badge markup, which
// is exactly how a UI ends up calling the same thing two different names.
const ICONS = {
  ship: Truck,
  confirm: MessageSquare,
  nudge: BadgePercent,
  review: UserSearch,
}

export default function ActionBadge({ action, size = 13 }) {
  const Icon = ICONS[action]
  return (
    <span className={`badge ${action}`}>
      {Icon && <Icon size={size} strokeWidth={2.25} aria-hidden="true" />}
      {action}
    </span>
  )
}

/**
 * An overruled recommendation is shown alongside what the human chose, never
 * replaced by it — the same rule the audit log itself follows.
 */
export function FinalAction({ recommended, final, overridden }) {
  if (!overridden) return <ActionBadge action={final} />
  return (
    <span className="override-pair">
      <span className="strike">{recommended}</span>
      <span className="arrow">&rarr;</span>
      <ActionBadge action={final} />
    </span>
  )
}
