"""Unit tests for LabelManager's color create/backfill behavior, isolated
from the rest of the triage pipeline."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LabelColor
from src.gmail_client import LabelManager


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeLabelsResource:
    def __init__(self, existing=None):
        self.labels = dict(existing or {})  # name -> {"id", "color"}
        self._next_id = len(self.labels) + 1
        self.create_calls = []
        self.patch_calls = []

    def list(self, userId):
        return _Exec({"labels": [{"id": v["id"], "name": k, "color": v["color"]} for k, v in self.labels.items()]})

    def create(self, userId, body):
        name = body["name"]
        label_id = f"LABEL_{self._next_id}"
        self._next_id += 1
        color = body.get("color")
        self.labels[name] = {"id": label_id, "color": color}
        self.create_calls.append(name)
        return _Exec({"id": label_id, "name": name, "color": color})

    def patch(self, userId, id, body):
        self.patch_calls.append((id, body))
        for entry in self.labels.values():
            if entry["id"] == id:
                entry["color"] = body.get("color", entry["color"])
                return _Exec({"id": id, "color": entry["color"]})
        raise KeyError(id)


class FakeUsers:
    def __init__(self, labels_resource):
        self._labels = labels_resource

    def labels(self):
        return self._labels


class FakeService:
    def __init__(self, existing=None):
        self._users = FakeUsers(FakeLabelsResource(existing))

    def users(self):
        return self._users


RED = LabelColor(background="#cc3a21", text="#ffffff")
BLUE = LabelColor(background="#4a86e8", text="#ffffff")


class TestLabelManagerColors(unittest.TestCase):
    def test_creates_new_label_with_color(self):
        service = FakeService()
        manager = LabelManager(service)
        manager.get_or_create("Triage/Security", color=RED)
        labels_resource = service.users().labels()
        self.assertEqual(labels_resource.labels["Triage/Security"]["color"], {"backgroundColor": "#cc3a21", "textColor": "#ffffff"})
        self.assertEqual(len(labels_resource.patch_calls), 0)

    def test_creates_new_label_without_color_when_none_given(self):
        service = FakeService()
        manager = LabelManager(service)
        manager.get_or_create("Triage/Other")
        labels_resource = service.users().labels()
        self.assertIsNone(labels_resource.labels["Triage/Other"]["color"])

    def test_backfills_color_on_preexisting_uncolored_label(self):
        existing = {"Triage/Finance": {"id": "LABEL_1", "color": None}}
        service = FakeService(existing)
        manager = LabelManager(service)
        manager.get_or_create("Triage/Finance", color=RED)
        labels_resource = service.users().labels()
        self.assertEqual(labels_resource.labels["Triage/Finance"]["color"], {"backgroundColor": "#cc3a21", "textColor": "#ffffff"})
        self.assertEqual(len(labels_resource.patch_calls), 1)
        self.assertEqual(labels_resource.patch_calls[0][0], "LABEL_1")

    def test_does_not_repatch_when_color_already_matches(self):
        existing = {
            "Triage/Finance": {"id": "LABEL_1", "color": {"backgroundColor": "#cc3a21", "textColor": "#ffffff"}}
        }
        service = FakeService(existing)
        manager = LabelManager(service)
        manager.get_or_create("Triage/Finance", color=RED)
        labels_resource = service.users().labels()
        self.assertEqual(len(labels_resource.patch_calls), 0)

    def test_repatches_when_color_changes_to_a_different_one(self):
        existing = {
            "Triage/Finance": {"id": "LABEL_1", "color": {"backgroundColor": "#cc3a21", "textColor": "#ffffff"}}
        }
        service = FakeService(existing)
        manager = LabelManager(service)
        manager.get_or_create("Triage/Finance", color=BLUE)
        labels_resource = service.users().labels()
        self.assertEqual(labels_resource.labels["Triage/Finance"]["color"], {"backgroundColor": "#4a86e8", "textColor": "#ffffff"})
        self.assertEqual(len(labels_resource.patch_calls), 1)

    def test_second_get_or_create_call_uses_cache_not_another_create(self):
        service = FakeService()
        manager = LabelManager(service)
        id1 = manager.get_or_create("Triage/Security", color=RED)
        id2 = manager.get_or_create("Triage/Security", color=RED)
        self.assertEqual(id1, id2)
        self.assertEqual(len(service.users().labels().create_calls), 1)
        self.assertEqual(len(service.users().labels().patch_calls), 0)


if __name__ == "__main__":
    unittest.main()
