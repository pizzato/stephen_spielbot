import { useState } from 'react'
import { Segmented } from '../components.jsx'
import Analytics from './Analytics.jsx'
import ManageVideos from './ManageVideos.jsx'
import Engagement from './Engagement.jsx'

// Top-level "Channel Analytics" page: the performance dashboard (YouTube/X) and
// the predictive reach model, as two tabs. The sidebar item and #/analytics open
// on Analytics; the legacy #/engagement deep link opens on the Predictive Model
// tab (each route remounts this screen, so initialTab sets the starting tab).
const TABS = [
  { value: 'analytics', label: 'Analytics' },
  { value: 'manage', label: 'Manage Videos' },
  { value: 'predictive', label: 'Predictive Model' },
]

export default function ChannelAnalytics({ meta, go, initialTab = 'analytics' }) {
  const [tab, setTab] = useState(initialTab)
  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Channel Analytics</span>
          <h1 className="display-md reveal reveal-d1">Channel Analytics</h1>
        </div>
      </div>

      <div className="reveal reveal-d1" style={{ marginBottom: 16 }}>
        <Segmented options={TABS} value={tab} onChange={setTab} />
      </div>

      {tab === 'analytics' ? <Analytics /> : tab === 'manage' ? <ManageVideos /> : <Engagement meta={meta} go={go} />}
    </div>
  )
}
