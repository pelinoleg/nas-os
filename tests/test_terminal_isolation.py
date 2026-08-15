"""Two terminal tabs must be two separate windows.

os.forkpty() hands the master fd back to the parent WITHOUT close-on-exec, so every shell
started afterwards inherited the master of every terminal opened before it. The symptom
was demonstrated on the box, not deduced: `echo cmd >&7` typed in the second tab ran that
command in the FIRST one, and reading fd 7 showed what was being typed there. Both shells
run as the same user, so this is not a privilege escalation — it is that closing one tab's
window was no protection at all, including for a shell left open on a screen someone else
can reach.

The test reproduces exactly that: open a terminal, keep it, open a second one, and let the
second one's shell look for the first one's master fd among its own. It is a behaviour
test on purpose — the fix is one flag on one fd, the kind of thing that survives reading
and dies in a rewrite.
"""
import importlib.util
import os
import select
import signal
import unittest

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


def _read_until_eof(fd, limit=4096, timeout=10):
    """Everything the child wrote before it exited (the pty gives EIO, not EOF)."""
    out = b""
    while len(out) < limit:
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            break
        try:
            chunk = os.read(fd, 1024)
        except OSError:              # EIO — the last slave closed, i.e. the child is gone
            break
        if not chunk:
            break
        out += chunk
    return out.decode("utf-8", "replace")


class SecondTabCannotReachTheFirst(unittest.TestCase):

    def setUp(self):
        self.kids = []
        self.fds = []

    def tearDown(self):
        for pid in self.kids:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def open_terminal(self, argv):
        """A terminal session the way the panel opens one. The child never returns."""
        pid, master = nas._pty_fork()
        if pid == 0:                                  # child: becomes the shell, or dies
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        self.kids.append(pid)
        self.fds.append(master)
        return pid, master

    def test_the_second_shell_does_not_hold_the_first_ones_master(self):
        _pid_a, master_a = self.open_terminal(["sleep", "30"])     # tab one, still open
        probe = ("test -e /proc/self/fd/%d && echo LEAK || echo CLEAN" % master_a)
        _pid_b, master_b = self.open_terminal(["sh", "-c", probe])  # tab two
        said = _read_until_eof(master_b)
        self.assertIn("CLEAN", said,
                      "tab two inherited tab one's terminal: %r" % said)
        self.assertNotIn("LEAK", said)

    def test_the_parent_can_still_talk_to_its_own_terminal(self):
        # close-on-exec must not be confused with closing it: the panel still needs the
        # master to type into the shell and to read it back
        _pid, master = self.open_terminal(["sh", "-c", "read x; echo GOT:$x"])
        os.write(master, b"ping\n")
        self.assertIn("GOT:ping", _read_until_eof(master))

    def test_the_child_still_owns_a_working_terminal(self):
        # the slave side is the child's stdin/stdout/stderr — a fix that broke that would
        # produce a panel where every terminal opens dead
        _pid, master = self.open_terminal(["sh", "-c", "tty >/dev/null && echo ISATTY"])
        self.assertIn("ISATTY", _read_until_eof(master))


if __name__ == "__main__":
    unittest.main()
