"""Pemanggilan AI untuk tiap agent: Claude, OpenAI, Gemini, atau DeepSeek, dengan AI cadangan.

Tiap agent memakai AI pilihan Owner (AI_<KEY AGENT> di .env). Jika AI itu gagal (key kosong, saldo habis,
error, jawaban tidak valid), permintaan otomatis diteruskan ke AI berikutnya di AI_FALLBACK.
"""
import datetime as dt
import json
import logging
import re
import time
from types import SimpleNamespace

import anthropic
import httpx

import config
import storage
from agents import Agent, system_prompt

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    """Dibuat saat pertama dipakai, agar program tetap jalan walau API key belum diisi."""
    global _client
    if _client is None:
        try:
            _client = anthropic.AsyncAnthropic()  # membaca ANTHROPIC_API_KEY dari .env
        except anthropic.AnthropicError as e:
            raise LLMError("API key Claude belum diisi. Isi di menu Pengaturan dashboard.") from e
    return _client

# Jika Claude Opus 5 menolak permintaan, server otomatis mengulang di model cadangan.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}

# Penyedia yang bisa dipilih per agent. Selain Claude, semuanya memakai API gaya OpenAI (chat/completions).
PROVIDERS = {
    "claude": {"name": "Claude", "key": "ANTHROPIC_API_KEY", "model": "CLAUDE_MODEL"},
    "openai": {"name": "OpenAI", "key": "OPENAI_API_KEY", "model": "OPENAI_MODEL",
               "base": "https://api.openai.com/v1", "json_schema": True},
    "gemini": {"name": "Gemini", "key": "GEMINI_API_KEY", "model": "GEMINI_MODEL",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai", "json_schema": True},
    "deepseek": {"name": "DeepSeek", "key": "DEEPSEEK_API_KEY", "model": "DEEPSEEK_MODEL",
                 "base": "https://api.deepseek.com/v1", "json_schema": False, "max_tokens": 8192},
    "openrouter": {"name": "OpenRouter", "key": "OPENROUTER_API_KEY", "model": "OPENROUTER_MODEL",
                   "base": "https://openrouter.ai/api/v1", "json_schema": True},
}
OPENROUTER_PRICES_KEY = "openrouter_prices"  # model -> [harga input, output] per 1 juta token, dari OpenRouter

# Harga per 1 juta token (USD) untuk estimasi biaya: (input, output). Model lain dihitung $5 / $25.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


class LLMError(Exception):
    pass


def provider_name(provider: str) -> str:
    return PROVIDERS.get(provider, {}).get("name", provider)


def provider_model(provider: str, agent: Agent | None = None) -> str:
    if provider == "openrouter" and agent and config.agent_model(agent.key):
        return config.agent_model(agent.key)
    return getattr(config, PROVIDERS[provider]["model"])


def configured(provider: str) -> bool:
    if provider not in PROVIDERS:
        return False
    key = config._str(PROVIDERS[provider]["key"])
    return bool(key) and "xxxx" not in key


def any_configured() -> bool:
    return any(configured(p) for p in PROVIDERS)


def chain(agent: Agent, provider: str | None = None, fallback: bool = True) -> list[str]:
    """Urutan AI yang dicoba untuk agent: AI pilihannya dulu, lalu cadangan. Hanya yang key-nya terisi."""
    first = provider or config.agent_provider(agent.key)
    order = [first] + ([p for p in config.AI_FALLBACK if p != first] if fallback else [])
    return [p for p in dict.fromkeys(order) if configured(p)]


def label(agent: Agent, provider: str | None = None) -> str:
    """Mis. "Developer (Claude)" atau "QA (OpenRouter · gpt-5.6-terra)"."""
    provider = provider or config.agent_provider(agent.key)
    if provider == "openrouter":
        return f"{agent.name} (OpenRouter · {provider_model(provider, agent).split('/')[-1]})"
    return f"{agent.name} ({provider_name(provider)})"


async def ask(agent: Agent, prompt: str, *, schema: dict | None = None, max_tokens: int = 16000,
              provider: str | None = None, fallback: bool = True):
    """Kirim prompt sebagai `agent`. Mengembalikan teks, atau dict jika `schema` diberikan."""
    result, _ = await ask_ex(agent, prompt, schema=schema, max_tokens=max_tokens, provider=provider,
                             fallback=fallback)
    return result


async def ask_ex(agent: Agent, prompt: str, *, schema: dict | None = None, max_tokens: int = 16000,
                 provider: str | None = None, fallback: bool = True):
    """Seperti ask(), tetapi juga mengembalikan AI yang akhirnya menjawab: (hasil, provider)."""
    order = chain(agent, provider, fallback)
    if not order:
        wanted = provider_name(provider or config.agent_provider(agent.key))
        raise LLMError(f"API key {wanted} untuk {agent.name} belum diisi, dan tidak ada AI cadangan yang siap. "
                       "Isi di menu Pengaturan → Penyedia AI.")
    failures = []
    for p in order:
        try:
            if p == "claude":
                result = await _ask_claude(agent, prompt, schema, max_tokens)
            else:
                result = await _ask_openai_style(p, agent, prompt, schema, max_tokens)
            if failures:
                log.warning("%s dijawab oleh %s setelah gagal: %s", agent.name, provider_name(p), "; ".join(failures))
            return result, p
        except LLMError as e:
            failures.append(f"{provider_name(p)}: {e}")
            log.warning("%s gagal memakai %s: %s", agent.name, provider_name(p), e)
    raise LLMError("Semua AI gagal. " + " | ".join(failures))


async def _ask_claude(agent: Agent, prompt: str, schema: dict | None, max_tokens: int):
    model = config.CLAUDE_MODEL
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        # System prompt tidak berubah antar-panggilan -> di-cache agar lebih murah.
        "system": [
            {"type": "text", "text": system_prompt(agent), "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": prompt}],
    }
    output_config: dict = {}
    if not model.startswith("claude-haiku"):
        output_config["effort"] = agent.effort
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        kwargs["output_config"] = output_config

    try:
        if model in FALLBACK_MODELS:
            response = await client().beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        else:
            response = await client().messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise LLMError("API key Claude tidak valid. Cek ANTHROPIC_API_KEY di .env.") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Kena batas pemakaian (rate limit) Claude API. Coba lagi sebentar.") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Tidak bisa terhubung ke Claude API. Cek koneksi internet.") from e

    storage.log_usage(agent.key, response.model, response.usage)

    if response.stop_reason == "refusal":
        raise LLMError("Permintaan ditolak oleh model.")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if response.stop_reason == "max_tokens":
        log.warning("Jawaban %s terpotong (max_tokens)", agent.key)
    return _parse(text) if schema else text


async def _ask_openai_style(provider: str, agent: Agent, prompt: str, schema: dict | None, max_tokens: int):
    info = PROVIDERS[provider]
    name, model = info["name"], provider_model(provider, agent)
    user = prompt
    body: dict = {"model": model}
    if provider == "openai":
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = min(max_tokens, info.get("max_tokens", max_tokens))
    if provider == "openrouter":
        # Model cadangan dicoba otomatis oleh OpenRouter jika model utama gagal/penuh.
        backups = [m for m in config.OPENROUTER_FALLBACK_MODELS if m != model][:2]
        if backups:
            body["models"] = [model] + backups
        body["usage"] = {"include": True}
        if schema:  # hanya ke server yang benar-benar mendukung format JSON yang diminta
            body["provider"] = {"require_parameters": True}
    if schema and info["json_schema"]:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "jawaban", "schema": schema, "strict": True}}
    elif schema:
        body["response_format"] = {"type": "json_object"}
        user += ("\n\nBalas HANYA dengan satu objek JSON valid (tanpa teks lain) yang mengikuti JSON Schema ini:\n"
                 + json.dumps(schema, ensure_ascii=False))
    body["messages"] = [{"role": "system", "content": system_prompt(agent)}, {"role": "user", "content": user}]

    try:
        async with httpx.AsyncClient(timeout=240) as http:
            r = await http.post(f"{info['base']}/chat/completions", json=body,
                                headers={"Authorization": f"Bearer {config._str(info['key'])}"})
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise LLMError(f"Tidak bisa terhubung ke {name}: {type(e).__name__}") from e
    if isinstance(data, list):  # Gemini kadang membungkus error dalam list
        data = data[0] if data else {}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error") if isinstance(data, dict) else None
        message = (err.get("message") if isinstance(err, dict) else err) or f"HTTP {r.status_code}"
        if r.status_code in (401, 403):
            raise LLMError(f"API key {name} tidak valid atau tidak punya akses ({message}).")
        if r.status_code == 429:
            raise LLMError(f"Kena batas pemakaian / saldo {name} habis ({message}).")
        if r.status_code == 404:
            raise LLMError(f"Model {model} tidak tersedia di {name} ({message}).")
        raise LLMError(f"{name} error {r.status_code}: {message}")

    usage = data.get("usage") or {}
    if provider == "openrouter":
        await _remember_price(data.get("model") or model)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    storage.log_usage(agent.key, data.get("model") or model, SimpleNamespace(
        input_tokens=max((usage.get("prompt_tokens") or 0) - cached, 0),
        output_tokens=usage.get("completion_tokens") or 0,
        cache_read_input_tokens=cached, cache_creation_input_tokens=0,
    ))
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise LLMError(f"Permintaan ditolak oleh {name}.")
    text = (message.get("content") or "").strip()
    if choice.get("finish_reason") == "length":
        log.warning("Jawaban %s (%s) terpotong", agent.key, name)
    if not text:
        raise LLMError(f"{name} tidak memberi jawaban.")
    return _parse(text) if schema else text


def _parse(text: str) -> dict:
    """JSON dari jawaban model, termasuk jika dibungkus ```json ... ```."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise LLMError("Jawaban terstruktur dari model tidak valid.")


async def _remember_price(model: str) -> None:
    """Simpan harga asli model OpenRouter (dari daftar model publik) untuk estimasi biaya. Diperbarui tiap hari."""
    prices = storage.get(OPENROUTER_PRICES_KEY, {})
    if model in prices and prices.get("_ts", 0) > time.time() - 86400:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            items = (await http.get(f"{PROVIDERS['openrouter']['base']}/models")).json().get("data", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Harga model OpenRouter tidak bisa diambil: %s", e)
        return
    prices = {"_ts": time.time()}
    for m in items:
        p = m.get("pricing") or {}
        try:
            prices[m["id"]] = [float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6]
        except (TypeError, ValueError, KeyError):
            continue
    storage.put(OPENROUTER_PRICES_KEY, prices)


def _price(model: str) -> tuple[float, float]:
    if model in PRICES:
        return PRICES[model]
    known = storage.get(OPENROUTER_PRICES_KEY, {})
    if model in known:
        return tuple(known[model])
    base = next((known[k] for k in known if k != "_ts" and model.startswith(k)), None)  # mis. model bertanggal
    return tuple(base) if base else (5.0, 25.0)


def row_cost(row: dict) -> float:
    """Estimasi biaya (USD) satu baris pemakaian token."""
    price_in, price_out = _price(row["model"])
    return (
        row["input_tokens"] * price_in
        + row["cache_write"] * price_in * 1.25
        + row["cache_read"] * price_in * 0.1
        + row["output_tokens"] * price_out
    ) / 1_000_000


def cost_since(since_ts: float) -> tuple[float, list[str]]:
    """Estimasi biaya AI (USD) sejak waktu tertentu."""
    total = 0.0
    lines = []
    for row in storage.usage_since(since_ts):
        cost = row_cost(row)
        total += cost
        lines.append(f"- {row['model']}: ${cost:.2f}")
    return total, lines


def start_of_day_ts() -> float:
    now = dt.datetime.now(config.TIMEZONE)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def start_of_month_ts() -> float:
    now = dt.datetime.now(config.TIMEZONE)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
