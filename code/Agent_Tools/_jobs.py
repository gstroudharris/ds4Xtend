# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""Background-process manager for the execute tool — handles, goals, leases, and GUARANTEED cleanup.

The execute tool can launch a long-lived process (a dev server, a watcher, a long task). The agent gets a
stable `job_id` (+ pid/pgid) to poll and stop it. But cleanup must NEVER depend on the agent remembering, so a
process is reaped the moment ANY of these fire (whichever is first), in layers so a single failure is caught:

  1. start_new_session + killpg  — every job is its own session/group; killing the GROUP reaps children too.
  2. wall-clock deadline (janitor)— SIGTERM -> grace -> SIGKILL once max_lifetime elapses.
  3. lease (janitor)             — a run-scoped job dies if the agent stops polling it (lease lapses).
  4. run-scoped cleanup          — the frontend reaps a run's jobs when the agent run ends.
  5. backend atexit / SIGTERM    — clean sidecar shutdown killpg's every job.
  6. startup sweep               — persisted (pid,pgid,starttime); a fresh backend kills survivors of a sidecar
                                   that was SIGKILLed (PID-reuse-safe via /proc start-time match).
  7. kernel limits (preexec)     — PR_SET_PDEATHSIG (child dies with the parent) + RLIMIT_CPU (kills a spinner).

PDEATHSIG note: the man page warns it is keyed to the *spawning thread*, which would be fatal from a
ThreadingHTTPServer request thread. So ALL spawning happens on one long-lived thread here, keying it to a thread
that lives as long as the process. The startup sweep (6) — not PDEATHSIG — is the deterministic backstop.
"""
import ctypes, json, os, queue, re, resource, signal, socket, subprocess, threading, time

try:                                          # glibc on Linux; absent on musl/macOS -> just lose the PDEATHSIG layer,
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)   # never hard-fail the whole sidecar at import
except OSError:
    _libc = None
PR_SET_PDEATHSIG = 1

# --- tunables (kept conservative; the frontend/config can scale these via JobManager(**opts)) ---
OUT_CAP = 256 * 1024          # bytes kept per stream (ring buffer)
JANITOR_INTERVAL = 2.0        # seconds between reap/deadline/lease scans
KILL_GRACE = 3.0              # SIGTERM -> wait -> SIGKILL window
LEASE_DEFAULT = 120           # a run-scoped job dies this long after its last poll (renewed on process_output)
LIFETIME_DEFAULT = 600        # background wall-clock default
LIFETIME_CEILING = 7200       # hard cap — a job can NEVER outlive this, whatever is requested
MAX_JOBS = 8                  # concurrent background jobs (prevents launch-fork-bombing)
READY_TIMEOUT = 30            # max wait for a ready_when condition before returning anyway
CPU_SECONDS = 600             # RLIMIT_CPU soft cap (kernel kills a CPU-bound runaway); idle servers cost ~0 CPU


def _preexec():
    """Runs in the child after fork, before exec. Kernel-level stopgaps."""
    try:
        _libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 60))
    except Exception:
        pass


def _starttime(pid):
    """Process start-time (clock ticks since boot) from /proc/<pid>/stat field 22 — identity across PID reuse."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        rparen = data.rfind(b")")               # comm may contain spaces/parens; fields start after the last ')'
        fields = data[rparen + 2:].split()
        return int(fields[19])                  # field 22 overall = index 19 after the (comm) split
    except Exception:
        return None


def _group_alive(pid):
    try:
        os.killpg(os.getpgid(pid), 0)
        return True
    except OSError:
        return False


def _pids_with_env(key, val):
    """Every pid whose environment contains key=val. Spawned processes are tagged with DS4_JOB / DS4_OWNER,
    and the environment is INHERITED across fork/setsid/exec — so this finds a daemon that setsid'd itself out
    of the process group and would otherwise escape killpg. (A child that scrubs its own env via `env -i`
    could still hide; only a cgroup/PID-namespace closes that last gap — see _wrap().)"""
    marker, out = ("%s=%s" % (key, val)).encode(), []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/environ" % name, "rb") as f:
                if marker in f.read():
                    out.append(int(name))
        except OSError:
            continue
    return out


def _sigkill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


_owner_seq = 0
_owner_lock = threading.Lock()


def _new_owner():
    """A token unique to one JobManager instance — even two created in the same process+second (tests)."""
    global _owner_seq
    with _owner_lock:
        _owner_seq += 1
        return "ds4_%d_%d_%d" % (os.getpid(), int(time.time()), _owner_seq)


class Job:
    __slots__ = ("id", "proc", "pid", "pgid", "command", "cwd", "goal", "run_id", "scope",
                 "started", "deadline", "lease_until", "lease_sec", "status", "exit_code",
                 "out", "err", "starttime", "lock")

    def __init__(self, jid, proc, command, cwd, goal, run_id, scope, lifetime, lease_sec):
        now = time.time()
        self.id, self.proc, self.pid = jid, proc, proc.pid
        self.pgid = os.getpgid(proc.pid)
        self.command, self.cwd, self.goal = command, cwd, goal
        self.run_id, self.scope = run_id, scope
        self.started, self.deadline = now, now + lifetime
        self.lease_sec, self.lease_until = lease_sec, now + lease_sec
        self.status, self.exit_code = "running", None
        self.out, self.err = bytearray(), bytearray()
        self.starttime = _starttime(proc.pid)
        self.lock = threading.Lock()

    def summary(self):
        return {"job_id": self.id, "pid": self.pid, "pgid": self.pgid, "goal": self.goal,
                "command": self.command, "status": self.status, "exit_code": self.exit_code,
                "run_id": self.run_id, "scope": self.scope,
                "age_sec": round(time.time() - self.started, 1),
                "expires_in_sec": max(0, round(self.deadline - time.time(), 1))}


class JobManager:
    def __init__(self, persist_path, env_fn):
        self.jobs = {}                 # id -> Job
        self.lock = threading.Lock()
        self.env_fn = env_fn
        self.persist_path = persist_path
        self.owner = _new_owner()      # uniquely tags THIS manager's processes (for the env-marker sweeps)
        self._n = 0
        self._spawn_q = queue.Queue()
        self._stopping = False
        self.sweep_startup()           # kill survivors of a previously SIGKILLed backend FIRST
        threading.Thread(target=self._spawn_worker, daemon=True, name="ds4-spawner").start()
        threading.Thread(target=self._janitor, daemon=True, name="ds4-janitor").start()

    # ---------- spawning (always on the single long-lived spawner thread: PDEATHSIG-safe) ----------
    def _spawn_worker(self):
        while True:
            argv, cwd, env, result = self._spawn_q.get()
            try:
                proc = subprocess.Popen(argv, cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True, preexec_fn=_preexec)
                result.put(proc)
            except Exception as e:        # surface the spawn error to the caller
                result.put(e)

    def spawn(self, argv, cwd, shown, *, goal, run_id=None, scope="run",
              max_lifetime=None, lease_sec=None, ready_when=None):
        if not (goal or "").strip():
            raise ValueError("a background process requires a 'goal' (why it is being started)")
        with self.lock:
            running = [j for j in self.jobs.values() if j.status == "running"]
            if len(running) >= MAX_JOBS:
                raise ValueError("too many background processes (%d) — stop one first with stop_process" % MAX_JOBS)
            self._n += 1
            jid = "job_%d" % self._n
        lifetime = min(LIFETIME_CEILING, int(max_lifetime or LIFETIME_DEFAULT))
        lease = int(lease_sec or LEASE_DEFAULT)
        env = dict(self.env_fn())
        env["DS4_JOB"], env["DS4_OWNER"] = self.owner + ":" + jid, self.owner   # owner-qualified so the /proc sweep can't match another backend's job_N
        result = queue.Queue()
        self._spawn_q.put((argv, cwd, env, result))
        proc = result.get()
        if isinstance(proc, Exception):
            raise proc
        job = Job(jid, proc, shown, cwd, goal, run_id, scope, lifetime, lease)
        threading.Thread(target=self._reader, args=(job, proc.stdout, job.out), daemon=True).start()
        threading.Thread(target=self._reader, args=(job, proc.stderr, job.err), daemon=True).start()
        with self.lock:
            self.jobs[jid] = job
        self._persist()
        ready = self._wait_ready(job, ready_when)
        s = job.summary()
        s["ready"] = ready
        return s

    def _reader(self, job, stream, buf):
        try:
            for chunk in iter(lambda: stream.read1(4096), b""):     # read1: return available bytes, don't block for a full 4096
                with job.lock:
                    buf.extend(chunk)
                    if len(buf) > OUT_CAP:
                        del buf[:len(buf) - OUT_CAP]    # keep the most recent OUT_CAP bytes
        except Exception:
            pass

    def _wait_ready(self, job, ready_when):
        """Block (up to READY_TIMEOUT) until a readiness condition holds, so the launch hands control back
        only once the process is usable. ready_when: {"port":N} | {"log":"regex"} | {"http":"url"} | None."""
        if not ready_when:
            return None
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if job.proc.poll() is not None:
                return False                              # it exited before becoming ready
            if "port" in ready_when:
                with socket.socket() as s:
                    s.settimeout(0.4)
                    if s.connect_ex(("127.0.0.1", int(ready_when["port"]))) == 0:
                        return True
            elif "log" in ready_when:
                with job.lock:
                    text = bytes(job.out).decode("utf-8", "replace") + bytes(job.err).decode("utf-8", "replace")
                if re.search(ready_when["log"], text):
                    return True
            elif "http" in ready_when:
                try:
                    import urllib.request
                    urllib.request.urlopen(ready_when["http"], timeout=0.5)
                    return True
                except Exception:
                    pass
            time.sleep(0.3)
        return False

    # ---------- query / control ----------
    def list(self, run_id=None):
        with self.lock:
            jobs = list(self.jobs.values())
        return [j.summary() for j in jobs if run_id is None or j.run_id == run_id]

    def output(self, job_id, tail=None):
        job = self._get(job_id)
        with job.lock:
            job.lease_until = time.time() + job.lease_sec     # polling renews the lease (proof the goal is still live)
            out = bytes(job.out).decode("utf-8", "replace")
            err = bytes(job.err).decode("utf-8", "replace")
        if tail:
            out, err = "\n".join(out.splitlines()[-int(tail):]), "\n".join(err.splitlines()[-int(tail):])
        s = job.summary()
        s.update({"stdout": out, "stderr": err})
        return s

    def stop(self, job_id):
        job = self._get(job_id)
        self._kill(job, "stopped")
        self._persist()
        return job.summary()

    def cleanup_run(self, run_id):
        with self.lock:
            targets = [j for j in self.jobs.values()
                       if j.status == "running" and j.run_id == run_id and j.scope != "session"]
        for j in targets:
            self._kill(j, "run_ended")
        self._persist()
        return {"reaped": [j.id for j in targets]}

    def _get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            raise ValueError("unknown job_id: %s — use list_processes to see active jobs" % job_id)
        return job

    # ---------- reaping ----------
    def _kill(self, job, status):
        if job.status != "running":
            return
        try:
            os.killpg(job.pgid, signal.SIGTERM)
        except OSError:
            pass
        end = time.time() + KILL_GRACE
        while time.time() < end and _group_alive(job.pid):
            time.sleep(0.1)
        if _group_alive(job.pid):
            try:
                os.killpg(job.pgid, signal.SIGKILL)
            except OSError:
                pass
        for pid in _pids_with_env("DS4_JOB", self.owner + ":" + job.id):     # catch a daemon that setsid'd out of the group
            _sigkill(pid)
        try:
            rc = job.proc.wait(timeout=KILL_GRACE)        # block briefly so we record the real signal (-9/-15)
        except Exception:
            rc = job.proc.poll()
        with job.lock:
            job.status = status
            job.exit_code = rc

    def _finalize_exited(self, job):
        rc = job.proc.poll()
        if rc is not None:
            with job.lock:
                job.status, job.exit_code = ("exited" if job.status == "running" else job.status), rc
            for pid in _pids_with_env("DS4_JOB", self.owner + ":" + job.id):   # launcher exited -> reap any daemon it left behind
                _sigkill(pid)

    def _janitor(self):
        while not self._stopping:
            time.sleep(JANITOR_INTERVAL)
            now = time.time()
            with self.lock:
                jobs = list(self.jobs.values())
            for j in jobs:
                if j.status != "running":
                    continue
                if j.proc.poll() is not None:
                    self._finalize_exited(j)
                elif now > j.deadline:
                    self._kill(j, "timed_out")
                elif j.scope == "run" and now > j.lease_until:
                    self._kill(j, "lease_expired")
            # forget long-finished jobs so the table doesn't grow unbounded
            with self.lock:
                done = [jid for jid, j in self.jobs.items()
                        if j.status != "running" and now - j.started > 1800]
                for jid in done:
                    del self.jobs[jid]
            self._persist()

    # ---------- persistence + startup sweep (survives a SIGKILLed backend) ----------
    def _persist(self):
        with self.lock:
            rows = [{"pid": j.pid, "pgid": j.pgid, "starttime": j.starttime}
                    for j in self.jobs.values() if j.status == "running"]
        try:
            tmp = self.persist_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"owner": self.owner, "jobs": rows}, f)
            os.replace(tmp, self.persist_path)
        except OSError:
            pass

    def sweep_startup(self):
        try:
            with open(self.persist_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        rows = data.get("jobs", []) if isinstance(data, dict) else (data or [])   # tolerate the old list format
        for r in rows:
            pid, pgid, st = r.get("pid"), r.get("pgid"), r.get("starttime")
            if pid and _starttime(pid) == st:          # PID-reuse-safe: only kill if it is STILL our process
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
        old_owner = data.get("owner") if isinstance(data, dict) else None
        if old_owner:                                  # also reap any setsid escapees of the dead backend
            for pid in _pids_with_env("DS4_OWNER", old_owner):
                _sigkill(pid)
        try:
            os.remove(self.persist_path)
        except OSError:
            pass

    def shutdown(self):
        """atexit / SIGTERM / SIGINT: reap every job immediately (no grace)."""
        self._stopping = True
        with self.lock:
            jobs = list(self.jobs.values())
        for j in jobs:
            try:
                os.killpg(j.pgid, signal.SIGKILL)
            except OSError:
                pass
        for pid in _pids_with_env("DS4_OWNER", self.owner):   # incl. daemons that setsid'd out of their group
            _sigkill(pid)
        try:
            os.remove(self.persist_path)
        except OSError:
            pass
