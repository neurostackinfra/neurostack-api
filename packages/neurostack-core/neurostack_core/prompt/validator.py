"""
NeuroStack - Prompt Engine - Validator
Validates LLM outputs against schemas.
"""

import json
import logging
import re
from typing import Optional

from neurostack_core.prompt.models import (
    HANDOFF_RESPONSE_SCHEMA,
    RESPONSE_SCHEMA,
    SAFETY_RESPONSE_SCHEMA,
    OutputSchema,
    PromptMode,
    ValidationResult,
)

logger = logging.getLogger(__name__)

JsonObject = dict[str, object]


class OutputValidator:
    """
    Validates LLM outputs against defined schemas.

    Supports:
    - JSON validation
    - Schema compliance checking
    - Output sanitization
    - Error recovery
    """

    def __init__(self) -> None:
        """Initialize with default schemas."""
        self._schemas: dict[PromptMode, OutputSchema] = {
            PromptMode.NORMAL: RESPONSE_SCHEMA,
            PromptMode.RAG: RESPONSE_SCHEMA,
            PromptMode.SAFETY: SAFETY_RESPONSE_SCHEMA,
            PromptMode.HANDOFF: HANDOFF_RESPONSE_SCHEMA,
        }

    def validate(
        self,
        output: str,
        mode: PromptMode,
        schema: Optional[OutputSchema] = None,
    ) -> ValidationResult:
        """
        Validate LLM output.

        Args:
            output: Raw LLM output string
            mode: Current prompt mode
            schema: Optional custom schema (uses default if not provided)

        Returns:
            ValidationResult with status and sanitized output
        """
        warnings: list[str] = []
        errors: list[str] = []

        # Get schema
        validation_schema = schema or self._schemas.get(mode, RESPONSE_SCHEMA)

        # Try to parse as JSON
        parsed = self._try_parse_json(output)

        if parsed is None:
            # Not JSON - treat as plain text response
            parsed = {"response": output.strip()}
            warnings.append("Output was not JSON, wrapped in response field")

        # Validate against schema
        is_valid, schema_errors = validation_schema.validate_output(parsed)
        errors.extend(schema_errors)

        # Sanitize output
        sanitized = self._sanitize_output(parsed, validation_schema)

        # Check for safety issues in content
        content_warnings = self._check_content_safety(parsed)
        warnings.extend(content_warnings)

        return ValidationResult(
            is_valid=is_valid and len(errors) == 0,
            errors=errors,
            warnings=warnings,
            sanitized_output=sanitized,
        )

    def validate_json(
        self, output: str
    ) -> tuple[bool, Optional[JsonObject], list[str]]:
        """
        Validate that output is valid JSON.

        Args:
            output: Raw output string

        Returns:
            Tuple of (is_valid, parsed_json, errors)
        """
        errors: list[str] = []

        # Try direct parsing
        parsed = self._try_parse_json(output)

        if parsed is not None:
            return True, parsed, errors

        # Try to extract JSON from markdown code blocks
        parsed = self._extract_json_from_markdown(output)

        if parsed is not None:
            return True, parsed, ["JSON extracted from markdown code block"]

        errors.append("Failed to parse output as JSON")
        return False, None, errors

    def _try_parse_json(self, text: str) -> Optional[JsonObject]:
        """Try to parse text as JSON."""
        try:
            parsed: object = json.loads(text.strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return {str(key): value for key, value in parsed.items()}

    def _extract_json_from_markdown(self, text: str) -> Optional[JsonObject]:
        """Extract JSON from markdown code blocks."""
        # Pattern for ```json ... ``` or ``` ... ```
        patterns = [
            r"```json\s*\n?(.*?)\n?```",
            r"```\s*\n?(.*?)\n?```",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    parsed: object = json.loads(match.group(1).strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return {str(key): value for key, value in parsed.items()}

        return None

    def _sanitize_output(
        self,
        output: JsonObject,
        schema: OutputSchema,
    ) -> JsonObject:
        """
        Sanitize output to match schema.

        - Removes unknown fields
        - Applies default values
        - Cleans string content
        """
        sanitized: JsonObject = {}

        for prop, spec in schema.properties.items():
            if prop in output:
                value = output[prop]

                # Clean strings
                if isinstance(value, str):
                    value = value.strip()

                sanitized[prop] = value
            elif prop in schema.required:
                # Add default for required fields
                prop_type = spec.get("type", "string")
                sanitized[prop] = self._get_default_value(prop_type)

        return sanitized

    def _get_default_value(self, prop_type: str) -> object:
        """Get default value for a property type."""
        defaults: dict[str, object] = {
            "string": "",
            "number": 0,
            "boolean": False,
            "array": [],
            "object": {},
        }
        return defaults.get(prop_type, None)

    def _check_content_safety(self, output: JsonObject) -> list[str]:
        """Check content for safety issues."""
        warnings: list[str] = []

        response = output.get("response", "")

        if not response:
            warnings.append("Empty response content")

        # Check for potentially dangerous patterns
        dangerous_patterns = [
            (r"\b\d{3}[.-]?\d{3}[.-]?\d{4}\b", "Contains phone number pattern"),
            (
                r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
                "Contains email pattern",
            ),
            (r"\b\d{3}[.-]?\d{3}[.-]?\d{3}[.-]?\d{2}\b", "Contains CPF pattern"),
        ]

        for pattern, message in dangerous_patterns:
            if re.search(pattern, str(response)):
                warnings.append(message)

        return warnings

    def register_schema(self, mode: PromptMode, schema: OutputSchema) -> None:
        """Register a custom schema for a mode."""
        self._schemas[mode] = schema
        logger.info(f"Registered custom schema for mode {mode.value}")

    def get_schema(self, mode: PromptMode) -> OutputSchema:
        """Get schema for a mode."""
        return self._schemas.get(mode, RESPONSE_SCHEMA)


class ResponseExtractor:
    """
    Extracts structured data from LLM responses.
    """

    @staticmethod
    def extract_response_text(output: JsonObject) -> str:
        """Extract the main response text."""
        response = output.get("response", "")
        return response if isinstance(response, str) else ""

    @staticmethod
    def extract_intent(output: JsonObject) -> Optional[str]:
        """Extract detected intent."""
        intent = output.get("intent_detected")
        return intent if isinstance(intent, str) else None

    @staticmethod
    def extract_confidence(output: JsonObject) -> float:
        """Extract confidence score."""
        confidence = output.get("confidence", 0.0)
        if isinstance(confidence, (int, float, str)):
            return float(confidence)
        return 0.0

    @staticmethod
    def extract_actions(output: JsonObject) -> list[str]:
        """Extract suggested actions."""
        actions = output.get("suggested_actions", [])
        if not isinstance(actions, list):
            return []
        return [action for action in actions if isinstance(action, str)]

    @staticmethod
    def to_simple_response(output: JsonObject) -> str:
        """
        Convert structured output to simple text response.

        Useful when you just need the text.
        """
        response_value = output.get("response", "")
        response = response_value if isinstance(response_value, str) else ""

        if not response and isinstance(output, dict):
            # Try to find any string value
            for value in output.values():
                if isinstance(value, str) and value:
                    return value

        return response


# ============================================
# Singleton Instance
# ============================================

_validator: Optional[OutputValidator] = None


def get_output_validator() -> OutputValidator:
    """Get or create the global OutputValidator instance."""
    global _validator
    if _validator is None:
        _validator = OutputValidator()
    return _validator
