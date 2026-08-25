from app.prompts.search_interpreter import SEARCH_INTERPRETER_INSTRUCTIONS
from app.search_contract import ClusterSearchQuery, SearchInterpretationModelOutput
from app.services.search_interpreter_provider import GatewaySearchInterpreterProvider
from app.services.signal_analysis_provider import GatewaySignalAnalysisProvider
from app.services.solution_provider import GatewaySolutionProvider
from app.signal_contract import SignalConversationModelOutput
from app.solution_contract import SolutionModelOutput


class RecordingGateway:
    name = "ollama"
    model = "local-model"

    def __init__(self, output):
        self.output = output
        self.request = None

    async def generate_structured(self, **kwargs):
        self.request = kwargs
        return self.output


def test_interpreter_prompt_requires_neutral_filters_for_new_searches():
    flattened = " ".join(SEARCH_INTERPRETER_INSTRUCTIONS.split())
    assert "Never invent or tighten a filter" in SEARCH_INTERPRETER_INSTRUCTIONS
    assert "observed_after null" in flattened
    assert "preserve current_draft filters" in flattened


async def test_search_adapter_uses_search_contract():
    output = SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(query="invoice delays"),
        assumptions=[],
        unsupported=[],
        clarification_question=None,
    )
    gateway = RecordingGateway(output)
    provider = GatewaySearchInterpreterProvider(gateway)

    result = await provider.generate(instructions="Search", payload={"message": "invoice"})

    assert result.draft.query == "invoice delays"
    assert gateway.request["schema_name"] == "search_interpretation"
    assert gateway.request["max_output_tokens"] == 2_400


async def test_analyze_conversation_adapter_uses_smaller_contract():
    output = SignalConversationModelOutput(
        text="The evidence is limited.",
        citations=[],
        proposal=None,
        insufficient_evidence=True,
    )
    gateway = RecordingGateway(output)
    provider = GatewaySignalAnalysisProvider(gateway)

    result = await provider.ask(instructions="Answer", payload={"case": {}})

    assert result.insufficient_evidence is True
    assert gateway.request["schema_name"] == "signal_conversation"
    assert gateway.request["max_output_tokens"] == 2_500


async def test_build_adapter_uses_build_contract_and_configured_limit():
    output = SolutionModelOutput.model_validate({
        "mvpGoal": "Send one invoice reminder.",
        "constraints": [],
        "nonGoals": ["Payment processing"],
        "primaryFlow": {
            "actor": "Freelancer",
            "trigger": "Invoice is overdue",
            "steps": ["Open invoice", "Send reminder"],
            "successOutcome": "Reminder is sent",
        },
        "features": [{
            "id": "feature-1",
            "title": "Send reminder",
            "summary": "Send a reminder for an overdue invoice.",
            "problemUnitIds": ["unit-1"],
            "evidenceIds": ["evidence-1"],
            "acceptanceCriteria": ["Reminder is recorded"],
        }],
        "architecture": {
            "stack": ["FastAPI"],
            "components": [{
                "id": "component-1",
                "name": "Reminder API",
                "responsibility": "Send reminders",
                "featureIds": ["feature-1"],
            }],
            "dataModel": [],
            "apiContracts": [],
            "integrations": [],
            "securityAndPrivacy": [],
        },
        "tasks": [{
            "id": "task-1",
            "title": "Implement reminder",
            "description": "Add the endpoint and persistence.",
            "featureIds": ["feature-1"],
            "dependsOn": [],
            "acceptanceChecks": ["Endpoint test passes"],
            "status": "todo",
        }],
        "testPlan": {"unit": [], "integration": [], "endToEnd": ["Send reminder"]},
        "deliveryChecklist": [],
    })
    gateway = RecordingGateway(output)
    provider = GatewaySolutionProvider(gateway, 7_500)

    result = await provider.generate(instructions="Build", payload={"problemCase": {}})

    assert result.mvp_goal == "Send one invoice reminder."
    assert gateway.request["schema_name"] == "solution_case"
    assert gateway.request["max_output_tokens"] == 7_500
