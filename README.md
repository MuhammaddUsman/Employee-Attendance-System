# Deploying to Vercel

1. Push this folder to a GitHub repo (or run `npx vercel` in this folder).
2. In Vercel: Add New > Project > import the repo. No build settings needed.
3. Project > Storage > Create/connect an **Upstash Redis** database (Marketplace).
   This auto-adds KV_REST_API_URL and KV_REST_API_TOKEN. Without it, data is lost.
4. Project > Settings > Environment Variables, add:
   - ADMIN_PASSWORD  (your admin password)
   - SECRET_KEY      (any long random string)
   - TIMEZONE        (optional, default Asia/Karachi)
5. Redeploy.

Local run: `pip install -r requirements.txt && python app.py`
