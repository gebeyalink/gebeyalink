# GebeyaLink staff console

Local development for the React admin console.

```sh
nvm use 20
npm ci
cp .env.example .env
npm run dev
```

The app reads `VITE_API_BASE_URL` at build time. The API is expected at
`http://localhost:8000/v1`; change the value in `.env` if yours is elsewhere. The Vite dev
server also proxies `/v1` to `http://localhost:8000` for a relative base URL.

`npm run build` runs the TypeScript check before bundling. `npm run typecheck` and
`npm run lint` are available for local use.
