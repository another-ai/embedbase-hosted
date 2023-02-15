import os
from typing import Tuple
import warnings
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from firebase_admin import initialize_app, credentials, firestore, auth
import posthog
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

_IGNORED_PATHS = [
    "openapi.json",
    "redoc",
    "docs",
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
    status_code = (
        exc.status_code
        if hasattr(exc, "status_code")
        else 500
    )
    message = exc.detail if hasattr(exc, "detail") else str(exc)

    warnings.warn(message)
    return JSONResponse(
        status_code=status_code,
        content={"message": message},
    )

async def check_api_key(scope):
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

    token_type, token = s
    assert (
        token_type == "Bearer"
    ), "Authorization header must be `Bearer` type. Like: `Bearer LONG_JWT`"

    assert token, "invalid api key"

    try:
        # check collection "apikeys" for token
        doc = fc.collection("apikeys").document(token).get()
        if not doc.exists:
            raise DetailedError(scope, 401, "invalid api key")
        data = doc.to_dict()
        scope["uid"] = data["userId"]
        print("uid", scope["uid"])
        print("token", token)
    except Exception as err:
        raise DetailedError(scope, 401, str(err))

    try:
        user: auth.UserRecord = auth.get_user(scope["uid"])
    except:
        raise DetailedError(scope, 401, "invalid uid")
    return scope["uid"]

def middleware(app: FastAPI):
    @app.middleware("http")
    async def auth_api_key(request: Request, call_next) -> Tuple[str, str]:
        """
        Only allow calls on search endpoint
        """
        if request.scope["type"] != "http":  # pragma: no cover
            return await call_next(request)

        # in development mode, allow redoc, openapi etc
        if ENVIRONMENT == "development" and any(
            path in request.scope["path"] for path in _IGNORED_PATHS
        ):
            return await call_next(request)

        try:
            user = await check_api_key(request.scope)
        except Exception as exc:
            return await on_auth_error(exc, request.scope)

        response = await call_next(request)
        return response