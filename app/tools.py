from anthropic.types import ToolParam

from app.fred import get_fred_data, search_fred_series

tools: list[ToolParam] = [
    {
        "name": "search_fred_series",
        "description": (
            "Search the FRED (Federal Reserve Economic Data) database for economic data series "
            "matching a text query. Use this FIRST when you don't already know the exact FRED "
            "series ID for the data you need — e.g. when the user asks about a concept like "
            "'30 year mortgage rates', 'federal funds rate', 'unemployment rate', or 'CPI inflation' "
            "rather than a specific series code. Returns up to 5 candidate series, each with an "
            "id, title, and frequency (e.g. Daily, Weekly, Monthly). Inspect the titles to pick the "
            "series that best matches the user's question, then pass its id to get_fred_data to "
            "fetch the actual values. If the results don't look relevant, try again with different "
            "or more general search terms."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search_text": {
                    "type": "string",
                    "description": (
                        "Plain-language keywords describing the economic data series to find, e.g. "
                        "'30 year fixed mortgage rate', 'unemployment rate', 'effective federal funds "
                        "rate', or '10 year treasury yield'. Use natural terms a user might say, not "
                        "a FRED series code — this is a free-text search, not an exact ID lookup."
                    ),
                }
            },
            "required": ["search_text"],
        },
    },
    {
        "name": "get_fred_data",
        "description": (
            "Fetch observations for a specific FRED economic data series. Requires an exact "
            "series_id (e.g. 'MORTGAGE30US', 'FEDFUNDS', 'CPIAUCSL') — if you don't already know "
            "the series_id for what the user is asking about, call search_fred_series first to "
            "find it, then pass the chosen id here.\n\n"
            "By default (no dates provided), this returns only the single most recent observation "
            "— use this for 'current' or 'latest' questions.\n\n"
            "To answer questions about a specific past date or time period (e.g. 'what was the "
            "unemployment rate in January 1960'), pass observation_start and/or observation_end to "
            "fetch the actual historical data for that period instead of the latest value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "series_id": {
                    "type": "string",
                    "description": (
                        "The exact FRED series identifier to fetch data for, e.g. 'MORTGAGE30US' or "
                        "'FEDFUNDS'. This must come from a known series code or from the 'id' field "
                        "of a result returned by search_fred_series — do not guess or invent one."
                    ),
                },
                "observation_start": {
                    "type": "string",
                    "description": (
                        "Optional. The earliest date to include, in YYYY-MM-DD format (e.g. "
                        "'1960-01-01'). Only provide this when the user is asking about a specific "
                        "past date or period rather than the current/latest value. Omit for "
                        "'current' or 'latest' questions."
                    ),
                },
                "observation_end": {
                    "type": "string",
                    "description": (
                        "Optional. The latest date to include, in YYYY-MM-DD format (e.g. "
                        "'1960-01-31'). Typically used together with observation_start to bound a "
                        "specific historical period. Omit for 'current' or 'latest' questions."
                    ),
                },
            },
            "required": ["series_id"],
        },
    },
]


def call_tool(name: str, tool_input: dict[str, object]) -> dict | list:
    """Dispatch a tool call by name, validating and casting the untyped
    tool_input dict into the specific argument types each function expects."""
    if name == "search_fred_series":
        search_text = tool_input.get("search_text")
        if not isinstance(search_text, str):
            return {"error": "search_fred_series requires a string 'search_text'"}
        return search_fred_series(search_text)

    if name == "get_fred_data":
        series_id = tool_input.get("series_id")
        if not isinstance(series_id, str):
            return {"error": "get_fred_data requires a string 'series_id'"}

        observation_start = tool_input.get("observation_start")
        if observation_start is not None and not isinstance(observation_start, str):
            return {"error": "observation_start must be a string in YYYY-MM-DD format"}

        observation_end = tool_input.get("observation_end")
        if observation_end is not None and not isinstance(observation_end, str):
            return {"error": "observation_end must be a string in YYYY-MM-DD format"}

        return get_fred_data(series_id, observation_start, observation_end)

    return {"error": f"Unknown tool: {name}"}
