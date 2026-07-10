"""HTTP layer — everything that speaks web to the outside world.

Contains the FastAPI app factory (app.py), authentication and connection
dependencies (dependencies.py), routers/ and schemas/. Kept separate so
business logic in services/ never imports a web framework.

How:   pytest tests/unit/api/ -v
"""
