"""Gateway route modules.

Each module exports a ``router: APIRouter`` that the FastAPI app factory
includes via ``app.include_router(...)``. Splitting the routes into modules
keeps ``omni_comfy_server.py`` focused on app setup, lifespan, and middleware.
"""
