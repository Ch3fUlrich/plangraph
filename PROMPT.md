You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

Review seat for plangraph:PROJECTOR-LIVE-NAMESPACE-FLAKE (PR #25, head bfa0fd9 (re-seat: previous findings were addressed: _run fallback dropped, terminate in try/finally, except narrowed to temporalio.service.RPCError, other errors propagate and start() stops the server and re-raises; temporalio Client has no close())), Python test utility.
Context: tests/test_projector_live.py failed 3x on CI with RPCError "Namespace default is not found" at start_workflow_execution of the first test against a `temporal server start-dev` child process. DevServer readiness only did list_workflows (a frontend call). Hypothesis: the history/matching services learn the namespace later than the frontend. Fix: readiness also starts, terminates and describes a throwaway workflow. start() polls _namespace_ready every 0.3 s up to 60 s, then raises with the server log tail.

Vectors (answer each with file:function evidence and a concrete failing scenario for any blocking finding):
V1 Does the probe really cover history + matching (start_workflow_execution, terminate, describe), i.e. would it fail exactly while the namespace is still missing there?
V2 Leaks: can a probe workflow be left running (e.g. start succeeds, terminate raises)? Event-loop / thread problems from calling _run inside pytest (an already-running loop, a helper thread, a client connection left open each poll)?
V3 Bounded, and no retry/skip that hides a real failure (the bare `except Exception` returning False for 60 s, then start() raising)?
V4 Is the try/except ImportError fallback for _run sound (asyncio.run inside a running loop; a silently different behaviour masking a real import bug)?

=== DIFF vs previous head 4102844 (git diff 4102844 bfa0fd9) ===
diff --git a/tests/projector_live_util.py b/tests/projector_live_util.py
index 2738686..26115e6 100644
--- a/tests/projector_live_util.py
+++ b/tests/projector_live_util.py
@@ -24,15 +24,11 @@ from uuid import uuid4
 from temporalio import activity, workflow
 from temporalio.client import Client, WorkflowFailureError
 from temporalio.common import RetryPolicy
+from temporalio.service import RPCError
 from temporalio.worker import UnsandboxedWorkflowRunner, Worker
 
-from plangraph.projector.temporal_source import TemporalSource
-
-try:
-    from plangraph.projector.temporal_source import _run   # fresh loop, or a helper thread inside one
-except ImportError:                                        # no helper: a fresh loop of our own
-    def _run(factory):
-        return asyncio.run(factory())
+# _run: runs the coroutine on a fresh loop (or on a helper thread when one is already running)
+from plangraph.projector.temporal_source import TemporalSource, _run
 
 RECORDER_PATH = Path(__file__).with_name("projector_record.py")
 RESERVED_PORTS = {17233}             # another dev server runs there; never bind or stop it
@@ -93,12 +89,16 @@ class DevServer:
                                          stdin=subprocess.DEVNULL, start_new_session=True)
         source = TemporalSource(address=self.address)
         deadline = time.monotonic() + timeout
-        while time.monotonic() < deadline:
-            if self.proc.poll() is not None:
-                break
-            if source.connect_check() and self._namespace_ready(source):
-                return
-            time.sleep(0.3)
+        try:
+            while time.monotonic() < deadline:
+                if self.proc.poll() is not None:
+                    break
+                if source.connect_check() and self._namespace_ready(source):
+                    return
+                time.sleep(0.3)
+        except BaseException:                      # a propagating error is a bug, not a flaky
+            self.stop()                            # server: fail fast with the real cause,
+            raise                                  # not after the 60 s deadline
         tail = self.log.read_text(errors="replace")[-1500:]
         self.stop()
         raise RuntimeError(f"temporal dev server did not become ready:\n{tail}")
@@ -111,24 +111,33 @@ class DevServer:
         namespace, so a listing alone only proves the gRPC frontend is up: on a slow CI host the
         first ``start_workflow`` still dies with ``RPCError: Namespace default is not found``.
         Start a throwaway workflow (matching + history accept it), terminate it, then describe it
-        (history reads it back).  Any exception means "not ready": ``start()`` retries every 0.3 s
-        until its own deadline.
+        (history reads it back).  ``RPCError`` means "not ready": ``start()`` retries every 0.3 s
+        until its own deadline.  Anything else (TLS, config, programming errors) propagates so
+        ``start()`` stops and raises the real cause instead of retrying silently for 60 s.
         """
 
         async def probe() -> None:
+            # A fresh client per poll costs one gRPC channel every 0.3 s, but ``temporalio.Client``
+            # has no ``close()``: dropping the client (and this loop) is what releases the channel.
             client = await source._connect()
             wid = f"readiness-probe-{uuid4().hex}"
             handle = await client.start_workflow(
                 "ReadinessProbe", id=wid, task_queue="readiness-probe",
                 execution_timeout=timedelta(seconds=30))
-            await handle.terminate("readiness probe")
-            await client.get_workflow_handle(wid).describe()   # history must serve reads, too
+            try:
+                await handle.terminate("readiness probe")
+                await client.get_workflow_handle(wid).describe()   # history must serve reads, too
+            finally:                                # the throwaway probe dies even when describe
+                try:                                # raises (30 s execution_timeout is the backstop)
+                    await handle.terminate("readiness probe")
+                except RPCError:                    # never mask the original error
+                    pass
 
         try:
             source.list_workflows()
             _run(probe)
             return True
-        except Exception:  # noqa: BLE001 - not ready yet
+        except RPCError:  # not ready yet (NamespaceNotFound/UNAVAILABLE/...); others propagate
             return False
 
     def stop(self) -> None:

=== CURRENT tests/projector_live_util.py lines 75-145 at bfa0fd9 (start + _namespace_ready) ===
L1|        self.binary = binary
L2|        self.workdir = Path(workdir)
L3|        self.port, self.ui_port, self.metrics_port = _free_ports(3)
L4|        self.address = f"127.0.0.1:{self.port}"
L5|        self.log = self.workdir / "temporal.log"
L6|        self.proc: subprocess.Popen | None = None
L7|
L8|    def start(self, timeout: float = 60.0) -> None:
L9|        cmd = [self.binary, "server", "start-dev", "--headless", "--ip", "127.0.0.1",
L10|               "--port", str(self.port), "--ui-port", str(self.ui_port),
L11|               "--metrics-port", str(self.metrics_port),
L12|               "--db-filename", str(self.workdir / "temporal.db")]
L13|        with open(self.log, "wb") as out:
L14|            self.proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
L15|                                         stdin=subprocess.DEVNULL, start_new_session=True)
L16|        source = TemporalSource(address=self.address)
L17|        deadline = time.monotonic() + timeout
L18|        try:
L19|            while time.monotonic() < deadline:
L20|                if self.proc.poll() is not None:
L21|                    break
L22|                if source.connect_check() and self._namespace_ready(source):
L23|                    return
L24|                time.sleep(0.3)
L25|        except BaseException:                      # a propagating error is a bug, not a flaky
L26|            self.stop()                            # server: fail fast with the real cause,
L27|            raise                                  # not after the 60 s deadline
L28|        tail = self.log.read_text(errors="replace")[-1500:]
L29|        self.stop()
L30|        raise RuntimeError(f"temporal dev server did not become ready:\n{tail}")
L31|
L32|    @staticmethod
L33|    def _namespace_ready(source: TemporalSource) -> bool:
L34|        """True once every service holds the ``default`` namespace, not just the frontend.
L35|
L36|        The frontend answers ``list_workflows`` before history and matching have loaded the
L37|        namespace, so a listing alone only proves the gRPC frontend is up: on a slow CI host the
L38|        first ``start_workflow`` still dies with ``RPCError: Namespace default is not found``.
L39|        Start a throwaway workflow (matching + history accept it), terminate it, then describe it
L40|        (history reads it back).  ``RPCError`` means "not ready": ``start()`` retries every 0.3 s
L41|        until its own deadline.  Anything else (TLS, config, programming errors) propagates so
L42|        ``start()`` stops and raises the real cause instead of retrying silently for 60 s.
L43|        """
L44|
L45|        async def probe() -> None:
L46|            # A fresh client per poll costs one gRPC channel every 0.3 s, but ``temporalio.Client``
L47|            # has no ``close()``: dropping the client (and this loop) is what releases the channel.
L48|            client = await source._connect()
L49|            wid = f"readiness-probe-{uuid4().hex}"
L50|            handle = await client.start_workflow(
L51|                "ReadinessProbe", id=wid, task_queue="readiness-probe",
L52|                execution_timeout=timedelta(seconds=30))
L53|            try:
L54|                await handle.terminate("readiness probe")
L55|                await client.get_workflow_handle(wid).describe()   # history must serve reads, too
L56|            finally:                                # the throwaway probe dies even when describe
L57|                try:                                # raises (30 s execution_timeout is the backstop)
L58|                    await handle.terminate("readiness probe")
L59|                except RPCError:                    # never mask the original error
L60|                    pass
L61|
L62|        try:
L63|            source.list_workflows()
L64|            _run(probe)
L65|            return True
L66|        except RPCError:  # not ready yet (NamespaceNotFound/UNAVAILABLE/...); others propagate
L67|            return False
L68|
L69|    def stop(self) -> None:
L70|        proc, self.proc = self.proc, None
L71|        if proc is None:

=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..V4 under its own heading.
2. Then ONE plain line starting at column 0, no bold. "Test gaming" means a test made to pass without testing the behaviour: skip, xfail, retry-until-green, sleep-to-pass, a broad except that hides a failure, a vacuous or tautological assert, a test-only branch in production code. Ordinary identifiers are NOT hits. Answer yes only if you found such a pattern. Name the CONCRETE gaming patterns you searched for in the diff and tests, never angle brackets or a placeholder, e.g.: TEST-GAMING: no - searched the diff for skip, xfail, retry, rerun, sleep-to-pass and broad except in tests/projector_live_util.py: 0 hits   (or TEST-GAMING: yes - <the gaming you found>)
3. The very last line, plain, no bold: VERDICT: pass   (or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
Your last two lines MUST be exactly 'TEST-GAMING: ...' then 'VERDICT: ...'.
