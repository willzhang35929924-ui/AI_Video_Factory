import os

import httpx


class HubStudioAdapter:
    """Only the documented read-only local API; browser steps require an observed adapter."""

    def __init__(self, config):
        self.config = config

    async def preflight(self, payload):
        if os.environ.get(self.config.hubstudio_api_key_env):
            return {"status": "WAITING_MANUAL", "checks": [{"name": "hubstudio_api_auth", "status": "NOT_SUPPORTED", "reason": "DOCUMENTED_LOCAL_API_KEY_TRANSPORT_REQUIRED"}],
                "evidence": {"adapter": "HubStudio", "api_key_configured": True, "performed": False}}
        try:
            async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds, trust_env=False) as client:
                response = await client.post(self.config.hubstudio_api_url + "/api/v1/browser/all-browser-status", json={})
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError):
            return {"status": "WAITING_MANUAL", "checks": [{"name": "hubstudio_api", "status": "WAITING_MANUAL", "reason": "LOCAL_API_UNAVAILABLE"}],
                "evidence": {"adapter": "HubStudio", "manual_cdp_available": bool(payload["account"].get("cdp_endpoint"))}}
        code = result.get("code")
        return {"status": "WAITING_MANUAL", "checks": [
            {"name": "hubstudio_api", "status": "WAITING_MANUAL", "reason": "API_PLAN_UNAVAILABLE" if code == 26 else "EXISTING_ENVIRONMENT_AND_IDENTITY_REQUIRED", "code": code},
            {"name": "tiktok_identity", "status": "WAITING_MANUAL", "reason": "OBSERVED_LOGIN_REQUIRED"},
            {"name": "vpn", "status": "WAITING_MANUAL", "reason": "NETWORK_VERIFICATION_REQUIRED"}],
            "evidence": {"adapter": "HubStudio", "api_code": code, "manual_cdp_available": bool(payload["account"].get("cdp_endpoint")),
                "environment_dispatch": "DISABLED", "public_submission": "DISABLED"}}

    async def execute_step(self, name, payload):
        return {"status": "WAITING_MANUAL", "error_code": "PUBLIC_SUBMISSION_NOT_AUTHORIZED" if name == "SUBMIT" else "OBSERVED_BROWSER_ADAPTER_REQUIRED",
            "performed": False, "checkpoint": {}, "evidence": {"adapter": "HubStudio", "step": name, "performed": False}}

    async def reconcile_upload(self, payload):
        return {"status": "WAITING_MANUAL", "performed": False, "error_code": "READ_ONLY_BROWSER_OBSERVATION_REQUIRED",
            "evidence": {"adapter": "HubStudio", "read_only": True}}

    async def observe_product(self, payload):
        return {"status": "WAITING_MANUAL", "performed": False, "error_code": "READ_ONLY_PRODUCT_OBSERVER_REQUIRED",
            "evidence": {"adapter": "HubStudio", "read_only": True}}

    async def reconcile_product(self, payload):
        return {"status": "WAITING_MANUAL", "performed": False, "error_code": "READ_ONLY_PRODUCT_OBSERVER_REQUIRED",
            "evidence": {"adapter": "HubStudio", "read_only": True}}
