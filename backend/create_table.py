import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
from db.base import Base
from db.database import engine
import db.models  # this loads all models so tables are registered

async def main():
    print("Connecting to database...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Done! All tables created successfully.")

asyncio.run(main())