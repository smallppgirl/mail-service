from functools import lru_cache
from urllib.parse import quote, urlsplit
import httpx
import msal
from mail_service.config import settings

BASE = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    def __init__(self, status, code="graph_error", retry_after=None, request_id=None):
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.request_id = request_id
        super().__init__(f"Graph HTTP {status}: {code}")

    def safe(self):
        return {"status": self.status, "code": self.code, "request_id": self.request_id}


@lru_cache
def auth_client():
    s = settings()
    if not all((s.graph_tenant_id, s.graph_client_id, s.graph_client_secret)):
        raise RuntimeError("Graph credentials are not configured")
    return msal.ConfidentialClientApplication(
        s.graph_client_id,
        authority=f"https://login.microsoftonline.com/{s.graph_tenant_id}",
        client_credential=s.graph_client_secret,
    )


def token():
    result = auth_client().acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise GraphError(401, result.get("error", "token_error"))
    return result["access_token"]


def segment(value):
    return quote(value, safe="")


def check_response(response):
    if response.status_code < 400:
        return
    try:
        code = response.json().get("error", {}).get("code", "graph_error")
    except (ValueError, AttributeError):
        code = "graph_error"
    retry = response.headers.get("Retry-After", "")
    raise GraphError(
        response.status_code,
        code,
        int(retry) if retry.isdigit() else None,
        response.headers.get("request-id"),
    )


class Graph:
    def __init__(self, user_id, guard=lambda: None, client=None, token_provider=token):
        self.root = f"/users/{segment(user_id)}"
        self.guard = guard
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
        self.token_provider = token_provider
        self.last_response = None

    def close(self):
        self.client.close()

    def url(self, path):
        if path.startswith("https://"):
            parsed = urlsplit(path)
            if parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/users/"):
                raise ValueError("Untrusted Graph continuation URL")
            return path
        if not path.startswith("/"):
            raise ValueError("Graph path must be absolute")
        return BASE + path

    def request(self, method, path, **kwargs):
        self.guard()
        headers = {
            "Authorization": f"Bearer {self.token_provider()}",
            "Prefer": 'IdType="ImmutableId", odata.maxpagesize=100',
        }
        headers.update(kwargs.pop("headers", {}))
        # No implicit HTTP retries: POST ambiguity is handled in durable workflow state.
        response = self.client.request(method, self.url(path), headers=headers, **kwargs)
        self.last_response = response
        check_response(response)
        return response.json() if response.content else {}

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def pages(self, path, **kwargs):
        while path:
            result = self.get(path, **kwargs)
            yield result
            path = result.get("@odata.nextLink")
            kwargs = {}

    def download(self, path, output, max_bytes):
        self.guard()
        size = 0
        with self.client.stream(
            "GET",
            self.url(path),
            headers={
                "Authorization": f"Bearer {self.token_provider()}",
                "Prefer": 'IdType="ImmutableId"',
            },
        ) as response:
            if response.status_code >= 400:
                response.read()
                check_response(response)
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Attachment exceeds configured size limit")
                output.write(chunk)
        output.seek(0)
        return size

    def upload_request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"outlook.office.com", "outlook.office365.com"}:
            raise ValueError("Untrusted attachment upload URL")
        self.guard()
        # Upload URLs are preauthenticated; do not attach the Graph bearer token.
        response = self.client.request(method, url, **kwargs)
        self.last_response = response
        check_response(response)
        return response
