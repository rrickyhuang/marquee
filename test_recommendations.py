"""Tests for recommendations.py helpers."""
import random
from difflib import SequenceMatcher

import pytest

import recommendations


@pytest.mark.parametrize("a,b", [
    ("", ""),
    ("alien", ""),
    ("alien", "alien"),
    ("toy story 5", "tokyo story"),
    ("crossing", "crashing"),
    ("amelie", "alien"),
    ("little darlings", "little prince"),
])
@pytest.mark.parametrize("threshold", [0.5, 0.7, 0.82, 0.95, 1.0])
def test_ratio_at_least_matches_plain_ratio(a, b, threshold):
    assert recommendations.ratio_at_least(a, b, threshold) == (
        SequenceMatcher(None, a, b).ratio() >= threshold
    )


def test_ratio_at_least_matches_plain_ratio_on_random_titles():
    rng = random.Random(1234)
    words = ["the", "story", "night", "alien", "blade", "runner", "toy", "sun", "red", "love", "2049", "ii"]
    def title():
        return " ".join(rng.choice(words) for _ in range(rng.randint(1, 4)))
    for _ in range(2000):
        a, b = title(), title()
        for threshold in (0.7, 0.82, 0.9):
            assert recommendations.ratio_at_least(a, b, threshold) == (
                SequenceMatcher(None, a, b).ratio() >= threshold
            ), (a, b, threshold)
