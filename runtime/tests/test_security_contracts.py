import pytest
from pydantic import ValidationError

from mae_runtime.contracts import RunRequest


def test_agent_prompt_overrides_are_bounded_and_normalized() -> None:
    request = RunRequest(
        harness="robust",
        prompt="run the certified agricultural analysis",
        agent_prompts={"sql_agent": "  use the approved SQL plan  "},
    )
    assert request.agent_prompts == {"sql_agent": "use the approved SQL plan"}


@pytest.mark.parametrize(
    "overrides",
    [
        {f"role_{index}": "valid prompt" for index in range(9)},
        {"sql_agent": "x" * 6001},
        {"x" * 65: "valid prompt"},
        {"sql_agent": "   "},
    ],
)
def test_agent_prompt_overrides_reject_resource_abuse(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        RunRequest(
            harness="robust",
            prompt="run the certified agricultural analysis",
            agent_prompts=overrides,
        )
