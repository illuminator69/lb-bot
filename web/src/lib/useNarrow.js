import { useEffect, useState } from 'react'

// Below 1000 px a list-and-detail page shows one pane at a time (index.css).
// A page needs to know, because "nothing selected" then means the list, not
// "select the first row".
const QUERY = '(max-width: 1000px)'

export function useNarrow() {
  const [narrow, setNarrow] = useState(() => window.matchMedia?.(QUERY).matches ?? false)
  useEffect(() => {
    const mq = window.matchMedia?.(QUERY)
    if (!mq) return
    const on = () => setNarrow(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return narrow
}
