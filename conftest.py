"""
Pytest configuration: stubs load_config() so the scripts can be imported
without a real config.yaml present.
"""
from unittest.mock import patch

MINIMAL_CFG = {
    "letterboxd": {"username": "testuser"},
    "email": {"from": "a@b.com", "to": "a@b.com"},
    "location": "Test City",
    "theatres": [],
    "matching": {
        "threshold": 0.82,
        "uncertain_floor": 0.70,
        "exclude_threshold": 0.82,
        "recommendation_score": 0.75,
    },
    "watchlist": {
        "lookahead_days": 8,
        "schedule_day": "sunday",
        "schedule_time": "20:00",
    },
    "recommendations": {
        "min": 2,
        "max": 6,
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "schedule_day": 1,
        "schedule_time": "21:00",
    },
    "taste_profile": {"description": "", "five_star_films": [], "liked_films": []},
}

patch("yaml.safe_load", return_value=MINIMAL_CFG).start()
