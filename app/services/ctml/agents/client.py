"""The chat client shared by all agents: an Azure AI Foundry model through its OpenAI v1 endpoint."""

from __future__ import annotations

from agent_framework import SupportsChatGetResponse

from app.services.ctml.config import Settings, openai_v1_url

# Sent with every tool loop. include_detailed_errors lets the model read argument errors
# (for example the allowed values of an enumerated field).
FUNCTION_INVOCATION = {
    "include_detailed_errors": True,
    "max_iterations": 30,
    "max_function_calls": 80,
    "max_consecutive_errors_per_request": 5,
}


def build_chat_client(settings: Settings) -> SupportsChatGetResponse:
    from agent_framework.openai import OpenAIChatClient

    settings.require_model()
    base_url = openai_v1_url(settings.foundry_endpoint)
    if settings.foundry_api_key is not None:
        return OpenAIChatClient(
            model=settings.foundry_model,
            api_key=settings.foundry_api_key.get_secret_value(),
            base_url=base_url,
            function_invocation_configuration=FUNCTION_INVOCATION,
        )
    from azure.identity import DefaultAzureCredential

    return OpenAIChatClient(
        model=settings.foundry_model,
        credential=DefaultAzureCredential(),
        base_url=base_url,
        function_invocation_configuration=FUNCTION_INVOCATION,
    )
