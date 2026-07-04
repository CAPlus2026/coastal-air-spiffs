"""ServiceTitan API client — OAuth2 client-credentials auth + Reporting API v2 helpers.

Credentials are read from .env (see .env.example). Never hardcode them here.
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

_ENV = os.environ.get("ST_ENVIRONMENT", "production").strip().lower()
_AUTH_HOST = "auth-integration.servicetitan.io" if _ENV == "integration" else "auth.servicetitan.io"
_API_HOST = "api-integration.servicetitan.io" if _ENV == "integration" else "api.servicetitan.io"

TOKEN_URL = f"https://{_AUTH_HOST}/connect/token"
API_BASE = f"https://{_API_HOST}"


class ServiceTitanClient:
    def __init__(self):
        self.client_id = os.environ["ST_CLIENT_ID"]
        self.client_secret = os.environ["ST_CLIENT_SECRET"]
        self.app_key = os.environ["ST_APP_KEY"]
        self.tenant_id = os.environ["ST_TENANT_ID"]
        self._token = None
        self._token_expires_at = 0

    def _get_token(self):
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 900)
        return self._token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "ST-App-Key": self.app_key,
            "Content-Type": "application/json",
        }

    def _request(self, method, path, retries=3, **kwargs):
        url = f"{API_BASE}{path}"
        for attempt in range(retries):
            resp = requests.request(method, url, headers=self._headers(), timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 15))
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else None
        raise RuntimeError(f"Repeated 429s calling {path}")

    # ── Reporting API v2 ──────────────────────────────────────────
    def list_report_categories(self):
        return self._request("GET", f"/reporting/v2/tenant/{self.tenant_id}/report-categories")

    def list_reports(self, category):
        return self._request(
            "GET", f"/reporting/v2/tenant/{self.tenant_id}/report-category/{category}/reports"
        )

    def get_report_data(self, category, report_id, parameters=None, page=1, page_size=500):
        body = {"parameters": parameters or [], "page": page, "pageSize": page_size}
        return self._request(
            "POST",
            f"/reporting/v2/tenant/{self.tenant_id}/report-category/{category}/reports/{report_id}/data",
            json=body,
        )

    def get_report_data_all_pages(self, category, report_id, parameters=None, page_size=500):
        """Fetch every page of a report's data and return the combined field/data structure."""
        page = 1
        first = self.get_report_data(category, report_id, parameters, page=page, page_size=page_size)
        fields = first.get("fields", [])
        rows = list(first.get("data", []))
        has_more = first.get("hasMore", False)
        while has_more:
            page += 1
            nxt = self.get_report_data(category, report_id, parameters, page=page, page_size=page_size)
            rows.extend(nxt.get("data", []))
            has_more = nxt.get("hasMore", False)
        return fields, rows
