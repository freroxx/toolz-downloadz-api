# Free cobalt hosting — NO credit card, NO PC

Both options run the **official open-source image** on someone else's cloud.
Your computer stays off. Total setup: ~5 minutes each.

| | Koyeb (Option A) | Back4App (Option B) |
|---|---|---|
| Card required | ❌ no (GitHub signup) | ❌ no |
| Method | official Docker image | builds a tiny Dockerfile from GitHub |
| Free limits | 1 web service, sleeps when idle | 0.5GB RAM, sleeps when idle |
| Sleep fix | cron ping below | cron ping below |

---

## Option A — Koyeb ⭐ (fastest, 4 clicks)

1. [koyeb.com](https://www.koyeb.com) → **Sign up with GitHub** (no card asked).
2. Dashboard → **Create App**.
3. Deployment method: **Docker image** → Image:
   ```
   ghcr.io/imputnet/cobalt:11
   ```
4. When asked for the **port** your app listens on: `9000`
5. Instance: **Free / Micro** · Region: any.
6. **Environment variables** (add one):
   - Leave empty for now — you'll add `API_URL` after Koyeb shows you the URL.
7. Name the app `toolz-cobalt` → **Deploy**.

After it's running, Koyeb gives you:
```
https://toolz-cobalt-<yourorg>.koyeb.app
```

8. Go back to the app → **Settings → Environment Variables** → add:
   - `API_URL` = `https://toolz-cobalt-<yourorg>.koyeb.app` (the URL from step above)
   → This triggers a redeploy automatically.

✅ **Verify:** open `https://<that-url>/ping` → JSON with `"cobalt"` info.

---

## Option B — Back4App Containers

1. Create a **new public GitHub repo** (e.g. `toolz-cobalt-deploy`) containing
   just the `Dockerfile` from `deploy/cobalt-selfhost/Dockerfile` in this repo.
2. [back4app.com](https://www.back4app.com) → sign up with GitHub (no card) →
   **Containers → New App → Import from GitHub** → pick that repo.
3. Configure: RAM default, port `9000`.
4. After first deploy, note your URL:
   `https://<name>.back4app.io`
5. Back4App dashboard → your app → **Environment Variables** → add:
   - `API_URL` = that URL → app redeploys.

✅ Verify same as above (`/ping`).

---

## Keep both warm + wire into the API

1. cron-job.org → two jobs (or reuse existing ones), every 10 min:
   - `GET https://<cobalt-url>/ping`
2. Vercel → `toolz-downloadz-api` → Settings → Env Vars:
   ```
   COBALT_API_URL = https://<cobalt-url>
   ```
   → Redeploy API.

Done. Every YouTube extraction now exposes **360p → 1440p → 4K + MP3**, converted by
*your own open-source instance*, streamed through *its* tunnel — nothing touches
Vercel's flagged IPs.
