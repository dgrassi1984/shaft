"""Synthetic cold-process recall benchmark. No embedding service or instance data.

Times exclude interpreter startup/imports. Each sample is a fresh process; its second recall is warm.

python3.12 -m supervisor.tests.bench_memory --baseline-ref a3218639668dd20229623b61c766317223220837
"""
import argparse
import base64
import cProfile
import hashlib
import importlib.util
import json
import math
import pstats
import statistics
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from array import array

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'template'))
from shaft import memory


def generate(root, count):
    folder = root / 'memory' / 'episodes'
    folder.mkdir(parents=True)
    for start in range(0, count, 100):
        rows = [dict(kind='talk', ts=f'2026-11-01T12:00:{i % 60:02}', role='user',
                     content=f'Synthetic memory {i}: weather forecast research ' + 'sample observation ' * 30,
                     tools=[{'result': 'synthetic unused payload ' * 150}])
                for i in range(start, min(start + 100, count))]
        (folder / f'{start:06}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    docs = memory.corpus(root)
    data = {}
    for i, (_, text) in enumerate(docs):
        last = 1 + (i + 1) / count
        scale = 1 / math.sqrt(1023 + last * last)
        v = array('f', [scale] * 1024)
        v[i % 1024] = last * scale
        data[hashlib.sha1(text.encode()).hexdigest()] = base64.b64encode(v.tobytes()).decode()
    (root / '.tmp').mkdir(exist_ok=True)
    (root / memory.INDEX).write_text(json.dumps(data))
    # Measure the first corpus build too; setup must not give the candidate a cache.
    for p in (root / '.tmp').glob('memory-corpus*'):
        shutil.rmtree(p) if p.is_dir() else p.unlink()


def load_module(source):
    spec = importlib.util.spec_from_file_location('shaft.benchmark_memory', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def append_two(root):
    texts = [f'Appended synthetic memory {time.time_ns()} {i}: weather forecast' for i in range(2)]
    path = root / 'memory' / 'episodes' / '000000.jsonl'
    with path.open('a') as f:
        for text in texts:
            f.write(json.dumps(dict(kind='talk', ts='2026-11-01T13:00:00', role='user', content=text)) + '\n')
    return texts


def worker(root, source, profile=False, append_mode=None, reference=None):
    module = load_module(source) if source else memory
    added = []
    def embed(texts, query):
        if not query and any(text not in added for text in texts):
            raise AssertionError('known document was re-embedded')
        return [[0.03125] * 1024 for _ in texts]
    module._embed = embed
    if append_mode == 'warm':
        module.recall(root, 'weather forecast')
    if append_mode:
        added = append_two(root)
    def shards():
        return {p.name: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
                for p in (root / memory.CORPUS_CACHE).glob('*.json')}
    if append_mode:
        before_index = (root / memory.BINARY_INDEX).stat()
        before_shards = shards()
    parts = {}
    def timed(name):
        original = getattr(module, name)
        def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                parts.setdefault(name, []).append(time.perf_counter() - start)
        return call
    for name in ('corpus', '_bm25', '_vectors', '_semantic'):
        setattr(module, name, timed(name))
    durations, cpu_durations, results = [], [], []
    profiler = cProfile.Profile() if profile else None
    for i in range(2):
        profiling = profiler and i == (0 if append_mode == 'warm' else 1)
        if profiling:
            profiler.enable()
        start = time.perf_counter()
        cpu_start = time.process_time()
        result = module.recall(root, 'weather forecast')
        durations.append(time.perf_counter() - start)
        cpu_durations.append(time.process_time() - cpu_start)
        results.append(result)
        if profiling:
            profiler.disable()
    if profiler:
        pstats.Stats(profiler).strip_dirs().sort_stats("cumtime").print_stats(18)
    assert results[0] == results[1], 'cold and warm results differ'
    writes = {}
    if append_mode:
        after_index = (root / memory.BINARY_INDEX).stat()
        after_shards = shards()
        assert after_index.st_ino == before_index.st_ino, 'append rewrote the vector log'
        writes = dict(vector_bytes=after_index.st_size - before_index.st_size,
                      shards=sum(after_shards[k] != v for k, v in before_shards.items()))
        assert writes == dict(vector_bytes=2 * (24 + 1024 * 4), shards=1), writes
    measured_parts = {key: values.copy() for key, values in parts.items()}
    if reference:
        old = load_module(reference)
        old.corpus, old._vectors, old._embed = module.corpus, module._vectors, embed
        assert old.recall(root, 'weather forecast') == result, 'old ranking differs'
    print(json.dumps(dict(cold=durations[0], warm=durations[1], cold_cpu=cpu_durations[0],
                         warm_cpu=cpu_durations[1], parts=measured_parts, writes=writes,
                         result=hashlib.sha256(result.encode()).hexdigest())))


def main():
    ap = argparse.ArgumentParser(__doc__)
    ap.add_argument('--baseline-ref')
    ap.add_argument('--profile', action='store_true', help='profile only the warm recall')
    ap.add_argument('--append-mode', choices=('cold', 'warm'))
    ap.add_argument('--reference')
    ap.add_argument('--worker', type=Path)
    ap.add_argument('--source')
    ap.add_argument('--count', type=int, default=29308)
    ap.add_argument('--runs', type=int, default=3)
    args = ap.parse_args()
    if args.worker:
        worker(args.worker, args.source, args.profile, args.append_mode, args.reference)
        return
    with tempfile.TemporaryDirectory(dir=BASE, prefix='.bench-memory-') as folder:
        root = Path(folder)
        generate(root, args.count)
        baseline = None
        if args.baseline_ref:
            baseline = root / 'baseline.py'
            baseline.write_bytes(subprocess.run(
                ['git', '-C', str(BASE / 'template'), 'show', args.baseline_ref + ':shaft/memory.py'],
                check=True, capture_output=True).stdout)
        expected = None
        for label, source in [('baseline', baseline), ('migration', None), ('cached', None),
                              ('append_cold', None), ('append_warm', None)]:
            if label == 'baseline' and not source:
                continue
            cmd = [sys.executable, '-m', 'supervisor.tests.bench_memory', '--worker', str(root)]
            if source:
                cmd += ['--source', str(source)]
            if label.startswith('append_'):
                cmd += ['--append-mode', label.removeprefix('append_')]
            if baseline and not source:
                cmd += ['--reference', str(baseline)]
            if args.profile:
                cmd += ['--profile']
            samples = []
            for _ in range(1 if label == 'migration' else args.runs):
                run = subprocess.run(cmd, check=True, capture_output=True, text=True)
                sample = json.loads(run.stdout.splitlines()[-1])
                if expected is None:
                    expected = sample['result']
                if not label.startswith('append_'):
                    assert sample['result'] == expected, (label, 'baseline result mismatch')
                samples.append(sample)
                print(label, run.stdout.strip(), flush=True)
            print(label, 'median', json.dumps({key: statistics.median(s[key] for s in samples)
                                              for key in ('cold', 'warm', 'cold_cpu', 'warm_cpu')}), flush=True)


if __name__ == '__main__':
    main()
