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
import json
import sys
from typing import Any, Mapping

from plangraph.spawner.clients import effective_child_base_url
from plangraph.spawner.leg_state import leg_status
from plangraph.spawner.resolver import (
    DEFAULT_FALLBACK_MODELS,
    detect_model_family,
    get_catalog_models,
    is_model_available,
    resolve_review_plan,
    score_model,
)
from plangraph.spawner.risk import evaluate_risk
from plangraph.spawner.runner import JobInfo, SpawnerRunner

TOOLS_METADATA = [
    {
        "name": "plangraph_agent_spawn",
        "description": "Spawn an agent task via OmniRoute ACP gateway or dry-run",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task instructions for the agent"},
                "model_or_combo": {"type": "string", "description": "Target model or combo (default 'l2-worker')"},
                "role": {"type": "string", "enum": ["implement", "review", "orchestrate", "drive"], "default": "implement"},
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
        "description": "Send steering instructions or context to an active job",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Target job ID"},
                "message": {"type": "string", "description": "Steering message or instructions"},
            },
            "required": ["job_id", "message"],
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


for _tool in TOOLS_METADATA:
    if _tool['name'] in ('plangraph_agent_status', 'plangraph_agent_cancel'):
        _tool['inputSchema']['properties']['run_id'] = {'type': 'string'}
        _tool['inputSchema'].pop('required', None)
    if _tool['name'] == 'plangraph_agent_spawn':
        _tool['inputSchema']['required'] = ['prompt', 'card']
        _tool['inputSchema']['properties'].update({
            'card': {'type': 'object'}, 'override_reason': {'type': 'string'},
            'writer_families': {'type': 'array', 'items': {'type': 'string'}},
            'review_scope': {'type': 'string', 'enum': ['per-fixer', 'batch']},
            'review_of': {'type': 'string'}, 'no_fallthrough': {'type': 'boolean'},
            'seats': {'type': 'boolean', 'description': 'role review: spawn every seat route picks (per writer part); inline-only legs get card.diff_ref inlined'}})
        _tool['inputSchema']['properties']['role']['enum'] = ['implement', 'review']
        _tool['inputSchema']['properties']['model_or_combo']['description'] = 'Exceptional audited override only; requires override_reason'
for _verb in ('route', 'ps', 'result'):
    TOOLS_METADATA.append({'name': 'plangraph_agent_' + _verb,
        'description': 'Card-based agent ' + _verb,
        'inputSchema': {'type': 'object', 'properties': {
            'card': {'type': 'object'}, 'role': {'type': 'string'},
            'run_id': {'type': 'string'}, 'writer_families': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['card'] if _verb == 'route' else (['run_id'] if _verb == 'result' else [])}})


for _tool in TOOLS_METADATA:
    if _tool['name'] in ('plangraph_agent_route', 'plangraph_agent_spawn'):
        _tool['inputSchema']['properties'].update({
            'require_bench': {'type': 'boolean'}, 'bench_db': {'type': 'string'}})


class MCPSpawnerServer:
    """Server implementing the Model Context Protocol (MCP) JSON-RPC standard."""

    def __init__(self, runner: SpawnerRunner | None = None, *, state=None):
        self.runner = runner or SpawnerRunner()
        from .runner import spawn_service
        self.service = spawn_service(self.runner, state=state)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Execute tool and return structured payload."""
        from .bench_entry import benchmark_options
        try:
            options = benchmark_options(arguments.get('require_bench', False), arguments.get('bench_db')) if name in ('plangraph_agent_route', 'plangraph_agent_spawn') else {}
        except Exception as exc:
            if name in ('plangraph_agent_route', 'plangraph_agent_spawn'):
                self.service.save_refusal(exc, arguments.get('role', 'implement'))
            raise
        if name == 'plangraph_agent_route':
            return self.service.route(arguments['card'], arguments.get('role', 'implement'),
                arguments.get('writer_families', ()), arguments.get('review_of'), **options).to_dict()
        if name == 'plangraph_agent_ps':
            return self.service.ps()
        if name == 'plangraph_agent_result' or (name == 'plangraph_agent_status' and 'run_id' in arguments):
            return self.service.result(arguments['run_id'])
        if name == 'plangraph_agent_cancel' and 'run_id' in arguments:
            return await self.service.cancel(arguments['run_id'])
        if name == "plangraph_agent_spawn":
            if 'prompt' not in arguments:
                exc = KeyError('prompt')
                self.service.save_refusal(exc, arguments.get('role', 'implement'))
                raise exc
            if not arguments.get('card'):
                from plangraph.planner.routing import SpawnRefused
                exc = ParameterRefused('spawn by card; card required')
                self.service.save_refusal(exc, arguments.get('role', 'implement'))
                raise exc
            return await self.service.spawn(card=arguments.get('card'), prompt=arguments['prompt'],
                role=arguments.get('role', 'implement'), model=arguments.get('model_or_combo'),
                override_reason=arguments.get('override_reason'), review_of=arguments.get('review_of'),
                writer_families=arguments.get('writer_families', ()),
                review_scope=arguments.get('review_scope'), seats=arguments.get('seats', False),
                dry_run=arguments.get('dry_run', False), wait_seconds=arguments.get('wait_seconds', 0),
                no_fallthrough=arguments.get('no_fallthrough', False), **options)

        if name == "plangraph_agent_status":
            job_id = arguments.get("job_id")
            res = await self.runner.status(job_id=job_id)
            if isinstance(res, JobInfo):
                return res.to_dict()
            if isinstance(res, list):
                return {"jobs": [j.to_dict() for j in res]}
            return res

        if name == "plangraph_agent_steer":
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
            all_models = get_catalog_models()
            active_jobs = [j for j in self.runner.jobs.values() if j.status == "running"]
            return {
                "status": "healthy",
                "spawner_version": "2.0.0",
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
                from plangraph.planner.routing import BenchRequired, BenchmarkError, SpawnRefused
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


async def run_stdio_server() -> None:
    """Run MCP server over standard input and output."""
    server = MCPSpawnerServer()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while not reader.at_eof():
        line = await reader.readline()
        if not line:
            break
        req = {}
        try:
            req = json.loads(line.decode("utf-8").strip())
            resp = await server.handle_rpc_request(req)
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except (json.JSONDecodeError, UnicodeDecodeError):
            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': None,
                'error': {'code': -32700, 'message': 'Parse error'}}) + '\n')
            sys.stdout.flush()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('stdio_failed: %s', type(exc).__name__)
            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': req.get('id') if isinstance(req, dict) else None,
                'error': {'code': -32603, 'message': 'Internal error'}}) + '\n')
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(run_stdio_server())
