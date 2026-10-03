"""Deterministic tests for opt-in conversation language switching."""

from unittest.mock import MagicMock

import pytest
from lingua import Language

from src.services.conversation_language import (
    LanguageDetection,
    decide_language_switch,
    detect_language,
    evaluate_conversation_language,
    normalize_configured_language,
)

_LONG_IDNA_PATH = "a" * 70
_PUNCTUATED_IDNA_PATH = f"{_LONG_IDNA_PATH}/segment_(keep)-v1"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("pt-BR", "pt-BR"),
        ("en", "en"),
        (None, "pt-BR"),
        ("", "pt-BR"),
        ("pt", "pt-BR"),
        ("EN", "pt-BR"),
        ({"language": "en"}, "pt-BR"),
    ],
)
def test_configured_language_normalization_is_fail_closed(configured, expected):
    assert normalize_configured_language(configured) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Olá, gostaria de agendar uma consulta para amanhã.", "pt-BR"),
        ("Preciso saber quais horários estão disponíveis hoje.", "pt-BR"),
        ("Hello, I would like to schedule an appointment tomorrow.", "en"),
        ("Could you tell me which appointment times are available?", "en"),
        ("Hola, quisiera programar una cita para mañana.", "es"),
        ("Bonjour, je voudrais prendre rendez-vous pour demain.", "fr"),
        ("Buongiorno, vorrei fissare un appuntamento per domani.", "it"),
        ("Guten Tag, ich möchte einen Termin für morgen vereinbaren.", "de"),
        ("Goedemorgen, ik wil graag een afspraak maken voor morgen.", "nl"),
    ],
)
def test_detector_fixed_latin_corpus(text, expected):
    detection = detect_language(text)

    assert detection.language == expected
    assert detection.confidence >= 0.80
    assert detection.margin >= 0.20


@pytest.mark.parametrize(
    "text",
    [
        "Привет как ваши дела сегодня",
        "您好我想预约明天的咨询",
        "مرحبا أريد حجز موعد غدا",
        "hello",
        "sim 123",
    ],
)
def test_detector_abstains_for_non_latin_and_short_inputs(text):
    detection = detect_language(text)

    assert detection.language is None


@pytest.mark.parametrize(
    "text",
    [
        "Olá hello gostaria appointment para tomorrow",
        "Preciso schedule uma consulta tomorrow please",
        "Hola preciso an appointment para mañana",
    ],
)
def test_detector_preserves_raw_winner_for_ineligible_mixed_input(text):
    detection = detect_language(text)
    result = decide_language_switch(
        text=text,
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        detection=detection,
    )

    assert detection.language in {"pt-BR", "en", "es"}
    assert detection.eligible_for_switch is False
    assert result.decision == "abstain"
    assert result.detected_language == detection.language


@pytest.mark.parametrize(
    "text",
    [
        "http://example.com/path",
        "https://example.com/path",
        "ftp://files.example.com/archive",
        "custom+transport://resource",
        "mailto:appointments@example.com",
        "urn:example:animal:ferret:nose",
        "www.example.com",
        "www.example.com/path",
        "english-appointment-scheduling.com",
        "appointment.schedule.com",
        "appointments.example.co.uk",
        "clinic.example.com/schedule/appointment/tomorrow",
        "clínica.com",
        "clínica.com/consulta/agendar/amanhã",
        "xn--clnica-4va.com/consulta/agendar/amanha",
        "Please visit https://example.com/path",
        "Please visit appointments.example.co.uk",
    ],
)
def test_detector_abstains_for_url_like_input_before_classification(text, mocker):
    detector_factory = mocker.patch(
        "src.services.conversation_language.get_language_detector"
    )

    detection = detect_language(text)

    assert detection == LanguageDetection(None, None, None)
    detector_factory.assert_not_called()


@pytest.mark.parametrize(
    "domain",
    [
        "q\u0307.com",
        "q\u0307.com/schedule",
        "xn--q-9bb.com",
        "xn--q-9bb.com/schedule",
        "clinic\u3002com",
        "clinic\u3002com/schedule",
        "clinic\uff0ecom",
        "clinic\uff0ecom/schedule",
        "clinic\uff61com",
        "clinic\uff61com/schedule",
        f"q\u0307.com/{_LONG_IDNA_PATH}",
        f"q\u0307.com:8443/{_LONG_IDNA_PATH}?slot=1#book",
        f"clinic\u3002com/{_LONG_IDNA_PATH}",
        f"clinic\u3002com:8443/{_LONG_IDNA_PATH}?slot=1#book",
    ],
)
def test_detector_abstains_for_idna_domains_before_classification(domain, mocker):
    detector_factory = mocker.patch(
        "src.services.conversation_language.get_language_detector"
    )

    detection = detect_language(f"please visit {domain} to schedule today")

    assert detection == LanguageDetection(None, None, None)
    detector_factory.assert_not_called()


@pytest.mark.parametrize(
    "domain",
    ["q\u0307.com", "xn--q-9bb.com", "example.com", "clinic\u3002com"],
)
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("(", ")."),
        ('"', '".'),
        ("visit(", ")"),
        ("[", "],"),
        ("<", ">;"),
        ("'", "',"),
    ],
)
def test_domain_guards_are_independent_of_adjacent_punctuation(
    domain, prefix, suffix, mocker
):
    text = (
        f"please {prefix}{domain}/{_PUNCTUATED_IDNA_PATH}{suffix} "
        "to schedule today now"
    )
    detector_factory = mocker.patch(
        "src.services.conversation_language.get_language_detector"
    )

    detection = detect_language(text)
    injected = decide_language_switch(
        text=text,
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        detection=LanguageDetection("en", 0.95, 0.40),
    )

    assert detection == LanguageDetection(None, None, None)
    detector_factory.assert_not_called()
    assert injected.decision == "abstain"
    assert injected.detected_language is None


def test_hostile_near_limit_domain_abstains_before_detector(mocker):
    hostile_input = "a." * 4999 + "1"
    detector_factory = mocker.patch(
        "src.services.conversation_language.get_language_detector"
    )

    detection = detect_language(hostile_input)

    assert len(hostile_input) == 9999
    assert detection == LanguageDetection(None, None, None)
    detector_factory.assert_not_called()


def test_hostname_scanner_work_grows_linearly_with_input_size():
    from src.services.conversation_language import (
        _contains_uri_scheme,
        _idna_hostname_candidates,
    )

    class CountingText(str):
        accesses: int
        operations: int

        def __new__(cls, value):
            instance = super().__new__(cls, value)
            instance.accesses = 0
            instance.operations = 0
            return instance

        def __getitem__(self, key):
            self.accesses += 1
            return super().__getitem__(key)

        def __iter__(self):
            for character in super().__iter__():
                self.operations += 1
                yield character

    operation_counts = []
    for repetitions in (999, 4999):
        text = CountingText("a." * repetitions + "1")

        assert _contains_uri_scheme(text) is False
        candidates = _idna_hostname_candidates(text)

        assert candidates == [str(text)]
        assert text.accesses <= len(text) * 2
        assert text.operations == len(text)
        operation_counts.append(text.operations)

    assert operation_counts[1] / operation_counts[0] == pytest.approx(9999 / 1999)


@pytest.mark.parametrize(
    "text",
    [
        "João Silva",
        "joão silva",
        "John Smith",
        "Antônio Carlos da Silva",
        "antônio carlos da silva",
    ],
)
def test_detector_abstains_for_names_with_at_most_four_tokens(text, mocker):
    detector_factory = mocker.patch(
        "src.services.conversation_language.get_language_detector"
    )

    detection = detect_language(text)

    assert detection == LanguageDetection(None, None, None)
    detector_factory.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "preciso agendar uma consulta amanhã",
        "please schedule my appointment tomorrow",
    ],
)
def test_five_alphabetic_tokens_reach_the_detector(text, mocker):
    from src.services import conversation_language

    detector_factory = mocker.spy(conversation_language, "get_language_detector")

    detection = detect_language(text)

    assert detection.language in {"pt-BR", "en"}
    detector_factory.assert_called_once_with()


@pytest.mark.parametrize(
    "text",
    [
        "preciso agendar consulta amanhã",
        "schedule my appointment tomorrow",
    ],
)
def test_four_alphabetic_tokens_abstain_before_detector(text, mocker):
    detector_factory = mocker.patch(
        "src.services.conversation_language.get_language_detector"
    )

    detection = detect_language(text)

    assert detection == LanguageDetection(None, None, None)
    detector_factory.assert_not_called()


def _detection(language: str) -> LanguageDetection:
    return LanguageDetection(language=language, confidence=0.95, margin=0.40)


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "schedule now",
        "schedule my appointment",
        "schedule my appointment tomorrow",
        "https://example.com/schedule/appointment/tomorrow",
        "mailto:appointments@example.com",
        "urn:example:animal:ferret:nose",
        "www.example.com/schedule/appointment/tomorrow",
        "appointments.example.com/schedule/tomorrow",
        "clínica.com/consulta/agendar/amanhã",
        "please visit q\u0307.com to schedule today",
        "please visit q\u0307.com/path to schedule today",
        "please visit xn--q-9bb.com to schedule today",
        "please visit xn--q-9bb.com/path to schedule today",
        "please visit clinic\u3002com to schedule today",
        "please visit clinic\u3002com/path to schedule today",
        f"please visit q\u0307.com/{_LONG_IDNA_PATH} to schedule today",
        (
            f"please visit q\u0307.com:8443/{_LONG_IDNA_PATH}"
            "?slot=1#book to schedule today"
        ),
        f"please visit clinic\u3002com/{_LONG_IDNA_PATH} to schedule today",
        (
            f"please visit clinic\u3002com:8443/{_LONG_IDNA_PATH}"
            "?slot=1#book to schedule today"
        ),
    ],
)
def test_injected_detection_cannot_bypass_input_guards(text):
    result = decide_language_switch(
        text=text,
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        detection=_detection("en"),
    )

    assert result.decision == "abstain"
    assert result.detected_language is None
    assert result.confidence is None
    assert result.margin is None


@pytest.mark.parametrize(
    ("active", "detected", "pending", "prompt"),
    [
        ("pt-BR", "en", "en", "Would you like to continue in English?"),
        ("en", "pt-BR", "pt-BR", "Deseja continuar em português?"),
    ],
)
def test_language_change_is_proposed_without_immediate_switch(
    active, detected, pending, prompt
):
    result = decide_language_switch(
        text="this message has enough alphabetic tokens",
        configured_language=active,
        active_language=active,
        pending_language=None,
        detection=_detection(detected),
    )

    assert result.decision == "propose"
    assert result.active_language == active
    assert result.pending_language == pending
    assert result.prompt == prompt
    assert result.state_updates == {
        "active_language": active,
        "pending_language": pending,
    }


@pytest.mark.parametrize(
    ("pending", "text", "expected_active"),
    [
        ("en", "yes", "en"),
        ("en", " YES PLEASE!? ", "en"),
        ("pt-BR", "sim", "pt-BR"),
        ("pt-BR", " SIM POR FAVOR. ", "pt-BR"),
    ],
)
def test_pending_language_accepts_only_declared_phrases(pending, text, expected_active):
    previous = "pt-BR" if pending == "en" else "en"
    result = decide_language_switch(
        text=text,
        configured_language=previous,
        active_language=previous,
        pending_language=pending,
        detection=None,
    )

    assert result.decision == "accept"
    assert result.active_language == expected_active
    assert result.pending_language is None


@pytest.mark.parametrize(
    ("pending", "text"),
    [
        ("en", "não"),
        ("en", "não obrigado!"),
        ("pt-BR", "no"),
        ("pt-BR", "no thanks."),
        ("en", "no"),
        ("en", "no thanks."),
        ("pt-BR", "não"),
        ("pt-BR", "não obrigado!"),
    ],
)
def test_exact_refusals_clear_any_pending_language_without_switch(pending, text):
    active = "pt-BR" if pending == "en" else "en"
    result = decide_language_switch(
        text=text,
        configured_language=active,
        active_language=active,
        pending_language=pending,
        detection=None,
    )

    assert result.decision == "reject"
    assert result.active_language == active
    assert result.pending_language is None


@pytest.mark.parametrize(
    ("pending", "text"),
    [
        ("en", "yes sim"),
        ("en", "yes appointment"),
        ("en", "nao"),
        ("en", "nao obrigado!"),
        ("en", "no thanks please"),
        ("pt-BR", "sim yes"),
        ("pt-BR", "sim consulta"),
        ("pt-BR", "nao"),
        ("pt-BR", "nao obrigado!"),
        ("pt-BR", "não obrigado sim"),
    ],
)
def test_mixed_or_substantive_pending_reply_is_ignored_and_cleared(pending, text):
    active = "pt-BR" if pending == "en" else "en"
    result = decide_language_switch(
        text=text,
        configured_language=active,
        active_language=active,
        pending_language=pending,
        detection=None,
    )

    assert result.decision == "ignore"
    assert result.active_language == active
    assert result.pending_language is None


@pytest.mark.parametrize(
    ("confidence", "margin"),
    [
        (0.79, 0.40),
        (0.95, 0.19),
        (None, 0.40),
        (0.95, None),
    ],
)
def test_injected_detection_below_required_evidence_abstains(confidence, margin):
    result = decide_language_switch(
        text="this message has enough alphabetic tokens",
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        detection=LanguageDetection("en", confidence, margin),
    )

    assert result.decision == "abstain"
    assert result.detected_language == "en"
    assert result.confidence == confidence
    assert result.margin == margin


def test_raw_winner_is_preserved_when_detector_evidence_is_below_threshold(mocker):
    detector = MagicMock()
    detector.compute_language_confidence_values.return_value = [
        MagicMock(language=Language.ENGLISH, value=0.79),
        MagicMock(language=Language.PORTUGUESE, value=0.39),
    ]
    mocker.patch(
        "src.services.conversation_language.get_language_detector",
        return_value=detector,
    )
    service_logger = MagicMock()
    mocker.patch("src.services.conversation_language.logger", service_logger)

    detection = detect_language("this message has enough alphabetic tokens")
    result = evaluate_conversation_language(
        text="this message has enough alphabetic tokens",
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        tenant_id="tenant-1",
        session_id="session-1",
        detection=detection,
    )

    assert detection.language == "en"
    assert detection.eligible_for_switch is False
    assert result.decision == "abstain"
    assert result.detected_language == "en"
    assert service_logger.info.call_args.kwargs["detected_language"] == "en"


@pytest.mark.parametrize(
    ("confidence", "margin"),
    [
        (0.80, 0.20),
        (0.81, 0.21),
    ],
)
def test_injected_detection_at_or_above_required_evidence_can_propose(
    confidence, margin
):
    result = decide_language_switch(
        text="this message has enough alphabetic tokens",
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        detection=LanguageDetection("en", confidence, margin),
    )

    assert result.decision == "propose"
    assert result.detected_language == "en"
    assert result.confidence == confidence
    assert result.margin == margin


def test_same_or_non_switchable_detected_language_is_ignored():
    same = decide_language_switch(
        text="esta mensagem tem palavras suficientes agora",
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        detection=_detection("pt-BR"),
    )
    spanish = decide_language_switch(
        text="este mensaje tiene suficientes palabras ahora",
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        detection=_detection("es"),
    )

    assert same.decision == "ignore"
    assert spanish.decision == "ignore"
    assert same.pending_language is None
    assert spanish.pending_language is None


def test_invalid_state_fails_closed_to_configured_language():
    result = decide_language_switch(
        text="a sufficiently long English sentence for detection",
        configured_language="en",
        active_language="invalid",
        pending_language="es",
        detection=_detection("en"),
    )

    assert result.decision == "ignore"
    assert result.active_language == "en"
    assert result.pending_language is None


def test_logging_uses_exact_allowlist_and_never_logs_message_or_pii(mocker):
    service_logger = MagicMock()
    mocker.patch("src.services.conversation_language.logger", service_logger)
    private_text = "Hello, I am Maria, phone +55 11 99999-0000"

    result = evaluate_conversation_language(
        text=private_text,
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        tenant_id="tenant-1",
        session_id="session-1",
        detection=_detection("en"),
    )

    assert result.decision == "propose"
    service_logger.info.assert_called_once()
    (event,) = service_logger.info.call_args.args
    fields = service_logger.info.call_args.kwargs
    assert event == "conversation_language_decision"
    assert set(fields) == {
        "tenant_id",
        "session_id",
        "configured_language",
        "active_language",
        "detected_language",
        "confidence",
        "margin",
        "decision",
    }
    assert fields == {
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "configured_language": "pt-BR",
        "active_language": "pt-BR",
        "detected_language": "en",
        "confidence": 0.95,
        "margin": 0.4,
        "decision": "propose",
    }
    serialized_log = repr(service_logger.info.call_args)
    assert private_text not in serialized_log
    assert "Maria" not in serialized_log
    assert "+55" not in serialized_log


def test_log_rounds_confidence_and_margin(mocker):
    service_logger = MagicMock()
    mocker.patch("src.services.conversation_language.logger", service_logger)

    evaluate_conversation_language(
        text="any private message with enough words",
        configured_language="pt-BR",
        active_language="pt-BR",
        pending_language=None,
        tenant_id="tenant-1",
        session_id="session-1",
        detection=LanguageDetection("en", 0.98765, 0.34567),
    )

    fields = service_logger.info.call_args.kwargs
    assert fields["confidence"] == 0.99
    assert fields["margin"] == 0.35
