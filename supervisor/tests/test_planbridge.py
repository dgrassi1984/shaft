"""Run the standalone relay as Grok would, without a real agent."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from supervisor import planbridge


def require_sockets(case):
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='pb-') as folder:
            probe.bind(folder + '/s')
    except PermissionError as exc:
        case.skipTest('sandbox refuses AF_UNIX bind: ' + str(exc))
    finally:
        probe.close()


class BridgeTest(unittest.TestCase):
    def setUp(self):
        require_sockets(self)
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='pb-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.address = str(self.folder / 'b')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.server.close)
        self.server.bind(self.address)
        self.server.listen()
        self.calls = []
        self.results = {}
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)
        self.server.settimeout(.1)
        def respond(c):
            with c, c.makefile('rwb') as f:
                value = json.loads(f.readline())
                if value['type'] == 'ping':
                    result = {}
                else:
                    self.calls.append(value)
                    self.results[value['id']] = event = threading.Event()
                    while not event.wait(.05) and not self.stop.is_set():
                        pass
                    result = {'content': [{'type': 'text', 'text': 'delivered'},
                                          {'type': 'image', 'mimeType': 'image/png', 'data': 'synthetic'}]}
                f.write((json.dumps(result)+'\n').encode()); f.flush()
        def accept():
            while not self.stop.is_set():
                try:
                    c, _ = self.server.accept()
                except (socket.timeout, OSError):
                    continue
                threading.Thread(target=respond, args=(c,), daemon=True).start()
        threading.Thread(target=accept, daemon=True).start()
        tools = self.folder / 'tools.json'
        tools.write_text(json.dumps([{'name': 'echo', 'inputSchema': {'type': 'object'}}]))
        self.child = subprocess.Popen([sys.executable, '-B', str(Path(planbridge.__file__)), '--tools', str(tools),
            '--socket', self.address], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_child)

    def cleanup_child(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=5)
        for pipe in (self.child.stdin, self.child.stdout, self.child.stderr):
            pipe.close()

    def send(self, ident, method, **params):
        self.child.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})+'\n')
        self.child.stdin.flush()

    def receive(self):
        import select
        self.assertTrue(select.select([self.child.stdout], [], [], 3)[0], 'bridge response timeout')
        return json.loads(self.child.stdout.readline())

    def test_initialize_list_unknown_and_parallel_results_reach_stdio(self):
        self.send(1, 'initialize', protocolVersion='2024-11-05')
        self.assertEqual(self.receive()['result']['serverInfo']['name'], 'shaft')
        self.send(2, 'tools/list')
        self.assertEqual(self.receive()['result']['tools'][0]['name'], 'echo')
        self.send(3, 'tools/call', name='unknown')
        self.assertTrue(self.receive()['result']['isError'])
        self.send(4, 'tools/call', name='echo', arguments={'n': 1})
        self.send(5, 'tools/call', name='echo', arguments={'n': 2})
        deadline = __import__('time').monotonic() + 3
        while len(self.results) < 2 and __import__('time').monotonic() < deadline:
            self.stop.wait(.01)
        self.assertEqual(len(self.results), 2)
        self.results[5].set()
        result = self.receive()
        self.assertEqual(result['id'], 5)
        self.assertEqual(result['result']['content'], [{'type': 'text', 'text': 'delivered'},
            {'type': 'image', 'mimeType': 'image/png', 'data': 'synthetic'}])
        self.results[4].set()
        self.assertEqual(self.receive()['id'], 4)

    def test_socket_disappearance_exits_with_pending_call(self):
        self.send(1, 'tools/call', name='echo')
        Path(self.address).unlink()
        self.assertEqual(self.child.wait(timeout=3), 0)

    def test_stdin_eof_exits_with_pending_call(self):
        self.send(1, 'tools/call', name='echo')
        self.child.stdin.close()
        self.assertEqual(self.child.wait(timeout=3), 0)
