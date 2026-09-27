export const NEWS_CHECK_MODES = [
  { value: 'off', label: 'Off — never check', hint: 'No news searches, including when you click Check news now.' },
  { value: 'manual', label: 'On demand', hint: 'Search only when you click Check news now in AI Ideas → News.' },
  { value: 'page_open', label: 'When opening AI Ideas', hint: 'Search when you visit AI Ideas. No background checks or repeated searches while the page stays open. Visits within one minute share a check.' },
  { value: 'scheduled', label: 'Scheduled', hint: 'Check while the backend is running, spaced evenly like publishing. Defaults to once a day.' },
]

export function newsCheckLabel(monitor) {
  if (!monitor?.enabled) return 'Off'
  if (monitor.check_mode === 'scheduled') return `${monitor.checks_per_day || 1} check${Number(monitor.checks_per_day || 1) === 1 ? '' : 's'} per day`
  return NEWS_CHECK_MODES.find((mode) => mode.value === (monitor.check_mode || 'manual'))?.label || 'On demand'
}
