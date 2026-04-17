# lyricsync timing editor

Vite + React UI served by `lyricsync serve`. Production build outputs to
`../src/lyricsync/editor_static/` (run `npm run build` from this folder).

```bash
npm ci
npm run dev
# Vite dev server proxies /api to http://127.0.0.1:8765 — run `lyricsync serve -o ../out` in another terminal.
```
