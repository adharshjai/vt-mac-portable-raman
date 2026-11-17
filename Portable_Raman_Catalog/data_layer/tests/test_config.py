"""Settings (HOME/catalog_config.json), the standard paths and the file helpers."""

import json
import os
import re
import stat
import unittest
from unittest import mock

import support
from raman_catalog import config
from raman_catalog.config import ConfigError, Paths, load_settings


class Settings(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.file = self.home / "catalog_config.json"

    def test_defaults_are_written_on_first_use(self):
        settings = load_settings(self.home)
        expected = {
            "catalog": {"root_alias": "raman-portable"},
            "review": {"confidence_threshold": None},
            "classifier": {"model_dir": "models/pca_svm_pure_20260922"},
            "sync": {"max_attempts": 5},
        }
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")), expected)
        self.assertEqual(settings.as_dict(), expected)
        self.assertEqual(settings.problems(), [])
        self.assertEqual(settings.root_alias, "raman-portable")
        self.assertIsNone(settings.confidence_threshold)
        self.assertEqual(settings.max_attempts, 5)
        self.assertEqual(settings.model_dir, self.home / "models" / "pca_svm_pure_20260922")

    def test_unknown_keys_are_kept_and_missing_ones_filled(self):
        self.file.write_text(json.dumps({"catalog": {"team": "x"},
                                         "studio": {"port": 8765}}), encoding="utf-8")
        settings = load_settings(self.home)
        self.assertEqual(settings.get("studio.port"), 8765)
        self.assertEqual(settings.get("catalog.team"), "x")
        self.assertEqual(settings.root_alias, "raman-portable")
        self.assertEqual(settings.max_attempts, 5)
        settings.set("sync.max_attempts", 3)
        settings.save()
        saved = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(saved["studio"], {"port": 8765})
        self.assertEqual(saved["catalog"]["team"], "x")
        self.assertEqual(saved["sync"], {"max_attempts": 3})

    def test_loading_does_not_rewrite_an_existing_file(self):
        self.file.write_text('{"sync": {"max_attempts": 4}}', encoding="utf-8")
        load_settings(self.home)
        self.assertEqual(self.file.read_text(encoding="utf-8"), '{"sync": {"max_attempts": 4}}')

    def test_broken_file_is_an_error_and_is_left_alone(self):
        for content in ("{not json", "[1, 2]"):
            self.file.write_text(content, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(self.home)
            self.assertEqual(self.file.read_text(encoding="utf-8"), content)

    def test_dotted_get_and_set(self):
        settings = load_settings(self.home)
        settings.set("catalog.root_alias", "lab")
        settings.set("arc.host", "login.example.org")
        self.assertEqual(settings.get("catalog.root_alias"), "lab")
        self.assertEqual(settings.get("arc.host"), "login.example.org")
        self.assertEqual(settings.get("no.such.key", "fallback"), "fallback")
        self.assertTrue(settings.is_known("review.confidence_threshold"))
        self.assertFalse(settings.is_known("arc.host"))
        with self.assertRaises(ConfigError):
            settings.set("catalog.root_alias.sub", 1)
        with self.assertRaises(ConfigError):
            settings.set("catalog..root_alias", 1)

    def test_values_given_as_text_are_typed(self):
        settings = load_settings(self.home)
        settings.set_from_text("review.confidence_threshold", "0.7")
        settings.set_from_text("sync.max_attempts", "3")
        settings.set_from_text("catalog.root_alias", "2026")
        settings.set_from_text("extra.flag", "true")
        settings.set_from_text("extra.name", "plain words")
        self.assertEqual(settings.confidence_threshold, 0.7)
        self.assertEqual(settings.max_attempts, 3)
        self.assertEqual(settings.root_alias, "2026")
        self.assertIs(settings.get("extra.flag"), True)
        self.assertEqual(settings.get("extra.name"), "plain words")
        self.assertEqual(settings.problems(), [])
        settings.set_from_text("review.confidence_threshold", "null")
        self.assertIsNone(settings.confidence_threshold)

    def test_every_invalid_setting_is_named(self):
        cases = {
            "catalog.root_alias": ["Has Upper", "", "x" * 28, "-leading", 7],
            "review.confidence_threshold": [0, 1.5, "high", True],
            "classifier.model_dir": ["", 3],
            "sync.max_attempts": [0, 2.5, "5", True],
        }
        for key, values in cases.items():
            for value in values:
                settings = load_settings(self.home)
                settings.set(key, value)
                problems = settings.problems()
                self.assertEqual(len(problems), 1, (key, value, problems))
                self.assertIn(key, problems[0])
                with self.assertRaises(ConfigError) as raised:
                    settings.require_valid()
                self.assertIn("config --set", raised.exception.detail)

    def test_longest_alias_built_from_the_root_fits_the_limit(self):
        settings = load_settings(self.home)
        settings.set("catalog.root_alias", "r" * 27)
        self.assertEqual(settings.problems(), [])
        self.assertEqual(len(settings.root_alias + "-needs-review"), 40)

    def test_catalog_key_changes_with_what_selects_the_catalog(self):
        settings = load_settings(self.home)
        keys = {settings.catalog_key}
        settings.set("catalog.root_alias", "other-root")
        keys.add(settings.catalog_key)
        self.assertEqual(len(keys), 2)
        settings.set("sync.max_attempts", 9)
        self.assertIn(settings.catalog_key, keys)

    def test_model_dir_may_be_absolute(self):
        settings = load_settings(self.home)
        settings.set("classifier.model_dir", str(self.home / "elsewhere"))
        self.assertEqual(settings.model_dir, self.home / "elsewhere")


class Helpers(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)

    def test_standard_paths(self):
        paths = Paths(self.home)
        self.assertEqual(paths.config_file, self.home / "catalog_config.json")
        self.assertEqual(paths.scan_sessions, self.home / "scan_sessions")
        self.assertEqual(paths.dark_measurements, self.home / "dark_measurements")
        self.assertEqual(paths.ledger_file, self.home / "data_catalog" / "ledger.sqlite")
        self.assertEqual(paths.sync_lock, self.home / "data_catalog" / "sync.lock")
        self.assertEqual(paths.local_demo, self.home / "data_catalog" / "local_demo")
        self.assertEqual(paths.staging, self.home / "data_catalog" / "staging")
        self.assertEqual(paths.sim_stage_file, self.home / "scan_sessions" / "_sim_stage.json")

    def test_utc_now_has_the_controllers_format(self):
        self.assertRegex(config.utc_now(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_atomic_write_replaces_in_one_step_and_keeps_the_mode(self):
        target = self.home / "file.json"
        config.atomic_write_json(target, {"a": 1})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        os.chmod(target, 0o600)
        config.atomic_write_text(target, "second")
        self.assertEqual(target.read_text(encoding="utf-8"), "second")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual([path.name for path in self.home.iterdir()], ["file.json"])

    def test_failed_write_leaves_the_old_file_and_no_leftover(self):
        target = self.home / "file.json"
        target.write_text("old", encoding="utf-8")
        with mock.patch.object(config.os, "replace", side_effect=OSError("disk trouble")):
            with self.assertRaises(OSError):
                config.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual([path.name for path in self.home.iterdir()], ["file.json"])

    def test_alias_pattern_is_the_one_of_the_contract(self):
        for alias in ("rs-0123456789ab", "raman-portable", "a.b_c-d", "9lives"):
            self.assertTrue(re.match(config.ALIAS_PATTERN, alias), alias)
        for alias in ("Upper", "sp ace", "", "-x", "a/b", "u:me:alias"):
            self.assertFalse(re.match(config.ALIAS_PATTERN, alias), alias)


if __name__ == "__main__":
    unittest.main()
