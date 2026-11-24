"""JSON-RPC 2.0 stdio MCP server for Plangraph Agent Spawner.

Exposes MCP tools:
  - plangraph_agent_spawn: Spawn an agent task with model/combo and risk configuration
  - plangraph_agent_status: Query status of spawned jobs
  - plangraph_agent_steer: Send steering instructions to active jobs
  - plangraph_agent_cancel: Cancel a running job
  - plangraph_agent_query_catalog: Query model catalog with filtering
  - plangraph_agent_health: Health check reporting runner, catalog, and gateway status
"""
from __future__ import annotations
from plangraph.routing_errors import ParameterRefused

import asyncio
import concurrent.futures
import copy
import json
import sys
import threading
from typing import Any, Mapping

from plangraph.planner.routing import BenchRequired, BenchmarkError, SpawnRefused
from plangraph.spawner import bench_entry, leg_probe, remote_ps
from plangraph.spawner.clients import effective_child_base_url
from plangraph.spawner.leg_state import leg_status
from plangraph.spawner.mcp_limits import (SourceGuard, checkout_head, encode_reply, ps_reply, publish_served,
                                          reload_spawn_sources, served_report)
from plangraph.spawner.resolver import (
    DEFAULT_FALLBACK_MODELS,
    detect_model_family,
    get_catalog_models,
    is_model_available,
    resolve_review_plan,
    score_model,
)
from plangraph.spawner.risk import evaluate_risk
from plangraph.spawner.runner import JobInfo, SpawnerRunner, spawn_service
from plangraph.spawner.sessions import Sessions
from plangraph.spawner.session_tools import SESSION_TOOLS, _session_arg_errors, dispatch_session_tool

TOOLS_METADATA = [
    {'name': 'plangraph_agent_probe', 'description': 'Run bounded writer commit probes',
     'inputSchema': {'type': 'object', 'properties': {
         'leg': {'type': 'array', 'items': {'type': 'string'}},
         'all_writers': {'type': 'boolean'}, 'timeout': {'type': 'number'}, 'json': {'type': 'boolean'}}}},
    {
        "name": "plangraph_agent_spawn",
        "description": "Spawn an agent task via OmniRoute ACP gateway or dry-run",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task instructions for the agent"},
                "model_or_combo": {"type": "string", "description": "Target model or combo (default 'l2-worker')"},
                "role": {"type": "string", "enum": ["implement", "review", "orchestrate", "drive", "research"], "default": "implement"},
                "risk": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
                "dry_run": {"type": "boolean", "description": "Plan without starting subprocess", "default": False},
                "wait_seconds": {"type": "number", "description": "Seconds to wait synchronously before returning", "default": 0.0},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "plangraph_agent_status",
        "description": "Check the status of spawned agent jobs",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Specific job ID to query (omit for all jobs)"},
            },
        },
    },
    {
        "name": "plangraph_agent_steer",
        "description": "Send steering instructions to an active job or native writer",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Target job ID"},
                "run_id": {"type": "string", "description": "Target native run ID"},
                "host": {"type": "string", "description": "Configured native run host"},
                "message": {"type": "string", "description": "Steering message or instructions"},
            },
            "required": ["message"],
            "anyOf": [{"required": ["job_id"]}, {"required": ["run_id"]}],
        },
    },
    {
        "name": "plangraph_agent_cancel",
        "description": "Cancel an active running job",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Target job ID to cancel"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "plangraph_agent_query_catalog",
        "description": "Query available models in catalog with intelligence and combo filters",
        "inputSchema": {
            "type": "object",
            "properties": {
                "combo": {"type": "string", "description": "Filter by combo name (e.g. 'l2-worker')"},
                "family": {"type": "string", "description": "Filter by model family (e.g. 'mimo', 'gpt', 'claude')"},
                "min_intelligence": {"type": "number", "description": "Minimum AA Intelligence score"},
            },
        },
    },
    {
        "name": "plangraph_agent_health",
        "description": "Health check for Plangraph Agent Spawner and OmniRoute gateway connectivity",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# D-3244: the schema names the field route refuses without, so callers send it.
CARD_SCHEMA = {'type': 'object', 'required': ['estimate_tokens'], 'properties': {
    'stall_min': {'type': 'number', 'exclusiveMinimum': 0},
    'first_edit_min': {'type': 'number', 'exclusiveMinimum': 0},
    'cwd': {'type': 'string', 'description': 'absolute path to an existing git checkout'},
    'max_min': {'type': 'number', 'exclusiveMinimum': 0},
    'estimate_tokens': {'type': 'integer', 'minimum': 1, 'description': 'required: expected tokens of the run'},
    'web_required': {'type': 'boolean', 'description': 'research: require a harness with web search or fetch'},
    'diff_ref': {'type': 'object', 'description': 'review: {repo, base, head}; inlined for inline-only seats'}}}
for _tool in TOOLS_METADATA:
    if _tool['name'] in ('plangraph_agent_status', 'plangraph_agent_cancel'):
        _tool['inputSchema']['properties']['run_id'] = {'type': 'string'}
        _tool['inputSchema'].pop('required', None)
    if _tool['name'] == 'plangraph_agent_spawn':
        _tool['inputSchema']['required'] = ['prompt', 'card']
        _tool['inputSchema']['properties'].update({
            'card': copy.deepcopy(CARD_SCHEMA), 'override_reason': {'type': 'string'},
            'writer_families': {'type': 'array', 'items': {'type': 'string'}},
            'review_scope': {'type': 'string', 'enum': ['per-fixer', 'batch']},
            'review_of': {'type': 'string'}, 'no_fallthrough': {'type': 'boolean'},
            'seats': {'type': 'boolean', 'description': 'role review: spawn every seat route picks (per writer part); inline-only legs get card.diff_ref inlined'}})
        _tool['inputSchema']['properties']['role']['enum'] = ['implement', 'review', 'research']
        _tool['inputSchema']['properties']['model_or_combo']['description'] = 'Exceptional audited override only; requires override_reason'
TOOLS_METADATA.extend(SESSION_TOOLS)


for _verb in ('route', 'ps', 'result'):
    TOOLS_METADATA.append({'name': 'plangraph_agent_' + _verb,
        'description': 'Card-based agent ' + _verb,
        'inputSchema': {'type': 'object', 'properties': {
            'card': copy.deepcopy(CARD_SCHEMA) if _verb == 'route' else {'type': 'object'}, 'role': {'type': 'string'},
            'run_id': {'type': 'string'}, 'writer_families': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['card'] if _verb == 'route' else (['run_id'] if _verb == 'result' else [])}})


for _tool in TOOLS_METADATA:
    if _tool['name'] in ('plangraph_agent_route', 'plangraph_agent_spawn'):
        _tool['inputSchema']['properties'].update({
            'require_bench': {'type': 'boolean'}, 'bench_db': {'type': 'string'}})


for _tool in TOOLS_METADATA:
    if _tool['name'] == 'plangraph_agent_spawn':
        _tool['inputSchema']['properties']['leg'] = {'type': 'string', 'description': 'Registered leg reference (explicit override)'}


class MCPSpawnerServer:
    """Server implementing the Model Context Protocol (MCP) JSON-RPC standard."""

    def __init__(self, runner: SpawnerRunner | None = None, *, state=None,
                 session_run=None, session_sleep=None, source_identity=None):
        self._source_guard = SourceGuard(source_identity)
        self.runner = runner or SpawnerRunner()
        self._session_run = session_run
        self._session_sleep = session_sleep
        self._build_service(state)
        # Route code of this process: the identity it was imported at, until a reload moves it.
        self._served_identity = self._source_guard.started
        self._served_head = checkout_head()
        self._reload_error = None
        self.publish_served()

    def publish_served(self):
        publish_served(self.service.state, self._served_head, self._served_identity, self._reload_error)

    def _build_service(self, state, previous=None):
        self.service = self._make_service(state, previous)

    def _make_service(self, state, previous=None):
        service = spawn_service(self.runner, state=state)
        if previous is not None:
            # Run tasks, records and stderr captures belong to the runs already started: share them.
            service.tasks = previous.tasks
            service._running_records = previous._running_records
            service._stderr_captures = previous._stderr_captures
        # An explicitly supplied default-equivalent runner is still a live path.
        from .seat_spawn import live_runner
        service.fresh_tree = live_runner(self.runner)
        return service

    def refresh_route_code(self):
        """Serve the checkout's current planner/routing code when its head moved, without a restart.

        Route and health never wait for a running job: the runner (job history) and the service's run
        tasks carry over to the new service. Modules, service and served sha change together or not at
        all; a failed reload keeps the previous generation and is reported by ``plangraph_agent_health``."""
        current = self._source_guard.identity()
        if current == self._served_identity:
            return
        previous, built = self.service, []
        try:
            reload_spawn_sources(globals(), commit=lambda: built.append(self._make_service(previous.state, previous)))
        except Exception as exc:
            self._reload_error = f'{type(exc).__name__}: {exc}'[:300]
            self.publish_served()
            return
        self.service = built[0]
        self._served_identity, self._served_head, self._reload_error = current, checkout_head(), None
        self.publish_served()

    def _sessions(self):
        """Fresh Sessions bound to the service state root (SESSION-SPAWN-MCP)."""
        import os
        return Sessions(state_root=self.service.state, run=self._session_run,
                        config_path=os.environ.get('PLANGRAPH_SESSIONS_CONFIG'),
                        sleep=self._session_sleep)

    async def _session_tool(self, name, arguments):
        return await dispatch_session_tool(self, name, arguments)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Execute tool and return structured payload."""
        self._source_guard.check(name, arguments)
        if name == 'plangraph_agent_route':
            self.refresh_route_code()  # before the benchmark options: they come from the module being replaced
        if isinstance(name, str) and name.startswith('plangraph_session_'):
            return await self._session_tool(name, arguments)
        if (name == 'plangraph_agent_route' and 'leg' in arguments
                or name == 'plangraph_agent_spawn' and arguments.get('leg') in ('agy-cli', 'claude-cli')):
            raise ParameterRefused('leg selection requires the Rust pg-spawnd server')
        if name == 'plangraph_agent_probe':
            try:
                return await leg_probe.probe_many(self.runner, arguments.get('leg', ()),
                                        arguments.get('all_writers', False), arguments.get('timeout'))
            except ParameterRefused as exc:
                return {'status': 'refused', 'refusal': 'ParameterRefused', 'error': str(exc), 'exit_code': 2}
        try:
            options = bench_entry.benchmark_options(arguments.get('require_bench', False), arguments.get('bench_db')) if name in ('plangraph_agent_route', 'plangraph_agent_spawn') else {}
        except Exception as exc:
            if name in ('plangraph_agent_route', 'plangraph_agent_spawn'):
                self.service.save_refusal(exc, arguments.get('role', 'implement'))
            raise
        if name == 'plangraph_agent_route':
            return self.service.route(arguments['card'], arguments.get('role', 'implement'),
                arguments.get('writer_families', ()), arguments.get('review_of'), **options).to_dict()
        if name == 'plangraph_agent_ps':
            return ps_reply(await asyncio.to_thread(remote_ps.list_runs, self.service.ps()['runs']))
        if name == 'plangraph_agent_result' or (name == 'plangraph_agent_status' and 'run_id' in arguments):
            return await self.service.refresh_result(arguments['run_id'])
        if name == 'plangraph_agent_cancel' and 'run_id' in arguments:
            return await self.service.cancel(arguments['run_id'])
        if name == "plangraph_agent_spawn":
            if 'prompt' not in arguments:
                exc = KeyError('prompt')
                self.service.save_refusal(exc, arguments.get('role', 'implement'))
                raise exc
            if not arguments.get('card'):
                exc = ParameterRefused('spawn by card; card required')
                self.service.save_refusal(exc, arguments.get('role', 'implement'))
                raise exc
            return await self.service.spawn(card=arguments.get('card'), prompt=arguments['prompt'],
                role=arguments.get('role', 'implement'), model=arguments.get('model_or_combo'), leg=arguments.get('leg'),
                override_reason=arguments.get('override_reason'), review_of=arguments.get('review_of'),
                writer_families=arguments.get('writer_families', ()),
                review_scope=arguments.get('review_scope'), seats=arguments.get('seats', False),
                dry_run=arguments.get('dry_run', False), wait_seconds=arguments.get('wait_seconds', 0),
                no_fallthrough=arguments.get('no_fallthrough', False), **options)

        if name == "plangraph_agent_status":
            job_id = arguments.get("job_id")
            res = await self.runner.status(job_id=job_id)
            if hasattr(res, 'to_dict'):  # duck-typed: retained jobs keep the class they were created with
                return res.to_dict()
            if isinstance(res, list):
                return {"jobs": [j.to_dict() for j in res]}
            return res

        if name == "plangraph_agent_steer":
            if 'run_id' in arguments:
                return await asyncio.to_thread(remote_ps.steer_run, self.service, arguments['run_id'],
                                               arguments['message'], arguments.get('host'))
            job_id = str(arguments["job_id"])
            message = str(arguments["message"])
            return await self.runner.steer(job_id=job_id, message=message)

        if name == "plangraph_agent_cancel":
            job_id = str(arguments["job_id"])
            return await self.runner.cancel(job_id=job_id)

        if name == "plangraph_agent_query_catalog":
            combo = arguments.get("combo")
            family = arguments.get("family")
            min_intel = arguments.get("min_intelligence")

            models = get_catalog_models()
            matched: list[dict[str, Any]] = []
            for m in models:
                m = dict(m, leg_state=leg_status(m["id"], combo=combo))
                if not is_model_available(m, lambda *a, **kw: {"state": "ok"}):
                    continue
                if combo and combo not in m.get("combos", []):
                    continue
                if family and detect_model_family(m["id"]) != family.lower():
                    continue
                if min_intel is not None and float(m.get("aa_intelligence", 0)) < float(min_intel):
                    continue
                matched.append(m)
            return {"count": len(matched), "models": matched}

        if name == "plangraph_agent_health":
            self.refresh_route_code()
            restart = self._source_guard.restart_required()
            all_models = get_catalog_models()
            active_jobs = [j for j in self.runner.jobs.values() if j.status == "running"]
            return {
                "status": "stale" if restart else "healthy",
                **({'error': restart} if restart else {}),
                "spawner_version": "2.0.0",
                # The checkout head the route code serves; spawn and probe still need a reconnect when stale.
                "served_sha": self._served_head,
                "checkout_sha": checkout_head(),
                **({'route_reload_error': self._reload_error} if self._reload_error else {}),
                "runner_jobs_total": len(self.runner.jobs),
                "active_jobs_count": len(active_jobs),
                "catalog_models_count": len(all_models),
                # The child's effective base URL, not merely the environment's:
                # a child config that already carries one keeps it (setdefault),
                # and this is the same helper build_child_env reads.
                "gateway_default_url": effective_child_base_url() or "",
            }

        raise ValueError(f"Unknown MCP tool: {name}")

    async def handle_rpc_request(self, req: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch a single JSON-RPC 2.0 request."""
        if not isinstance(req, Mapping) or req.get('jsonrpc') != '2.0' or not isinstance(req.get('method'), str) or not isinstance(req.get('params', {}), Mapping):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "plangraph-spawner", "version": "2.0.0"},
                    "capabilities": {"tools": {}},
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS_METADATA},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            try:
                result = await self.call_tool(tool_name, tool_args)
                if isinstance(result, dict) and result.get('status') == 'refused':
                    return {'jsonrpc': '2.0', 'id': req_id, 'error': {
                        'code': -32602 if result.get('refusal') in ('ParameterRefused', 'BenchRequired', 'BenchDataError') else -32000, 'message': result.get('error', 'benchmark rows missing'),
                        'data': {'type': result.get('refusal', 'SpawnRefused'), 'exit_code': result.get('exit_code', 2), 'legs': result.get('legs', []),
                                 'run_id': result.get('run_id')}}}
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(result, indent=2)}
                        ],
                        "isError": False,
                    },
                }
            except Exception as exc:
                if isinstance(exc, SpawnRefused):
                    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32602 if isinstance(exc, (BenchmarkError, ParameterRefused)) else -32000, 'message': str(exc), 'data': {'type': type(exc).__name__, 'exit_code': 3 if isinstance(exc, BenchmarkError) else 2, 'legs': list(exc.legs) if isinstance(exc, BenchRequired) else []}}}
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602 if isinstance(exc, (ValueError, KeyError, TypeError)) else -32603,
                              "message": str(exc), 'data': {'type': type(exc).__name__, 'exit_code': 1}},
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method {method} not found"},
        }


async def stdio_reply(server, line: bytes) -> str | None:
    """Handle one input line; notifications never dispatch or receive replies."""
    req = {}
    try:
        req = json.loads(line.decode('utf-8').strip())
        if isinstance(req, dict) and isinstance(req.get('method'), str) and 'id' not in req:
            return None
        resp = await server.handle_rpc_request(req)
        return encode_reply(resp)
    except (json.JSONDecodeError, UnicodeDecodeError):
        resp = {'jsonrpc': '2.0', 'id': None,
                'error': {'code': -32700, 'message': 'Parse error'}}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('stdio_failed: %s', type(exc).__name__)
        resp = {'jsonrpc': '2.0', 'id': req.get('id') if isinstance(req, dict) else None,
                'error': {'code': -32603, 'message': 'Internal error'}}
    return encode_reply(resp)


def _read_line(stdin) -> asyncio.Future:
    """Read one line on a daemon thread, so a blocked read never delays process exit.

    An executor thread would: asyncio.run and interpreter exit both join those.
    """
    done = concurrent.futures.Future()
    done.set_running_or_notify_cancel()

    def read() -> None:
        try:
            done.set_result(stdin.readline())
        except Exception as exc:
            done.set_exception(exc)

    threading.Thread(target=read, daemon=True, name='mcp-stdin').start()
    return asyncio.wrap_future(done)


async def keep_current(server, interval) -> None:
    """Follow the checkout head while the session is idle, so an update reaches it within ``interval``."""
    while True:
        await asyncio.sleep(interval)
        try:
            server.refresh_route_code()
        except Exception as exc:  # a failed check must not end the idle follower
            server._reload_error = f'{type(exc).__name__}: {exc}'[:300]


async def run_stdio_server() -> None:
    """Run MCP server over standard input and output."""
    server = MCPSpawnerServer()
    import os
    refresher = asyncio.create_task(keep_current(server, float(os.environ.get('PLANGRAPH_MCP_RELOAD_S', '60'))))
    # connect_read_pipe cannot serve an inherited stdin on Windows: the Proactor loop fails inside
    # a transport callback and the awaiting reader hangs forever. A private reader, not
    # sys.stdin.buffer, because a daemon thread holding that lock can abort interpreter shutdown.
    stdin = open(sys.stdin.fileno(), 'rb', closefd=False)

    try:
        while line := await _read_line(stdin):
            reply = await stdio_reply(server, line)
            if reply is not None:
                sys.stdout.write(reply + '\n')
                sys.stdout.flush()
    finally:
        refresher.cancel()


def served_main(state=None) -> int:
    """``--served-sha``: print the sha each live MCP session serves; exit 1 when any is not the checkout head."""
    import os
    from pathlib import Path
    state = Path(state or os.environ.get('PLANGRAPH_SPAWN_STATE', Path(os.environ.get(
        'XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
    rows = served_report(state)
    for row in rows:
        print(f"pid={row['pid']} served={row['served_sha'][:12] or 'unknown'} checkout={row['checkout_sha'][:12]} "
              f"{'current' if row['current'] else 'STALE'}" + (f" error={row['route_reload_error']}" if row['route_reload_error'] else ''))
    return 0 if rows and all(row['current'] for row in rows) else 1


if __name__ == "__main__":
    if '--served-sha' in sys.argv[1:]:
        raise SystemExit(served_main())
    asyncio.run(run_stdio_server())
