#!/usr/bin/env python3
"""Tests for the background-process JobManager (_jobs.py) — every cleanup layer must actually reap.

Run:  python3 test_jobs.py        (these spawn real short-lived processes; ~10s)
The janitor interval + kill grace are shrunk so deadline/lease tests run fast.
"""
import json, os, signal, subprocess, tempfile, time, unittest
import _jobs

_jobs.JANITOR_INTERVAL = 0.25
_jobs.KILL_GRACE = 0.4


def env():
    e = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    e.setdefault("PATH", "/usr/bin:/bin")
    return e


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Base(unittest.TestCase):
    def mgr(self):
        pf = os.path.join(tempfile.mkdtemp(prefix="ds4jobs_"), "jobs.json")
        m = _jobs.JobManager(pf, env)
        self.addCleanup(m.shutdown)
        return m


class TestJobManager(Base):
    def test_spawn_list_output_stop(self):
        m = self.mgr()
        s = m.spawn(["bash", "-c", "echo HELLO; sleep 30"], "/tmp", "echo+sleep", goal="demo")
        self.assertEqual(s["status"], "running")
        jid, pid = s["job_id"], s["pid"]
        time.sleep(0.3)
        self.assertTrue(alive(pid))
        self.assertIn(jid, [j["job_id"] for j in m.list()])
        self.assertIn("HELLO", m.output(jid)["stdout"])
        m.stop(jid)
        time.sleep(0.3)
        self.assertFalse(alive(pid))

    def test_group_reap_kills_grandchild(self):
        m = self.mgr()
        s = m.spawn(["bash", "-c", "sleep 30 & echo $!; wait"], "/tmp", "fork", goal="demo")
        time.sleep(0.3)
        grandchild = int(m.output(s["job_id"])["stdout"].split()[0])
        self.assertTrue(alive(grandchild))
        m.stop(s["job_id"])
        time.sleep(0.5)
        self.assertFalse(alive(s["pid"]))
        self.assertFalse(alive(grandchild))      # killpg reaped the whole group, not just the leader

    def test_deadline_kill(self):
        m = self.mgr()
        s = m.spawn(["sleep", "30"], "/tmp", "sleep", goal="demo", max_lifetime=1)
        time.sleep(2.0)
        self.assertFalse(alive(s["pid"]))
        st = next(j for j in m.list() if j["job_id"] == s["job_id"])
        self.assertEqual(st["status"], "timed_out")

    def test_lease_expiry_kills_unpolled(self):
        m = self.mgr()
        s = m.spawn(["sleep", "30"], "/tmp", "sleep", goal="demo", lease_sec=1, max_lifetime=60)
        time.sleep(2.0)                          # never polled -> lease lapses -> killed
        self.assertFalse(alive(s["pid"]))

    def test_lease_renew_keeps_alive(self):
        m = self.mgr()
        s = m.spawn(["sleep", "30"], "/tmp", "sleep", goal="demo", lease_sec=1, max_lifetime=60)
        for _ in range(4):
            time.sleep(0.5)
            m.output(s["job_id"])                # polling renews the lease
        self.assertTrue(alive(s["pid"]))         # survived 2s only because we kept polling
        m.stop(s["job_id"])

    def test_readiness_port(self):
        m = self.mgr()
        s = m.spawn(["python3", "-m", "http.server", "8769"], "/tmp", "server",
                    goal="serve", ready_when={"port": 8769}, max_lifetime=60)
        self.assertTrue(s["ready"])              # launch returned only once the port was accepting
        m.stop(s["job_id"])

    def test_cleanup_run_only_reaps_that_run(self):
        m = self.mgr()
        a = m.spawn(["sleep", "30"], "/tmp", "s", goal="demo", run_id="R1", max_lifetime=60)
        b = m.spawn(["sleep", "30"], "/tmp", "s", goal="demo", run_id="R2", max_lifetime=60)
        m.cleanup_run("R1")
        time.sleep(0.4)
        self.assertFalse(alive(a["pid"]))
        self.assertTrue(alive(b["pid"]))         # a different run's job is untouched
        m.stop(b["job_id"])

    def test_session_scope_survives_run_cleanup(self):
        m = self.mgr()
        s = m.spawn(["sleep", "30"], "/tmp", "s", goal="demo", run_id="R1", scope="session", max_lifetime=60)
        m.cleanup_run("R1")
        time.sleep(0.3)
        self.assertTrue(alive(s["pid"]))         # session-scoped jobs outlive a run
        m.stop(s["job_id"])

    def test_setsid_escapee_reaped(self):
        # A daemon that setsid's into its OWN session escapes killpg — the env-marker /proc sweep must still reap it.
        m = self.mgr()
        s = m.spawn(["bash", "-c", "setsid sleep 300 >/dev/null 2>&1 & echo $!; sleep 1"], "/tmp",
                    "daemonize", goal="demo")
        daemon = None
        for _ in range(30):                      # capture the escapee pid while the launcher is still alive
            time.sleep(0.05)
            txt = m.output(s["job_id"])["stdout"].strip()
            if txt:
                daemon = int(txt.split()[0]); break
        self.assertIsNotNone(daemon)
        self.assertEqual(os.getsid(daemon), daemon)   # setsid: it's its OWN session leader (escaped the job's group)
        m.stop(s["job_id"])
        dead = False
        for _ in range(40):
            time.sleep(0.05)
            if not alive(daemon):
                dead = True; break
        self.assertTrue(dead)                    # DS4_JOB env-marker /proc sweep found + killed the escapee

    def test_kill_records_real_signal(self):
        m = self.mgr()
        s = m.spawn(["bash", "-c", "trap '' TERM; sleep 30"], "/tmp", "sigterm-ignorer", goal="demo")
        time.sleep(0.3)
        st = m.stop(s["job_id"])
        self.assertEqual(st["exit_code"], -signal.SIGKILL)   # we wait() after SIGKILL so -9 is recorded, not None

    def test_goal_required(self):
        m = self.mgr()
        with self.assertRaises(ValueError):
            m.spawn(["sleep", "1"], "/tmp", "x", goal="   ")

    def test_concurrency_cap(self):
        m = self.mgr()
        for _ in range(_jobs.MAX_JOBS):
            m.spawn(["sleep", "30"], "/tmp", "s", goal="demo", max_lifetime=60)
        with self.assertRaises(ValueError):
            m.spawn(["sleep", "30"], "/tmp", "s", goal="demo")

    def test_startup_sweep_kills_orphan(self):
        # Simulate a SIGKILLed backend: an orphaned group recorded in a persist file is reaped on next startup.
        d = tempfile.mkdtemp(prefix="ds4sweep_")
        pf = os.path.join(d, "jobs.json")
        p = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: alive(p.pid) and os.kill(p.pid, signal.SIGKILL))
        with open(pf, "w") as f:
            json.dump([{"pid": p.pid, "pgid": os.getpgid(p.pid), "starttime": _jobs._starttime(p.pid)}], f)
        m = _jobs.JobManager(pf, env)            # __init__ runs sweep_startup()
        self.addCleanup(m.shutdown)
        self.assertEqual(p.wait(timeout=2), -signal.SIGKILL)   # swept (wait() reaps the zombie so we see the signal)
        self.assertFalse(os.path.exists(pf))     # persist cleared

    def test_startup_sweep_pid_reuse_safe(self):
        # A persisted entry whose starttime no longer matches (PID reused) must NOT be killed.
        d = tempfile.mkdtemp(prefix="ds4safe_")
        pf = os.path.join(d, "jobs.json")
        victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: alive(victim.pid) and os.kill(victim.pid, signal.SIGKILL))
        with open(pf, "w") as f:                 # record the REAL pid but a WRONG starttime
            json.dump([{"pid": victim.pid, "pgid": os.getpgid(victim.pid), "starttime": 1}], f)
        m = _jobs.JobManager(pf, env)
        self.addCleanup(m.shutdown)
        time.sleep(0.3)
        self.assertTrue(alive(victim.pid))       # identity mismatch -> spared
        os.kill(victim.pid, signal.SIGKILL)
        victim.wait(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
