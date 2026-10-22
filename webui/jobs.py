"""Background jobs for the web UI: an analysis run or a backtest sweep, reported as a stream of events.

A job appends events to a log that any number of browser tabs can replay and
follow, so closing or reloading the page never loses a run in progress.
"""

from __future__ import annotations

import datetime
import threading
import time
import traceback
import uuid
from pathlib import Path

from cli.display import (
    ANALYST_AGENT_NAMES,
    ANALYST_ORDER,
    ANALYST_REPORT_MAP,
    AnalystWallTimeTracker,
    classify_message_type,
    format_tool_args,
    sync_analyst_tracker_from_chunk,
)
from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import is_review, run_rating
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import _report_parts

FIXED_AGENTS = {
    "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
    "Trading Team": ["Trader"],
    "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
    "Portfolio Management": ["Portfolio Manager"],
}

REPORT_TITLES = {
    "market_report": "Market Analysis",
    "sentiment_report": "Sentiment Analysis",
    "news_report": "News Analysis",
    "fundamentals_report": "Fundamentals Analysis",
    "bull": "Bull Researcher",
    "bear": "Bear Researcher",
    "research_manager": "Research Manager Decision",
    "trader": "Trader Plan",
    "aggressive": "Aggressive Risk Analyst",
    "conservative": "Conservative Risk Analyst",
    "neutral": "Neutral Risk Analyst",
    "portfolio_manager": "Portfolio Manager Decision",
}


class Cancelled(Exception):
    """The user stopped the job."""


class Job:
    """One run, its event log and its state. Events are append-only dicts numbered by position."""

    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.status = "queued"   # queued | running | completed | failed | cancelled
        self.created = time.time()
        self.events: list[dict] = []
        self.cancel_requested = threading.Event()
        self._cond = threading.Condition()

    def emit(self, type_: str, **data) -> None:
        with self._cond:
            self.events.append({"type": type_, "t": round(time.time() - self.created, 2), **data})
            self._cond.notify_all()

    def check_cancelled(self) -> None:
        if self.cancel_requested.is_set():
            raise Cancelled()

    def finish(self, status: str) -> None:
        with self._cond:
            self.status = status
            self._cond.notify_all()

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def wait_for(self, index: int, timeout: float) -> list[dict]:
        """Events from ``index`` on, waiting up to ``timeout`` seconds for one to arrive."""
        with self._cond:
            if index >= len(self.events) and not self.finished:
                self._cond.wait(timeout)
            return self.events[index:]

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "created": self.created, "events": len(self.events)}


class JobManager:
    """Runs one job at a time: provider keys and the data layer's config are process-wide."""

    def __init__(self):
        self._lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self.active: Job | None = None

    def start(self, job: Job, target) -> Job:
        with self._lock:
            if self.active is not None and not self.active.finished:
                raise RuntimeError(f"“{self.active.title}” is still running; stop it or wait for it to finish.")
            self.active = job
            self.jobs[job.id] = job
            # Keep the last few jobs for replay; older ones live on disk as reports.
            for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-10]:
                if old.finished:
                    self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, target), daemon=True, name=f"job-{job.id}").start()
        return job

    def _run(self, job: Job, target) -> None:
        job.status = "running"
        job.emit("started", title=job.title)
        try:
            target(job)
            job.emit("finished", status="completed")
            job.finish("completed")
        except Cancelled:
            job.emit("finished", status="cancelled")
            job.finish("cancelled")
        except Exception as exc:  # noqa: BLE001 - any failure is shown to the user, not swallowed
            job.emit("error", message=f"{type(exc).__name__}: {exc}", detail=traceback.format_exc(limit=8))
            job.emit("finished", status="failed")
            job.finish("failed")

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)


class RunTracker:
    """Turns the graph's state chunks into agent statuses and report panels, as the terminal view does."""

    def __init__(self, job: Job, analyst_keys: list[str]):
        self.job = job
        self.selected = analyst_keys
        self.status: dict[str, str] = {}
        self.teams: dict[str, list[str]] = {}
        analysts = [ANALYST_AGENT_NAMES[k] for k in analyst_keys]
        self.teams["Analyst Team"] = analysts
        self.teams.update(FIXED_AGENTS)
        for agents in self.teams.values():
            for agent in agents:
                self.status[agent] = "pending"
        self.reports: dict[str, str] = {}
        self._seen_ids: set = set()
        self._sent_status: dict | None = None

    # -- emitting -------------------------------------------------------------------------------
    def set_status(self, agent: str, status: str) -> None:
        if agent in self.status:
            self.status[agent] = status

    def push_status(self) -> None:
        if self.status != self._sent_status:
            self._sent_status = dict(self.status)
            self.job.emit("agents", teams=self.teams, status=dict(self.status))

    def set_report(self, key: str, content) -> None:
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content)
        content = (content or "").strip()
        if content and self.reports.get(key) != content:
            self.reports[key] = content
            self.job.emit("report", key=key, title=REPORT_TITLES.get(key, key), content=content)

    # -- messages -------------------------------------------------------------------------------
    def messages(self, messages) -> None:
        for message in messages:
            msg_id = getattr(message, "id", None)
            if msg_id is not None:
                if msg_id in self._seen_ids:
                    continue
                self._seen_ids.add(msg_id)
            kind, content = classify_message_type(message)
            if content and content.strip():
                self.job.emit("message", kind=kind, content=content[:4000], clock=_clock())
            for call in getattr(message, "tool_calls", None) or []:
                name, args = (call["name"], call["args"]) if isinstance(call, dict) else (call.name, call.args)
                self.job.emit("tool", name=name, args=format_tool_args(args, 160), clock=_clock())

    # -- state ----------------------------------------------------------------------------------
    def chunk(self, chunk: dict, wall_time: AnalystWallTimeTracker) -> None:
        sync_analyst_tracker_from_chunk(wall_time, chunk)
        all_filed = True
        for key in ANALYST_ORDER:
            if key not in self.selected:
                continue
            report_key = ANALYST_REPORT_MAP[key]
            if chunk.get(report_key):
                self.set_report(report_key, chunk[report_key])
            done = report_key in self.reports
            self.set_status(ANALYST_AGENT_NAMES[key], "completed" if done else "in_progress")
            all_filed = all_filed and done
        if all_filed and self.selected and self.status.get("Bull Researcher") == "pending":
            self.set_status("Bull Researcher", "in_progress")

        debate = chunk.get("investment_debate_state")
        if debate:
            bull, bear = (debate.get("bull_history") or "").strip(), (debate.get("bear_history") or "").strip()
            judge = (chunk.get("investment_plan") or "").strip()
            if bull or bear:
                for agent in FIXED_AGENTS["Research Team"]:
                    self.set_status(agent, "in_progress")
            self.set_report("bull", bull)
            self.set_report("bear", bear)
            if judge:
                self.set_report("research_manager", judge)
                for agent in FIXED_AGENTS["Research Team"]:
                    self.set_status(agent, "completed")
                self.set_status("Trader", "in_progress")

        if chunk.get("trader_investment_plan"):
            self.set_report("trader", chunk["trader_investment_plan"])
            if self.status["Trader"] != "completed":
                self.set_status("Trader", "completed")
                self.set_status("Aggressive Analyst", "in_progress")

        risk = chunk.get("risk_debate_state")
        if risk:
            for agent, field, key in (("Aggressive Analyst", "aggressive_history", "aggressive"),
                                      ("Conservative Analyst", "conservative_history", "conservative"),
                                      ("Neutral Analyst", "neutral_history", "neutral")):
                text = (risk.get(field) or "").strip()
                if text:
                    if self.status[agent] != "completed":
                        self.set_status(agent, "in_progress")
                    self.set_report(key, text)
            judge = (chunk.get("final_trade_decision") or "").strip()
            if judge and self.status["Portfolio Manager"] != "completed":
                self.set_report("portfolio_manager", judge)
                for agent in FIXED_AGENTS["Risk Management"] + FIXED_AGENTS["Portfolio Management"]:
                    self.set_status(agent, "completed")
        self.push_status()

    def complete_all(self) -> None:
        for agent in self.status:
            self.status[agent] = "completed"
        self.push_status()


def _clock() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _sections_payload(final_state: dict) -> list[dict]:
    """The finished report as sections of (agent, markdown), for the results view."""
    return [{"heading": heading, "items": [{"agent": a, "text": t} for a, _f, t in written]}
            for heading, _folder, written in _report_parts(final_state)]


def run_analysis_job(job: Job, config: dict, params: dict, portfolio) -> None:
    """Build the graph and stream one analysis into the job's events."""
    ticker, trade_date, asset_type = params["ticker"], params["date"], params["asset_type"]
    analyst_keys = [a for a in ANALYST_ORDER if a in params["analysts"]]
    plan = build_analyst_execution_plan(analyst_keys)
    wall_time = AnalystWallTimeTracker(plan)
    stats = StatsCallbackHandler()
    tracker = RunTracker(job, analyst_keys)
    started = time.time()

    def push_stats():
        s = stats.get_stats()
        job.emit("stats", **s, elapsed=round(time.time() - started, 1),
                 agents_done=sum(1 for v in tracker.status.values() if v == "completed"),
                 agents_total=len(tracker.status))

    stop_ticker = threading.Event()

    def heartbeat():
        while not stop_ticker.wait(2.0):
            push_stats()

    job.emit("log", message=f"Analyzing {ticker} on {trade_date} with {', '.join(analyst_keys)}")
    tracker.push_status()
    graph = TradingAgentsGraph(analyst_keys, config=config, debug=True, callbacks=[stats])
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        for spec in plan.specs:
            tracker.set_status(spec.agent_node, "in_progress")
            wall_time.mark_started(spec.key)
        tracker.push_status()

        init_state = graph.create_run_state(ticker, trade_date, asset_type, portfolio)
        args = graph.propagator.get_graph_args(callbacks=[stats])
        thread = graph.begin_checkpoint(ticker, trade_date, asset_type, portfolio)
        if thread is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = thread
            job.emit("log", message=("Resuming the saved run" if getattr(graph, "_resuming", False)
                                     else "Starting fresh (checkpointing on)"))
        trace = []
        try:
            for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
                job.check_cancelled()
                tracker.messages(messages)
                if chunk is not None:
                    tracker.chunk(chunk, wall_time)
                    trace.append(chunk)
                push_stats()
            final_state = {}
            for chunk in trace:
                final_state.update(chunk)
            graph.record_decision(ticker, trade_date, final_state)
            graph.clear_checkpoint_on_success(ticker, trade_date, asset_type, portfolio)
        finally:
            graph.end_checkpoint()

        tracker.complete_all()
        rating = run_rating(final_state)
        saved = None
        if params.get("save", True):
            path = graph.default_report_path(ticker)
            graph.save_reports(final_state, ticker, path, html=True)
            saved = str(path.resolve())
        push_stats()
        job.emit("result", ticker=ticker, date=trade_date, rating=rating, review=is_review(rating),
                 sections=_sections_payload(final_state), saved_to=saved,
                 wall_time=wall_time.format_summary())
    finally:
        stop_ticker.set()


def run_backtest_job(job: Job, config: dict, params: dict, portfolio) -> None:
    """Sweep tickers × dates, scoring settled decisions, with a progress event per cell."""
    from tradingagents.backtest import iter_grid, run_backtest, summarize

    dates = iter_grid(params["start"], params["end"], params["every"])
    tickers = params["tickers"]
    job.emit("log", message=f"{len(tickers)} ticker(s) × {len(dates)} date(s)")
    job.emit("progress", done=0, total=len(tickers) * len(dates), label="Starting")

    def progress(done, total, ticker, date):
        job.check_cancelled()
        job.emit("progress", done=done - 1, total=total, label=f"{ticker} · {date}")

    result = run_backtest(
        tickers, dates, config, asset_type=params["asset_type"], portfolio=portfolio,
        selected_analysts=params["analysts"], run_id=params.get("run_id") or None, progress=progress,
    )
    summary = summarize(result)
    job.emit("progress", done=result.cells_run, total=result.cells_run, label="Done")
    job.emit("backtest_result", run_id=result.run_id, log_path=str(result.log_path),
             cells_run=result.cells_run, skipped=result.skipped,
             failures=[{"ticker": t, "date": d, "reason": r} for t, d, r in result.failures],
             settlement_failures=[{"ticker": t, "reason": r} for t, r in result.settlement_failures],
             summary={"resolved": summary.resolved, "pending": summary.pending, "unscored": summary.unscored,
                      "holding": summary.holding, "text": summary.render(),
                      "by_rating": [{"rating": k, "count": v.count, "hit_rate": v.hit_rate,
                                     "mean_alpha": v.mean_alpha} for k, v in summary.by_rating.items()]})


def default_config() -> dict:
    return DEFAULT_CONFIG.copy()


def results_dir() -> Path:
    return Path(DEFAULT_CONFIG["results_dir"])
