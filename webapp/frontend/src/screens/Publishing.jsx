import { useState, useEffect } from 'react'
import { Segmented } from '../components.jsx'
import PublishSchedule from './PublishSchedule.jsx'
import Publish from './Publish.jsx'

// Top-level "Publishing" page: the scheduled publish queue and the manual
// per-film publisher, as two tabs. Deep links to a specific film
// (#/publish/<slug>, e.g. the "Publish" button on Films/Queue) open the manual
// tab with that film selected; otherwise it opens on the schedule.
const TABS = [
  { value: 'schedule', label: 'Schedule' },
  { value: 'manual', label: 'Publish a film' },
]

export default function Publishing({ go, meta, initialWorkDir }) {
  const [tab, setTab] = useState(initialWorkDir ? 'manual' : 'schedule')
  // Follow the URL: a film deep-link (#/publish/<slug>) shows the manual tab,
  // the bare #/publish shows the schedule — even when this screen is already
  // mounted (a manual tab pick in between still sticks until the URL changes).
  useEffect(() => { setTab(initialWorkDir ? 'manual' : 'schedule') }, [initialWorkDir])
  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Publishing</span>
          <h1 className="display-md reveal reveal-d1">Publishing</h1>
        </div>
      </div>

      <div style={{ marginBottom: 22 }}>
        <Segmented options={TABS} value={tab} onChange={setTab} />
      </div>

      {tab === 'schedule'
        ? <PublishSchedule go={go} meta={meta} />
        : <Publish initialWorkDir={initialWorkDir} go={go} />}
    </div>
  )
}
