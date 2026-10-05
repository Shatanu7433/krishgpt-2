"""
KrishiGPT (Groq edition) - farming AI agent for Indian farmers (Hindi / Marathi / English).

Tools the agent can call:
  1. get_weather      - live weather + 5-day forecast (Open-Meteo, no key needed)
  2. get_crop_advice  - season / soil / water based crop suggestions

Setup:
    pip install groq requests
    export GROQ_API_KEY="gsk_..."      # free key: console.groq.com/keys

Run:
    python krishigpt.py
"""

import json
import os
import sys

import requests
from groq import Groq

MODEL = "llama-3.3-70b-versatile"  # tool-capable; fallback: llama-3.1-8b-instant
MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """You are KrishiGPT, a practical farming assistant for Indian farmers.

- Reply in the language the farmer writes in: Hindi, Marathi or English (match their script too).
- Keep answers short, simple and actionable. Avoid jargon; use local units (quintal, acre, kg).
- Use tools for anything that depends on live data: weather or crop selection.
  Never guess forecasts. If a tool fails, say so honestly.
- You have no live mandi price data. If asked about prices, say so and point the farmer to the
  Agmarknet app, the eNAM portal, or their local APMC mandi.
- If the farmer has not said where they are, ask for their district/village once.
- For pesticide, fertiliser or disease questions, give general guidance and recommend confirming
  with the local Krishi Vigyan Kendra (KVK) or agriculture officer before spraying.
- Give a clear next step at the end of each answer."""


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 80: "rain showers",
    81: "heavy showers", 82: "violent showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


def get_weather(location: str) -> dict:
    """Current weather and 5-day forecast for an Indian place name."""
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1, "country_code": "IN"},
        timeout=10,
    ).json()
    if not geo.get("results"):
        return {"error": f"Could not find location '{location}'. Try a nearby town or district."}
    place = geo["results"][0]

    w = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
            "forecast_days": 5,
            "timezone": "Asia/Kolkata",
        },
        timeout=10,
    ).json()

    cur, daily = w["current"], w["daily"]
    return {
        "place": f"{place['name']}, {place.get('admin1', '')}",
        "now": {
            "temp_c": cur["temperature_2m"],
            "humidity_pct": cur["relative_humidity_2m"],
            "rain_mm": cur["precipitation"],
            "wind_kmh": cur["wind_speed_10m"],
            "summary": WMO_CODES.get(cur["weather_code"], "unknown"),
        },
        "forecast": [
            {
                "date": daily["time"][i],
                "min_c": daily["temperature_2m_min"][i],
                "max_c": daily["temperature_2m_max"][i],
                "rain_mm": daily["precipitation_sum"][i],
                "summary": WMO_CODES.get(daily["weather_code"][i], "unknown"),
            }
            for i in range(len(daily["time"]))
        ],
    }


# Compact knowledge base: season -> crops with soil / water fit and sowing window.
CROP_KB = {
    "kharif": [
        {"crop": "Soybean", "soil": ["black", "loamy"], "water": ["low", "medium"], "sowing": "mid-June to mid-July"},
        {"crop": "Cotton", "soil": ["black"], "water": ["medium", "high"], "sowing": "June to early July"},
        {"crop": "Tur (pigeon pea)", "soil": ["black", "red", "loamy"], "water": ["low", "medium"], "sowing": "June to July"},
        {"crop": "Bajra", "soil": ["sandy", "red", "loamy"], "water": ["low"], "sowing": "late June to July"},
        {"crop": "Paddy", "soil": ["clay", "loamy"], "water": ["high"], "sowing": "June to July"},
        {"crop": "Maize", "soil": ["loamy", "red"], "water": ["medium"], "sowing": "June to July"},
    ],
    "rabi": [
        {"crop": "Wheat", "soil": ["loamy", "black", "clay"], "water": ["medium", "high"], "sowing": "November to mid-December"},
        {"crop": "Chickpea (harbhara)", "soil": ["black", "loamy"], "water": ["low", "medium"], "sowing": "October to mid-November"},
        {"crop": "Jowar (rabi)", "soil": ["black"], "water": ["low"], "sowing": "late September to October"},
        {"crop": "Onion", "soil": ["loamy", "red"], "water": ["medium", "high"], "sowing": "transplant November to December"},
        {"crop": "Safflower", "soil": ["black"], "water": ["low"], "sowing": "late September to October"},
    ],
    "zaid": [
        {"crop": "Watermelon", "soil": ["sandy", "loamy"], "water": ["medium"], "sowing": "February to March"},
        {"crop": "Cucumber", "soil": ["loamy", "sandy"], "water": ["medium"], "sowing": "February to March"},
        {"crop": "Green gram (moong)", "soil": ["loamy", "red"], "water": ["low", "medium"], "sowing": "March to April"},
        {"crop": "Fodder maize", "soil": ["loamy"], "water": ["medium", "high"], "sowing": "March to April"},
    ],
}


def get_crop_advice(season: str, soil_type: str, water_availability: str) -> dict:
    """Suggest crops that fit the season, soil and water situation."""
    season, soil, water = season.lower(), soil_type.lower(), water_availability.lower()
    if season not in CROP_KB:
        return {"error": f"Season must be one of {list(CROP_KB)}"}

    matches = [c for c in CROP_KB[season] if soil in c["soil"] and water in c["water"]]
    near = [c for c in CROP_KB[season] if c not in matches and (soil in c["soil"] or water in c["water"])]
    return {
        "season": season,
        "best_fit": matches,
        "partial_fit": near,
        "note": "General guidance only. Confirm variety and sowing date with the local KVK.",
    }


# --------------------------------------------------------------------------- #
# Tool schemas + dispatch (OpenAI-style format used by Groq)
# --------------------------------------------------------------------------- #

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather and a 5-day forecast for a village, town or district in India.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Place name, e.g. 'Pune' or 'Baramati'"}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_crop_advice",
            "description": "Recommend crops for a season, soil type and water availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "season": {"type": "string", "enum": ["kharif", "rabi", "zaid"]},
                    "soil_type": {"type": "string", "enum": ["black", "red", "loamy", "sandy", "clay"]},
                    "water_availability": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["season", "soil_type", "water_availability"],
            },
        },
    },
]

DISPATCH = {
    "get_weather": get_weather,
    "get_crop_advice": get_crop_advice,
}


def run_tool(name: str, args: dict) -> str:
    fn = DISPATCH.get(name)
    if not fn:
        return json.dumps({"error": f"Unknown tool {name}"}, ensure_ascii=False)
    try:
        return json.dumps(fn(**args), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"{name} failed: {e}"}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------------- #

def ask(client: Groq, history: list, user_text: str) -> str:
    """Send one farmer message, run tool calls until the model is done, return the reply."""
    history.append({"role": "user", "content": user_text})

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=1500,
            )
        except Exception as e:
            history.pop()  # drop the failed turn so the chat can continue
            return f"Sorry, the AI service returned an error: {e}"

        msg = resp.choices[0].message

        if not msg.tool_calls:
            history.append({"role": "assistant", "content": msg.content or ""})
            return msg.content or ""

        history.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"  [tool] {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": run_tool(tc.function.name, args),
            })

    return "Sorry, I could not finish that request. Please try again."


def main() -> None:
    if not os.environ.get("GROQ_API_KEY"):
        sys.exit("Set GROQ_API_KEY first.")

    client = Groq()
    history: list = []
    print("KrishiGPT | नमस्कार! Ask in Hindi, Marathi or English. Type 'exit' to quit.\n")

    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit", "q"}:
            break
        if not text:
            continue
        print(f"\nKrishiGPT: {ask(client, history, text)}\n")


if __name__ == "__main__":
    main()
