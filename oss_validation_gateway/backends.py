"""HTTP clients for Prometheus, Loki, Alertmanager, and Kubernetes.

Transport is injectable so unit tests never open a network socket.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        ...


class UrlLibTransport:
    """stdlib HTTP transport. TLS verify can be disabled for demo kube APIs."""

    def __init__(self, tls_verify: bool = True, ca_file: str | None = None) -> None:
        self._tls_verify = tls_verify
        self._ca_file = ca_file

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self._tls_verify:
            ctx = ssl._create_unverified_context()
            return ctx
        if self._ca_file:
            return ssl.create_default_context(cafile=self._ca_file)
        return ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(filtered)
        data = None
        req_headers = dict(headers or {})
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            else:
                data = json.dumps(body).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
        context = self._ssl_context() if url.startswith("https://") else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise ConnectionError(f"HTTP {exc.code} for {url}: {payload[:300]}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"unreachable {url}: {exc.reason}") from exc
        if not raw:
            return {}
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


@dataclass
class Settings:
    prometheus_url: str = ""
    loki_url: str = ""
    alertmanager_url: str = ""
    kubernetes_api_url: str = ""
    kubernetes_token: str = ""
    kubernetes_ca_file: str = ""
    kubernetes_namespace: str = "default"
    kube_mutations: bool = False
    kube_tls_verify: bool = True
    timeout_seconds: float = 5.0
    gateway_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        def _url(name: str, default: str) -> str:
            return os.environ.get(name, default).strip().rstrip("/")

        def _bool(name: str, default: str = "false") -> bool:
            return os.environ.get(name, default).strip().lower() in {"1", "true", "yes"}

        kube_url = os.environ.get("KUBERNETES_API_URL", "").strip()
        if not kube_url:
            host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
            if host:
                kube_url = f"https://{host}:{port}"
        token = os.environ.get("KUBERNETES_TOKEN", "").strip()
        ca_file = os.environ.get("KUBERNETES_CA_FILE", "").strip()
        sa_token = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        sa_ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        if not token and os.path.isfile(sa_token):
            with open(sa_token, encoding="utf-8") as fh:
                token = fh.read().strip()
        if not ca_file and os.path.isfile(sa_ca):
            ca_file = sa_ca
        return cls(
            prometheus_url=_url("PROMETHEUS_URL", "http://localhost:9090"),
            loki_url=_url("LOKI_URL", "http://localhost:3100"),
            alertmanager_url=_url("ALERTMANAGER_URL", "http://localhost:9093"),
            kubernetes_api_url=kube_url.rstrip("/"),
            kubernetes_token=token,
            kubernetes_ca_file=ca_file,
            kubernetes_namespace=os.environ.get("KUBERNETES_NAMESPACE", "default").strip() or "default",
            kube_mutations=_bool("OSS_KUBE_MUTATIONS", "false"),
            kube_tls_verify=_bool("OSS_KUBE_TLS_VERIFY", "true"),
            timeout_seconds=float(os.environ.get("OSS_BACKEND_TIMEOUT_SECONDS", "5")),
            gateway_token=os.environ.get("OSS_GATEWAY_TOKEN", "").strip()
            or os.environ.get("GATEWAY_ACCESS_TOKEN", "").strip(),
        )


@dataclass
class Backends:
    settings: Settings
    transport: Transport = field(default_factory=UrlLibTransport)

    def _get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        return self.transport.request(
            "GET", url, params=params, headers=headers, timeout=self.settings.timeout_seconds,
        )

    def _post(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.transport.request(
            "POST", url, body=body, headers=headers, timeout=self.settings.timeout_seconds,
        )

    # -- Alertmanager --------------------------------------------------- #

    def alertmanager_alerts(self) -> list[dict[str, Any]]:
        url = f"{self.settings.alertmanager_url}/api/v2/alerts"
        data = self._get(url)
        if isinstance(data, list):
            return [a for a in data if isinstance(a, dict)]
        if isinstance(data, dict):
            alerts = data.get("alerts") or data.get("data") or []
            if isinstance(alerts, list):
                return [a for a in alerts if isinstance(a, dict)]
        return []

    # -- Loki ----------------------------------------------------------- #

    def loki_query_range(self, logql: str, start_ns: str | None = None, end_ns: str | None = None, limit: int = 50) -> dict[str, Any]:
        url = f"{self.settings.loki_url}/loki/api/v1/query_range"
        params: dict[str, Any] = {"query": logql, "limit": str(limit), "direction": "backward"}
        if start_ns:
            params["start"] = start_ns
        if end_ns:
            params["end"] = end_ns
        data = self._get(url, params=params)
        return data if isinstance(data, dict) else {}

    # -- Prometheus ----------------------------------------------------- #

    def prometheus_query(self, promql: str) -> dict[str, Any]:
        url = f"{self.settings.prometheus_url}/api/v1/query"
        data = self._get(url, params={"query": promql})
        return data if isinstance(data, dict) else {}

    def prometheus_query_range(self, promql: str, start: str, end: str, step: str = "30s") -> dict[str, Any]:
        url = f"{self.settings.prometheus_url}/api/v1/query_range"
        data = self._get(url, params={"query": promql, "start": start, "end": end, "step": step})
        return data if isinstance(data, dict) else {}

    # -- Kubernetes (read-only unless OSS_KUBE_MUTATIONS=true) ---------- #

    def kubernetes_configured(self) -> bool:
        return bool(self.settings.kubernetes_api_url and self.settings.kubernetes_token)

    def _kube_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.kubernetes_token}",
            "Accept": "application/json",
        }

    def kubernetes_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.kubernetes_configured():
            raise ConnectionError("kubernetes_not_configured")
        url = f"{self.settings.kubernetes_api_url}{path}"
        return self._get(url, params=params, headers=self._kube_headers())
