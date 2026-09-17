"""Business logic — the rules of how Kerno works, independent of any framework.

Services here orchestrate the models and database helpers: setting tenant
context, capturing overrides, anonymising telemetry, recalculating each tenant's
search calibration, and running calibrated retrieval. No HTTP or framework code
lives in this layer.

How:   pytest tests/unit/services/ -v
"""
