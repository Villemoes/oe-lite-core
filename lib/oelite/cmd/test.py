import errno
import os
import select
import shutil
import signal
import sys
import tempfile
import time
import unittest

import oelite.util
import oelite.signal

description = "Run tests of internal utility functions"
def add_parser_options(parser):
    parser.add_option("-s", "--show",
                      action="store_true", default=False,
                      help="Show list of tests")

class OEliteTest(unittest.TestCase):
    def setUp(self):
        self.wd = tempfile.mkdtemp()
        os.chdir(self.wd)

    def tearDown(self):
        os.chdir("/")
        shutil.rmtree(self.wd)

    def test_makedirs(self):
        """Test the semantics of oelite.util.makedirs"""

        makedirs = oelite.util.makedirs
        touch = oelite.util.touch
        self.assertIsNone(makedirs("x"))
        self.assertIsNone(makedirs("x"))
        self.assertIsNone(makedirs("y/"))
        self.assertIsNone(makedirs("y/"))
        self.assertIsNone(makedirs("x/y/z"))
        # One can create multiple leaf directories in one go; mkdir -p
        # behaves the same way.
        self.assertIsNone(makedirs("z/.././z//w//../v"))
        self.assertTrue(os.path.isdir("z/w"))
        self.assertTrue(os.path.isdir("z/v"))

        self.assertIsNone(touch("x/a"))
        with self.assertRaises(OSError) as cm:
            makedirs("x/a")
        self.assertEqual(cm.exception.errno, errno.ENOTDIR)
        with self.assertRaises(OSError) as cm:
            makedirs("x/a/z")
        self.assertEqual(cm.exception.errno, errno.ENOTDIR)

        self.assertIsNone(os.symlink("a", "x/b"))
        with self.assertRaises(OSError) as cm:
            makedirs("x/b")
        self.assertEqual(cm.exception.errno, errno.ENOTDIR)
        with self.assertRaises(OSError) as cm:
            makedirs("x/b/z")
        self.assertEqual(cm.exception.errno, errno.ENOTDIR)

        self.assertIsNone(os.symlink("../y", "x/c"))
        self.assertIsNone(makedirs("x/c"))
        self.assertIsNone(makedirs("x/c/"))

        self.assertIsNone(os.symlink("nowhere", "broken"))
        with self.assertRaises(OSError) as cm:
            makedirs("broken")
        self.assertEqual(cm.exception.errno, errno.ENOENT)

        self.assertIsNone(os.symlink("loop1", "loop2"))
        self.assertIsNone(os.symlink("loop2", "loop1"))
        with self.assertRaises(OSError) as cm:
            makedirs("loop1")
        self.assertEqual(cm.exception.errno, errno.ELOOP)

class MakedirsRaceTest(OEliteTest):
    def child(self):
        signal.alarm(2) # just in case of infinite recursion bugs
        try:
            # wait for go
            select.select([self.r], [], [], 1)
            oelite.util.makedirs(self.path)
            # no exception? all right
            res = "OK"
        except OSError as e:
            # errno.errorcode(errno.ENOENT) == "ENOENT" etc.
            res = errno.errorcode.get(e.errno) or str(e.errno)
        except Exception as e:
            res = "??"
        finally:
            # Short pipe writes are guaranteed atomic
            os.write(self.w, res+"\n")
            os._exit(0)

    def setUp(self):
        super(MakedirsRaceTest, self).setUp()
        self.path = "x/" * 10
        self.r, self.w = os.pipe()
        self.children = []
        for i in range(8):
            pid = os.fork()
            if pid == 0:
                self.child()
            self.children.append(pid)

    def runTest(self):
        """Test concurrent calls of oelite.util.makedirs"""

        os.write(self.w, "go go go\n")
        time.sleep(0.01)
        os.close(self.w)
        with os.fdopen(self.r) as f:
            v = [v.strip() for v in f]
        d = {x: v.count(x) for x in v if x != "go go go"}
        # On failure this won't give a very user-friendly error
        # message, but it should contain information about the errors
        # encountered.
        self.assertEqual(d, {"OK": len(self.children)})
        self.assertTrue(os.path.isdir(self.path))

    def tearDown(self):
        for pid in self.children:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        super(MakedirsRaceTest, self).tearDown()
    
class SigPipeTest(OEliteTest):
    def run_sub(self, preexec_fn):
        from subprocess import PIPE, Popen

        sub = Popen(["yes"], stdout=PIPE, stderr=PIPE,
                    preexec_fn = preexec_fn)
        # Force a broken pipe.
        sub.stdout.close()
        err = sub.stderr.read()
        ret = sub.wait()
        return (ret, err)

    @unittest.skipIf(sys.version_info >= (3, 2), "Python is new enough")
    def test_no_restore(self):
        """Check that subprocesses inherit the SIG_IGNORE disposition for SIGPIPE."""
        (ret, err) = self.run_sub(None)
        # This should terminate with a write error; we assume that
        # 'yes' is so well-behaved that it both exits with a non-zero
        # exit code as well as prints an error message containing
        # strerror(errno).
        self.assertGreater(ret, 0)
        self.assertIn(os.strerror(errno.EPIPE), err)

    def test_restore(self):
        """Check that oelite.signal.restore_defaults resets the SIGPIPE disposition."""
        (ret, err) = self.run_sub(oelite.signal.restore_defaults)
        # This should terminate due to SIGPIPE, and not get a chance
        # to write to stderr.
        self.assertEqual(ret, -signal.SIGPIPE)
        self.assertEqual(err, "")

def run(options, args, config):
    suite = unittest.TestSuite()
    suite.addTest(MakedirsRaceTest())
    suite.addTest(OEliteTest('test_makedirs'))
    suite.addTest(SigPipeTest('test_no_restore'))
    suite.addTest(SigPipeTest('test_restore'))

    if options.show:
        for t in suite:
            print str(t), "--", t.shortDescription()
        return 0
    runner = unittest.TextTestRunner(verbosity=3)
    runner.run(suite)

    return 0
