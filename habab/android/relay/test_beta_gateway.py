"""Installation credentials authorize both existing gateway key formats."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from aiohttp.test_utils import TestClient, TestServer

from beta_codes import BetaCodes, create_config
from openai_gateway import Gateway


class BetaGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "beta" / "config.json"
        create_config(self.path)
        self.codes = BetaCodes(self.path)
        self.legacy = "fixture-existing-service-code"
        self.gateway = Gateway(hashlib.sha256(self.legacy.encode()).hexdigest(), beta_codes=self.codes)
        self.client = TestClient(TestServer(self.gateway.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_issued_code_authenticates_and_existing_code_survives_closed_issuance(self):
        response = await self.client.post("/beta/enroll", json={"installation_id": str(uuid.uuid4())})
        self.assertEqual(response.status, 200)
        code = (await response.json())["code"]
        for headers in ({"Authorization": "Bearer " + code}, {"x-goog-api-key": code}):
            response = await self.client.get("/v1/capabilities", headers=headers)
            self.assertEqual(response.status, 200)
        config = json.loads(self.path.read_text())
        config["issue_codes_enabled"] = False
        self.path.write_text(json.dumps(config))
        response = await self.client.post("/beta/enroll", json={"installation_id": str(uuid.uuid4())})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/v1/capabilities", headers={"Authorization": "Bearer " + code})
        self.assertEqual(response.status, 200)

    async def test_legacy_and_tampered_credentials_keep_separate_authorization(self):
        response = await self.client.get("/v1/capabilities", headers={"Authorization": "Bearer " + self.legacy})
        self.assertEqual(response.status, 200)
        response = await self.client.get("/v1/capabilities", headers={"Authorization": "Bearer hb1.invalid"})
        self.assertEqual(response.status, 401)
        response = await self.client.get("/v1/capabilities", headers={
            "Authorization": "Bearer " + self.legacy, "x-goog-api-key": self.legacy})
        self.assertEqual(response.status, 401)
