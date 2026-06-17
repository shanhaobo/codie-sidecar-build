"""Codie media_mcp sidecar — per-instance media/document toolchain over MCP.

Exposes a single generic tool, `media_shell_exec(tool, args, workDir?, timeout?)`,
that runs one of an allowlisted set of binaries (ffmpeg, ffprobe, yt-dlp,
convert, identify, mogrify, pdftotext, pdftoppm, pdfinfo, pandoc) with the
supplied argv. List-mode subprocess, no shell — safe against injection.

Bridge's MediaMcpSupervisor spawns one container per agent instance, bind-
mounting a shared workspace at /workspace and passing a per-start bearer
token via the MEDIA_MCP_TOKEN env. The agent container reaches this sidecar
by its Docker network hostname (e.g. `codie-media-<instance>:8787`).

Auth is a single static bearer, not rotated — the sidecar is reachable only
on the per-instance Docker network and only by the paired agent. If the
token is ever suspected leaked, Bridge just restarts the sidecar (new token
lands at next mutation sync).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ALLOWED_TOOLS = {
    "ffmpeg",
    "ffprobe",
    "yt-dlp",
    "convert",
    "identify",
    "mogrify",
    "pdftotext",
    "pdftoppm",
    "pdfinfo",
    "pandoc",
}

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 3600
MAX_OUTPUT_BYTES = 1_000_000  # 1 MB per stream; beyond that we truncate


class BearerAuthMiddleware:
    """Static-bearer ASGI gate. Token read once from env at startup."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            resp = JSONResponse({"error": "missing bearer"}, status_code=401)
            await resp(scope, receive, send)
            return
        if auth[len("Bearer ") :].strip() != self._token:
            resp = JSONResponse({"error": "bearer rejected"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _resolve_work_dir(work_dir: str | None, workspace_root: Path) -> Path:
    """Resolve `work_dir` under workspace_root; reject paths that escape."""
    if not work_dir:
        return workspace_root
    candidate = (workspace_root / work_dir).resolve()
    try:
        candidate.relative_to(workspace_root.resolve())
    except ValueError:
        raise ValueError(
            f"workDir {work_dir!r} escapes workspace root {workspace_root}"
        )
    if not candidate.exists():
        candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def build_mcp_server(workspace_root: Path) -> FastMCP:
    mcp = FastMCP(
        "codie-media-mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    @mcp.tool()
    async def media_shell_exec(
        tool: str,
        args: list[str] | None = None,
        workDir: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict:
        """Run one of the allowlisted media tools with argv.

        Returns: {exit_code, stdout, stderr, truncated: bool, tool, duration_ms}.
        `workDir` is interpreted relative to /workspace; paths escaping the
        workspace are rejected. `timeout` is clamped to [1, 3600] seconds.
        """
        if tool not in ALLOWED_TOOLS:
            return {
                "error": f"tool {tool!r} not in allowlist",
                "allowed": sorted(ALLOWED_TOOLS),
            }
        argv = [tool, *(args or [])]
        try:
            cwd = _resolve_work_dir(workDir, workspace_root)
        except ValueError as e:
            return {"error": str(e)}
        clamped_timeout = max(1, min(int(timeout), MAX_TIMEOUT))

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return {"error": f"binary not found: {tool}"}
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=clamped_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return {
                "error": f"timeout after {clamped_timeout}s",
                "tool": tool,
                "timed_out": True,
            }
        duration_ms = int((loop.time() - t0) * 1000)
        truncated = len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES
        return {
            "exit_code": proc.returncode,
            "stdout": stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "stderr": stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "truncated": truncated,
            "tool": tool,
            "duration_ms": duration_ms,
        }

    @mcp.tool()
    def media_list_tools() -> dict:
        """Return the set of allowlisted tools and a short description for each."""
        return {
            "tools": sorted(ALLOWED_TOOLS),
            "workspace": str(workspace_root),
            "note": "Use media_shell_exec(tool=<name>, args=[...]) to invoke.",
        }

    return mcp


def main() -> int:
    token = os.environ.get("MEDIA_MCP_TOKEN", "").strip()
    if not token:
        print("media_mcp: MEDIA_MCP_TOKEN not set; refusing to start", file=sys.stderr)
        return 2
    host = os.environ.get("MEDIA_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MEDIA_MCP_PORT", "8787"))
    workspace_root = Path(os.environ.get("MEDIA_MCP_WORKSPACE", "/workspace"))
    workspace_root.mkdir(parents=True, exist_ok=True)

    mcp = build_mcp_server(workspace_root=workspace_root)
    mcp_app = mcp.streamable_http_app()
    app = Starlette(lifespan=mcp_app.router.lifespan_context)
    app.mount("/mcp", mcp_app)
    app.add_middleware(BearerAuthMiddleware, token=token)

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
