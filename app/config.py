from dotenv import load_dotenv
import os

load_dotenv()

class Settings:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
    TOKEN_LIMIT = int(os.getenv("TOKEN_LIMIT", 100000))
    LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
    LIVEKIT_SECRET = os.getenv("LIVEKIT_SECRET")
    DAILY_API_KEY = os.getenv("DAILY_API_KEY")
    SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")

settings = Settings()
