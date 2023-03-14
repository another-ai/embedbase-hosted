import os
import yaml
from typing import Tuple
import warnings
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from firebase_admin import initialize_app, credentials, firestore, auth
import posthog
from starlette.middleware.base import BaseHTTPMiddleware
from supabase import create_client, Client


ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
SECRET_PATH = "/secrets" if os.path.exists("/secrets") else ".."
# if can't find config.yaml in .. try . now (local dev)
if not os.path.exists(SECRET_PATH + "/config.yaml"):
    SECRET_PATH = "."

if not os.path.exists(SECRET_PATH + "/config.yaml"):
    # exit process with error
    print("ERROR: Missing config.yaml file")

data = yaml.safe_load(open(f"{SECRET_PATH}/config.yaml"))
SUPABASE_URL = data["supabase_url"]
SUPABASE_KEY = data["supabase_key"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DEVELOPMENT_IGNORED_PATHS = [
    "openapi.json",
    "redoc",
    "docs",
]

PRODUCTION_IGNORED_PATHS = [
    "health",
]

SECRET_FIREBASE_PATH = (
    "/secrets_firebase" if os.path.exists("/secrets_firebase") else ".."
)

posthog.project_api_key = "phc_plfzAimxHysKLaS80RK3NPaL0OJhlg983m3o5Zuukp"
posthog.host = "https://app.posthog.com"
posthog.debug = ENVIRONMENT == "development"

if not os.path.exists(SECRET_FIREBASE_PATH + "/svc.prod.json"):
    SECRET_FIREBASE_PATH = "."
cred = credentials.Certificate(SECRET_FIREBASE_PATH + "/svc.prod.json")
initialize_app(cred)
fc = firestore.client()


class DetailedError(Exception):
    def __init__(self, scope: dict, status_code: int, detail: str) -> None:
        self.scope = scope
        self.status_code = status_code
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


async def on_auth_error(exc: Exception, scope: dict):
    status_code = exc.status_code if hasattr(exc, "status_code") else 500
    message = exc.detail if hasattr(exc, "detail") else str(exc)

    warnings.warn(message)
    return JSONResponse(
        status_code=status_code,
        content={"message": message},
    )


def get_in_firebase(api_key: str, scope: dict):
    doc = fc.collection("apikeys").document(api_key).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if "userId" not in data:
        return None
    return data["userId"]


def get_in_supabase(api_key: str, scope: dict):
    res = supabase.from_("api-keys").select("*").eq("api_key", api_key).execute()
    print("res", res)
    if not res.data or "user_id" not in res.data[0]:
        return None
    return res.data[0]["user_id"]


async def check_api_key(scope: dict) -> Tuple[str, str]:
    # extract token from header
    for name, value in scope["headers"]:  # type: bytes, bytes
        if name == b"authorization":
            authorization = value.decode("utf8")
            break
    else:
        authorization = None

    if not authorization:
        raise DetailedError(scope, 401, "missing authorization header")

    s = authorization.split(" ")

    if len(s) != 2:
        raise DetailedError(scope, 401, "invalid authorization header")

    token_type, api_key = s
    assert (
        token_type == "Bearer"
    ), "Authorization header must be `Bearer` type. Like: `Bearer LONG_JWT`"

    assert api_key, "invalid api key"

    try:
        uid = get_in_firebase(api_key, scope) or get_in_supabase(api_key, scope)
        if not uid:
            raise DetailedError(scope, 401, "invalid api key")
        scope["uid"] = uid
    except Exception as err:
        raise DetailedError(scope, 401, str(err))

    # try:
    #     user: auth.UserRecord = auth.get_user(scope["uid"])
    # except:
    #     raise DetailedError(scope, 401, "invalid uid")

    return scope["uid"], api_key


class AuthApiKey(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Tuple[str, str]:
        """ """
        if request.scope["type"] != "http":  # pragma: no cover
            return await call_next(request)

        if any(path in request.scope["path"] for path in PRODUCTION_IGNORED_PATHS):
            return await call_next(request)
        # in development mode, allow redoc, openapi etc
        if ENVIRONMENT == "development" and any(
            path in request.scope["path"] for path in DEVELOPMENT_IGNORED_PATHS
        ):
            return await call_next(request)

        path_segments = request.scope["path"].split("/")
        try:
            user_id, api_key = await check_api_key(request.scope)
            request.scope["uid"] = user_id
            posthog.identify(user_id)
            event = None
            # POST /v1/{vault_id}/search
            if "search" == path_segments[-1]:
                event = "search"
            # POST /v1/{vault_id}
            elif request.scope["method"] == "POST":
                event = "add"
            # otherwise we don't track
            if event:
                posthog.capture(
                    user_id,
                    event=event,
                    properties={
                        "api_key": api_key,
                    },
                )
        except Exception as exc:
            return await on_auth_error(exc, request.scope)
        response = await call_next(request)
        return response
