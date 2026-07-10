"""FastAPI routers — one module per API surface, all thin translation layers.

Routers translate HTTP (auth dependencies, status codes, response models) to
service calls and back; no business logic lives here (that is services/).

How:   pytest tests/unit/api/ -v
"""
