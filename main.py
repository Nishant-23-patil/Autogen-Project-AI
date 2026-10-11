import logging
import os
import html
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlencode

from dotenv import load_dotenv
import msal
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from pymongo import DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()
logger = logging.getLogger(__name__)
session_secret = os.getenv("SESSION_SECRET")
if not session_secret:
    raise RuntimeError("SESSION_SECRET must be configured in the environment.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    mongo_uri = os.getenv("MONGODB_URI")
    if not mongo_uri:
        raise RuntimeError("MONGODB_URI must be configured in the environment.")

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
    try:
        client.admin.command("ping")
        collection = client[
            os.getenv("MONGODB_DATABASE", "weather_db")
        ][os.getenv("MONGODB_COLLECTION", "weather_readings")]
        collection.create_index([("city", 1), ("observed_at", DESCENDING)])
        app.state.weather_collection = collection
        yield
    except PyMongoError:
        logger.exception("MongoDB operation failed during application startup.")
        raise
    finally:
        client.close()


app = FastAPI(title="Weather Records", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
)


class WeatherReadingInput(BaseModel):
    city: str = Field(min_length=1, max_length=100)
    temperature_c: float = Field(ge=-100, le=100)
    condition: str = Field(min_length=1, max_length=100)


def get_collection(request: Request) -> Collection:
    return request.app.state.weather_collection


def get_msal_app() -> msal.ConfidentialClientApplication:
    tenant_id = os.getenv("ENTRA_TENANT_ID")
    client_id = os.getenv("ENTRA_CLIENT_ID")
    client_secret = os.getenv("ENTRA_CLIENT_SECRET")
    if not tenant_id or not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail="Microsoft Entra sign-in is not fully configured.",
        )

    return msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )


def get_redirect_uri() -> str:
    redirect_uri = os.getenv("ENTRA_REDIRECT_URI")
    if not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="ENTRA_REDIRECT_URI is not configured.",
        )
    return redirect_uri


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Sign-in required.")
    return str(user)


@app.get("/healthz")
def health_check(request: Request):
    try:
        get_collection(request).database.client.admin.command("ping")
    except PyMongoError as exc:
        logger.exception("MongoDB health check failed.")
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = request.session.get("user")
    account_links = (
        f"<p>Signed in as {html.escape(str(user))} · "
        '<a href="/auth/logout">Sign out</a></p>'
        if user
        else '<p><a href="/auth/login">Sign in with Microsoft</a> to use this app.</p>'
    )
    page = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Weather Records</title>
  <style>
    body { font: 16px system-ui, sans-serif; margin: 2rem auto; max-width: 44rem; padding: 0 1rem; }
    form { display: grid; gap: .75rem; max-width: 25rem; }
    label { display: grid; gap: .25rem; }
    input, button { font: inherit; padding: .55rem; }
    li { margin: .6rem 0; }
    #message { min-height: 1.5em; }
  </style>
</head>
<body>
  <h1>Weather Records</h1>
  __ACCOUNT_LINKS__
  <form id="weather-form">
    <label>City <input name="city" maxlength="100" required></label>
    <label>Temperature (°C) <input name="temperature_c" type="number" min="-100" max="100" step="any" required></label>
    <label>Condition <input name="condition" maxlength="100" required></label>
    <button type="submit">Save reading</button>
  </form>
  <p id="message" role="status"></p>
  <h2>Recent readings</h2>
  <ul id="readings"></ul>
  <script>
    const form = document.querySelector("#weather-form");
    const message = document.querySelector("#message");
    const readings = document.querySelector("#readings");

    async function refreshReadings() {
      const response = await fetch("/api/weather");
      if (response.status === 401) return;
      if (!response.ok) throw new Error("Could not load weather readings.");
      const items = await response.json();
      readings.replaceChildren();
      for (const item of items) {
        const row = document.createElement("li");
        row.textContent = `${item.city}: ${item.temperature_c} °C, ${item.condition} (${new Date(item.observed_at).toLocaleString()})`;
        readings.append(row);
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      message.textContent = "";
      if (!__SIGNED_IN__) {
        message.textContent = "Sign in with Microsoft before saving a reading.";
        return;
      }
      const data = new FormData(form);
      const response = await fetch("/api/weather", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          city: data.get("city"),
          temperature_c: Number(data.get("temperature_c")),
          condition: data.get("condition")
        })
      });
      if (!response.ok) {
        message.textContent = "Could not save the reading. Check the values and try again.";
        return;
      }
      form.reset();
      message.textContent = "Reading saved.";
      await refreshReadings();
    });

    refreshReadings().catch((error) => { message.textContent = error.message; });
  </script>
</body>
</html>
"""
    page = page.replace("__ACCOUNT_LINKS__", account_links)
    page = page.replace("__SIGNED_IN__", str(bool(user)).lower())
    return HTMLResponse(page)


@app.get("/auth/login")
def auth_login(request: Request):
    client = get_msal_app()
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    authorization_url = client.get_authorization_request_url(
        scopes=["openid", "profile", "email"],
        state=state,
        redirect_uri=get_redirect_uri(),
    )
    return RedirectResponse(authorization_url)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None):
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=400, detail="Invalid sign-in state.")
    if not code:
        raise HTTPException(status_code=401, detail="Microsoft sign-in was not completed.")

    result = get_msal_app().acquire_token_by_authorization_code(
        code,
        scopes=["openid", "profile", "email"],
        redirect_uri=get_redirect_uri(),
    )
    claims = result.get("id_token_claims")
    if not claims:
        logger.warning("Microsoft sign-in failed with error code %s.", result.get("error"))
        raise HTTPException(status_code=401, detail="Microsoft sign-in failed.")

    request.session.clear()
    request.session["user"] = (
        claims.get("preferred_username") or claims.get("name") or "Signed-in user"
    )
    return RedirectResponse("/", status_code=303)


@app.get("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    redirect_uri = get_redirect_uri().removesuffix("/auth/callback")
    logout_url = (
        f"https://login.microsoftonline.com/{os.getenv('ENTRA_TENANT_ID')}"
        f"/oauth2/v2.0/logout?{urlencode({'post_logout_redirect_uri': redirect_uri})}"
    )
    return RedirectResponse(logout_url)


@app.get("/api/weather")
def list_weather(
    request: Request,
    _: Annotated[str, Depends(require_login)],
    city: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"city": city.strip()} if city and city.strip() else {}
    try:
        return list(
            get_collection(request)
            .find(query, {"_id": 0})
            .sort("observed_at", DESCENDING)
            .limit(limit)
        )
    except PyMongoError as exc:
        logger.exception("Failed to retrieve weather readings.")
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc


@app.post("/api/weather", status_code=201)
def save_weather(
    reading: WeatherReadingInput,
    request: Request,
    _: Annotated[str, Depends(require_login)],
):
    city = reading.city.strip()
    condition = reading.condition.strip()
    if not city or not condition:
        raise HTTPException(status_code=422, detail="City and condition are required.")

    document = {
        "city": city,
        "temperature_c": reading.temperature_c,
        "condition": condition,
        "observed_at": datetime.now(timezone.utc),
    }
    try:
        result = get_collection(request).insert_one(document)
    except PyMongoError as exc:
        logger.exception("Failed to save a weather reading.")
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc

    return {
        "id": str(result.inserted_id),
        "city": document["city"],
        "temperature_c": document["temperature_c"],
        "condition": document["condition"],
        "observed_at": document["observed_at"],
    }
