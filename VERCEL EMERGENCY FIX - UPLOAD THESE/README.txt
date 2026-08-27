VERCEL EMERGENCY FIX

Upload these to the ROOT of your GitHub project:

1. vercel.json
2. frontend/package.json
3. frontend/package-lock.json

Why:
Vercel is still trying to run:
cd frontend && npm ci

These tiny frontend files make that command pass even though the website is already built static HTML.

After upload, redeploy on Vercel.
