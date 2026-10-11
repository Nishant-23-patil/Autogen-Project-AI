# Weather Records

A small FastAPI app for saving and viewing weather readings in MongoDB Atlas. Microsoft Entra ID sign-in is required before the weather API can be used.

## Run locally

1. Create a virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`. Set `MONGODB_URI` and `SESSION_SECRET`. Keep `.env` private; it is ignored by Git.
3. For local Entra sign-in, register a web application in Microsoft Entra ID and add `http://localhost:8000/auth/callback` as a redirect URI. Set its tenant ID, client ID, and client secret in `.env`.
4. Start the app:

   ```powershell
   uvicorn main:app --reload
   ```

Open `http://localhost:8000`. The `/healthz` endpoint checks MongoDB connectivity.

## Deploy on Render

The root `render.yaml` is a Render Blueprint for the web service. Connect the repository containing this project to Render and create a Blueprint. Add the values marked `sync: false` in the Render dashboard; Render generates the session signing secret automatically. Do not commit credentials.

For Microsoft Entra sign-in:

1. Register a web app in Microsoft Entra ID.
2. After Render creates the service, set the app registration's Web redirect URI to `https://<your-render-host>/auth/callback`.
3. Set `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, and `ENTRA_REDIRECT_URI` in the Render service environment. Set `ENTRA_REDIRECT_URI` to that exact callback URL.
4. Set `MONGODB_URI` to a newly rotated MongoDB Atlas connection string. Do not reuse a database password that has been shared in chat.
5. Set `SESSION_SECRET` to a cryptographically random value and keep `SESSION_COOKIE_SECURE=true`.

The MongoDB Atlas network access rules must allow the Render service to connect. Prefer a restricted network policy or private networking where available rather than broadly exposing the database.
