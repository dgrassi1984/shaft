#!/usr/bin/env python3
"""Standalone stdlib MCP relay; no supervisor imports or tool execution."""
import argparse
import concurrent.futures
import json
import os
import queue
from pathlib import Path
import socket
import sys
import threading


def relay(address, value):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        if value.get('type') == 'ping':
            connection.settimeout(1)
        connection.connect(address)
        with connection.makefile('rwb') as stream:
            stream.write((json.dumps(value) + '\n').encode())
            stream.flush()
            raw = stream.readline()
            if not raw:
                raise ConnectionError('channel disconnected')
            return json.loads(raw)


def serve(tools_path, address, source=None, destination=None):
    tools = json.loads(Path(tools_path).read_text())
    names = {t['name'] for t in tools}
    source, destination = source or sys.stdin, destination or sys.stdout
    outgoing = queue.Queue()
    stopped = threading.Event()
    parent = os.getppid()

    def watch():
        while not stopped.wait(.5):
            try:
                if os.getppid() != parent:
                    os._exit(0)
                relay(address, {'type': 'ping'})
            except (OSError, ValueError):
                os._exit(0)

    def respond(message):
        if 'id' not in message:
            return
        response = {'jsonrpc': '2.0', 'id': message['id']}
        try:
            method = message.get('method')
            if method == 'initialize':
                result = {'protocolVersion': message.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'shaft', 'version': '1'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': tools}
            elif method == 'tools/call':
                params = message.get('params') or {}
                if params.get('name') not in names:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': 'Unknown tool'}]}
                else:
                    result = relay(address, {'type': 'call', 'id': message['id'], 'params': params})
            else:
                response['error'] = {'code': -32601, 'message': 'Method not found'}
                result = None
            if 'error' not in response:
                response['result'] = result
        except (OSError, ValueError, TypeError):
            response['error'] = {'code': -32603, 'message': 'Bridge disconnected'}
        outgoing.put(response)

    def write():
        while True:
            response = outgoing.get()
            try:
                destination.write(json.dumps(response) + '\n')
                destination.flush()
            except (OSError, ValueError):
                os._exit(0)

    threading.Thread(target=write, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    # Calls wait independently; the stdin reader never waits for a held result.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as workers:
        for raw in source:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError('object required')
            except ValueError:
                outgoing.put({'jsonrpc': '2.0', 'id': None,
                              'error': {'code': -32700, 'message': 'Parse error'}})
                continue
            workers.submit(respond, message)
        stopped.set()
        # A closed parent stdin must not wait forever for outstanding relay calls.
        os._exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools', required=True)
    parser.add_argument('--socket', required=True)
    args = parser.parse_args()
    serve(args.tools, args.socket)


if __name__ == '__main__':
    main()
