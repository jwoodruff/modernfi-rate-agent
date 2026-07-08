CREATE TABLE queries (
    id SERIAL PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'max_iterations', 'error')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    iterations INTEGER,
    tool_calls INTEGER,
    duration_ms INTEGER,
    tools_used JSONB
);