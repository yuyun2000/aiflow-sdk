from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .storage import Storage


AUTH_SCHEME = "AIFLOW-HMAC-SHA256-V1"
KEY_ID_HEADER = "X-AIFlow-Client-Key"
TIMESTAMP_HEADER = "X-AIFlow-Timestamp"
NONCE_HEADER = "X-AIFlow-Nonce"
CONTENT_HASH_HEADER = "X-AIFlow-Content-SHA256"
SIGNATURE_HEADER = "X-AIFlow-Signature"
RESPONSE_TIMESTAMP_HEADER = "X-AIFlow-Response-Timestamp"
RESPONSE_SIGNATURE_HEADER = "X-AIFlow-Response-Signature"
AUTH_VERSION_HEADER = "X-AIFlow-Auth-Version"


class ClientAuthError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ClientPrincipal:
    key_id: str
    request_nonce: str
    secret: bytes


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_target(request: Request) -> str:
    raw_path = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    query = request.scope.get("query_string") or b""
    return raw_path.decode("latin-1") + (("?" + query.decode("latin-1")) if query else "")


def canonical_request(method: str, target: str, timestamp: str, nonce: str, content_hash: str) -> bytes:
    return "\n".join((AUTH_SCHEME, method.upper(), target, timestamp, nonce, content_hash)).encode("utf-8")


def canonical_response(request_nonce: str, status_code: int, timestamp: str) -> bytes:
    return "\n".join((AUTH_SCHEME + "-RESPONSE", request_nonce, str(status_code), timestamp)).encode("utf-8")


def sign_bytes(secret: bytes, payload: bytes) -> str:
    digest = hmac.new(secret, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sign_request(secret: bytes, method: str, target: str, timestamp: str, nonce: str, content_hash: str) -> str:
    return sign_bytes(secret, canonical_request(method, target, timestamp, nonce, content_hash))


def sign_response(secret: bytes, request_nonce: str, status_code: int, timestamp: str) -> str:
    return sign_bytes(secret, canonical_response(request_nonce, status_code, timestamp))


class ClientAuthenticator:
    PUBLIC_API_PATHS = {
        "/api/v3/capabilities",
        "/api/v3/system/status",
    }

    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.keys = dict(settings.client_auth_keys)

    def requires_authentication(self, request: Request) -> bool:
        if not self.settings.client_auth_enabled or request.method == "OPTIONS":
            return False
        path = request.url.path
        if not path.startswith("/api/v3/") or path in self.PUBLIC_API_PATHS:
            return False
        if request.method == "GET" and re.fullmatch(r"/api/v3/tasks/[^/]+/events", path):
            return False
        return True

    async def authenticate(self, request: Request) -> ClientPrincipal:
        key_id = request.headers.get(KEY_ID_HEADER, "").strip()
        timestamp = request.headers.get(TIMESTAMP_HEADER, "").strip()
        nonce = request.headers.get(NONCE_HEADER, "").strip()
        supplied_hash = request.headers.get(CONTENT_HASH_HEADER, "").strip().lower()
        supplied_signature = request.headers.get(SIGNATURE_HEADER, "").strip()
        if not all((key_id, timestamp, nonce, supplied_hash, supplied_signature)):
            raise ClientAuthError("client_signature_required", "official client signature headers are required")

        secret = self.keys.get(key_id)
        if secret is None:
            raise ClientAuthError("unknown_client_key", "client key is not recognized")
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise ClientAuthError("invalid_client_timestamp", "client timestamp must be Unix seconds") from exc
        now = int(time.time())
        if abs(now - timestamp_value) > self.settings.client_auth_clock_skew_seconds:
            raise ClientAuthError("client_timestamp_expired", "client timestamp is outside the allowed clock window")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
            raise ClientAuthError("invalid_client_nonce", "client nonce format is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
            raise ClientAuthError("invalid_content_hash", "request content hash must be SHA-256 hex")

        body = await request.body()
        if not hmac.compare_digest(body_hash(body), supplied_hash):
            raise ClientAuthError("content_hash_mismatch", "request body does not match its signed hash")
        expected = sign_request(secret, request.method, request_target(request), timestamp, nonce, supplied_hash)
        if not hmac.compare_digest(expected, supplied_signature):
            raise ClientAuthError("invalid_client_signature", "client signature is invalid")

        claim = self.storage.claim_client_request(
            key_id,
            nonce,
            expires_at=now + self.settings.client_auth_nonce_ttl_seconds,
            now=now,
            requests_per_minute=self.settings.client_auth_requests_per_minute,
        )
        if claim == "replay":
            raise ClientAuthError("client_request_replayed", "client nonce has already been used", 409)
        if claim == "rate_limited":
            raise ClientAuthError("client_rate_limited", "client request rate limit exceeded", 429)
        return ClientPrincipal(key_id=key_id, request_nonce=nonce, secret=secret)

    @staticmethod
    def response_headers(principal: ClientPrincipal, status_code: int) -> dict[str, str]:
        timestamp = str(int(time.time()))
        return {
            AUTH_VERSION_HEADER: "1",
            RESPONSE_TIMESTAMP_HEADER: timestamp,
            RESPONSE_SIGNATURE_HEADER: sign_response(
                principal.secret,
                principal.request_nonce,
                status_code,
                timestamp,
            ),
        }
