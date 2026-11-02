# GuardSQL web UI

A small React + TypeScript page for the GuardSQL API: paste an API key, ask a question in
English, and see the SQL that ran, the result table, the confidence level and timings. Every
error the API can return has plain-language text.

```bash
npm install
cp .env.example .env     # optional: VITE_API_BASE points at another API deployment
npm run dev              # http://localhost:5173
npm test                 # unit tests for the API client
npm run build            # type-check and build to dist/
```

For the browser to accept the API's answers, the API must list this page's address in
`CORS_ALLOWED_ORIGINS` (for example `http://localhost:5173` for `npm run dev`).

`public/_headers` sets security headers (Content-Security-Policy and others) when the site is
hosted on Cloudflare Pages. Its `connect-src` must name the API's address.
