#!/usr/bin/env python3
"""Concurrent loop runner for design-principles-review-pipeline and issue-pipeline."""

import argparse, asyncio, logging, signal, sys, time
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

@dataclass
class Loop:
    name: str
    cmd: tuple[str, ...]
    interval: int
    runs: int = 0
    last_run: float = 0
    last_dur: float = 0
    last_code: int = 0

@dataclass
class Stats:
    start: float = field(default_factory=time.time)
    dp_runs: int = 0
    dp_err: int = 0
    ip_runs: int = 0
    ip_err: int = 0

def build_cmd(base: tuple, provider: Optional[str] = None, model: Optional[str] = None) -> list[str]:
    c = ["pi", "-p"]
    if provider: c += ["--provider", provider]
    if model:  c += ["--model", model]
    c.extend(base)
    return c

def join_cmd(cmd: list[str]) -> str:
    try:
        import shlex
        return shlex.join(cmd)
    except Exception:
        return " ".join(cmd)

async def run(cmd: list[str], name: str, sem: asyncio.Semaphore) -> tuple[int, float]:
    t0 = time.time()
    log.info(f"[{name}] Starting: {' '.join(cmd)}")
    try:
        async with sem:
            proc = await asyncio.create_subprocess_exec(*cmd)
            code = await proc.wait()
        dt = time.time() - t0
        if code == 0:
            log.info(f"[{name}] Done in {dt:.1f}s")
        else:
            log.warning(f"[{name}] Failed in {dt:.1f}s (exit={code})")
        return code, dt
    except Exception as e:
        log.error(f"[{name}] Error: {e}")
        return -1, time.time() - t0

async def loop_run(lp: Loop, st: Stats, stop: asyncio.Event, sem: asyncio.Semaphore, provider: Optional[str], model: Optional[str]):
    while not stop.is_set():
        t0 = time.time()
        code, dur = await run(build_cmd(lp.cmd, provider, model), lp.name, sem)
        lp.runs += 1; lp.last_run = t0; lp.last_dur = dur; lp.last_code = code
        if lp.name == "design-principles":
            st.dp_runs += 1; 
            if code != 0: st.dp_err += 1
        else:
            st.ip_runs += 1
            if code != 0: st.ip_err += 1
        if lp.runs % 5 == 0:
            prov = provider or "(default)"
            mod = model or "(default)"
            log.info(f"[{lp.name}] runs={lp.runs} last={lp.last_dur:.1f}s exit={lp.last_code} uptime={time.time()-st.start:.0f}s")
        if lp.interval > 0:
            try: await asyncio.wait_for(stop.wait(), lp.interval)
            except asyncio.TimeoutError: pass

async def stats_log(st: Stats, loops: list[Loop], stop: asyncio.Event):
    while not stop.is_set():
        try: await asyncio.wait_for(stop.wait(), 60)
        except asyncio.TimeoutError: pass
        up = time.time() - st.start
        log.info(f"[GLOBAL] up={up:.0f}s dp: runs={st.dp_runs} err={st.dp_err} | ip: runs={st.ip_runs} err={st.ip_err}")
        for l in loops:
            if l.last_run:
                log.info(f"  [{l.name}] runs={l.runs} ago={time.time()-l.last_run:.0f}s dur={l.last_dur:.1f}s exit={l.last_code}")

async def main():
    ap = argparse.ArgumentParser(description="Run two pi pipelines concurrently")
    for prefix, dinterval in [("dp", 1800), ("issue", 10)]:
        ap.add_argument(f"--{prefix}-interval", type=int, default=dinterval)
    ap.add_argument("--provider", default=None, help="PI provider (e.g. ollama, anthropic)")
    ap.add_argument("--model", default=None, help="PI model id (e.g. qwen3.6:latest, claude-sonnet-4)")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG","INFO","WARNING","ERROR"])
    ap.add_argument("--top-issues", type=int, default=3, help="Number of top issues for issue-pipeline (default: 3)")
    a = ap.parse_args()
    logging.getLogger().setLevel(a.log_level)

    dp = Loop(
        "design-principles",
        ("/run-chain", "design-principles-review-pipeline", "--", "Review the codebase main branch for design principle violations, find gaps against existing issues, and create issues for new violations."),
        a.dp_interval,
    )
    ip = Loop(
        "issue-pipeline",
        ("/run-chain", "issue-pipeline", "--", f"Analyze all open issues, implement the top {a.top_issues}, review, and merge passing ones."),
        a.issue_interval,
    )

    st = Stats(); stop = asyncio.Event(); sem = asyncio.Semaphore(1)
    for s in (signal.SIGINT, signal.SIGTERM):
        try: asyncio.get_running_loop().add_signal_handler(s, stop.set)
        except NotImplementedError: pass

    dp_prov = a.provider or "(default)"
    dp_mod = a.model or "(default)"
    log.info(f"Starting: dp every {dp.interval}s | ip every {ip.interval}s ({dp_prov}/{dp_mod})")
    try:
        await asyncio.gather(loop_run(dp, st, stop, sem, a.provider, a.model), loop_run(ip, st, stop, sem, a.provider, a.model), stats_log(st, [dp, ip], stop))
    finally:
        up = time.time() - st.start
        log.info(f"=== Done: up={up:.0f}s dp={st.dp_runs}/{st.dp_err} ip={st.ip_runs}/{st.ip_err} (provider: {dp_prov}|model: {dp_mod}) ===")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: sys.exit(0)