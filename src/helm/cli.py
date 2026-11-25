"""`helm` command-line entry point. Subcommands are added milestone by milestone."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from helm import __version__

Handler = Callable[[argparse.Namespace], int]


def _cmd_config_check(args: argparse.Namespace) -> int:
    from helm.infra.config import load_config

    cfg = load_config(args.helm, args.risk, args.promotion)
    print(f"ok: {len(cfg.engine.bots)} bot(s); risk={cfg.risk.model_dump(mode='json')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="helm", description="Helm trading terminal")
    p.add_argument("--version", action="version", version=f"helm {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    cfg = sub.add_parser("config", help="configuration tools")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    check = cfg_sub.add_parser("check", help="validate config files")
    check.add_argument("--helm", default="config/helm.example.toml")
    check.add_argument("--risk", default="config/risk.example.toml")
    check.add_argument("--promotion", default="config/promotion.example.toml")
    check.set_defaults(handler=_cmd_config_check)

    for register in _extensions():
        register(sub)
    return p


def _extensions() -> list[Callable[[argparse._SubParsersAction[argparse.ArgumentParser]], None]]:
    from helm.app.cli import register as app_cli
    from helm.app.paper_cli import register as paper_cli
    from helm.data.cli import register as data_cli

    return [data_cli, app_cli, paper_cli]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
