"""The web UI's HTTP server: a small JSON API, a live event stream per job, and the static page.

Standard library only, so the UI needs nothing beyond what the CLI already installs.
It listens on this machine by default and has no login of its own: anyone who can
reach the port can start runs on your API keys, so keep it on localhost unless
the network in between is trusted.
"""

from __future__ import annotations

import datetime
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import find_dotenv, unset_key
from dotenv import set_key as dotenv_set_key

import tradingagents
from cli.models import AnalystType, AssetType
from cli.prefs import load_last_run, save_last_run
from cli.prompts import (
    _fetch_openrouter_models,
    _llm_provider_table,
    detect_asset_type,
    filter_analysts_for_asset_type,
    parse_analysis_date,
    parse_analysts,
    parse_ticker,
    resolve_backend_url,
)
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV, get_api_key_env
from tradingagents.llm_clients.factory import REASONING_EFFORT_PROVIDERS
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS, SIGNED_IN_PROVIDERS, check_endpoint
from tradingagents.portfolio import PortfolioContext
from webui.jobs import Job, JobManager, results_dir, run_analysis_job, run_backtest_job

STATIC = Path(__file__).parent / "static"
MANAGER = JobManager()

# Keys that are not an LLM provider's but that data tools read.
EXTRA_KEYS = {
    "ALPHA_VANTAGE_API_KEY": "Alpha Vantage (market data vendor)",
    "FRED_API_KEY": "FRED (macro data)",
}

LANGUAGES = ["English", "Chinese", "Japanese", "Korean", "Hindi", "Spanish", "Portuguese",
             "French", "German", "Arabic", "Russian"]
DEPTHS = [
    {"value": 1, "label": "Shallow", "hint": "Quick research, one debate round"},
    {"value": 3, "label": "Medium", "hint": "Moderate debate and strategy discussion"},
    {"value": 5, "label": "Deep", "hint": "Comprehensive research, in-depth debate"},
]
ANALYST_LABELS = {"market": "Market", "social": "Sentiment", "news": "News", "fundamentals": "Fundamentals"}
THINKING = {
    "google": {"label": "Gemini thinking mode", "config": "google_thinking_level",
               "options": [("high", "Enabled (recommended)"), ("minimal", "Minimal / off")]},
    "openai": {"label": "Reasoning effort", "config": "openai_reasoning_effort",
               "options": [("medium", "Medium (default)"), ("high", "High (more thorough)"), ("low", "Low (faster)")]},
    "anthropic": {"label": "Claude effort", "config": "anthropic_effort",
                  "options": [("high", "High (recommended)"), ("medium", "Medium"), ("low", "Low (faster, cheaper)")]},
}
# Providers with a second, China-region account that has its own key and endpoint.
REGIONAL = {
    "qwen-cn": ("Qwen (China)", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "glm-cn": ("GLM (China, BigModel)", "https://open.bigmodel.cn/api/paas/v4/"),
    "minimax-cn": ("MiniMax (China)", "https://api.minimaxi.com/v1"),
}
LIVE_MODELS = ("openrouter", "chatgpt")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- catalog -------------------------------------------------------------------------------------

def _thinking_kind(provider: str) -> str | None:
    p = provider.lower()
    if p == "google":
        return "google"
    if p in REASONING_EFFORT_PROVIDERS:
        return "openai"
    if p == "anthropic":
        return "anthropic"
    return None


def _models(provider: str, mode: str) -> list[dict]:
    try:
        options = get_model_options(provider, mode)
    except KeyError:
        return []
    return [{"label": label, "value": value} for label, value in options if value != "custom"]


def providers_catalog() -> list[dict]:
    table = [(name, key, url) for name, key, url in _llm_provider_table()]
    for key, (name, url) in REGIONAL.items():
        table.append((name, key, url))
    out = []
    for name, key, url in table:
        env = get_api_key_env(key)
        spec = OPENAI_COMPATIBLE_PROVIDERS.get(key)
        kind = _thinking_kind(key)
        out.append({
            "key": key, "name": name, "url": url, "key_env": env,
            "key_set": bool(env and os.environ.get(env)),
            "key_optional": bool(spec and spec.key_optional),
            "signed_in_plan": key in SIGNED_IN_PROVIDERS,
            "needs_url": key == "openai_compatible",
            "live_models": key in LIVE_MODELS,
            "models": {"quick": _models(key, "quick"), "deep": _models(key, "deep")},
            "thinking": None if kind is None else {
                "label": THINKING[kind]["label"],
                "options": [{"value": v, "label": l} for v, l in THINKING[kind]["options"]]},
        })
    return out


def chatgpt_state() -> dict:
    try:
        from tradingagents.llm_clients import chatgpt_auth
        return {"signed_in_as": chatgpt_auth.signed_in_as()}
    except Exception:  # noqa: BLE001 - the status line must never break the page
        return {"signed_in_as": None}


def meta() -> dict:
    last = load_last_run()
    cfg = DEFAULT_CONFIG
    provider = last.get("llm_provider") or cfg["llm_provider"]
    return {
        "version": tradingagents.__version__,
        "providers": providers_catalog(),
        "analysts": [{"value": a.value, "label": ANALYST_LABELS[a.value]} for a in AnalystType],
        "depths": DEPTHS,
        "languages": LANGUAGES,
        "ratings": list(RATINGS_5_TIER),
        "chatgpt": chatgpt_state(),
        "defaults": {
            "ticker": "",
            "date": datetime.date.today().isoformat(),
            "analysts": last.get("analysts") or [a.value for a in AnalystType],
            "depth": last.get("research_depth") or 1,
            "language": last.get("output_language") or cfg["output_language"],
            "provider": provider,
            "backend_url": last.get("backend_url"),
            "quick_model": last.get("quick_think_llm") or cfg["quick_think_llm"],
            "deep_model": last.get("deep_think_llm") or cfg["deep_think_llm"],
            "checkpoint": bool(cfg["checkpoint_enabled"]),
            "thinking": {"google": cfg["google_thinking_level"], "openai": cfg["openai_reasoning_effort"],
                         "anthropic": cfg["anthropic_effort"]},
        },
        "paths": {"results": str(results_dir()), "cache": cfg["data_cache_dir"],
                  "memory": cfg["memory_log_path"]},
        "busy": MANAGER.active.summary() if MANAGER.active and not MANAGER.active.finished else None,
    }


def live_models(provider: str) -> list[dict]:
    if provider == "openrouter":
        return [{"label": name, "value": mid} for name, mid in _fetch_openrouter_models()[:60]]
    if provider == "chatgpt":
        from tradingagents.llm_clients import chatgpt_auth
        return [{"label": name, "value": mid} for name, mid in chatgpt_auth.list_models()]
    return []


# --- starting jobs -------------------------------------------------------------------------------

def _portfolio(data) -> PortfolioContext | None:
    if not data:
        return None
    positions = [p for p in data.get("positions") or [] if str(p.get("ticker", "")).strip()]
    cash, currency = data.get("cash"), (data.get("currency") or "").strip() or None
    if not positions and cash in (None, ""):
        return None
    try:
        return PortfolioContext.model_validate({
            "cash": None if cash in (None, "") else float(cash), "currency": currency,
            "positions": [{"ticker": str(p["ticker"]).strip().upper(), "quantity": float(p["quantity"]),
                           "average_price": None if p.get("average_price") in (None, "") else float(p["average_price"])}
                          for p in positions]})
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(f"Portfolio is not usable: {exc}") from None


def _preflight(provider: str, url: str | None) -> None:
    """The checks the CLI makes before it spends anything: endpoint, sign-in and key."""
    try:
        check_endpoint(provider, url)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    if provider in SIGNED_IN_PROVIDERS:
        from tradingagents.llm_clients import chatgpt_auth
        try:
            chatgpt_auth.access_token()
        except chatgpt_auth.ChatGPTAuthError as exc:
            raise ApiError(f"{exc}\nSign in from a terminal with:  tradingagents login chatgpt") from None
        except chatgpt_auth.ChatGPTUnreachable as exc:
            raise ApiError(str(exc)) from None
        return
    env = get_api_key_env(provider)
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
    if env and not os.environ.get(env) and not (spec and spec.key_optional):
        raise ApiError(f"{env} is not set. Add it under Settings → API keys.")


def build_config(p: dict) -> dict:
    provider = str(p.get("provider") or "").lower()
    known = {c["key"] for c in providers_catalog()}
    if provider not in known:
        raise ApiError(f"Unknown provider {provider!r}.")
    quick, deep = str(p.get("quick_model") or "").strip(), str(p.get("deep_model") or "").strip()
    if not quick or not deep:
        raise ApiError("Choose a quick-thinking and a deep-thinking model.")
    url = resolve_backend_url(provider, (p.get("backend_url") or "").strip() or None,
                              env_url=DEFAULT_CONFIG["backend_url"])
    if provider == "openai_compatible" and not url:
        raise ApiError("The OpenAI-compatible provider needs an endpoint URL.")

    config = DEFAULT_CONFIG.copy()
    depth = int(p.get("depth") or 1)
    if depth not in (1, 3, 5):
        raise ApiError("Research depth must be 1, 3 or 5.")
    config.update(llm_provider=provider, backend_url=url, quick_think_llm=quick, deep_think_llm=deep,
                  max_debate_rounds=depth, max_risk_discuss_rounds=depth,
                  output_language=(p.get("language") or "English").strip() or "English",
                  checkpoint_enabled=bool(p.get("checkpoint")))
    kind = _thinking_kind(provider)
    if kind and p.get("thinking"):
        config[THINKING[kind]["config"]] = p["thinking"]

    used = {provider}
    for tier in ("quick", "deep"):
        tier_provider = (config.get(f"{tier}_think_provider") or provider).lower()
        tier_url = config.get(f"{tier}_think_backend_url") or (url if tier_provider == provider else None)
        _preflight(tier_provider, tier_url)
        used.add(tier_provider)
    return config


def start_analysis(p: dict) -> Job:
    try:
        ticker = parse_ticker(str(p.get("ticker") or ""))
        date = parse_analysis_date(str(p.get("date") or ""))
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    asset = detect_asset_type(ticker)
    try:
        analysts = parse_analysts(",".join(p.get("analysts") or []), asset)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    config = build_config(p)
    portfolio = _portfolio(p.get("portfolio"))
    params = {"ticker": ticker, "date": date, "asset_type": asset.value,
              "analysts": [a.value for a in analysts], "save": True}
    job = Job("analysis", f"{ticker} · {date}")
    MANAGER.start(job, lambda j: run_analysis_job(j, config, params, portfolio))
    save_last_run({"output_language": config["output_language"], "analysts": analysts,
                   "research_depth": config["max_debate_rounds"], "llm_provider": config["llm_provider"],
                   "backend_url": config["backend_url"], "quick_think_llm": config["quick_think_llm"],
                   "deep_think_llm": config["deep_think_llm"]})
    return job


def start_backtest(p: dict) -> Job:
    from tradingagents.backtest import iter_grid

    names = [t.strip() for t in str(p.get("tickers") or "").replace(" ", ",").split(",") if t.strip()]
    if not names:
        raise ApiError("Enter at least one ticker, e.g. NVDA, AAPL.")
    try:
        tickers = [parse_ticker(n) for n in names]
        start, end = parse_analysis_date(str(p.get("start") or "")), parse_analysis_date(str(p.get("end") or ""))
        every = int(p.get("every") or 7)
        dates = iter_grid(start, end, every)
        asset = AssetType(str(p.get("asset_type") or "stock").lower())
        chosen = (parse_analysts(",".join(p["analysts"]), asset) if p.get("analysts")
                  else filter_analysts_for_asset_type(list(AnalystType), asset))
    except (ValueError, TypeError) as exc:
        raise ApiError(str(exc)) from None
    if len(tickers) * len(dates) > 200:
        raise ApiError(f"{len(tickers) * len(dates)} runs is a lot to pay for at once; narrow the grid (max 200).")
    config = build_config(p)
    config["checkpoint_enabled"] = False
    portfolio = _portfolio(p.get("portfolio"))
    run_id = (p.get("run_id") or "").strip() or None
    if run_id:
        try:
            safe_ticker_component(run_id)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
    params = {"tickers": tickers, "start": start, "end": end, "every": every, "asset_type": asset.value,
              "analysts": [a.value for a in chosen], "run_id": run_id}
    job = Job("backtest", f"Backtest · {', '.join(tickers)}")
    return MANAGER.start(job, lambda j: run_backtest_job(j, config, params, portfolio))


# --- saved runs ----------------------------------------------------------------------------------

_LOG_GLOB = "*/TradingAgentsStrategy_logs/full_states_log_*.json"


def _log_path(ticker: str, date: str) -> Path:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ApiError("Bad date.", 404)
    try:
        safe = safe_ticker_component(ticker)
    except ValueError:
        raise ApiError("Bad ticker.", 404) from None
    path = results_dir() / safe / "TradingAgentsStrategy_logs" / f"full_states_log_{date}.json"
    if not path.is_file():
        raise ApiError("No such saved run.", 404)
    return path


def history() -> list[dict]:
    rows = []
    for path in results_dir().glob(_LOG_GLOB):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        settings = data.get("run_settings") or {}
        rows.append({
            "ticker": data.get("company_of_interest") or path.parts[-3],
            "date": data.get("trade_date") or path.stem.removeprefix("full_states_log_"),
            "rating": data.get("final_rating"),
            "provider": settings.get("llm_provider"), "model": settings.get("deep_think_llm"),
            "analysts": settings.get("analysts"), "saved": path.stat().st_mtime,
        })
    rows.sort(key=lambda r: r["saved"], reverse=True)
    return rows


def history_detail(ticker: str, date: str) -> dict:
    from webui.jobs import _sections_payload
    data = json.loads(_log_path(ticker, date).read_text(encoding="utf-8"))
    return {"ticker": data.get("company_of_interest"), "date": data.get("trade_date"),
            "rating": data.get("final_rating"), "settings": data.get("run_settings"),
            "sections": _sections_payload(data)}


def export_run(ticker: str, date: str, fmt: str) -> tuple[bytes, str, str]:
    from tradingagents.reporting import write_report_tree
    data = json.loads(_log_path(ticker, date).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        write_report_tree(data, data.get("company_of_interest") or ticker, tmp,
                          settings=data.get("run_settings"), html=(fmt == "html"))
        name = "complete_report.html" if fmt == "html" else "complete_report.md"
        body = (Path(tmp) / name).read_bytes()
    kind = "text/html; charset=utf-8" if fmt == "html" else "text/markdown; charset=utf-8"
    return body, kind, f"{safe_ticker_component(ticker)}_{date}.{fmt}"


def memory_entries() -> list[dict]:
    from tradingagents.memory import TradingMemoryLog
    entries = TradingMemoryLog(DEFAULT_CONFIG).load_entries()
    return list(reversed(entries))


def backtests() -> list[dict]:
    from tradingagents.backtest import summarize
    out = []
    for log in sorted((results_dir() / "backtest").glob("*/trading_memory.md"),
                      key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            s = summarize(log)
        except Exception:  # noqa: BLE001 - one unreadable sweep must not hide the others
            continue
        out.append({"run_id": log.parent.name, "modified": log.stat().st_mtime, "resolved": s.resolved,
                    "pending": s.pending, "text": s.render(),
                    "by_rating": [{"rating": k, "count": v.count, "hit_rate": v.hit_rate,
                                   "mean_alpha": v.mean_alpha} for k, v in s.by_rating.items()]})
    return out


# --- keys ----------------------------------------------------------------------------------------

def keys_status() -> list[dict]:
    rows = {}
    for provider, env in PROVIDER_API_KEY_ENV.items():
        if env:
            rows.setdefault(env, {"env": env, "label": [], "set": bool(os.environ.get(env))})["label"].append(provider)
    out = [{"env": r["env"], "label": ", ".join(r["label"]), "set": r["set"]} for r in rows.values()]
    out += [{"env": env, "label": label, "set": bool(os.environ.get(env))} for env, label in EXTRA_KEYS.items()]
    return out


def save_key(env: str, value: str) -> None:
    allowed = {e for e in PROVIDER_API_KEY_ENV.values() if e} | set(EXTRA_KEYS)
    if env not in allowed:
        raise ApiError("That is not a key this app uses.")
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ApiError("A key is one line.")
    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    if not value:
        os.environ.pop(env, None)
        if os.path.exists(env_path):
            unset_key(env_path, env)
        return
    # Same handling as the CLI: the file holds credentials, so owner-only.
    if not os.path.exists(env_path):
        os.close(os.open(env_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    os.chmod(env_path, 0o600)
    dotenv_set_key(env_path, env, value)
    os.environ[env] = value


# --- HTTP ----------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsWeb"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: the terminal belongs to the launcher's own lines
        pass

    # -- plumbing --
    def _host_ok(self) -> bool:
        if self.server.allow_any_host:
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError("Expected JSON.", 415)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ApiError("Request too large.", 413)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError("Bad JSON.") from None
        if not isinstance(data, dict):
            raise ApiError("Expected a JSON object.")
        return data

    def _guard(self, fn) -> None:
        try:
            if not self._host_ok() or not self._origin_ok():
                raise ApiError("Forbidden.", 403)
            fn()
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - report, don't kill the connection thread silently
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except OSError:
                pass

    def do_GET(self):  # noqa: N802
        self._guard(self._get)

    def do_POST(self):  # noqa: N802
        self._guard(self._post)

    # -- GET --
    def _get(self) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if not path.startswith("/api/"):
            return self._static(path)
        if path == "/api/meta":
            return self._json(meta())
        if path == "/api/models":
            provider = (query.get("provider") or [""])[0]
            try:
                return self._json({"models": live_models(provider)})
            except Exception as exc:  # noqa: BLE001
                return self._json({"models": [], "error": str(exc)})
        if path == "/api/validate":
            raw = (query.get("ticker") or [""])[0]
            try:
                ticker = parse_ticker(raw)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)})
            asset = detect_asset_type(ticker)
            allowed = [a.value for a in filter_analysts_for_asset_type(list(AnalystType), asset)]
            return self._json({"ok": True, "ticker": ticker, "asset_type": asset.value, "analysts": allowed})
        if path == "/api/jobs":
            return self._json({"jobs": [j.summary() for j in sorted(MANAGER.jobs.values(), key=lambda j: -j.created)],
                               "active": MANAGER.active.id if MANAGER.active and not MANAGER.active.finished else None})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/events", path)
        if m:
            return self._stream(m.group(1), query)
        if path == "/api/history":
            return self._json({"runs": history()})
        m = re.fullmatch(r"/api/history/([^/]+)/(\d{4}-\d{2}-\d{2})(/export)?", path)
        if m:
            if m.group(3):
                fmt = (query.get("format") or ["html"])[0]
                if fmt not in ("html", "md"):
                    raise ApiError("format must be html or md")
                body, ctype, name = export_run(m.group(1), m.group(2), fmt)
                return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
            return self._json(history_detail(m.group(1), m.group(2)))
        if path == "/api/memory":
            return self._json({"entries": memory_entries()})
        if path == "/api/backtests":
            return self._json({"runs": backtests()})
        if path == "/api/keys":
            return self._json({"keys": keys_status()})
        raise ApiError("Not found.", 404)

    # -- POST --
    def _post(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/runs":
            try:
                job = start_analysis(body)
            except RuntimeError as exc:
                raise ApiError(str(exc), 409) from None
            return self._json({"id": job.id}, 201)
        if path == "/api/backtests":
            try:
                job = start_backtest(body)
            except RuntimeError as exc:
                raise ApiError(str(exc), 409) from None
            return self._json({"id": job.id}, 201)
        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path)
        if m:
            job = MANAGER.get(m.group(1))
            if not job:
                raise ApiError("No such job.", 404)
            job.cancel_requested.set()
            return self._json({"ok": True})
        if path == "/api/keys":
            save_key(str(body.get("env") or ""), str(body.get("value") or ""))
            return self._json({"keys": keys_status()})
        if path == "/api/checkpoints/clear":
            from tradingagents.graph.checkpointer import clear_all_checkpoints
            return self._json({"cleared": clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])})
        raise ApiError("Not found.", 404)

    # -- streaming and files --
    def _stream(self, job_id: str, query) -> None:
        job = MANAGER.get(job_id)
        if not job:
            raise ApiError("No such job.", 404)
        start = int((query.get("from") or [self.headers.get("Last-Event-ID") or "-1"])[0]) + 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        index = max(0, start)
        try:
            while True:
                events = job.wait_for(index, 15)
                if not events:
                    if job.finished:
                        break
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    payload = json.dumps(event, ensure_ascii=False, default=str)
                    self.wfile.write(f"id: {index}\ndata: {payload}\n\n".encode())
                    index += 1
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            raise ApiError("Not found.", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_any_host = False


def serve(host: str = "127.0.0.1", port: int = 8501, open_browser: bool = True) -> None:
    """Run the UI until interrupted."""
    server = None
    for candidate in range(port, port + 20):
        try:
            server = Server((host, candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free port in {port}-{port + 19}.")
    server.allow_any_host = host not in ("127.0.0.1", "localhost", "::1")
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    url = f"http://{shown}:{server.server_address[1]}/"
    print(f"TradingAgents web UI: {url}   (Ctrl+C to stop)")
    if server.allow_any_host:
        print("Warning: listening beyond this machine with no login. Anyone who can reach this "
              "address can run analyses on your API keys.", file=sys.stderr)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()
