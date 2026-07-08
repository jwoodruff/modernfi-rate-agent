import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from app import db
from app.agent import run_agent
from app.models import QueryRecord, QuestionRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"message": "Hello World!"}


@app.get("/health")
async def health_check():
    """Liveness/readiness check for the ALB — confirms the app can actually
    reach Postgres, not just that the process is running."""
    try:
        await db.check_connection()
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unreachable", "error": str(e)},
        )

    return {"status": "healthy", "database": "connected"}


@app.get("/history", response_model=list[QueryRecord])
async def get_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows = await db.fetch_history(limit=limit, offset=offset)

    return [
        QueryRecord(
            id=row["id"],
            question=row["question"],
            answer=row["answer"],
            status=row["status"],
            created_at=row["created_at"],
            iterations=row["iterations"],
            tool_calls=row["tool_calls"],
            duration_ms=row["duration_ms"],
            tools_used=json.loads(row["tools_used"]),
        )
        for row in rows
    ]


@app.post("/ask")
async def ask_question(question: QuestionRequest):
    result = run_agent(question.question)

    await db.save_query(
        question=question.question,
        answer=result.answer,
        status=result.status,
        iterations=result.iterations,
        tool_calls=result.tool_calls,
        duration_ms=result.duration_ms,
        tools_used=result.tools_used,
    )

    if result.status == "max_iterations":
        return {
            "answer": "I wasn't able to find an answer within the allowed number of steps."
        }

    if result.status == "error":
        return JSONResponse(
            status_code=503,
            content={
                "answer": "The agent is temporarily unavailable — please try again shortly."
            },
        )

    return {"answer": result.answer}
