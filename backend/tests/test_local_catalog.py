import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import now
from app.main import create_app


class LocalCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-catalog-unit-")
        self.root = Path(self.temporary.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=True,
            database_url="sqlite:///" + str(self.root / "factory.db"), storage_root=self.root / "storage")
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.repository = self.app.state.repository

    def tearDown(self):
        self.client.close()
        self.repository.engine.dispose()
        self.temporary.cleanup()

    def product(self, identifier="unit-local-product"):
        payload = {"id": identifier, "name": "UNIT_TEST_ONLY_PRODUCT", "country": "巴西", "image": "", "url": "",
            "binding_reference": "", "description": "UNIT_TEST_ONLY", "status": "draft", "keywords": ["unit", "unit"]}
        response = self.client.post("/api/products", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["product"]

    def test_product_real_crud_survives_new_repository_without_binding_requirement(self):
        product = self.product()
        self.assertEqual(product["keywords"], ["unit"])
        updated = {**product, "description": "UPDATED_UNIT", "status": "active"}
        self.assertEqual(self.client.put("/api/products/" + product["id"], json=updated).status_code, 200)
        reopened = create_app(self.settings)
        with TestClient(reopened) as other:
            rows = other.get("/api/products").json()
            self.assertEqual(rows[0]["description"], "UPDATED_UNIT")
            self.assertEqual(rows[0]["binding_reference"], "")
        self.assertEqual(self.client.delete("/api/products/" + product["id"]).status_code, 422)
        response = self.client.delete("/api/products/" + product["id"] + "?confirmed=true")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/products").json(), [])

    def test_actual_image_upload_and_validation_no_mock_images(self):
        image = self.root / "unit-reference.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=16x16", "-frames:v", "1", str(image)],
            check=True, capture_output=True, timeout=15)
        uploaded = self.client.post("/api/collector/images", content=image.read_bytes(),
            headers={"Content-Type": "image/png", "X-Filename": image.name})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        product = self.product()
        product["image"] = uploaded.json()["path"]
        self.assertEqual(self.client.put("/api/products/" + product["id"], json=product).status_code, 200)
        self.assertEqual(self.client.get("/api/products/" + product["id"] + "/images/0").status_code, 200)
        self.assertEqual(self.client.get("/api/products/" + product["id"] + "/details").json()["images"], [product["image"]])
        product["image"] = "/mock/product.svg"
        self.assertEqual(self.client.put("/api/products/" + product["id"], json=product).status_code, 422)
        product["image"] = str(image)
        self.assertEqual(self.client.put("/api/products/" + product["id"], json=product).status_code, 422)

    def test_publishing_product_edit_accepts_factory_inactive_and_uploaded_image(self):
        image = self.root / "unit-product-reference.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=16x16",
            "-frames:v", "1", str(image)], check=True, capture_output=True, timeout=15)
        uploaded = self.client.post("/api/collector/images", content=image.read_bytes(),
            headers={"Content-Type": "image/png", "X-Filename": image.name})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        payload = {"name": "UNIT_IMAGE_PRODUCT", "country": "墨西哥", "status": "inactive",
            "image": uploaded.json()["path"], "keywords": ["UNIT_KEYWORD"]}
        created = self.client.post("/api/publishing/products", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        product = created.json()
        self.assertEqual(product["status"], "disabled")
        self.assertEqual(Path(product["image"]).read_bytes(), image.read_bytes())
        self.assertEqual(self.client.get("/api/products/" + product["id"] + "/image").content, image.read_bytes())
        saved = self.client.put("/api/publishing/products/" + product["id"],
            json={**payload, "name": "UNIT_IMAGE_PRODUCT_EDITED", "status": "draft"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "disabled")
        reopened = create_app(self.settings)
        with TestClient(reopened) as other:
            rows = other.get("/api/publishing/products").json()
            current = next(row for row in rows if row["id"] == product["id"])
            self.assertEqual(current["name"], "UNIT_IMAGE_PRODUCT_EDITED")
            self.assertEqual(current["image"], product["image"])
            self.assertEqual(current["status"], "disabled")
        invalid = self.client.put("/api/publishing/products/" + product["id"], json={**payload, "status": "UNKNOWN"})
        self.assertEqual(invalid.status_code, 422)

    def test_product_linked_task_context_and_country_cannot_be_overwritten_or_deleted(self):
        product = self.product()
        task = {**deepcopy(self.repository.seed()["tasks"][0]), "id": "unit-product-task", "project_id": "proj-local",
            "country": product["country"], "product_id": product["id"], "execution_mode": "integration", "source_video_id": None,
            "created_at": now(), "updated_at": now(), "collector_result": {},
            "context": {"task_context": {"country": product["country"], "product_images": [], "context_id": "unit-context"}}}
        self.repository.mutate(lambda state: state["tasks"].append(task))
        response = self.client.delete("/api/products/" + product["id"] + "?confirmed=true")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "PRODUCT_REFERENCED")
        self.assertEqual(self.client.put("/api/products/" + product["id"], json={**product, "country": "法国"}).status_code, 409)
        self.assertEqual(self.client.put("/api/products/" + product["id"], json={**product, "description": "EDIT_WITHOUT_CONTEXT_CHANGE"}).status_code, 200)
        self.assertEqual(self.repository.state()["tasks"][0]["context"], task["context"])
        self.assertEqual(self.client.get("/api/products/" + product["id"] + "/details").json()["contexts"], [task["context"]["task_context"]])

    def test_account_real_crud_local_status_and_never_fake_connected(self):
        product = self.product()
        payload = {"name": "UNIT_LOCAL_ACCOUNT", "country": "巴西", "status": "NOT_CONNECTED", "product_ids": [product["id"]]}
        response = self.client.post("/api/accounts", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        account = response.json()["account"]
        self.assertFalse(response.json()["authorization_verified"])
        self.assertEqual(self.client.get("/api/accounts/" + account["id"]).json()["authorization"]["mode"], "REAL_LOCAL_NOT_CONNECTED")
        self.assertEqual(self.client.put("/api/accounts/" + account["id"], json={**payload, "status": "WAITING_MANUAL"}).status_code, 200)
        denied = self.client.put("/api/accounts/" + account["id"], json={**payload, "status": "CONNECTED"})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["detail"]["code"], "AUTHORIZATION_NOT_VERIFIED")
        self.assertEqual(self.client.post("/api/accounts", json={**payload, "password": "FORBIDDEN_UNIT_FIELD"}).status_code, 422)
        self.assertEqual(self.client.delete("/api/products/" + product["id"] + "?confirmed=true").status_code, 409)
        self.assertEqual(self.client.put("/api/products/" + product["id"], json={**product, "country": "法国"}).status_code, 409)
        self.assertEqual(self.client.delete("/api/accounts/" + account["id"] + "?confirmed=true").status_code, 200)
        self.assertEqual(self.client.get("/api/accounts").json(), [])

    def test_settings_persisted_and_actual_production_policy_consumes_same_values(self):
        response = self.client.get("/api/settings/production")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        payload = {**data["settings"], "production_policy": {**data["effective_policy"], "auto_resume": False,
            "retry_limit": 1, "background_strategy": "manual", "quality_thresholds": {"max_hole_ratio": 0.075}}}
        saved = self.client.put("/api/settings/production", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.app.state.production.policy()["retry_limit"], 1)
        self.assertEqual(self.app.state.production.policy()["background_strategy"], "manual")
        self.assertEqual(self.app.state.production.policy()["quality_thresholds"]["max_hole_ratio"], 0.075)
        fresh = create_app(self.settings)
        with TestClient(fresh) as other:
            reopened = other.get("/api/settings/production").json()
            self.assertEqual(reopened["effective_policy"]["auto_resume"], False)
            self.assertEqual(reopened["quality_thresholds"]["max_hole_ratio"], 0.075)
        payload["production_policy"]["concurrency"] = 2
        self.assertEqual(self.client.put("/api/settings/production", json=payload).status_code, 422)
        payload["production_policy"]["concurrency"] = 1
        payload["production_policy"]["quality_thresholds"] = {"fake_unconsumed": 0.5}
        self.assertEqual(self.client.put("/api/settings/production", json=payload).status_code, 422)


if __name__ == "__main__":
    unittest.main()
