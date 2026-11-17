"""Talks to the AI. Tries your main model first, then your backups, so one rate limit does not stop the app.

generate_text() is the only function the rest of the app uses.
Backups are optional and set in .env (see .env.example):
  - GEMINI_FALLBACK_MODELS : other models on the same key (each model has its own limits)
  - GEMINI_EXTRA_KEYS      : keys from OTHER Google projects (keys in one project share one quota)
  - FALLBACK_API_URL/KEY/MODEL : any OpenAI-compatible service such as Groq or OpenRouter
"""
import os
import time

import requests


class AIError(Exception):
    def __init__(self, message: str, retryable: bool = False, code: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


# ---------- one request to one provider ----------
def _gemini(key: str, model: str):
    base = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

    def call(system, prompt, temperature, max_tokens):
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature, **({"maxOutputTokens": max_tokens} if max_tokens else {})}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        try:
            res = requests.post(f"{base}/models/{model}:generateContent", json=body,
                                headers={"x-goog-api-key": key}, timeout=120)
        except requests.RequestException:
            raise AIError("Could not reach the AI service. Check your internet connection.", True, "network")
        if not res.ok:
            try:
                detail = res.json().get("error", {}).get("message", res.reason)
            except ValueError:
                detail = res.reason
            _raise_for_status(res.status_code, detail, "GEMINI_API_KEY", "GEMINI_MODEL")
        data = res.json()
        if data.get("promptFeedback", {}).get("blockReason"):
            raise AIError("The AI declined to process part of this document.", False, "blocked")
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise AIError("The AI returned an empty answer.", True, "empty")
        return text

    return call


def _openai_compatible(url: str, key: str, model: str):
    def call(system, prompt, temperature, max_tokens):
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens
        try:
            res = requests.post(f"{url.rstrip('/')}/chat/completions", json=body,
                                headers={"Authorization": f"Bearer {key}"}, timeout=120)
        except requests.RequestException:
            raise AIError("Could not reach the backup AI service.", True, "network")
        if not res.ok:
            try:
                detail = res.json().get("error", {}).get("message", res.reason)
            except (ValueError, AttributeError):
                detail = res.reason
            _raise_for_status(res.status_code, detail, "FALLBACK_API_KEY", "FALLBACK_MODEL")
        text = ((res.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        if not text:
            raise AIError("The AI returned an empty answer.", True, "empty")
        return text

    return call


def _raise_for_status(status: int, detail: str, key_var: str, model_var: str):
    if status == 429:
        raise AIError("The AI rate limit was reached. On the free tier this can also mean the daily limit is used up.", True, "rate_limited")
    if status >= 500:
        raise AIError("The AI service is temporarily unavailable.", True, "unavailable")
    if status in (401, 403) or (status == 400 and "api key" in detail.lower()):
        raise AIError(f"The API key was rejected. Check {key_var} in .env.", False, "bad_key")
    if status == 404:
        raise AIError(f"The model name was not found. Check {model_var} in .env (names are listed in Google AI Studio).", False, "bad_model")
    raise AIError(f"The AI request failed: {detail}", False)


# ---------- the list of providers to try, in order ----------
class _Provider:
    def __init__(self, name, call):
        self.name, self.call = name, call
        self.cool_until = 0.0   # skip this provider until this time (after a rate limit)
        self.dead = False       # bad key / bad model: do not try again


_chain: list[_Provider] | None = None


def _providers() -> list[_Provider]:
    global _chain
    if _chain is not None:
        return _chain
    chain = []
    key, model = os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_MODEL")
    if key and model:
        keys = [key, *_list("GEMINI_EXTRA_KEYS")]
        for m in [model, *_list("GEMINI_FALLBACK_MODELS")]:        # preferred model first, on every key
            for i, k in enumerate(keys):
                chain.append(_Provider(f"gemini:{m}" + (f" (key {i + 1})" if i else ""), _gemini(k, m)))
    url, fkey, fmodel = os.getenv("FALLBACK_API_URL"), os.getenv("FALLBACK_API_KEY"), os.getenv("FALLBACK_MODEL")
    if url and fkey and fmodel:
        chain.append(_Provider(f"backup:{fmodel}", _openai_compatible(url, fkey, fmodel)))
    if not chain:
        raise AIError("AI is not set up yet. Add GEMINI_API_KEY and GEMINI_MODEL to the .env file and restart the app.", False, "ai_not_configured")
    _chain = chain
    return chain


def check_configured() -> None:
    """Raises a clear error if no AI is set up. Call before starting a job."""
    _providers()


def generate_text(prompt: str, system: str | None = None, temperature: float = 0.3, max_tokens: int | None = None) -> str:
    chain = _providers()
    last: AIError | None = None
    for _ in range(6):  # rounds; each round tries every provider that is not cooling down
        now = time.time()
        usable = [p for p in chain if not p.dead and p.cool_until <= now]
        if not usable:
            alive = [p for p in chain if not p.dead]
            if not alive:
                raise last or AIError("No working AI is available. Check your keys and model names in .env.", False, "bad_key")
            time.sleep(max(1, min(15, min(p.cool_until for p in alive) - now)))
            continue
        content_errors = 0
        for p in usable:
            try:
                return p.call(system, prompt, temperature, max_tokens)
            except AIError as e:
                last = e
                if e.code in ("blocked", "empty"):       # about this text, not about the provider
                    content_errors += 1
                    continue
                if e.code in ("bad_key", "bad_model"):
                    p.dead = True
                elif e.code == "rate_limited":
                    p.cool_until = time.time() + 30
                elif e.retryable:
                    p.cool_until = time.time() + 10
                print(f"[ai] {p.name} failed ({e.code or 'error'}); trying the next option")
        if content_errors == len(usable):
            raise last  # every option refused this text: retrying will not help
    raise last or AIError("The AI is busy. Please try again in a few minutes.", True, "rate_limited")
