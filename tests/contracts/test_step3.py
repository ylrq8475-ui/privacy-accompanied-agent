from __future__ import annotations

import copy
import unittest

from pydantic import ValidationError

from backend.app.schemas.actions import ActionProposal
from backend.app.schemas.step3 import Step3Output
from tests.helpers import step3_data


class Step3ContractTests(unittest.TestCase):
    def test_valid_structured_output(self) -> None:
        output = Step3Output.model_validate(step3_data())
        self.assertEqual(len(output.state_hypotheses), 2)

    def test_missing_hypotheses_is_rejected(self) -> None:
        data = step3_data()
        del data["state_hypotheses"]
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)

    def test_top_level_execute_is_rejected(self) -> None:
        data = step3_data()
        data["execute"] = True
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)

    def test_top_level_authorization_is_rejected(self) -> None:
        data = step3_data()
        data["authorization_status"] = "APPROVED"
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)

    def test_nested_execution_field_is_rejected(self) -> None:
        data = step3_data()
        data["recommended_action"]["execute"] = True
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)

    def test_string_confidence_is_rejected(self) -> None:
        data = step3_data()
        data["state_hypotheses"][0]["confidence"] = "0.6"
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)

    def test_confidence_out_of_range_is_rejected(self) -> None:
        data = step3_data()
        data["state_hypotheses"][0]["confidence"] = -0.1
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)

    def test_unknown_state_label_is_rejected(self) -> None:
        data = step3_data()
        data["state_hypotheses"][0]["label"] = "EXECUTE_TOOL"
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)

    def test_step3_output_cannot_parse_as_action_proposal(self) -> None:
        output = Step3Output.model_validate(step3_data())
        with self.assertRaises(ValidationError):
            ActionProposal.model_validate(output.model_dump())

    def test_clarification_extra_field_is_rejected(self) -> None:
        data = copy.deepcopy(step3_data())
        data["clarification_candidates"][0]["skip_confirmation"] = True
        with self.assertRaises(ValidationError):
            Step3Output.model_validate(data)


if __name__ == "__main__":
    unittest.main()
