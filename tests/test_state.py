import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from state import (
    MacroStateStore,
    StateConflictError,
    StateCorruptError,
    StateValidationError,
    empty_state,
)


def sample_state():
    state = empty_state()
    state["steps"] = [
        {"id": "step-1", "type": "input", "target": "crm-demo", "field": "work-code", "value": "WO-555"},
        {"id": "step-2", "type": "click", "target": "crm-demo", "action": "add-item"},
        {"id": "step-3", "type": "click", "target": "crm-demo", "action": "mark-ready", "key": "WO-777"},
    ]
    state["items"] = [{"code": "WO-555", "summary": "Sample schedule", "status": "Open"}]
    return state


class MacroStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "macro-state.json"
        self.store = MacroStateStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_is_empty_and_allows_one_bootstrap(self):
        snapshot = self.store.load()
        self.assertFalse(snapshot.persisted)
        self.assertTrue(snapshot.bootstrap_allowed)
        self.assertEqual(snapshot.state, empty_state())
        saved = self.store.bootstrap(sample_state())
        self.assertEqual(saved.state["revision"], 1)
        self.assertFalse(saved.bootstrap_allowed)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(StateConflictError):
            self.store.bootstrap(sample_state())

    def test_restart_on_new_store_instance_keeps_state(self):
        initial = self.store.bootstrap(sample_state())
        revised = {**initial.state, "macroName": "Daily follow-up", "settings": {"maxLoops": 3}}
        saved = self.store.save(revised, expected_revision=initial.state["revision"])
        restarted = MacroStateStore(self.path).load()
        self.assertTrue(restarted.persisted)
        self.assertEqual(restarted.state, saved.state)
        self.assertEqual(restarted.state["revision"], 2)

    def test_stale_revision_is_rejected(self):
        self.store.bootstrap(sample_state())
        stale = self.store.load().state
        self.store.save(stale, expected_revision=1)
        with self.assertRaises(StateConflictError):
            self.store.save(stale, expected_revision=1)

    def test_corrupt_file_is_preserved_and_reported(self):
        raw = b'{"schemaVersion":99,"doNotReplace":"preserve"}\n'
        self.path.write_bytes(raw)
        with self.assertRaises(StateCorruptError):
            self.store.load()
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.path.parent.glob(".macro-state-*")), [])

    def test_oversized_persisted_file_is_preserved(self):
        raw = b"x" * 256_001
        self.path.write_bytes(raw)
        with self.assertRaises(StateCorruptError):
            self.store.load()
        self.assertEqual(self.path.read_bytes(), raw)

    def test_symlink_state_is_rejected_without_following_target(self):
        target = self.path.with_name("outside.json")
        target.write_text(json.dumps(sample_state()), encoding="utf-8")
        self.path.symlink_to(target)
        with self.assertRaises(StateCorruptError):
            self.store.load()
        self.assertTrue(target.exists())
        self.assertTrue(self.path.is_symlink())

    def test_step_and_collection_limits_are_enforced(self):
        state = sample_state()
        state["steps"] = [state["steps"][0]] * 121
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)
        state = sample_state()
        state["items"] = [{"code": f"WO-{index}", "summary": "", "status": "Open"} for index in range(31)]
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)

    def test_sensitive_unknown_and_invalid_actions_are_rejected(self):
        for value in ("person@example.test", "123-45-6789", "4111 1111 1111 1111", "555-123-4567", "sk-secret_abcdefgh"):
            state = sample_state()
            state["steps"][0]["value"] = value
            with self.subTest(value=value), self.assertRaises(StateValidationError):
                self.store.bootstrap(state)
        state = sample_state()
        state["unexpected"] = "hidden data"
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)
        state = sample_state()
        state["steps"][1]["action"] = "open-url"
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)

    def test_unknown_step_keys_and_boolean_numbers_are_rejected(self):
        state = sample_state()
        state["steps"][0]["url"] = "https://example.test"
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)
        state = sample_state()
        state["settings"]["maxLoops"] = True
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)

    def test_step_ids_must_be_unique_and_names_cannot_store_sensitive_values(self):
        state = sample_state()
        state["steps"][1]["id"] = state["steps"][0]["id"]
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)
        state = sample_state()
        state["macroName"] = "person@example.test"
        with self.assertRaises(StateValidationError):
            self.store.bootstrap(state)

    def test_duplicate_json_object_keys_are_corrupt_and_preserved(self):
        raw = b'{"schemaVersion":1,"schemaVersion":1}\n'
        self.path.write_bytes(raw)
        with self.assertRaises(StateCorruptError):
            self.store.load()
        self.assertEqual(self.path.read_bytes(), raw)

    def test_failed_atomic_replace_keeps_previous_state(self):
        saved = self.store.bootstrap(sample_state())
        changed = {**saved.state, "macroName": "Must not replace"}
        with patch("state.os.replace", side_effect=OSError("simulated replacement failure")):
            with self.assertRaises(OSError):
                self.store.save(changed, expected_revision=1)
        self.assertEqual(self.store.load().state, saved.state)
        self.assertEqual(list(self.path.parent.glob(".macro-state-*")), [])

    def test_concurrent_writes_from_separate_store_objects_are_serialized(self):
        self.store.bootstrap(sample_state())
        barrier = threading.Barrier(2)
        outcomes = []

        def write(name):
            store = MacroStateStore(self.path)
            state = store.load().state
            state["macroName"] = name
            barrier.wait(timeout=3)
            try:
                store.save(state, expected_revision=1)
                outcomes.append("saved")
            except StateConflictError:
                outcomes.append("conflict")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(write, name) for name in ("First", "Second")]
            for future in futures:
                future.result(timeout=5)
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        self.assertEqual(self.store.load().state["revision"], 2)


if __name__ == "__main__":
    unittest.main()
