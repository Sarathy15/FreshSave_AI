from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.database import close_mongo_connection, connect_to_mongo, ping_database
from app.routers import products, inventory, pricing, deals, simulation, dashboard, sales, settings, seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    monitor_task = None
    try:
        from app.database import init_database_indexes, migrate_database
        await migrate_database()
        await init_database_indexes()
        from app.routers.seed import seed_database
        await seed_database()
        import asyncio
        from app.scheduler import start_periodic_monitoring_worker
        monitor_task = asyncio.create_task(start_periodic_monitoring_worker(interval_seconds=14400))
        yield
    finally:
        if monitor_task:
            monitor_task.cancel()
        await close_mongo_connection()


app = FastAPI(
    title="FreshSave AI API",
    description=(
        "Shared API for the FreshSave AI retailer and customer websites. "
        "Simulation data must remain separate from live inventory and orders."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Local development origins. Update these for your actual frontend URLs.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(products.router)
app.include_router(inventory.router)
app.include_router(pricing.router)
app.include_router(deals.router)
app.include_router(sales.router)
app.include_router(dashboard.router)
app.include_router(settings.router)
app.include_router(simulation.router)
app.include_router(seed.router)


@app.get("/", tags=["General"])
async def root():
    return {
        "project": "FreshSave AI",
        "message": "Backend is running",
        "docs": "/docs",
    }


@app.get("/health", tags=["General"])
async def health():
    try:
        await ping_database()
        return {"status": "healthy", "database": "connected"}
    except Exception as exc:
        # Avoid returning credentials or internal connection details to clients.
        raise HTTPException(
            status_code=503,
            detail="Database connection failed. Check server logs and configuration.",
        ) from exc
