# usp-max web UI

Tiny React + Vite + Tailwind SPA that talks to the Starlette service in
`usp.web`.

## Development

```bash
cd web
npm install
npm run dev   # Vite dev server on http://localhost:5173
```

The dev server proxies `/api/*` to `http://127.0.0.1:8088` (the
`usp-max serve` server). Start the backend in another terminal:

```bash
usp-max serve --host 127.0.0.1 --port 8088
```

Then open <http://localhost:5173>.

## Production

```bash
cd web
npm install
npm run build      # outputs web/dist
usp-max serve      # serves web/dist automatically
```

## Layout

```
web/
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── postcss.config.js
├── tsconfig.json
├── index.html
├── public/
│   └── favicon.svg
└── src/
    ├── main.tsx           # React root
    ├── App.tsx            # top-level state machine (job lifecycle)
    ├── api.ts             # fetch wrappers + EventSource
    ├── index.css          # tailwind layers
    ├── types.ts           # JSON shapes shared with backend
    └── components/
        ├── CrawlForm.tsx
        ├── LogPanel.tsx
        ├── StatusBar.tsx
        └── ResultPanel.tsx
```

## How the live updates work

* Submitting the form POSTs to `/api/crawl` and gets back a job id.
* The UI then opens an `EventSource` on `/api/jobs/{id}/stream` (SSE).
* The backend pushes three categories of events:
  * `snapshot` — replay of current state on (re)connect;
  * `log` — formatted log lines from the crawler;
  * `progress` — every 50th URL with running stats;
  * `heartbeat` — every 15 s while the worker is busy;
  * `done` / `error` / `cancelled` — terminal.
* On `done`, the UI re-fetches `/api/jobs/{id}/urls` to count discovered
  URLs and exposes a "Download urls.txt" button.