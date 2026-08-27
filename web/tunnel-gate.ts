import type { Plugin } from 'vite'
import { timingSafeEqual } from 'node:crypto'

export function tunnelGate(): Plugin {
  return {
    name: 'tunnel-gate',
    apply: 'serve',
    configureServer(server) {
      const token = process.env.TUNNEL_TOKEN
      if (!token) {
        return
      }

      const strict = process.env.TUNNEL_STRICT === '1'
      const tokenBytes = Buffer.from(token)

      server.middlewares.use((req, res, next) => {
        // 1. Localhost exemption
        const hostHeader = req.headers.host ?? ''
        const hostname = hostHeader.split(':')[0].replace(/^\[|\]$/g, '')
        const isLocalhost =
          hostname === 'localhost' ||
          hostname === '127.0.0.1' ||
          hostname === '::1' ||
          hostname.endsWith('.localhost')
        if (isLocalhost && !strict) {
          return next()
        }

        // 2. Cookie hit
        const cookieHeader = req.headers.cookie ?? ''
        const cookieMatch = cookieHeader.match(/js_tunnel_key=([^;]+)/)
        if (cookieMatch) {
          const cookieToken = cookieMatch[1]
          if (cookieToken.length === tokenBytes.length) {
            const cookieBytes = Buffer.from(cookieToken)
            if (timingSafeEqual(cookieBytes, tokenBytes)) {
              return next()
            }
          }
        }

        // 3. Token presentation
        const url = new URL(req.url ?? '/', 'http://x')
        const queryToken = url.searchParams.get('k')
        const headerToken = req.headers['x-tunnel-key'] as string | undefined
        const presentedToken = queryToken ?? headerToken

        if (presentedToken && presentedToken.length === tokenBytes.length) {
          const presentedBytes = Buffer.from(presentedToken)
          if (timingSafeEqual(presentedBytes, tokenBytes)) {
            // Set cookie
            const cookie = `js_tunnel_key=${presentedToken}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400`
            res.setHeader('Set-Cookie', cookie)

            // If query form, strip k and redirect
            if (queryToken) {
              url.searchParams.delete('k')
              const cleanedUrl = url.pathname + url.search + url.hash
              res.writeHead(302, { Location: cleanedUrl })
              res.end()
              return
            }
            // Header form
            return next()
          }
        }

        // 4. Reject
        res.writeHead(404)
        res.end('Not Found')
      })
    },
  }
}