#!/usr/bin/env python3
"""Prepare source-reader runtime and optional Codex/Claude MCP registration."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]


def sh_quote(value: str) -> str:
    return shlex.quote(value)


class Installer:
    def __init__(self, root: pathlib.Path, force: bool, dry_run: bool) -> None:
        self.root = root.resolve()
        self.force = force
        self.dry_run = dry_run
        self.updated: list[pathlib.Path] = []
        self.registered_mcp: list[str] = []

    def ensure_runtime_dirs(self) -> None:
        if self.dry_run:
            return
        (self.root / ".source-reader" / "profiles" / "default").mkdir(parents=True, exist_ok=True)
        (self.root / ".source-reader" / "runs").mkdir(parents=True, exist_ok=True)
        (self.root / ".source-reader" / "mcp").mkdir(parents=True, exist_ok=True)

    def write_mcp_runtime_files(self, port: int = 8765) -> None:
        if self.dry_run:
            return
        self.ensure_runtime_dirs()
        mcp_dir = self.root / ".source-reader" / "mcp"
        wrapper_path = mcp_dir / "source-reader-mcp.sh"
        wrapper = (
            "#!/bin/sh\n"
            f"cd {sh_quote(str(self.root))} || exit 1\n"
            f"exec {sh_quote(sys.executable)} scripts/source_reader.py mcp\n"
        )
        wrapper_path.write_text(wrapper, encoding="utf-8")
        wrapper_path.chmod(0o755)
        self.updated.append(wrapper_path)

        server_cmd = ["/bin/sh", str(wrapper_path)]
        runtime = {
            "name": "source-reader",
            "command": server_cmd[0],
            "args": server_cmd[1:],
            "cwd": str(self.root),
            "service": {
                "host": "127.0.0.1",
                "port": port,
                "health": f"http://127.0.0.1:{port}/health",
            },
        }
        runtime_path = mcp_dir / "source-reader.runtime.json"
        runtime_path.write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.updated.append(runtime_path)

        codex_config = (
            "[mcp_servers.source-reader]\n"
            f"command = {json.dumps(server_cmd[0])}\n"
            f"args = {json.dumps(server_cmd[1:], ensure_ascii=False)}\n"
            f"cwd = {json.dumps(str(self.root), ensure_ascii=False)}\n"
        )
        codex_path = mcp_dir / "source-reader.codex.toml"
        codex_path.write_text(codex_config, encoding="utf-8")
        self.updated.append(codex_path)

        claude_config = {
            "mcpServers": {
                "source-reader": {
                    "command": server_cmd[0],
                    "args": server_cmd[1:],
                    "cwd": str(self.root),
                }
            }
        }
        claude_path = mcp_dir / "source-reader.claude.json"
        claude_path.write_text(json.dumps(claude_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.updated.append(claude_path)

    def install_core(self) -> None:
        if self.dry_run:
            return
        if not (self.root / "package.json").exists():
            raise SystemExit(f"package.json does not exist: {self.root / 'package.json'}")
        print("\ninstalling source-reader Node modules...")
        subprocess.run(["npm", "install"], cwd=self.root, check=True)

    def install_browser(self) -> None:
        if self.dry_run:
            print("\n[dry-run] would install Playwright Chromium (~300MB)")
            return
        print("\ninstalling Playwright Chromium (one-time, ~300MB)...")
        subprocess.run(["npx", "playwright", "install", "chromium"], cwd=self.root, check=True)

    def install_scrapling(self) -> None:
        if self.dry_run:
            print("\n[dry-run] would install Scrapling + Camoufox (~200MB)")
            return
        print("\ninstalling Scrapling fetchers...")
        subprocess.run([sys.executable, "-m", "pip", "install", "scrapling[fetchers]"], check=True)
        print("\ninstalling Camoufox browser (one-time, ~200MB)...")
        subprocess.run(["scrapling", "install"], check=True)

    def install_yt_dlp(self) -> None:
        vendor_dir = self.root / ".source-reader" / "vendor"
        if self.dry_run:
            print(f"\n[dry-run] would install yt-dlp into {vendor_dir.relative_to(self.root)}")
            return
        vendor_dir.mkdir(parents=True, exist_ok=True)
        print(f"\ninstalling yt-dlp into {vendor_dir.relative_to(self.root)}...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--target", str(vendor_dir), "yt-dlp"],
            cwd=self.root,
            check=True,
        )

    @staticmethod
    def scrapling_installed() -> bool:
        import importlib.util
        return importlib.util.find_spec("scrapling") is not None

    def yt_dlp_status(self) -> str:
        local_package = self.root / ".source-reader" / "vendor" / "yt_dlp"
        local_bin = self.root / ".source-reader" / "vendor" / "bin" / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
        if local_bin.exists():
            return f"installed (project vendor bin: {local_bin.relative_to(self.root)})"
        if local_package.exists():
            return f"installed (project vendor python package: {local_package.relative_to(self.root)})"
        if shutil.which("yt-dlp"):
            return "installed (PATH)"
        return "not installed (run --install-yt-dlp for video transcripts)"

    def service_pid_path(self) -> pathlib.Path:
        return self.root / ".source-reader" / "source-reader.pid"

    def service_log_path(self) -> pathlib.Path:
        return self.root / ".source-reader" / "source-reader.log"

    def mcp_wrapper_path(self) -> pathlib.Path:
        return self.root / ".source-reader" / "mcp" / "source-reader-mcp.sh"

    def service_is_running(self) -> bool:
        pid_path = self.service_pid_path()
        if not pid_path.exists():
            return False
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def start_service(self, port: int = 8765) -> None:
        if self.dry_run:
            return
        self.ensure_runtime_dirs()
        if self.service_is_running():
            print("\nsource-reader service already running")
            return
        log_file = self.service_log_path().open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                "scripts/source_reader.py",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.service_pid_path().write_text(str(proc.pid), encoding="utf-8")
        time.sleep(0.5)
        print(f"\nsource-reader service started: http://127.0.0.1:{port}")
        print(f"service pid: {proc.pid}")
        print(f"service log: {self.service_log_path().relative_to(self.root)}")

    def codex_config_path(self) -> pathlib.Path:
        return pathlib.Path.home() / ".codex" / "config.toml"

    def codex_mcp_block(self) -> str:
        return (
            "[mcp_servers.source-reader]\n"
            "command = \"/bin/sh\"\n"
            f"args = {json.dumps([str(self.mcp_wrapper_path())], ensure_ascii=False)}\n"
            f"cwd = {json.dumps(str(self.root), ensure_ascii=False)}\n"
        )

    def register_codex_mcp(self) -> None:
        if self.dry_run:
            return
        config_path = self.codex_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        section = "[mcp_servers.source-reader]"
        if section in existing and not self.force:
            print("\nCodex MCP already has source-reader; use --force to replace it")
            return
        if existing:
            backup = config_path.with_suffix(config_path.suffix + f".bak-source-reader-{int(time.time())}")
            shutil.copy2(config_path, backup)
            print(f"\nCodex config backup: {backup}")
        lines = existing.splitlines()
        output: list[str] = []
        skipping = False
        for line in lines:
            stripped = line.strip()
            if stripped == section or stripped.startswith("[mcp_servers.source-reader."):
                skipping = True
                continue
            if skipping and stripped.startswith("[") and stripped.endswith("]"):
                skipping = False
            if not skipping:
                output.append(line)
        if output and output[-1].strip():
            output.append("")
        output.append(self.codex_mcp_block().rstrip())
        config_path.write_text("\n".join(output) + "\n", encoding="utf-8")
        self.registered_mcp.append("codex")
        print(f"\nCodex MCP registered: {config_path}")

    def register_claude_mcp(self) -> None:
        if self.dry_run:
            return
        wrapper = str(self.mcp_wrapper_path())
        existing = subprocess.run(
            ["claude", "mcp", "get", "source-reader"],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if existing.returncode == 0 and not self.force:
            print("\nClaude MCP already has source-reader; use --force to replace it")
            return
        if existing.returncode == 0 and self.force:
            subprocess.run(["claude", "mcp", "remove", "--scope", "user", "source-reader"], cwd=self.root, check=False)
        subprocess.run(
            ["claude", "mcp", "add", "--scope", "user", "source-reader", "--", "/bin/sh", wrapper],
            cwd=self.root,
            check=True,
        )
        self.registered_mcp.append("claude")
        print("\nClaude MCP registered: source-reader")

    def register_mcp(self, target: str) -> None:
        self.write_mcp_runtime_files()
        if target in {"codex", "both"}:
            self.register_codex_mcp()
        if target in {"claude", "both"}:
            self.register_claude_mcp()

    # Files left over from removed features. Keep this list narrow — only add
    # paths that the code no longer reads or writes anywhere.
    ORPHAN_PATHS: tuple[str, ...] = (
        ".source-reader/history.db",
        ".source-reader/history.db-journal",
        ".source-reader/history.db-wal",
        ".source-reader/history.db-shm",
    )

    def list_orphans(self) -> list[tuple[pathlib.Path, int]]:
        found: list[tuple[pathlib.Path, int]] = []
        for rel in self.ORPHAN_PATHS:
            path = self.root / rel
            if path.exists() and path.is_file():
                found.append((path, path.stat().st_size))
        return found

    def clean_orphans(self, assume_yes: bool) -> None:
        found = self.list_orphans()
        if not found:
            print("\nno orphan files to clean.")
            return
        print("\nfound orphan files (left over from removed features):")
        total = 0
        for path, size in found:
            print(f"  - {path.relative_to(self.root)}  ({size:,} bytes)")
            total += size
        print(f"\ntotal: {total:,} bytes")
        if self.dry_run:
            print("[dry-run] would delete the files above; nothing removed.")
            return
        if not assume_yes:
            try:
                answer = input("\ndelete these files? type 'yes' to confirm: ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "yes":
                print("aborted; no files deleted.")
                return
        for path, _ in found:
            try:
                path.unlink()
                print(f"  deleted: {path.relative_to(self.root)}")
            except OSError as exc:
                print(f"  failed: {path.relative_to(self.root)}: {exc}")

    def print_summary(self) -> None:
        print(f"root: {self.root}")
        print(f"updated: {len(self.updated)}")
        mcp_runtime = self.root / ".source-reader" / "mcp" / "source-reader.runtime.json"
        if mcp_runtime.exists():
            print("\nMCP runtime file:")
            print(f"- {mcp_runtime.relative_to(self.root)}")
        if self.registered_mcp:
            print(f"Global MCP registered: {', '.join(self.registered_mcp)}")
        else:
            print("Global Codex/Claude MCP registration is intentionally not modified by default.")
        scrapling_status = "installed" if self.scrapling_installed() else "not installed (run --install-scrapling for anti-bot support)"
        print(f"Scrapling: {scrapling_status}")
        print(f"yt-dlp: {self.yt_dlp_status()}")
        print(
            "\n[security] .source-reader/profiles/ 含登录态等敏感凭据，"
            "禁止提交 Git、禁止分享项目目录给他人。"
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install source-reader runtime and adapters")
    parser.add_argument("--root", default=str(ROOT), help="source-reader project root")
    parser.add_argument("--force", action="store_true", help="replace existing MCP registrations when needed")
    parser.add_argument("--dry-run", action="store_true", help="show intended work without writing files")
    parser.add_argument("--install-core", action="store_true", help="run npm install for source-reader Node modules (no browser)")
    parser.add_argument("--install-browser", action="store_true", help="install Playwright Chromium (one-time, ~300MB)")
    parser.add_argument("--no-browser", action="store_true", help="skip Playwright Chromium even when --install-mcp implies it")
    parser.add_argument("--install-scrapling", action="store_true", help="install Scrapling + Camoufox anti-bot fetcher (one-time, ~200MB)")
    parser.add_argument("--no-scrapling", action="store_true", help="skip Scrapling even when --install-mcp implies it")
    parser.add_argument("--install-yt-dlp", action="store_true", help="install project-local yt-dlp into .source-reader/vendor for video subtitles")
    parser.add_argument("--install-runtime", action="store_true", help="DEPRECATED: equivalent to --install-core --install-browser; will be removed")
    parser.add_argument("--start-service", action="store_true", help="start local source-reader service after setup")
    parser.add_argument("--install-mcp", action="store_true", help="write project-local MCP config snippets")
    parser.add_argument(
        "--register-mcp",
        choices=["none", "codex", "claude", "both"],
        default="none",
        help="register source-reader MCP in global Codex/Claude client config",
    )
    parser.add_argument("--service-port", type=int, default=8765, help="localhost port for source-reader service")
    parser.add_argument("--clean-orphans", action="store_true", help="remove files left over from removed features (e.g. history.db); prompts before deleting")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompts (only effective with --clean-orphans)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root = pathlib.Path(args.root).expanduser()
    if not root.is_absolute():
        root = (pathlib.Path.cwd() / root).resolve()

    installer = Installer(root=root, force=args.force, dry_run=args.dry_run)
    installer.ensure_runtime_dirs()
    if args.install_mcp or args.register_mcp != "none":
        installer.write_mcp_runtime_files(args.service_port)
    if args.install_runtime:
        print("\n[deprecated] --install-runtime is being replaced; use --install-core --install-browser instead.")
    if args.install_core or args.install_runtime:
        installer.install_core()
    should_install_browser = (
        args.install_browser
        or args.install_runtime
        or (args.install_mcp and not args.no_browser)
    )
    if should_install_browser:
        installer.install_browser()
    if args.install_scrapling and not args.no_browser:
        installer.install_scrapling()
    if args.install_yt_dlp:
        installer.install_yt_dlp()
    if args.register_mcp != "none":
        installer.register_mcp(args.register_mcp)
    if args.start_service:
        installer.start_service(args.service_port)
    if args.clean_orphans:
        installer.clean_orphans(args.yes)
    installer.print_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
