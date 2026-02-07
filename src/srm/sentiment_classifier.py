"""
NeuroStack - SRM - Sentiment Classifier
Classic ML sentiment classifier using Multinomial Naive Bayes.
"""

import math
import re
import unicodedata
from collections import Counter
from typing import Optional

from src.srm.models import SentimentLabel, SentimentResult


DEFAULT_TRAINING_DATA: dict[SentimentLabel, list[str]] = {
    SentimentLabel.POSITIVE: [
        "otimo atendimento",
        "muito bom obrigado",
        "excelente experiencia",
        "gostei muito",
        "perfeito atendimento",
        "foi rapido e eficiente",
        "adorei o suporte",
        "muito satisfeito",
        "que bom",
        "top atendimento",
    ],
    SentimentLabel.NEGATIVE: [
        "pessimo atendimento",
        "muito ruim",
        "demorou demais",
        "estou insatisfeito",
        "nao gostei",
        "experiencia horrivel",
        "que problema",
        "atendimento terrivel",
        "frustrado com o suporte",
        "ruim demais",
    ],
    SentimentLabel.NEUTRAL: [
        "qual o horario de funcionamento",
        "preciso de informacao",
        "gostaria de saber o valor",
        "posso agendar uma consulta",
        "qual o endereco",
        "quais servicos voces oferecem",
        "aceita convenio",
        "quero remarcar a consulta",
        "confirmo o horario",
        "ok obrigado",
    ],
}


class SentimentClassifier:
    """
    Classic ML sentiment classifier using Multinomial Naive Bayes.
    """

    def __init__(
        self,
        training_data: Optional[dict[SentimentLabel, list[str]]] = None,
        smoothing: float = 1.0,
    ):
        self._smoothing = smoothing
        self._vocab: set[str] = set()
        self._label_counts: dict[SentimentLabel, int] = {}
        self._token_counts: dict[SentimentLabel, Counter[str]] = {}
        self._total_tokens: dict[SentimentLabel, int] = {}
        self._log_priors: dict[SentimentLabel, float] = {}
        self._log_likelihoods: dict[SentimentLabel, dict[str, float]] = {}
        self._default_log_prob: dict[SentimentLabel, float] = {}

        data = training_data or DEFAULT_TRAINING_DATA
        self._train(data)

    def classify(self, message: str) -> SentimentResult:
        """Classify sentiment for a message."""
        tokens = self._tokenize(message)
        if not tokens:
            return SentimentResult(
                label=SentimentLabel.NEUTRAL,
                score=0.0,
                confidence=0.0,
                probabilities={},
            )

        log_scores: dict[SentimentLabel, float] = {}
        for label in self._label_counts:
            score = self._log_priors[label]
            token_probs = self._log_likelihoods[label]
            default_prob = self._default_log_prob[label]
            for token in tokens:
                score += token_probs.get(token, default_prob)
            log_scores[label] = score

        probabilities = self._softmax(log_scores)
        best_label = max(probabilities, key=probabilities.get)
        confidence = probabilities[best_label]

        pos = probabilities.get(SentimentLabel.POSITIVE, 0.0)
        neg = probabilities.get(SentimentLabel.NEGATIVE, 0.0)
        score = pos - neg

        return SentimentResult(
            label=best_label,
            score=round(score, 3),
            confidence=round(confidence, 3),
            probabilities={k.value: round(v, 4) for k, v in probabilities.items()},
        )

    def _train(self, data: dict[SentimentLabel, list[str]]) -> None:
        """Train model parameters from labeled data."""
        total_docs = 0

        for label, texts in data.items():
            self._label_counts[label] = len(texts)
            total_docs += len(texts)
            counter: Counter[str] = Counter()
            for text in texts:
                tokens = self._tokenize(text)
                counter.update(tokens)
                self._vocab.update(tokens)
            self._token_counts[label] = counter
            self._total_tokens[label] = sum(counter.values())

        for label, count in self._label_counts.items():
            self._log_priors[label] = math.log(count / total_docs) if total_docs else 0.0

        vocab_size = max(len(self._vocab), 1)
        for label in self._label_counts:
            total = self._total_tokens[label]
            denom = total + self._smoothing * vocab_size
            self._default_log_prob[label] = math.log(self._smoothing / denom)
            token_probs: dict[str, float] = {}
            for token in self._vocab:
                token_count = self._token_counts[label][token]
                token_probs[token] = math.log((token_count + self._smoothing) / denom)
            self._log_likelihoods[label] = token_probs

    def _tokenize(self, text: str) -> list[str]:
        """Normalize and tokenize text."""
        normalized = unicodedata.normalize("NFKD", text)
        normalized = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        normalized = normalized.lower()
        return re.findall(r"[a-z0-9]+", normalized)

    def _softmax(
        self,
        log_scores: dict[SentimentLabel, float],
    ) -> dict[SentimentLabel, float]:
        """Convert log-scores to probabilities."""
        max_log = max(log_scores.values())
        exp_scores = {k: math.exp(v - max_log) for k, v in log_scores.items()}
        total = sum(exp_scores.values()) or 1.0
        return {k: v / total for k, v in exp_scores.items()}


# ============================================
# Singleton Instance
# ============================================

_classifier: Optional[SentimentClassifier] = None


def get_sentiment_classifier() -> SentimentClassifier:
    """Get or create the global SentimentClassifier instance."""
    global _classifier
    if _classifier is None:
        _classifier = SentimentClassifier()
    return _classifier
