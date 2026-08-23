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
        worker.log.error(
            "Auto Recorder requires exactly one Gunicorn worker; monitor disabled."
        )
        return
    import app as dashboard

    dashboard.start_auto_recorder_monitor()


def worker_exit(server, worker) -> None:
    del server, worker
    import app as dashboard

    dashboard.shutdown_worker_runtime()
