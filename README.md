# halite-api

The API is stateless with respect to uploaded business files. The web app stores
context files in browser IndexedDB and posts them to the analysis endpoints when
needed. Dataset and upload compatibility routes keep data in process memory only,
so no SQLite file or writable persistent volume is required for deployment.

## Free deployment on Render

This repository includes [`render.yaml`](./render.yaml) for a Render Free web
service. From the Render dashboard:

1. Create a **Blueprint** from the repository and select `render.yaml`.
2. Set `FRONTEND_ORIGINS` to the deployed web app origin, such as
   `https://your-app.vercel.app`. Multiple origins may be comma-separated.
3. Set `CLERK_JWKS_URL` to the JWKS URL from Clerk. Use `CLERK_JWT_KEY`
   instead only when using a PEM signing key.
4. Set `OPENROUTER_API_KEY` as a secret environment variable.
5. Deploy and verify `https://your-service.onrender.com/health` returns
   `{"ok":true}`.

The Next.js app must use the Render URL as
`NEXT_PUBLIC_API_BASE_URL` in its hosting provider. Render's free service sleeps
after inactivity, and this API stores uploads, analyses, and chat sessions in
memory, so that state is lost when the service restarts or spins down. This is
appropriate for a prototype/demo, not durable production storage.
