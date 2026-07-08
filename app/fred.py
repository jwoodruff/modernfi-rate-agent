import os

import httpx


def get_fred_data(
    series_id: str,
    observation_start: str | None = None,
    observation_end: str | None = None,
) -> dict:
    params = {
        "series_id": series_id,
        "api_key": os.environ.get("FRED_API_KEY"),
        "file_type": "json",
    }

    if observation_start or observation_end:
        # Historical range requested — fetch the actual data for that period
        # instead of just the latest observation.
        if observation_start:
            params["observation_start"] = observation_start
        if observation_end:
            params["observation_end"] = observation_end
    else:
        # No dates provided — default to just the latest observation.
        params["sort_order"] = "desc"
        params["limit"] = 1

    try:
        response = httpx.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params=params,
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        return {"error": f"FRED API timed out fetching data for '{series_id}'"}
    except httpx.HTTPStatusError as e:
        return {
            "error": f"FRED API returned {e.response.status_code} for '{series_id}'. "
            "The series_id or date range may be invalid."
        }
    except httpx.RequestError as e:
        return {"error": f"Network error contacting FRED API: {e}"}

    data = response.json()
    observations = data.get("observations", [])

    if not observations:
        return {
            "error": (
                f"No observations found for series_id '{series_id}' "
                f"in the requested date range"
                if (observation_start or observation_end)
                else f"No observations found for series_id '{series_id}'"
            )
        }

    if observation_start or observation_end:
        # Return the full set of observations for the requested period.
        return {
            "observations": [
                {"date": o["date"], "value": o["value"]} for o in observations
            ]
        }

    latest = observations[0]
    return {"date": latest["date"], "value": latest["value"]}


def search_fred_series(search_text: str) -> dict | list:
    try:
        response = httpx.get(
            "https://api.stlouisfed.org/fred/series/search",
            params={
                "search_text": search_text,
                "api_key": os.environ.get("FRED_API_KEY"),
                "file_type": "json",
                "limit": 5,
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        return {"error": f"FRED API timed out searching for '{search_text}'"}
    except httpx.HTTPStatusError as e:
        return {"error": f"FRED API returned {e.response.status_code} while searching"}
    except httpx.RequestError as e:
        return {"error": f"Network error contacting FRED API: {e}"}

    data = response.json()
    seriess = data.get("seriess", [])

    if not seriess:
        return {"error": f"No series found matching '{search_text}'"}

    return [
        {"id": s["id"], "title": s["title"], "frequency": s["frequency"]}
        for s in seriess
    ]
