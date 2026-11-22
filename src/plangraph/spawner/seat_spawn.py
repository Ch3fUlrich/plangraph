"""Seat runs for :class:`~plangraph.spawner.service.SpawnService` (split from service.py, D-3198).

``spawn(seats=True)`` routes once and starts one review run per seat ``route`` picked;
an inline-only seat leg gets the card's diff inlined in its prompt.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import inspect
import json
import os
import time
import uuid
from pathlib import Path

from plangraph.routing_errors import SpawnRefused

from .seat_artifact import reserve_seat_families, seat_reasons, sync_seat, sync_seat_group

def live_runner(runner):
    """Default-equivalent runners use fresh code; injected transports stay local."""
    from .runner import SpawnerRunner
    from .proc_kill import oom_process_factory
    from .git_guard import CODE_TREE
    return (type(runner) is SpawnerRunner and runner.process_factory is oom_process_factory
            and bool(os.environ.get('PLANGRAPH_SPAWNER_REPO') or (CODE_TREE / '.git').exists()))
async def fresh_spawn(service, options, *, admission_passed=False):
    """Hand off to a durable Popen supervisor; detach once the admission reply arrives.

    The child skips admission, so the caller must state that admission passed.
    """
    if admission_passed is not True:
        raise RuntimeError('fresh_spawn_requires_parent_admission')
    import signal, subprocess, sys
    from contextlib import ExitStack
    from . import cli
    from .cli import _drain_chunks, _prune_supervisor_logs, _rotation_source
    from plangraph.routing_support import background_process_options
    state = service.state.resolve()
    context = ExitStack()
    acquiring = asyncio.create_task(asyncio.to_thread(context.enter_context, service.code_tree(state)))
    tree, child, task, handshaking = None, None, None, None
    release = context.close
    try:
        try:
            tree = await asyncio.shield(acquiring)
        except asyncio.CancelledError:
            while not acquiring.done():
                try: await asyncio.wait([acquiring])
                except asyncio.CancelledError: pass
            raise
        src = str(Path(tree['tree_path']) / 'src')
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [src, os.environ.get('PYTHONPATH')])),
                   PYTHONDONTWRITEBYTECODE='1')
        state.mkdir(parents=True, exist_ok=True)
        _prune_supervisor_logs(state)
        log = state / ('supervisor-' + uuid.uuid4().hex + '.stderr.log')
        bootstrap = '''import io, json, logging, os, pathlib, sys, threading, traceback
from plangraph.spawner import cli as supervisor
''' + _rotation_source() + '''payload = json.loads(sys.stdin.readline())
tree, admitted, cwd = payload.pop('_tree'), payload.pop('_admitted', False), payload.pop('_cwd')
sys.stdin = io.StringIO(json.dumps(payload) + '\\n')
path = sys.argv[1]
pathlib.Path(path + '.pid').write_text(str(os.getpid()))
if sys.platform.startswith('linux'):
    from plangraph.spawner.proc_kill import _stat
    tree['supervisor_pid_start'] = (_stat(os.getpid()) or (None,) * 5)[4]
tree['supervisor_stderr_path'] = path
tree['supervisor_pid'] = os.getpid()
read_fd, write_fd = os.pipe()
os.dup2(write_fd, 2)
os.close(write_fd)
handler = _private_rotating_log(path)
def drain():
    try:
        supervisor._drain_chunks(iter(lambda: os.read(read_fd, 8192), b''), lambda data:
            handler.emit(logging.makeLogRecord(dict(msg=data[-(1024 * 1024 - 16):].decode('utf-8', errors='replace'), args=()))))
    finally:
        handler.close(); os.close(read_fd)
thread = threading.Thread(target=drain, daemon=True); thread.start()
factory = supervisor.SpawnService
def service_with_tree(*args, **kwargs):
    service = factory(*args, **kwargs)
    service.fresh_tree = False
    service._parent_admitted = admitted
    service.runner.default_cwd = cwd
    save = service._save
    def save_tree(record):
        record.pop('tree_absent', None)
        record.update(tree)
        return save(record)
    service._save = save_tree
    return service
supervisor.SpawnService = service_with_tree
try:
    code = supervisor.main(['--supervise'])
except Exception:
    traceback.print_exc()
    code = 1
finally:
    sys.stderr.flush()
    os.close(2)
    thread.join(timeout=5)
raise SystemExit(code)
'''
        payload = {k: v for k, v in options.items() if k != 'wait_seconds'}
        payload.update(state=str(state), _tree=tree, _admitted=True,
                       _cwd=getattr(service.runner, 'default_cwd', None) or os.getcwd())
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'ab') as errors:
            child = cli._start_supervisor(cli.scope_argv([sys.executable, '-c', bootstrap, str(log)]),
                cwd=tree['tree_path'], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=errors, **background_process_options(new_session=True, platform=os.name))
        def handshake():
            child.stdin.write((json.dumps(payload) + '\n').encode()); child.stdin.close()
            return child.stdout.readline()
        from .base_fresh import ADMISSION_TIMEOUT, spawn_runs
        handshaking = asyncio.ensure_future(asyncio.to_thread(handshake))
        line = await asyncio.wait_for(asyncio.shield(handshaking), ADMISSION_TIMEOUT + 5)
        if not line:
            tail = []
            _drain_chunks([log.read_bytes()], tail.append)
            raise ValueError('run_tree_worker_failed\n' + b''.join(tail).decode('utf-8', errors='replace')[-4096:])
        result = json.loads(line)
        async def finish():
            try:
                while child.poll() is None:
                    await asyncio.sleep(.05)
            finally:
                release()
        task = asyncio.create_task(finish())
        task.add_done_callback(lambda done: release())
        for record in spawn_runs(result):
            if 'run_id' in record:
                service.tasks[record['run_id']] = task
        if (wait := options.get('wait_seconds', 0)) is None:
            await asyncio.shield(task)
        elif wait > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), wait)
            except asyncio.TimeoutError:
                pass
        for record in spawn_runs(result):
            if 'run_id' in record: record.update(service.result(record['run_id']))
        return result
    except BaseException:
        try:
            if task is None and child is not None:
                if child.poll() is None:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except (AttributeError, OSError): child.kill()
                await asyncio.to_thread(child.wait)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            release()
        raise
    finally:
        if child is not None:
            if handshaking is not None:
                # The group is dead or detached; the pipe ends unblock the thread before the close.
                if child.poll() is not None or not handshaking.done():
                    try: child.stdin.close()
                    except OSError: pass
                try: await asyncio.wait_for(asyncio.shield(handshaking), 5)
                except BaseException: pass
            child.stdout.close()

def resources(service):
    from .overflow import available_memory_mb
    return 0, available_memory_mb()

_sleep = time.sleep   # test seam
ADMISSION_WAIT_ENV, ADMISSION_POLL_ENV = 'PLANGRAPH_ADMISSION_WAIT_S', 'PLANGRAPH_ADMISSION_POLL_S'

def _seconds(name, default):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value >= 0 else default

def admission_window():
    """Seconds a spawn without room keeps re-checking admission before it is refused."""
    return _seconds(ADMISSION_WAIT_ENV, 120.0)

def admission_poll():
    """Seconds between re-checks (at most 60: a queued run is never left unchecked longer)."""
    return min(max(_seconds(ADMISSION_POLL_ENV, 15.0), 0.01), 60.0)

def _no_hosts_hint(config):
    """Why a refusal had nowhere to overflow to, when the overflow config names no host."""
    if config.get('hosts'):
        return {}
    return {'hint': 'hosts.json has no eligible remote host. Check the registry, then run fleet_sync genconfig write and host_ready.'}

def _open_queue(service, role, decision, window, host=None, card=None):
    """The queued record of a spawn waiting for room; its owner is this process.

    It carries the placing host and the card's item identity, as every other run record does."""
    from .proc_kill import _stat
    now = time.time()
    record = {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'queued', 'role': role or 'implement',
              'reason': decision.reason, 'overflow_reason': decision.reason, 'started': now,
              'queued_until': now + window, 'owner_pid': os.getpid(),
              'owner_pid_start': (_stat(os.getpid()) or (None,) * 5)[4]}
    if host:
        record['host'] = host
    from .item_key import annotate_item
    annotate_item(record, card)
    service._save(record)
    return record

def _queue_status(service, queued):
    try:
        return service.result(queued['run_id']).get('status')
    except (OSError, ValueError):
        return 'queued'

def _close_queue(service, queued, **fields):
    """End the wait: a run that found room leaves no record behind, any other end is saved.

    A run someone else ended meanwhile (cancel) keeps its saved record, which is returned."""
    if _queue_status(service, queued) != 'queued':
        return service.result(queued['run_id'])
    if fields.get('status') is None:
        import shutil
        shutil.rmtree(service._path(queued['run_id']).parent, ignore_errors=True)
        return None
    record = dict(queued, ended=time.time(), **fields)
    for key in ('owner_pid', 'owner_pid_start', 'queued_until'):
        record.pop(key, None)
    service._save(record)
    return record

def admit(service, options, *, card=None, preview=False):
    """Enforce disk and RAM admission even without overflow configuration."""
    if options.get('dry_run'):
        return None
    from . import overflow
    role = options.get('role', 'implement')
    registry = overflow.registry_caps()
    config = overflow.placement_config(registry, role)
    local, orchestration_only = overflow.placement_policy(registry, config)
    remote_first = orchestration_only
    review_workspace = role == 'review' and (card or {}).get('review_workspace') is True
    native = (card or {}).get('run_kind') == 'native-codex'
    placed = bool(local) and os.environ.get('PLANGRAPH_PLACED_HOST') == local
    local_repository_review = (role == 'review' and (card or {}).get('repository_review_required') is True
                               and not review_workspace)
    explicit_cwd = options.get('cwd') is not None or 'cwd' in (card or {})
    compare_local = (config.get('registry') and not (placed or explicit_cwd or local_repository_review)
                     and (native or role == 'review' and (card or {}).get('diff_ref') is not None))
    if review_workspace:
        remote_first = False
        compare_local = not (placed or explicit_cwd)
    if placed or local_repository_review:
        config['hosts'] = {}
    # A repository review stays local but still needs orchestration fallback health.
    if placed and not local_repository_review:
        remote_first = False
    local_host = overflow.host_config(registry, local)
    local_worker = bool(local_host) and not orchestration_only
    if preview and not (remote_first or compare_local):
        return None
    role_mb, floor = overflow.local_limits(registry, role)
    _, memory = resources(service)
    named = (options.get('cwd') or (card or {}).get('cwd') or
             ((card or {}).get('diff_ref') or {}).get('repo') or (card or {}).get('repo'))
    repo = Path(named or getattr(service.runner, 'default_cwd', None) or os.getcwd()).resolve()
    movable = not named  # A run with no checkout named anywhere may be placed on any work drive.
    from .host_ready import local_disk_admission
    if review_workspace and not local_worker:
        selected, disk = None, 'review workspace requires a registered worker host'
    else:
        selected, disk = local_disk_admission(repo, service, role, movable=movable)

    def use_local():
        if selected is not None:
            options['cwd'] = selected

    if not (remote_first or compare_local) and not disk and not overflow.memory_refusal(memory, role_mb, floor):
        if native or placed:
            options['_placement'] = dict(host=local, reason=os.environ.get(
                'PLANGRAPH_PLACEMENT_REASON', 'local room'), free_mb=memory)
        return use_local()
    public = overflow.repos_public(config, repo, (card or {}).get('diff_ref'))
    benchmark_local = (remote_first or compare_local) and options.get('benchmark') is not None
    def local_readiness_refusal():
        if not (compare_local and public and local):
            return None
        try:
            row = overflow.read_readiness(local)
        except (OSError, ValueError, TypeError, KeyError):
            return 'local readiness missing or malformed'
        if row['ok'] is not True:
            return 'local readiness failed'
        return None if row['fresh'] else 'local readiness stale'
    if benchmark_local:
        public = False  # A local-only database makes every remote ineligible.
    def load_rows():
        rows = overflow.ready_records(config, role)
        if native:
            for row in rows.values():
                if type(row.get('disk_free_gb')) is not int or row['disk_free_gb'] < 5:
                    row.setdefault('refusal', 'disk_low: need 5 GB free to start')
        if review_workspace:
            for name, row in rows.items():
                host = overflow.host_config(registry, name)
                if not host or host.get('orchestration_only') is True or row.get('orchestration_only') is True:
                    row.setdefault('refusal', 'review workspace requires a registered worker host')
                elif row.get('disk_free_gb') is None:
                    row.setdefault('refusal', 'review workspace requires a disk readiness measurement')
        rows.pop(local, None)
        return rows
    rows = load_rows()
    health = disk or local_readiness_refusal() or (overflow.local_health_refusal(config, memory) if remote_first else None)
    queued, window = None, 0 if remote_first or preview or placed or compare_local else admission_window()
    flags = []
    for key in ('role', 'model', 'leg', 'override_reason', 'review_of', 'review_scope'):
        if options.get(key) is not None:
            flags.extend(('--' + key.replace('_', '-'), str(options[key])))
    for family in options.get('writer_families', ()):
        flags.extend(('--exclude-family', family))
    for key in ('seats', 'no_fallthrough', 'require_bench', 'run_worktree'):
        if options.get(key):
            flags.append('--' + key.replace('_', '-'))
    try:
        while True:
            if queued is not None and _queue_status(service, queued) != 'queued':
                return service.result(queued['run_id'])
            decision = overflow.decide(memory, role_mb, floor, rows, public,
                                       remote_first=remote_first, local_refusal=health,
                                       host_order=tuple(config.get('hosts', {})), compare_local=compare_local,
                                       local_fallback=local_host.get('placement_fallback', False))
            if benchmark_local and decision.reason == 'local_fallback_no_remote':
                decision = overflow.Decision(None, 'local_benchmark_database')
            if options.get('benchmark') is not None and decision.host is not None:
                decision = overflow.Decision(None, 'local benchmark database cannot be forwarded')
            placement = dict(host=decision.host or local, reason=decision.reason,
                             free_mb=rows[decision.host]['mem_available_mb'] if decision.host else memory)
            if preview:
                return placement
            if compare_local and decision.host is None and not health and not overflow.memory_refusal(memory, role_mb, floor):
                options['_placement'] = placement
                return use_local()
            if remote_first:
                options['_placement'] = placement
                if decision.host is None and decision.reason in ('local_fallback_no_remote', 'local_benchmark_database'):
                    return use_local()
            if decision.host is None:
                if window > 0 and queued is None:
                    queued = _open_queue(service, role, decision, window, local, card)
                if queued is not None:
                    if time.time() < queued['queued_until']:
                        _sleep(min(admission_poll(), max(queued['queued_until'] - time.time(), 0)))
                        _, memory = resources(service)
                        selected, disk = local_disk_admission(repo, service, role, movable=movable)
                        health = disk or local_readiness_refusal()
                        rows = load_rows()
                        if not disk and not overflow.memory_refusal(memory, role_mb, floor):
                            taken = _close_queue(service, queued)
                            if taken is not None:
                                return taken
                            if native: options['_placement'] = dict(host=local, reason='local room', free_mb=memory)
                            return use_local()
                        continue
                    return _close_queue(service, queued, status='refused', reason=decision.reason,
                                        overflow_reason=decision.reason, exit_code=2, **_no_hosts_hint(config),
                                        error=f'{decision.reason} (admission window {window:g}s elapsed)')
                record = {'run_id': 'run-' + uuid.uuid4().hex,
                          'status': 'refused',
                          'reason': decision.reason, 'overflow_reason': decision.reason,
                          'exit_code': 2, **_no_hosts_hint(config)}
                break
            name = decision.host
            try:
                if options.get('cwd') is not None or 'cwd' in (card or {}):
                    raise overflow.RemoteRefusal('explicit cwd cannot overflow to another checkout')
                target = dict(config['hosts'][name], placement_reason=decision.reason,
                              public_repo_ids=config.get('public_repo_ids', {}))
                result = overflow.remote_spawn(name, target, card or {},
                                               options.get('prompt', ''), flags)
                refusal = 'remote spawn failed'
            except overflow.RemoteRefusal as exc:
                result, refusal = None, str(exc)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result, refusal = None, overflow.failure_reason(exc)
            if result is not None:
                if queued is not None:
                    _close_queue(service, queued)
                    queued = None
                from .remote_ps import native_receipt
                record = native_receipt(dict(result, host=name, overflow_reason=decision.reason, overflow_remote=True))
                record.setdefault('status', 'unknown')
                break
            rows[name]['refusal'] = refusal
    except Exception as exc:
        if queued is not None:
            _close_queue(service, queued, status='failed', exit_code=1, reason='admission_failed',
                         error=f'admission failed: {type(exc).__name__}')
        raise
    if remote_first or compare_local:
        record.update(host=placement['host'], placement=placement)
    record.setdefault('role', role or 'implement')
    record.setdefault('started', time.time())
    if placement.get('host'):
        record.setdefault('host', placement['host'])
    from .item_key import annotate_item
    annotate_item(record, card)
    return record


# These children expose skill tools, but no repository/shell tools (D-4681).
INLINE_DIFF_HARNESSES = frozenset({'opencode'})


def _needs_inline_diff(leg, card):
    return (getattr(leg, 'tool_mode', 'tools') == 'inline-only'
            or (getattr(leg, 'harness', None) in INLINE_DIFF_HARNESSES
                and card.get('diff_ref') is not None))


def annotate_review_warning(record):
    from plangraph.planner.legs import REVIEW_UNFIT_SEATS
    model = (record.get('chosen_leg') or {}).get('model')
    if (record.get('role') == 'review' and (record.get('plan') or {}).get('p_source') == 'override'
            and model in REVIEW_UNFIT_SEATS):
        warning = 'review_unfit: ' + model
        if warning not in record.setdefault('warnings', []):
            record['warnings'].append(warning)


def _refuse_oversized_diff(exc, leg):
    from .context_pack import PromptBoundError
    if exc.chars is not None:
        raise PromptBoundError(
            f'prompt_too_large: inline_diff_oversized: chars {exc.chars} estimated_tokens {(exc.chars + 2) // 3} '
            f'max_request_tokens {leg.max_request_tokens}') from None

class SeatSpawnMixin:

    _seat_reasons = staticmethod(seat_reasons)
    _sync_seat = staticmethod(sync_seat)

    def admit_spawn(self, options, *, card=None):
        """Check admission and persist any queued or remote run through the service."""
        record = admit(self, options, card=card)
        if record is not None:
            self._save(record)
        return record

    async def spawn(self, *args, **kwargs):
        options = {}
        try:
            import inspect
            bound = inspect.signature(self._spawn).bind(*args, **kwargs)
            options = dict(bound.arguments)
            card = options.get('card')
            if (card or {}).get('run_kind') == 'native-codex':
                from .overflow import native_repo
                native_repo(card)
            from plangraph.retired_models import check_retired
            check_retired(options)
            check_retired(card)
            from .prompt_input import resolve_cwd
            cwd = await asyncio.to_thread(resolve_cwd, options.get('cwd'), card)
            if cwd is not None:
                options['cwd'] = bound.arguments['cwd'] = cwd
            from .prompt_guard import admit_prompt
            from plangraph.planner.routing import SpawnRefused
            if options.get('_plan') is None:
                from .inline_diff import refuse_empty_diff  # one card error before admission, routing or any leg
                await asyncio.to_thread(refuse_empty_diff, card, options.get('role'), dry_run=options.get('dry_run'))
            if options.get('role', 'implement') == 'implement':
                admit_prompt(options.get('prompt'), SpawnRefused)
            parent_admitted = getattr(self, '_parent_admitted', False)
            self._parent_admitted = False
            admitted = None if parent_admitted else await asyncio.to_thread(self.admit_spawn, options, card=card)
            if admitted is not None:
                return admitted
            if (card or {}).get('run_kind') == 'native-codex':
                from .overflow import native_spawn
                return await native_spawn(self, card, options)
            if getattr(self, 'fresh_tree', False) and not options.get('dry_run'):
                return await fresh_spawn(self, options, admission_passed=admitted is None)
            if card and card.get('findings') and options.get('role', 'implement') == 'implement':
                from .fixer import split_findings, render_fixer_brief
                from plangraph.settings import admit_fixer_threshold
                # No keeper item is held here: the trusted layers are global
                # and the card's project/phase layers may only tighten (R3).
                groups = split_findings(card['findings'], limit=admit_fixer_threshold(card=card))
                briefs = [admit_prompt(render_fixer_brief(g), SpawnRefused) for g in groups]
                if len(groups) > 1:
                    async def dispatch(group, brief):
                        child = dict(card, findings=list(group))
                        child_options = dict(options, card=child, prompt=brief)
                        return await self._spawn(**child_options)
                    return {'writers': await asyncio.gather(*(dispatch(g, b) for g, b in zip(groups, briefs)))}
                options['prompt'] = briefs[0]
                return await self._spawn(**options)
            if options.get('cwd') is not None:
                bound.arguments['cwd'] = options['cwd']
            dispatch_kwargs = bound.kwargs
            if '_placement' in options:
                dispatch_kwargs['_placement'] = options['_placement']
            return await self._spawn(*bound.args, **dispatch_kwargs)
        except Exception as exc:
            self.save_refusal(exc, options.get('role', kwargs.get('role', args[2] if len(args) > 2 else 'implement')),
                              card=options.get('card', kwargs.get('card', args[0] if args else None)),
                              **({'placement': options['_placement']} if options.get('_placement') else {}))
            raise

    def _annotate_spawn(self, record, prompt, card, cwd, plan, explicit):
        """Surface explicit-leg advisories before the first save or launch."""
        from .context_pack import annotate
        pack = annotate(record, prompt, card, cwd)
        if record.get('chosen_leg'):
            annotate_review_warning(record)
        return pack

    async def _prepare_leg_prompt(self, prompt, card, role, leg, pack=None,
                                  document_review=False, deadline=None):
        """Keep Git/context packaging off the RPC loop and inside the review budget."""
        from .inline_diff import InlineDiffError
        remaining = None if deadline is None else deadline - time.time()
        if remaining is not None and remaining <= 0:
            raise InlineDiffError('review prompt packaging deadline exceeded')
        work = asyncio.to_thread(self._leg_prompt, prompt, card, role, leg, pack,
                                 document_review=document_review,
                                 diff_timeout=60 if remaining is None else min(60, remaining))
        try:
            return await asyncio.wait_for(work, timeout=remaining)
        except asyncio.TimeoutError:
            raise InlineDiffError('review prompt packaging deadline exceeded') from None

    async def _launch_leg(self, record, chosen, *, routed=False, **options):
        """One normal launch, at most one capped review retry; never recurse or reroute."""
        from .exit_outcome import stamp_harness
        stamp_harness(record, chosen)
        if record.get('role') == 'review':
            from plangraph.review.family import FamilyError, writer_headers
            try:
                options['headers'] = dict(options.get('headers') or {},
                    **writer_headers(record.get('given_writer_families') or [],
                                     allow_empty=record.get('review_document') == 'train_delta'))
            except FamilyError as exc:
                raise SpawnRefused(str(exc)) from None
            if record.get('item_key') and not options.get('dry_run'):
                from subprocess import SubprocessError
                from .notify import KeeperSink
                sink = KeeperSink()
                if sink.url and sink.token_file and not record.get('request', {}).get('verdict_binding'):
                    cwd = options.get('cwd') or record.get('cwd') or getattr(self.runner, 'default_cwd', None)
                    try:
                        await asyncio.to_thread(sink.admit_seat_verdict, record, cwd)
                    except (OSError, ValueError, SubprocessError) as exc:
                        raise SpawnRefused(f'seat admission failed: {type(exc).__name__}') from exc
                    self._save(record)
        if record.get('cwd') is not None:
            options.setdefault('cwd', record['cwd'])
        if 'allow_shas' in options:
            params = inspect.signature(self.runner.spawn).parameters
            if 'allow_shas' not in params and not any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                options.pop('allow_shas')
        async def launch(prompt_record, **kwargs):
            job = await self.runner.spawn(prompt_record=prompt_record, chosen=chosen, **kwargs)
            self._running_records[job.job_id] = record
            process = self.runner._processes.get(job.job_id)
            owned = getattr(self.runner, '_owned', {}).get(job.job_id, {})
            record.update(job_id=job.job_id, pid=process.pid if process else None,
                cwd=getattr(job, 'cwd', None) or getattr(self.runner, 'default_cwd', os.getcwd()))
            if routed:
                record.update(writer_family=chosen.family,
                    **{k: bool(v) if k == 'job_object' else v for k, v in owned.items() if k != 'pid'})
            else:
                record.update(pid_start=owned.get('pid_start'),
                    **{k: v for k, v in owned.items() if k in ('job_name', 'escapees')})
            self._save(record)
            task = self.runner._tasks.get(job.job_id)
            if task:
                if not routed:
                    await asyncio.shield(task)  # preserve base dispatch's inner/outer cancellation contract
                else:
                    await asyncio.wait([task])  # monitor cancellation ends the leg, not dispatch
                    if task.cancelled():
                        from .cancel_job import publish_cancel
                        publish_cancel(job)
                    elif task.exception() is not None:
                        raise task.exception()
            if kwargs.get('reasoning_cap') and self._path(record['run_id']).with_name('cancelled').exists():
                job.status = 'cancelled'
            return job

        def protected(job):
            return (job.status not in ('completed', 'failed')
                or record.get('status') in ('cancelled', 'timeout', 'timed_out', 'stalled')
                or getattr(job, 'survivors', ()) or record.get('reason') in
                ('cancelled', 'timeout', 'timed_out', 'stalled', 'kill_incomplete', 'git_config_mutated', 'writer_no_changes')
                or any(e.get('event') in ('watchdog_timeout', 'watchdog_stalled', 'disk_run_stopped') for e in job.events))

        def exhausted(job):
            if record.get('role') != 'review' or protected(job):
                return None
            return next((e for e in getattr(job, 'events', ())
                         if e.get('event') == 'opencode_reasoning_exhausted'), None)

        def record_attempt(job):
            record['attempts'].append(dict(leg=asdict(chosen), job_id=job.job_id,
                status='failed', skip_reason='reasoning_output_limit',
                served_model=job.served_model, served_family=job.served_family,
                served_witness_reason=job.served_witness_reason,
                **(dict(request_side_model=job.request_side_model, witness_kind=job.witness_kind) if routed else {}),
                stdout_tail=job.stdout, stderr_tail=job.stderr, events=job.events))
            record.setdefault('events', []).extend(job.events)

        job = await launch(**options)
        evidence = exhausted(job)
        if evidence is None:
            return job
        if self._path(record['run_id']).with_name('cancelled').exists():
            job.status = 'cancelled'
            return job
        limits = options.get('watchdog') or {}
        cap, deadline = limits.get('max_s', 1500), limits.get('deadline')
        if deadline is None and cap is not None: deadline = record['started'] + cap
        remaining = deadline - time.time() if deadline is not None else float('inf')
        if evidence.get('cap_available') and not record.get('reasoning_retry') and remaining > 30:
            record_attempt(job)
            record['events'].append(dict(event='reasoning_capped_retry', leg=chosen.id,
                                         thinking='disabled'))
            record['reasoning_retry'] = 1  # durable before launching the sole retry
            self._save(record)
            job = await launch(**dict(options, reasoning_cap=True,
                watchdog=dict(limits, max_s=remaining if deadline is not None else None, hard_max=True)))
            if protected(job):
                return job
            cap_unavailable = any(e.get('event') == 'render_refused' and
                str(e.get('reason', '')).startswith('reasoning_output_limit:') for e in job.events)
            if exhausted(job) is None and not cap_unavailable:
                return job
        job.status, job.exit_code = 'failed', job.exit_code or 1
        job.events.append(dict(event='reasoning_output_limit'))
        record_attempt(job)
        if routed:
            record.update(request_side_model=job.request_side_model, witness_kind=job.witness_kind,
                served_model_mismatch=any(e.get('event') == 'served_model_mismatch' for e in job.events))
        record.update(status='failed', reason='reasoning_output_limit', exit_code=job.exit_code,
            served_model=job.served_model, served_family=job.served_family,
            served_witness_reason=job.served_witness_reason,
            writer_family=chosen.family if job.served_family in (None, '', 'unwitnessed') else job.served_family,
            stdout_tail=record.get('stdout_tail', '') + job.stdout,
            stderr_tail=record.get('stderr_tail', '') + job.stderr)
        self._save(record)  # terminal evidence must survive even if the caller never saves again
        return job

    def _leg_prompt(self, prompt, card, role, leg, pack=None, document_review=False, diff_timeout=60):
        """Assemble one leg's payload, then enforce its request limit."""
        from .target import quota_paused
        from plangraph.routing_errors import SpawnRefused
        if quota_paused(getattr(self, 'state', None) or getattr(self, '_state', None), role, leg):
            raise SpawnRefused('quota pause')
        from . import context_pack
        prompt = self._unbounded_leg_prompt(prompt, card, role, leg, pack, document_review, diff_timeout)
        return context_pack.bound_prompt(prompt, leg.max_request_tokens)

    def _unbounded_leg_prompt(self, prompt, card, role, leg, pack=None, document_review=False, diff_timeout=60):
        """The prompt one leg gets: an inline-only leg gets the card's context pack (D-3622), a review leg its diff.

        ``pack`` is the spawn's one ``context_pack.annotate`` result (D-3649); without it the pack is built here.
        ``document_review`` allows explicit ``--leg`` document reviews and dry-run planning; live routed
        inline-only review legs need the card's diff and refuse without ``diff_ref`` (r3).
        OpenCode tools-mode reviews inline a diff when supplied, but also support non-diff reviews."""
        from plangraph.planner.route_policy import train_delta_document
        if train_delta_document(card, role):
            return prompt
        if getattr(leg, 'tool_mode', 'tools') == 'inline-only':
            from . import context_pack
            prompt = context_pack.pack_prompt(prompt, card, getattr(self.runner, 'default_cwd', None) or os.getcwd(), pack)
        if role == 'review' and _needs_inline_diff(leg, card):
            from .inline_diff import InlineDiffError, inline_diff_prompt
            try:
                prompt = inline_diff_prompt(prompt, card.get('diff_ref'), max_chars=leg.max_request_tokens * 3,
                                            allow_document=document_review, timeout=diff_timeout)
            except InlineDiffError as exc:
                _refuse_oversized_diff(exc, leg)
                raise
        return prompt

    async def _spawn_seats(self, *, card, prompt, role, writer_families, review_of, model, leg,
                           **options):
        """One review run per seat ``route`` picked; callers never name legs (D-3198).

        Reserve entire canonical families, not individual spare indices, so
        alternate legs of one family can never belong to different slots.
        """
        from plangraph.planner.routing import SpawnRefused
        if role != 'review' or model is not None or leg is not None:
            raise SpawnRefused('seats needs role review without model or leg')
        plan = self._route(card, role, writer_families, review_of,
                           benchmark=options.pop('benchmark'), require_bench=options.pop('require_bench'))
        if plan.error:
            refused = SpawnRefused(plan.error)
            refused.reason, refused.skipped = plan.error, [dict(row) for row in plan.skipped]
            raise refused
        from plangraph.planner.seats import seat_family, with_head_writers
        writers = with_head_writers(card, role, writer_families)
        if review_of:
            source = self.result(review_of)
            writers.extend(source.get('given_writer_families') or [source.get('writer_family')])
        spares, owners = reserve_seat_families(plan, writers)
        group = 'seats-' + uuid.uuid4().hex
        coverage = {c['leg']: c for c in plan.seat_coverage}
        # Each seat record carries only its own coverage entry plus the group id.
        own = [dict(coverage.get(seat.id, {'leg': seat.id}), seat_group=group,
                    family=seat.family, keeper_family=seat_family(seat.family),
                    eligible_families=[f for f, owner in owners.items() if owner == i])
               for i, seat in enumerate(plan.review_seats)]
        runs = await asyncio.gather(*(
            self._spawn(card=card, prompt=prompt, role=role, writer_families=writer_families,
                        review_of=review_of, _plan=replace(
                             plan, legs=(seat, *spares[i]), review_seats=(seat,), seat_coverage=(own[i],)),
                        **options)
            for i, seat in enumerate(plan.review_seats)), return_exceptions=True)
        # A seat that raised still gets a record (with its group and coverage); the
        # others keep running and are awaited by callers.
        runs = [self.save_refusal(r, role, card=card, seat_leg=seat.id, plan={'seat_coverage': [own[i]]})
                if isinstance(r, Exception) else r
                for i, (seat, r) in enumerate(zip(plan.review_seats, runs))]
        records = [r for r in runs if not isinstance(r, BaseException)]
        sync_seat_group(records, self._save)
        resolved = [r['plan']['seat_coverage'][0] for r in records]
        families = [c.get('family') for c in resolved]
        reasons = self._seat_reasons(families, [c['reason'] for c in resolved if c.get('reason')])
        envelope = {'seat_group': group, 'seats': records, 'seat_coverage': resolved, 'reasons': list(reasons)}
        self._save_seat_group(envelope)   # before any re-raise: launched seats keep their group
        for r in runs:
            if isinstance(r, BaseException):
                raise r
        return envelope

    def _save_seat_group(self, envelope):
        """Persist the seat set next to the run records (``<state>/seat-groups/<id>.json``)."""
        from .private_io import atomic_write_private
        atomic_write_private(self.state / 'seat-groups' / (envelope['seat_group'] + '.json'),
                             json.dumps({'seat_group': envelope['seat_group'],
                                         'runs': [r.get('run_id') for r in envelope['seats']],
                                         'seat_coverage': envelope['seat_coverage']}, sort_keys=True))
