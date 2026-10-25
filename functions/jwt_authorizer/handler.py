"""Custom TOKEN authorizer validating Cognito-issued JWTs via RS256
signature verification against the User Pool's public JWKS.

Needed because API Gateway's native COGNITO_USER_POOLS authorizer does
not support client_credentials (M2M) grant access tokens - confirmed
empirically via `aws apigateway test-invoke-authorizer` against a
textbook-valid M2M token (right issuer, right scopes, unexpired,
token_use=access) that it still rejected with a bare 401. See
DEPLOYMENT.md for the investigation. Verified locally against PyJWT
before being wired in here: a real token decodes successfully, and a
tampered one is correctly rejected (signature check fails).
"""
import os

import jwt
from jwt import PyJWKClient

USER_POOL_ID = os.environ["USER_POOL_ID"]
# AWS_REGION is a Lambda-reserved env var, auto-populated by the runtime -
# never set manually in the function's own Environment.Variables block.
ISSUER = f"https://cognito-idp.{os.environ['AWS_REGION']}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

# Cached at cold start and reused across warm invocations in this
# container - PyJWKClient handles its own internal key cache/refresh.
_jwk_client = PyJWKClient(JWKS_URL)


def _policy(principal_id, effect, method_arn, context=None):
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": method_arn}],
        },
        "context": context or {},
    }


def handler(event, context):
    method_arn = event["methodArn"]
    auth_header = event.get("authorizationToken") or ""
    token = auth_header[7:] if auth_header.lower().startswith("bearer ") else auth_header

    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            # Access tokens (both end-user and M2M) carry no `aud` claim -
            # only ID tokens do. Nothing here relies on audience checking.
            options={"verify_aud": False},
        )
        if claims.get("token_use") != "access":
            raise jwt.InvalidTokenError("not an access token")
    except Exception as exc:
        # API Gateway TOKEN authorizers specifically require the literal
        # string "Unauthorized" to produce a 401 - any other message/error
        # type produces a 500 instead. Real reason still goes to logs.
        print(f"JWT rejected: {exc}")
        raise Exception("Unauthorized") from exc

    # Authorizer context values must be flat strings - downstream service
    # Lambdas read these directly off event.requestContext.authorizer
    # (not nested under .claims, unlike the native Cognito authorizer).
    client_id = str(claims.get("client_id") or claims.get("sub") or "unknown")
    out_context = {
        "client_id": client_id,
        "sub": str(claims.get("sub", "")),
        "scope": str(claims.get("scope", "")),
        "iat": str(claims.get("iat", "")),
    }
    return _policy(client_id, "Allow", method_arn, out_context)
