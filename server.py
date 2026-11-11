"""FastAPI server for the Fishing Tracker progressive web app."""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


APP_DIRECTORY = Path(__file__).resolve().parent
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SPECIES = 150

app = FastAPI(title="Fishing Tracker API", version="0.1.0")
allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


class IdentifyRequest(BaseModel):
    image_data: str = Field(max_length=MAX_IMAGE_BYTES * 2)
    content_type: str
    species_options: list[str] = Field(min_length=1, max_length=MAX_SPECIES)


class SpeciesSuggestion(BaseModel):
    species: str
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=240)


class IdentifyResponse(BaseModel):
    suggestions: list[SpeciesSuggestion]
    needs_better_photo: bool


def _read_json(url: str, headers: dict[str, str], body: bytes) -> dict:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as error:
        raise HTTPException(
            status_code=502, detail="The species recognition service rejected the request."
        ) from error
    except (URLError, TimeoutError) as error:
        raise HTTPException(
            status_code=502, detail="The species recognition service is unavailable."
        ) from error
    except (json.JSONDecodeError, OSError) as error:
        raise HTTPException(
            status_code=502, detail="The species recognition service returned an invalid response."
        ) from error


def _verify_supabase_user(access_token: str, project_url: str, anon_key: str) -> None:
    request = Request(
        f"{project_url.rstrip('/')}/auth/v1/user",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {access_token}",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise HTTPException(status_code=401, detail="Sign in to identify a fish.")
    except HTTPError as error:
        if error.code in (401, 403):
            raise HTTPException(
                status_code=401, detail="Your session expired. Sign in again."
            ) from error
        raise HTTPException(
            status_code=502, detail="Could not verify your account with the sync service."
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise HTTPException(
            status_code=502, detail="Could not verify your account with the sync service."
        ) from error


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/identify", response_model=IdentifyResponse)
def identify_species(
    payload: IdentifyRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> IdentifyResponse:
    project_url = os.environ.get("SUPABASE_URL", "").strip()
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not project_url or not anon_key:
        raise HTTPException(
            status_code=503, detail="Cloud sign-in is not configured on the server."
        )
    if not api_key:
        raise HTTPException(
            status_code=503, detail="AI species suggestions are not configured yet."
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in before requesting an AI suggestion.")

    content_type = payload.content_type.lower().strip()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=415, detail="Choose a JPEG, PNG, or WebP photo."
        )
    try:
        image_bytes = base64.b64decode(payload.image_data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=400, detail="The photo data is invalid.") from error
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413, detail="Choose a photo smaller than 8 MB."
        )

    access_token = authorization.removeprefix("Bearer ").strip()
    _verify_supabase_user(access_token, project_url, anon_key)

    species_options = list(dict.fromkeys(name.strip() for name in payload.species_options))
    species_options = [name for name in species_options if name][:MAX_SPECIES]
    if not species_options:
        raise HTTPException(status_code=400, detail="No species choices were provided.")

    image_url = (
        f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    request_body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a cautious freshwater fish identification assistant. "
                        "Only suggest names from the user's allowed species list. "
                        "Do not guess confidently when the photo is unclear. "
                        "Return JSON with keys suggestions and needs_better_photo. "
                        "suggestions must be a list of up to 3 objects with species, "
                        "confidence (a rough model estimate between 0 and 1), and "
                        "reason (brief visual evidence). Set needs_better_photo true "
                        "when the fish is not visible or identification is too uncertain. "
                        "A suggested species must still be confirmed by the user."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Allowed species: "
                                + ", ".join(species_options)
                                + ". Identify the likely freshwater fish in this photo."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url, "detail": "low"},
                        },
                    ],
                },
            ],
        }
    ).encode("utf-8")
    upstream = _read_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        request_body,
    )
    try:
        content = upstream["choices"][0]["message"]["content"]
        result = json.loads(content)
        allowed_by_casefold = {name.casefold(): name for name in species_options}
        suggestions: list[SpeciesSuggestion] = []
        for item in result.get("suggestions", [])[:3]:
            matched_name = allowed_by_casefold.get(str(item.get("species", "")).casefold())
            if matched_name is None:
                continue
            suggestions.append(
                SpeciesSuggestion(
                    species=matched_name,
                    confidence=max(0.0, min(1.0, float(item.get("confidence", 0)))),
                    reason=str(item.get("reason", "Visual estimate"))[:240],
                )
            )
        return IdentifyResponse(
            suggestions=suggestions,
            needs_better_photo=bool(result.get("needs_better_photo", not suggestions)),
        )
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=502, detail="The AI service returned an invalid species suggestion."
        ) from error


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(APP_DIRECTORY / "index.html")


@app.get("/config.js", include_in_schema=False)
def config() -> Response:
    config_path = APP_DIRECTORY / "config.js"
    if config_path.is_file():
        return FileResponse(config_path, media_type="application/javascript")
    return PlainTextResponse(
        (APP_DIRECTORY / "config.example.js").read_text(encoding="utf-8"),
        media_type="application/javascript",
    )


app.mount("/", StaticFiles(directory=APP_DIRECTORY), name="web")
