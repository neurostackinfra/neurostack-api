"""
NeuroStack - SRM - Intent Classifier (Naive Bayes)
Multinomial Naive Bayes classifier for intent classification.
"""

import math
import re
import unicodedata
from collections import Counter
from typing import Optional

from neurostack_core.srm.models import IntentCategory, IntentResult


class IntentNaiveBayes:
    """
    Multinomial Naive Bayes classifier for intent classification.

    Similar to SentimentClassifier but adapted for multiple intent classes.
    """

    def __init__(
        self,
        training_data: Optional[dict[str, list[str]]] = None,
        smoothing: float = 1.0,
    ):
        """
        Initialize the Naive Bayes intent classifier.

        Args:
            training_data: Dictionary mapping intent names to lists of example messages
            smoothing: Laplace smoothing parameter (default: 1.0)
        """
        self._smoothing = smoothing
        self._vocab: set[str] = set()
        self._intent_counts: dict[str, int] = {}
        self._token_counts: dict[str, Counter[str]] = {}
        self._total_tokens: dict[str, int] = {}
        self._log_priors: dict[str, float] = {}
        self._log_likelihoods: dict[str, dict[str, float]] = {}
        self._default_log_prob: dict[str, float] = {}
        self._intent_categories: dict[str, IntentCategory] = {}

        if training_data:
            self._train(training_data)

    def classify(self, message: str) -> IntentResult:
        """
        Classify intent for a message.

        Args:
            message: User message to classify

        Returns:
            IntentResult with intent, category, and confidence
        """
        tokens = self._tokenize(message)
        if not tokens:
            return IntentResult(
                intent="unknown",
                category=IntentCategory.UNKNOWN,
                confidence=0.0,
                matched_patterns=[],
            )

        if not self._intent_counts:
            return IntentResult(
                intent="unknown",
                category=IntentCategory.UNKNOWN,
                confidence=0.0,
                matched_patterns=[],
            )

        log_scores: dict[str, float] = {}
        for intent_name in self._intent_counts:
            score = self._log_priors.get(intent_name, 0.0)
            token_probs = self._log_likelihoods.get(intent_name, {})
            default_prob = self._default_log_prob.get(intent_name, 0.0)

            for token in tokens:
                score += token_probs.get(token, default_prob)
            log_scores[intent_name] = score

        probabilities = self._softmax(log_scores)
        best_intent = max(probabilities, key=lambda intent: probabilities[intent])
        confidence = probabilities[best_intent]
        category = self._intent_categories.get(best_intent, IntentCategory.UNKNOWN)

        return IntentResult(
            intent=best_intent,
            category=category,
            confidence=round(confidence, 3),
            matched_patterns=[],  # Naive Bayes doesn't use patterns
        )

    def _train(self, data: dict[str, list[str]]) -> None:
        """
        Train model parameters from labeled data.

        Args:
            data: Dictionary mapping intent names to lists of example messages
        """
        total_docs = 0

        for intent_name, texts in data.items():
            if not texts:
                continue

            self._intent_counts[intent_name] = len(texts)
            total_docs += len(texts)
            counter: Counter[str] = Counter()

            for text in texts:
                tokens = self._tokenize(text)
                counter.update(tokens)
                self._vocab.update(tokens)

            self._token_counts[intent_name] = counter
            self._total_tokens[intent_name] = sum(counter.values())

        if total_docs == 0:
            return

        # Calculate log priors
        for intent_name, count in self._intent_counts.items():
            self._log_priors[intent_name] = math.log(count / total_docs)

        # Calculate log likelihoods with smoothing
        vocab_size = max(len(self._vocab), 1)
        for intent_name in self._intent_counts:
            total = self._total_tokens[intent_name]
            denom = total + self._smoothing * vocab_size
            self._default_log_prob[intent_name] = math.log(self._smoothing / denom)

            token_probs: dict[str, float] = {}
            for token in self._vocab:
                token_count = self._token_counts[intent_name].get(token, 0)
                token_probs[token] = math.log((token_count + self._smoothing) / denom)

            self._log_likelihoods[intent_name] = token_probs

    def set_intent_categories(self, categories: dict[str, IntentCategory]) -> None:
        """Set the category mapping for intents."""
        self._intent_categories = categories

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
        log_scores: dict[str, float],
    ) -> dict[str, float]:
        """Convert log-scores to probabilities."""
        if not log_scores:
            return {}

        max_log = max(log_scores.values())
        exp_scores = {k: math.exp(v - max_log) for k, v in log_scores.items()}
        total = sum(exp_scores.values()) or 1.0
        return {k: v / total for k, v in exp_scores.items()}

    def update_training_data(self, intent_name: str, examples: list[str]) -> None:
        """
        Update training data for a specific intent.

        This allows incremental learning by adding new examples.
        """
        if intent_name not in self._intent_counts:
            self._intent_counts[intent_name] = 0
            self._token_counts[intent_name] = Counter()
            self._total_tokens[intent_name] = 0

        for text in examples:
            tokens = self._tokenize(text)
            self._token_counts[intent_name].update(tokens)
            self._vocab.update(tokens)
            self._intent_counts[intent_name] += 1
            self._total_tokens[intent_name] += len(tokens)

        # Retrain with updated data
        total_docs = sum(self._intent_counts.values())
        if total_docs > 0:
            for intent_name in self._intent_counts:
                count = self._intent_counts[intent_name]
                self._log_priors[intent_name] = math.log(count / total_docs)

            vocab_size = max(len(self._vocab), 1)
            for intent_name in self._intent_counts:
                total = self._total_tokens[intent_name]
                denom = total + self._smoothing * vocab_size
                self._default_log_prob[intent_name] = math.log(self._smoothing / denom)

                token_probs: dict[str, float] = {}
                for token in self._vocab:
                    token_count = self._token_counts[intent_name].get(token, 0)
                    token_probs[token] = math.log(
                        (token_count + self._smoothing) / denom
                    )

                self._log_likelihoods[intent_name] = token_probs
