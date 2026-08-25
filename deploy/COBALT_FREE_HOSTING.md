# Deploy your own cobalt instance — FREE (no self-host server)

Your cobalt API must live somewhere Google doesn't bot-wall. Both options below
run the **official open-source image** (`ghcr.io/imputnet/cobalt`) on free tiers,
entirely via web dashboards — no CLI, no credit card.

After deploying either one, come back and set:

```
COBALT_API_URL=https://<your-app-url>
```
on the **toolz-downloadz-api** Vercel project → Redeploy → quality ladder (360p→4K + MP3) goes live.

---

## Option 1 — Render (recommended: easiest)

1. [render.com](https://render.com) → sign in with GitHub.
2. **New → Web Service** → *Existing Image* → Image URL:
   ```
   ghcr.io/imputnet/cobalt:11
   ```
3. Instance: **Free**. 
4. Environment variables:
   | Key | Value |
   |---|---|
   | `API_URL` | your assigned on-render.com URL (fill in after naming the service, e.g. `https://toolz-cobalt.onrender.com`) |
   | `API_PORT` | `10000` |
5. Create → wait for deploy → note the URL.

⚠️ Free tier sleeps after ~15 min idle → first request wakes it (~30-60s).
Pair with the cron-job.org ping (below) to stay warm.

## Option 2 — Koyeb

1. [koyeb.com](https://koyeb.com) → **Create App** → Docker image:
   ```
   ghcr.io/imputnet/cobalt:11
   ```
2. Exposed port: `9000` · Instance: **Free**.
3. Env vars: `API_URL=https://<app>.koyeb.app`
4. Deploy → URL ready.

---

## Keep it warm (cron-job.org, free)

cron-job.org → Create job → `GET https://<your-cobalt-url>/ping` → every 10 minutes.
Cobalt answers `/ping` instantly and stays out of sleep.

## Verify

```bash
curl https://<your-cobalt-url>/ping
# {"cobalt":{"version":"..."},"server_uptime":...}
```

Then in Vercel (`toolz-downloadz-api` → Settings → Env):
```
COBALT_API_URL=https://<your-cobalt-url>
```
Redeploy the API. Extract any YouTube link on the site → the quality row
(360p…4K, MP3) appears; conversions stream **through your own instance**
(open-source AGPL, you control it) and downloads come straight from its tunnel.
