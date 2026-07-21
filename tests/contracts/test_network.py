from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend.app.schemas.events import NetworkScope
from backend.app.schemas.network import NetworkCall, validate_connector_route
from tests.helpers import NOW


def call_data(scope: str, destination: str) -> dict:
    return {
        "call_id": "call-001",
        "source_agent": "test-agent",
        "destination": destination,
        "network_scope": scope,
        "payload": {"synthetic": True},
        "privacy_level": "INTERNAL",
        "timestamp": NOW,
    }


class NetworkContractTests(unittest.TestCase):
    def test_valid_local_call(self) -> None:
        call = NetworkCall.model_validate(call_data("LOCAL", "STEP_AUDIO"))
        validate_connector_route(call.network_scope, False)

    def test_valid_lan_call(self) -> None:
        call = NetworkCall.model_validate(call_data("LAN", "AC_DEVICE"))
        validate_connector_route(call.network_scope, False)

    def test_valid_internet_call(self) -> None:
        call = NetworkCall.model_validate(call_data("INTERNET", "WEATHER_API"))
        validate_connector_route(call.network_scope, True)

    def test_invalid_network_scope_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            NetworkCall.model_validate(call_data("PUBLIC", "WEATHER_API"))

    def test_internet_cannot_bypass_connector(self) -> None:
        with self.assertRaises(ValueError):
            validate_connector_route(NetworkScope.INTERNET, False)

    def test_local_cannot_use_connector(self) -> None:
        with self.assertRaises(ValueError):
            validate_connector_route(NetworkScope.LOCAL, True)

    def test_lan_cannot_use_connector(self) -> None:
        with self.assertRaises(ValueError):
            validate_connector_route(NetworkScope.LAN, True)


if __name__ == "__main__":
    unittest.main()
