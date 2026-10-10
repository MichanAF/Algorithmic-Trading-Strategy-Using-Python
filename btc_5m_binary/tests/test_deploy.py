"""The deployment files, checked against the code they claim to run.

A runbook rots silently: a flag is renamed, the unit file keeps the old one, and
nobody finds out until a box has been writing nothing for a week. So every
command in deploy/ is parsed with the real CLI parser here, and the one security
invariant of this stage -- that nothing deployed holds a credential -- is
asserted rather than trusted.
"""

import json
import re
import shlex
from pathlib import Path

import pytest

from btc5m.cli import build_parser

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
FILES = ("Dockerfile", "btc5m-watch.service", "btc5m-settle.service",
         "btc5m-settle.timer", "README.md")


def text(name: str) -> str:
    return (DEPLOY / name).read_text()


def unit_commands(name: str) -> list[list[str]]:
    """Every ExecStart in a unit, as an argv list, line continuations joined."""
    joined = text(name).replace("\\\n", " ")
    out = []
    for line in joined.splitlines():
        if line.strip().startswith("ExecStart="):
            argv = shlex.split(line.split("=", 1)[1])
            out.append(argv)
    return out


def btc5m_args(argv: list[str]) -> list[str]:
    """The part of a command the btc5m parser owns: everything after -m btc5m."""
    return argv[argv.index("btc5m") + 1:]


def test_every_deployment_file_is_present():
    assert DEPLOY.is_dir()
    for name in FILES:
        assert (DEPLOY / name).is_file(), name


@pytest.mark.parametrize("unit", ["btc5m-watch.service", "btc5m-settle.service"])
def test_every_command_a_unit_runs_is_a_real_command(unit):
    commands = unit_commands(unit)
    assert commands, unit
    for argv in commands:
        assert argv[0].endswith("/python"), argv[0]
        assert argv[1:3] == ["-m", "btc5m"], argv[1:3]
        args = build_parser().parse_args(btc5m_args(argv))
        assert args.func is not None


def test_the_watch_unit_runs_until_stopped_on_the_pinned_config():
    argv, = unit_commands("btc5m-watch.service")
    args = build_parser().parse_args(btc5m_args(argv))
    assert args.windows == 0                    # a box is stopped, not counted out
    assert args.venue == "polymarket-btc-5m"
    assert args.offsets == "5,15,30"
    assert args.config.endswith("configs/fade-flow-pooled-5m.json")
    assert args.out.startswith("/var/lib/btc5m/")


def test_the_settle_unit_settles_then_reports_the_same_file():
    settle, report = unit_commands("btc5m-settle.service")
    first = build_parser().parse_args(btc5m_args(settle))
    second = build_parser().parse_args(btc5m_args(report))
    assert first.quotes == second.quotes
    assert first.quotes == "/var/lib/btc5m/quotes.csv"
    assert second.config.endswith("configs/fade-flow-pooled-5m.json")


def test_the_docker_command_is_a_real_command_too():
    """CMD is a JSON array split over lines; it still has to parse as arguments."""
    body = text("Dockerfile").replace("\\\n", " ")
    cmd = re.search(r"^CMD (\[.+\])\s*$", body, re.M)
    assert cmd, "no CMD in the Dockerfile"
    # json.loads, not a comma split: the exec form is JSON, an argument may
    # contain a comma (--offsets 5,15,30 does), and a malformed exec form
    # silently degrades to a shell command.
    argv = json.loads(cmd.group(1))
    args = build_parser().parse_args(argv)
    assert args.windows == 0 and args.venue == "polymarket-btc-5m"
    assert args.out.startswith("/data/")        # the volume, not the container
    entry = re.search(r'^ENTRYPOINT \["python", "-m", "btc5m"\]', body, re.M)
    assert entry, "the entrypoint must be the package itself"


def test_the_config_every_deployment_file_names_exists():
    configs = Path(__file__).resolve().parents[1] / "configs"
    named = set()
    for name in FILES:
        named.update(re.findall(r"configs/([\w.-]+\.json)", text(name)))
    assert named, "no config is named anywhere in deploy/"
    for config in sorted(named):
        assert (configs / config).is_file(), config


def test_nothing_deployed_asks_for_a_key():
    """The read side needs no credential, so a deployment file mentioning one is
    either a mistake or a leak. The runbook may discuss keys in prose; it must
    not put one in a command or an environment variable."""
    forbidden = re.compile(
        r"(?:PRIVATE_KEY|PRIVKEY|SECRET_KEY|MNEMONIC|SEED_PHRASE|"
        r"0x[0-9a-fA-F]{40,}|-----BEGIN)")
    for name in FILES:
        for number, line in enumerate(text(name).splitlines(), 1):
            assert not forbidden.search(line), f"{name}:{number}: {line.strip()}"
    # No unit may carry an Environment= or EnvironmentFile= line at all.
    for unit in ("btc5m-watch.service", "btc5m-settle.service"):
        assert "Environment" not in text(unit), unit


def test_the_units_are_hardened_and_unprivileged():
    for unit in ("btc5m-watch.service", "btc5m-settle.service"):
        body = text(unit)
        assert "User=btc5m" in body and "Group=btc5m" in body
        assert "NoNewPrivileges=true" in body
        assert "ProtectSystem=strict" in body and "ProtectHome=true" in body
        assert "CapabilityBoundingSet=" in body
        # Writable exactly where the CSV lives, and nowhere else.
        writes = re.findall(r"^ReadWritePaths=(.+)$", body, re.M)
        assert writes == ["/var/lib/btc5m"], writes
    watch = text("btc5m-watch.service")
    assert "Restart=always" in watch          # a window missed is a window lost
    assert "chronyd" in watch                 # the clock is the load-bearing part


def test_the_watcher_starts_after_the_clock_is_synchronised():
    """Five-minute boundaries and a 30-second entry budget: a wrong clock writes
    rows that claim an offset they were not read at, and nothing detects it."""
    assert "After=network-online.target chronyd.service" in text("btc5m-watch.service")
    runbook = text("README.md")
    assert "chronyc tracking" in runbook
    assert "169.254.169.123" in runbook       # the EC2 clock the region provides


def test_the_settle_timer_is_hourly_not_per_window():
    body = text("btc5m-settle.timer")
    assert "OnCalendar=hourly" in body
    assert "Persistent=true" in body
    assert re.search(r"RandomizedDelaySec=\d+", body)


def test_the_runbook_says_what_is_not_deployed():
    body = text("README.md")
    for needle in ("holds no key", "session keys", "hot wallet",
                   "terms and the law", "Chainlink", "TWAP",
                   "data-api.binance.vision", "bar lag"):
        assert needle in body, needle
    # It must not promise a price it cannot know.
    assert "price list" in body
