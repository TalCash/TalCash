"""The wallet's connection to a node's API."""

import httpx

from ..core.tx import Transfer


class NodeError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class NodeClient:
    def __init__(self, url: str, timeout: float = 15.0) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(base_url=self.url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError:
            raise NodeError("unreachable", f"no node at {self.url} (is `tc node` running?)") from None
        if response.status_code >= 400:
            try:
                data = response.json()
            except ValueError:
                raise NodeError("http-error", f"status {response.status_code}") from None
            raise NodeError(data.get("error", "error"), data.get("detail", ""))
        return response.json()

    def status(self) -> dict:
        return self._request("GET", "/v1/status")

    def address(self, address: str) -> dict:
        return self._request("GET", f"/v1/address/{address}")

    def history(self, address: str, limit: int = 50) -> list[dict]:
        return self._request("GET", f"/v1/address/{address}/history", params={"limit": limit})

    def transaction(self, txid: str) -> dict | None:
        try:
            return self._request("GET", f"/v1/tx/{txid}")
        except NodeError as error:
            if error.code == "unknown-transaction":
                return None
            raise

    def submit(self, tx: Transfer) -> dict:
        return self._request("POST", "/v1/tx", json={"hex": tx.serialize().hex()})
