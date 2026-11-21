"""A local DevDiary pairing server for tests, speaking pairing contract v2.

It applies the server's rules for the start, the approval and its 127.0.0.1
redirect, the status poll and the verifier exchange. A test plays the person:
it approves or denies, and follows the redirect as a browser would.
"""

import json
import re
import secrets
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field

START = "/ingest/v1/collector_pairings"
EXCHANGE = START + "/exchange"
ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
CODE = re.compile(rf"\A[{ALPHABET}]{{8}}\Z")
HARNESSES = ("claude-code", "codex")
UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
REPOSITORY = re.compile(r"\A[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}\Z")
TEXT = {
    "hostname": 253,
    "os_name": 64,
    "os_version": 64,
    "arch": 32,
    "cli_version": 32,
}
REQUIRED = {"schema_version", "installation_id", "harnesses", "scope_kind", *TEXT}
OPTIONAL = {"repository", "callback_port"}
COLLECTOR_REF = "urn:devdiary:collector:11111111-1111-4111-8111-111111111111"
LIFETIME = 600
WRONG_VERIFIERS = 5


class Invalid(Exception):
    pass


def normalise(code):
    return re.sub(r"[\s-]", "", code).upper()


def display(code):
    return f"{code[:4]}-{code[4:]}"


def unique_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Invalid
    return dict(pairs)


def depth(value):
    if isinstance(value, dict):
        return 1 + max(map(depth, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(depth, value), default=0)
    return 0


def text(value, limit):
    return (
        isinstance(value, str)
        and 1 <= len(value.encode()) <= limit
        and not any(unicodedata.category(c) in ("Cc", "Cf") for c in value)
    )


def port(value):
    if value is None:
        return True
    return type(value) is int and 1024 <= value <= 65535


@dataclass
class Request:
    body: dict
    token: str
    code: str
    expires: float
    status: str = "pending"
    verifier: str | None = None
    failures: int = 0
    last_poll: float | None = None
    connections: list = field(default_factory=list)

    @property
    def port(self):
        return self.body.get("callback_port")

    @property
    def approval_path(self):
        return f"/collector/approve?code={display(self.code)}"


class Contract:
    """Server state for every request a test starts. Mutate the knobs to vary it."""

    def __init__(self):
        self.requests = []
        self.calls = []
        self.start_limit = 10
        self.start_times = []
        self.workspace_name = "acme-diary"
        self.collector_ref = COLLECTOR_REF
        self.repository_refs = ["https://github.com/acme/demo"]
        self.observation_schema_versions = [1, 2]
        # Keys to replace in each issued connection; None removes the key.
        self.connection_shape = {}
        # Runs as the person, right after a start succeeds: request -> None.
        self.on_start = approve_in_browser
        # Runs before the server answers an exchange call: (request, body) -> None.
        self.on_exchange = None
        # Replaces the server's answer: (path, body) -> None or "close" or
        # (status, headers, raw body).
        self.intercept = None
        self.pages = []

    @property
    def latest(self):
        return self.requests[-1]

    def starts(self):
        return [body for path, body, _, _ in self.calls if path == START]

    def exchanges(self):
        return [body for path, body, _, _ in self.calls if path == EXCHANGE]

    def start(self, raw, content_type):
        if content_type != "application/json" or len(raw) > 1024:
            return 422, {}, {"error": "invalid_pairing"}
        try:
            body = json.loads(raw, object_pairs_hook=unique_keys)
        except (Invalid, ValueError):
            return 422, {}, {"error": "invalid_pairing"}
        if not isinstance(body, dict):
            return 422, {}, {"error": "invalid_pairing"}
        if (
            body.get("schema_version") not in (1, 2)
            or type(body.get("schema_version")) is not int
        ):
            return 422, {}, {"error": "unsupported_schema_version", "supported": [1, 2]}
        if body["schema_version"] != 2 or not self.valid(body):
            return 422, {}, {"error": "invalid_pairing"}
        now = time.time()
        self.start_times = [t for t in self.start_times if now - t < 600]
        if len(self.start_times) >= self.start_limit:
            return 429, {"Retry-After": "600"}, {"error": "rate_limited"}
        self.start_times.append(now)
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        request = Request(
            body=body,
            token="dp_pair_" + secrets.token_urlsafe(32),
            code=code,
            expires=now + LIFETIME,
        )
        self.requests.append(request)
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(request.expires))
        return (
            201,
            {"Cache-Control": "no-store"},
            {
                "schema_version": 2,
                "pairing_token": request.token,
                "approval_code": display(code),
                "approval_path": request.approval_path,
                "expires_at": expires,
                "interval": 5,
            },
        )

    def valid(self, body):
        keys = set(body)
        harnesses = body.get("harnesses")
        installation = body.get("scope_kind") == "installation"
        return (
            REQUIRED <= keys
            and keys <= REQUIRED | OPTIONAL
            and depth(body) <= 2
            and isinstance(body["installation_id"], str)
            and UUID.match(body["installation_id"]) is not None
            and isinstance(harnesses, list)
            and 1 <= len(harnesses) <= len(HARNESSES)
            and len(set(map(str, harnesses))) == len(harnesses)
            and all(h in HARNESSES for h in harnesses)
            and body["scope_kind"] in ("installation", "repository")
            and ("repository" in body) != installation
            and (
                installation
                or (
                    isinstance(body["repository"], str)
                    and REPOSITORY.match(body["repository"]) is not None
                )
            )
            and all(text(body[key], limit) for key, limit in TEXT.items())
            and port(body.get("callback_port"))
        )

    def approve(self, request=None):
        """The person presses Approve. Returns the 303 Location."""
        request = request or self.latest
        assert request.status == "pending" and time.time() < request.expires
        request.status = "approved"
        request.verifier = "".join(secrets.choice(ALPHABET) for _ in range(8))
        if request.port is None:
            return request.approval_path
        return (
            f"http://127.0.0.1:{request.port}/callback"
            f"?code={request.code}&verifier={request.verifier}"
        )

    def deny(self, request=None):
        request = request or self.latest
        request.status, request.verifier = "denied", None

    def expire(self, request=None):
        (request or self.latest).expires = time.time() - 1

    def exchange(self, authorization, raw):
        request = next(
            (r for r in self.requests if authorization == f"Bearer {r.token}"), None
        )
        if request is None:
            return 401, {}, {"error": "unauthorized"}
        try:
            body = json.loads(raw, object_pairs_hook=unique_keys)
        except (Invalid, ValueError):
            return 422, {}, {"error": "invalid_pairing"}
        poll = body == {"schema_version": 2}
        attempt = (
            isinstance(body, dict)
            and set(body) == {"schema_version", "verifier"}
            and body["schema_version"] == 2
            and isinstance(body["verifier"], str)
        )
        if not (poll or attempt):
            return 422, {}, {"error": "invalid_pairing"}
        if self.on_exchange is not None:
            self.on_exchange(request, body)
        if request.status in ("consumed", "denied"):
            return 410, {}, {"error": request.status}
        if time.time() >= request.expires:
            return 410, {}, {"error": "expired"}
        if poll:
            now = time.monotonic()
            if request.last_poll is not None and now - request.last_poll < 5:
                return 429, {"Retry-After": "5"}, {"error": "rate_limited"}
            request.last_poll = now
            return 200, {}, {"schema_version": 2, "status": request.status}
        verifier = normalise(body["verifier"])
        if not CODE.match(verifier):
            return 422, {}, {"error": "invalid_verifier"}
        if request.verifier is None or not secrets.compare_digest(
            verifier, request.verifier
        ):
            request.failures += 1
            if request.failures < WRONG_VERIFIERS:
                left = WRONG_VERIFIERS - request.failures
                return 403, {}, {"error": "wrong_verifier", "attempts_left": left}
            request.status, request.verifier = "denied", None
            return 410, {}, {"error": "denied"}
        request.connections = [
            self.connection(request, h) for h in request.body["harnesses"]
        ]
        request.status, request.verifier = "consumed", None
        return (
            200,
            {},
            {
                "schema_version": 2,
                "status": "consumed",
                "workspace_name": self.workspace_name,
                "connections": request.connections,
            },
        )

    def connection(self, request, harness):
        made = {
            "collector_ref": self.collector_ref,
            "token": "dc_live_" + secrets.token_urlsafe(32),
            "installation_id": request.body["installation_id"],
            "runtime": harness,
            "endpoint_path": "/ingest/v1/observations",
            "scope_kind": "installation",
            "repository_refs": list(self.repository_refs),
            "scope_path": "/ingest/v1/collector/scope",
            "observation_schema_versions": list(self.observation_schema_versions),
            **self.connection_shape,
        }
        return {k: v for k, v in made.items() if v is not None}

    def handle(self, handler):
        """Answer a pairing call on a BaseHTTPRequestHandler; False for other paths."""
        if handler.path not in (START, EXCHANGE):
            return False
        raw = handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        authorization = handler.headers.get("Authorization")
        self.calls.append((handler.path, body, authorization, time.monotonic()))
        answer = self.intercept(handler.path, body) if self.intercept else None
        if answer == "close":
            handler.close_connection = True
            handler.connection.close()
            return True
        if answer is None:
            if handler.path == START:
                answer = self.start(raw, handler.headers.get("Content-Type"))
            else:
                answer = self.exchange(authorization, raw)
            status, headers, doc = answer
            answer = status, headers, json.dumps(doc).encode()
        status, headers, out = answer
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(out)))
        handler.end_headers()
        handler.wfile.write(out)
        if handler.path == START and status == 201 and self.on_start is not None:
            self.on_start(self, self.latest)
        return True

    def browse(self, location):
        """Follow a redirect as the browser does; record and return (status, page)."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(location, timeout=30) as page:
                result = page.status, page.read().decode()
        except urllib.error.HTTPError as error:
            result = error.code, error.read().decode()
        except OSError as error:
            result = None, str(error)
        self.pages.append(result)
        return result


def approve_in_browser(contract, request):
    """The person approves at once and the browser follows the 303 to the CLI."""
    if request.port is None:
        return
    threading.Thread(
        target=lambda: contract.browse(contract.approve(request)), daemon=True
    ).start()


def nobody(contract, request):
    """Nobody opens the link: the request stays pending."""
