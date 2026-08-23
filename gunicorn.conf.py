"""Production Gunicorn lifecycle for the single-worker dashboard."""

bind = "0.0.0.0:8787"
workers = 1
threads = 4
timeout = 300
graceful_timeout = 60


def _single_worker(worker) -> bool:
    try:
        return int(worker.cfg.workers) == 1
    except (AttributeError, TypeError, ValueError):
        return False


def post_worker_init(worker) -> None:
    if not _single_worker(worker):
        worker.log.critical(
            "Persistent dashboard runtime requires exactly one Gunicorn worker."
        )
        raise RuntimeError("unsupported_worker_count")
    import app as dashboard

    result = dashboard.initialize_worker_runtime(worker_count=worker.cfg.workers)
    if not result.get("usable"):
        reason = str(result.get("reason") or "runtime_initialization_failed")
        worker.log.critical(
            "Persistent dashboard runtime initialization failed (%s).", reason
        )
        raise RuntimeError(reason)


def worker_exit(server, worker) -> None:
    del server, worker
    import app as dashboard

    dashboard.shutdown_worker_runtime()
