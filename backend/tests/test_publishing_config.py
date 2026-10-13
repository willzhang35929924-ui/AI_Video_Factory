"""Only synthetic dotenv files are read; no user credentials or services are used."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.integrations.publishing import config as publishing_config


class PublishingDotenvConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="publishing-dotenv-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "backend").mkdir()
        self.path = self.root / "config.yaml"
        self.path.write_text("publishing:\n  hubstudio_api_key_env: UNIT_HUB_KEY\n  description_ai:\n    api_key_env: UNIT_AI_KEY\n", encoding="utf-8")
        root_patch = patch.object(publishing_config, "PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = patch.dict(os.environ, {}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_only_explicit_keys_load_backend_overrides_root_and_public_is_redacted(self):
        (self.root / ".env").write_text("UNIT_HUB_KEY=UNIT_ROOT_HUB\nUNIT_AI_KEY=UNIT_ROOT_AI\nUNRELATED_SECRET=UNIT_DO_NOT_LOAD\nDATABASE_URL=UNIT_NO_DATABASE_OVERRIDE\n", encoding="utf-8")
        (self.root / "backend" / ".env").write_text("UNIT_HUB_KEY=UNIT_BACKEND_HUB\nUNRELATED_BACKEND_SECRET=UNIT_DO_NOT_LOAD_EITHER\n", encoding="utf-8")
        config = publishing_config.load_publishing_config(self.path)
        self.assertEqual(dict(os.environ), {"UNIT_HUB_KEY": "UNIT_BACKEND_HUB", "UNIT_AI_KEY": "UNIT_ROOT_AI"})
        public = config.public()
        self.assertTrue(public["hubstudio_api_key_configured"])
        for secret in ("UNIT_BACKEND_HUB", "UNIT_ROOT_AI", "UNIT_DO_NOT_LOAD"):
            self.assertNotIn(secret, str(public))

    def test_existing_environment_even_empty_is_never_overwritten(self):
        (self.root / ".env").write_text("UNIT_HUB_KEY=UNIT_FROM_FILE\nUNIT_AI_KEY=UNIT_FROM_FILE_AI\n", encoding="utf-8")
        os.environ.update(UNIT_HUB_KEY="UNIT_EXISTING", UNIT_AI_KEY="")
        publishing_config.load_publishing_config(self.path)
        self.assertEqual(dict(os.environ), {"UNIT_HUB_KEY": "UNIT_EXISTING", "UNIT_AI_KEY": ""})

    def test_dotenv_does_not_interpolate_other_file_or_process_secrets(self):
        (self.root / ".env").write_text("OTHER_FILE_SECRET=UNIT_PRIVATE_FILE\nUNIT_HUB_KEY=${OTHER_FILE_SECRET}\nUNIT_AI_KEY=${OTHER_PROCESS_SECRET}\n", encoding="utf-8")
        os.environ["OTHER_PROCESS_SECRET"] = "UNIT_PRIVATE_PROCESS"
        publishing_config.load_publishing_config(self.path)
        self.assertEqual(os.environ["UNIT_HUB_KEY"], "${OTHER_FILE_SECRET}")
        self.assertEqual(os.environ["UNIT_AI_KEY"], "${OTHER_PROCESS_SECRET}")
        self.assertNotIn("OTHER_FILE_SECRET", os.environ)
        self.assertEqual(os.environ["OTHER_PROCESS_SECRET"], "UNIT_PRIVATE_PROCESS")

    def test_missing_files_and_valueless_keys_do_not_fabricate_credentials(self):
        config = publishing_config.load_publishing_config(self.path)
        self.assertEqual(dict(os.environ), {})
        self.assertFalse(config.public()["hubstudio_api_key_configured"])
        (self.root / "backend" / ".env").write_text("UNIT_HUB_KEY\nUNIT_AI_KEY=\n", encoding="utf-8")
        config = publishing_config.load_publishing_config(self.path)
        self.assertNotIn("UNIT_HUB_KEY", os.environ)
        self.assertEqual(os.environ["UNIT_AI_KEY"], "")
        self.assertFalse(config.public()["hubstudio_api_key_configured"])

    def test_invalid_config_is_rejected_before_environment_changes(self):
        (self.root / ".env").write_text("UNIT_HUB_KEY=UNIT_MUST_NOT_LOAD\n", encoding="utf-8")
        self.path.write_text("publishing:\n  concurrency: 0\n", encoding="utf-8")
        with patch.object(publishing_config, "dotenv_values") as reader:
            with self.assertRaises(ValueError):
                publishing_config.load_publishing_config(self.path)
        reader.assert_not_called()
        self.assertEqual(dict(os.environ), {})


if __name__ == "__main__":
    unittest.main()
