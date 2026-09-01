import { PackageSearch, PenLine, ScanLine } from 'lucide-react'

const KINDS = {
  decision: { label: 'decided', cls: 'muted', Icon: ScanLine },
  override: { label: 'overridden', cls: 'nudge', Icon: PenLine },
  outcome: { label: 'outcome', cls: 'confirm', Icon: PackageSearch },
}

export default function AuditTimeline({ events }) {
  if (!events || events.length === 0) {
    return <p className="note">Nothing recorded yet.</p>
  }

  return (
    <ul className="timeline">
      {events.map((event, i) => {
        const kind = KINDS[event.kind] || { label: event.kind, cls: 'muted', Icon: ScanLine }
        const { Icon } = kind
        return (
          <li key={i}>
            <span className="when">{event.at.replace('T', ' ')}</span>
            <span className={`badge ${kind.cls}`}>
              <Icon size={13} strokeWidth={2.25} aria-hidden="true" />
              {kind.label}
            </span>
            <span>{event.summary}</span>
          </li>
        )
      })}
    </ul>
  )
}
