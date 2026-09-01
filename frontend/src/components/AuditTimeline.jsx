const KIND_LABEL = {
  decision: 'decided',
  override: 'overridden',
  outcome: 'outcome',
}

const KIND_CLASS = {
  decision: 'muted',
  override: 'nudge',
  outcome: 'confirm',
}

export default function AuditTimeline({ events }) {
  if (!events || events.length === 0) {
    return <p className="note">Nothing recorded yet.</p>
  }

  return (
    <ul className="timeline">
      {events.map((event, i) => (
        <li key={i}>
          <span className="when">{event.at.replace('T', ' ')}</span>
          <span className={`badge ${KIND_CLASS[event.kind] || 'muted'}`}>
            {KIND_LABEL[event.kind] || event.kind}
          </span>
          <span>{event.summary}</span>
        </li>
      ))}
    </ul>
  )
}
