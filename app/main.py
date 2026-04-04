from fastapi import FastAPI
from app.routes import ai, messaging, calling

app = FastAPI()

app.include_router(ai.router, prefix="/ai")
app.include_router(messaging.router, prefix="/messages")
app.include_router(calling.router, prefix="/calls")