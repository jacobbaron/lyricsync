# lyricsync web

Mobile web app for the `shorten/` workflow: upload phone videos →
transcribe → pick ranges → render a cut → download. See
[`../shorten/WEBAPP_PLAN.md`](../shorten/WEBAPP_PLAN.md) and
[`../shorten/PHASE1_TICKETS.md`](../shorten/PHASE1_TICKETS.md).

Next.js 15 (App Router) + TypeScript + Tailwind CSS v4.

## Local development

```bash
npm install
cp .env.example .env.local   # fill in as tickets land; not needed for the home page
npm run dev                  # http://localhost:3000
```

Other scripts:

```bash
npm run build   # production build
npm run lint    # eslint
```

## Environment variables

All keys are documented in [`.env.example`](./.env.example). Values
prefixed `NEXT_PUBLIC_` are exposed to the browser; everything else is
server-only. Set the same keys in the Vercel project's environment.

## Deploying to Vercel

This app lives in the `web/` subdirectory of the repo, so the Vercel
project's **Root Directory** must be set to `web`.

1. Vercel dashboard → Add New Project → import `jacobbaron/lyricsync`.
2. Set **Root Directory** to `web`. Framework Preset auto-detects Next.js.
3. Add the environment variables from `.env.example`.
4. Deploy. Pushes to `main` then deploy automatically.
5. Note the production URL — needed for the Supabase auth redirect (P1-02).
