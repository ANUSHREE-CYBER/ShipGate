import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'

// Inter, bundled rather than fetched from a CDN. The dashboard is demoed and
// recorded on machines that may be offline or behind a blocked network, and a
// font that silently falls back mid-recording is not worth the 30kB saved.
// Only the four weights the stylesheet actually asks for are imported.
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'

import './styles.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
