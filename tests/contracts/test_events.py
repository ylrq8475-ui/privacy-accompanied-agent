from __future__ import annotations

import unittest
from datetime import datetime

from pydantic import ValidationError

from backend.app.schemas.events import Event, NetworkScope
from tests.helpers import event_data


class EventContractTests(unittest.TestCase):
    def test_valid_event(self) -> None:
        event = Event.model_validate(event_data())
        self.assertEqual(event.network_scope, NetworkScope.LOCAL)

    def test_missing_required_field_is_rejected(self) -> None:
        data = event_data()
        del data["event_id"]
        with self.assertRaises(ValidationError):
            Event.model_validate(data)

    def test_extra_field_is_rejected(self) -> None:
        data = event_data()
        data["execute"] = True
        with self.assertRaises(ValidationError):
            Event.model_validate(data)

    def test_invalid_confidence_type_is_rejected(self) -> None:
        data = event_data()
        data["confidence"] = "0.8"
        with self.assertRaises(ValidationError):
            Event.model_validate(data)

    def test_confidence_out_of_range_is_rejected(self) -> None:
        data = event_data()
        data["confidence"] = 1.1
        with self.assertRaises(ValidationError):
            Event.model_validate(data)

    def test_naive_timestamp_is_rejected(self) -> None:
        data = event_data()
        data["timestamp"] = datetime(2026, 7, 20, 12, 0)
        with self.assertRaises(ValidationError):
            Event.model_validate(data)

    def test_invalid_network_scope_is_rejected(self) -> None:
        data = event_data()
        data["network_scope"] = "PUBLIC"
        with self.assertRaises(ValidationError):
            Event.model_validate(data)


if __name__ == "__main__":
    unittest.main()
