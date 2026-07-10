import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import StaticApp from './StaticApp.tsx'
import { ThemeProvider } from '@/components/theme-provider'
import { Toaster } from '@/components/ui/sonner'

// `npm run build:static` (Vite --mode static) builds the read-only public
// dashboard instead of the full local control panel — see web/README or
// PLAN.md §8 Phase 10 for the two build modes.
const Root = import.meta.env.MODE === 'static' ? StaticApp : App

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <Root />
      <Toaster />
    </ThemeProvider>
  </StrictMode>,
)
