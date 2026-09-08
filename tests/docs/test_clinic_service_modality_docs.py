"""Deterministic contract checks for clinic service-modality documentation."""

import json
import re
from pathlib import Path

from src.api.main import app

ROOT = Path(__file__).resolve().parents[2]


def _section(document: str, heading: str, next_heading: str) -> str:
    start = document.index(heading)
    end = document.index(next_heading, start)
    return document[start:end]


def _json_blocks(section: str) -> list[dict[str, object]]:
    return [
        json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", section, re.S)
    ]


def test_service_modality_docs_example_and_openapi_contract() -> None:
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    api_contracts = (ROOT / "docs/api-contracts.md").read_text(encoding="utf-8")
    example = json.loads(
        (ROOT / "docs/examples/tenant-config-with-clinic.json").read_text(
            encoding="utf-8"
        )
    )

    configuration_section = _section(
        configuration, "## Modalidades dos serviços", "## Exemplo completo"
    )
    contracts_section = _section(
        api_contracts, "### Configuração da clínica", "### Tenant"
    )
    expected_request = {
        "service_format": "online",
        "services": [
            {"name": "Teleconsulta", "service_format": "online"},
            "Retorno",
        ],
    }
    expected_response = {
        "clinic_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "version": 1,
        **expected_request,
    }
    expected_conflict = {
        "detail": {
            "error": "service_format_conflict",
            "message": "One or more service modalities are incompatible with the clinic service format.",
            "clinic_service_format": "online",
            "allowed_service_formats": ["online"],
            "conflicts": [{"index": 1, "service_format": "presential"}],
            "action": "Change the listed service modalities or broaden the clinic service format.",
        }
    }

    assert _json_blocks(contracts_section) == [
        expected_request,
        expected_response,
        expected_conflict,
    ]
    configuration_blocks = _json_blocks(configuration_section)
    assert configuration_blocks[0]["service_format"] == "both"
    assert configuration_blocks[0]["services"] == [
        {
            "name": "Consulta por vídeo",
            "service_format": "online",
            "frontend_metadata": {"color": "blue"},
        },
        "Retorno",
    ]
    assert configuration_blocks[1] == configuration_blocks[0]
    assert configuration_blocks[2] == expected_conflict

    for section in (configuration_section, contracts_section):
        normalized = " ".join(section.lower().split())
        assert "Omitir `services` preserva" in section
        assert "`services: []`" in section
        assert "`services: null`" in section
        assert "string legad" in normalized
        assert "substitui o array" in section

    for section in (configuration_section, contracts_section):
        assert "| `online` | `online` | `online` |" in section
        assert "| `presential` | `presential` | `presential` |" in section
        assert "| `both` | `online`, `presential`, `both` | nenhum |" in section
        assert "| vazio ou histórico |" in section

    clinic_config = example["clinic_config"]
    assert clinic_config["operations"]["service_format"] == "both"
    assert clinic_config["services"][0]["service_format"] == "online"
    assert clinic_config["services"][1]["service_format"] == "presential"

    schemas = app.openapi()["components"]["schemas"]
    service_item = schemas["ServicePatchItem"]
    assert service_item["additionalProperties"] is True
    assert service_item["properties"]["service_format"] == {
        "anyOf": [
            {"type": "string", "enum": ["online", "presential", "both"]},
            {"type": "null"},
        ],
        "title": "Service Format",
    }
    services_items = schemas["ConfigPatchRequest"]["properties"]["services"]["anyOf"][
        0
    ]["items"]["anyOf"]
    assert services_items == [
        {"$ref": "#/components/schemas/ServicePatchItem"},
        {"type": "string"},
    ]
