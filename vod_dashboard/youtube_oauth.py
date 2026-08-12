from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from vod_dashboard.youtube import bootstrap_youtube_oauth


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Authorize Twitch VOD Dashboard with a Google Desktop OAuth client "
            "and persist the resulting YouTube token."
        )
    )
    parser.add_argument(
        "--client-secret",
        required=True,
        type=Path,
        help="Path to the Google Desktop OAuth client JSON file.",
    )
    parser.add_argument(
        "--token",
        required=True,
        type=Path,
        help="Path where the authorized-user token JSON will be stored.",
    )
    return parser


def run_bootstrap(
    client_secret_path: Path,
    token_path: Path,
    *,
    bootstrap: Callable[..., Any] = bootstrap_youtube_oauth,
) -> Path:
    secret = Path(client_secret_path).expanduser().resolve(strict=False)
    destination = Path(token_path).expanduser().resolve(strict=False)
    if not secret.is_file():
        raise FileNotFoundError(f"Client secret file not found: {secret}")
    bootstrap(secret, destination)
    return destination


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    bootstrap: Callable[..., Any] = bootstrap_youtube_oauth,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        token_path = run_bootstrap(
            args.client_secret,
            args.token,
            bootstrap=bootstrap,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=stderr)
        return 2
    except Exception as exc:
        print(
            "YouTube OAuth authorization failed "
            f"({type(exc).__name__}). No token was written.",
            file=stderr,
        )
        return 1

    print(f"YouTube OAuth token saved to: {token_path}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
