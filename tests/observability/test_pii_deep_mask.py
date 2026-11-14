"""Tests for src.observability.pii.mask_carteirinha_deep.

The deep helper walks nested dicts and lists and masks every string with
mask_carteirinha, leaving non-string scalars and dict keys untouched and
never mutating its input.
"""

import copy

from src.observability.pii import MASK_TOKEN, mask_carteirinha_deep

CARD = "998877665544"
CARD_SPACED = "9988 7766 5544"


def _nested_payload() -> dict[str, object]:
    return {
        "content": f"minha carteirinha é {CARD}",
        "count": 42,
        "ratio": 0.75,
        "flag": True,
        "off": False,
        "missing": None,
        CARD: "a key made of digits stays a key",
        "nested": {
            "constraints": [f"nunca repita {CARD_SPACED}", 7, None],
            "deeper": [{"system_prompt": f"Paciente {CARD}", "n": 3}],
        },
        "items": [CARD, [CARD_SPACED, 1.5], {"x": f"tel {CARD}"}],
    }


class TestMaskCarteirinhaDeep:
    def test_masks_every_string_in_nested_dicts_and_lists(self) -> None:
        out = mask_carteirinha_deep(_nested_payload())

        assert out["content"] == f"minha carteirinha é {MASK_TOKEN}"
        assert out["nested"]["constraints"][0] == f"nunca repita {MASK_TOKEN}"
        assert out["nested"]["deeper"][0]["system_prompt"] == f"Paciente {MASK_TOKEN}"
        assert out["items"][0] == MASK_TOKEN
        assert out["items"][1][0] == MASK_TOKEN
        assert out["items"][2]["x"] == f"tel {MASK_TOKEN}"
        rendered = repr({k: v for k, v in out.items() if k != CARD})
        assert CARD not in rendered
        assert CARD_SPACED not in rendered

    def test_leaves_non_string_scalars_and_keys_intact(self) -> None:
        out = mask_carteirinha_deep(_nested_payload())

        assert out["count"] == 42 and type(out["count"]) is int
        assert out["ratio"] == 0.75 and type(out["ratio"]) is float
        assert out["flag"] is True
        assert out["off"] is False
        assert out["missing"] is None
        assert out["nested"]["constraints"][1:] == [7, None]
        assert out["nested"]["deeper"][0]["n"] == 3
        assert out["items"][1][1] == 1.5
        # Keys are never masked, even when they look like a card number.
        assert list(out.keys()) == list(_nested_payload().keys())
        assert out[CARD] == "a key made of digits stays a key"

    def test_does_not_mutate_input(self) -> None:
        payload = _nested_payload()
        snapshot = copy.deepcopy(payload)

        out = mask_carteirinha_deep(payload)

        assert payload == snapshot
        assert out is not payload
        assert out["nested"] is not payload["nested"]
        assert out["items"] is not payload["items"]

    def test_top_level_scalars(self) -> None:
        assert mask_carteirinha_deep(CARD) == MASK_TOKEN
        assert mask_carteirinha_deep("") == ""
        assert mask_carteirinha_deep(None) is None
        assert mask_carteirinha_deep(5) == 5
        assert mask_carteirinha_deep(True) is True
        assert mask_carteirinha_deep([]) == []
        assert mask_carteirinha_deep({}) == {}
