"""Memory search regressions with synthetic files and no embedding service."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from array import array
from datetime import datetime
from unittest.mock import patch

from supervisor.tests.test_memory import memory, concept_vector

BASE = Path(__file__).resolve().parents[2]


class MemorySpeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'notes').mkdir()
        (self.root / 'notes' / 'example.md').write_text(
            'rain forecast weather\n\n' + 'weather observation ' * 60 + '\n\n'
            'employer example work\n\n' + 'job report ' * 100 + '\n\nsonno sleep')

    def embed(self, texts, query):
        return [concept_vector(t) for t in texts]

    def test_ranking_matches_float_products_and_word_scores(self):
        """C dot products and cached postings must not change the order of fused hits."""
        with patch.object(memory, '_embed', side_effect=self.embed):
            docs = memory.corpus(self.root)
            expected = {
                'weather': ['weather observation ', 'rain forecast weather', 'job report ', 'sonno sleep'],
                'lavoro': ['job report ', 'employer example work', 'weather observation ', 'sonno sleep'],
            }
            for query, prefixes in expected.items():
                qv = concept_vector(query)
                vecs = [array('f', concept_vector(t)) for _, t in docs]
                old = [i for _, i in sorted(((sum(a*b for a, b in zip(qv, v)), i)
                                             for i, v in enumerate(vecs)), reverse=True)]
                self.assertEqual(memory._semantic(self.root, docs, query, 50), old)
                hits = memory.search(self.root, query, k=4)
                self.assertEqual([h.score for h in hits],
                                 [32.79, 32.26, 15.87, 15.62] if query == 'weather'
                                 else [16.39, 16.13, 15.87, 15.62])
                self.assertEqual(len(hits), 4)
                for hit, prefix in zip(hits, prefixes):
                    self.assertTrue(hit.text.startswith(prefix), (query, [h.text[:30] for h in hits]))

    def test_json_migration_never_embeds_known_documents(self):
        """Upgrading an existing JSON index must not re-embed known vectors."""
        docs = memory.corpus(self.root)
        encoded = {memory.hashlib.sha1(t.encode()).hexdigest():
                   base64.b64encode(array('f', concept_vector(t)).tobytes()).decode() for _, t in docs}
        (self.root / memory.INDEX).write_text(json.dumps(encoded))
        def query_only(texts, query):
            self.assertTrue(query)
            return self.embed(texts, query)
        with patch.object(memory, '_embed', side_effect=query_only):
            first = memory.recall(self.root, 'weather')
            self.assertTrue((self.root / memory.BINARY_INDEX).exists())
            # Neither unchanged source files nor either index is read a second time.
            with patch.object(Path, 'read_text', side_effect=AssertionError('re-read text')), \
                 patch.object(Path, 'open', side_effect=AssertionError('re-read index')):
                self.assertEqual(memory.recall(self.root, 'weather'), first)

    def test_truncated_binary_recovers_known_legacy_vectors(self):
        """A damaged derived index must rebuild without re-embedding surviving legacy vectors."""
        docs = memory.corpus(self.root)
        encoded = {memory.hashlib.sha1(t.encode()).hexdigest():
                   base64.b64encode(array('f', concept_vector(t)).tobytes()).decode() for _, t in docs}
        (self.root / memory.INDEX).write_text(json.dumps(encoded))
        texts = [t for _, t in docs]
        with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
            expected = memory._vectors(self.root, texts)
            (self.root / memory.INDEX).write_text(json.dumps(encoded))
            (self.root / memory.BINARY_INDEX).write_bytes(b'{broken')
            self.assertEqual(memory._vectors(self.root, texts), expected)
            memory._vector_root = None
            self.assertEqual(memory._vectors(self.root, texts), expected)

    def test_disk_corpus_cache_invalidates_same_size_preserved_mtime_edit(self):
        """A file edit must invalidate cached chunks even if size and mtime are preserved."""
        file = self.root / 'notes' / 'example.md'
        old = memory.corpus(self.root)
        # Switch roots to simulate losing in-process caches.
        with tempfile.TemporaryDirectory(dir=BASE) as other:
            memory.corpus(Path(other))
        with patch.object(memory, '_file_docs', side_effect=AssertionError('cache miss')):
            self.assertEqual(memory.corpus(self.root), old)
        stat = file.stat()
        file.write_text(file.read_text().replace('rain', 'snow'))
        os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIn('snow forecast', memory.corpus(self.root)[0][1])
        file.unlink()
        self.assertEqual(memory.corpus(self.root), [])

    def test_verdict_day_and_surprise_expiry_invalidate_disk_cache(self):
        """Disk snapshots must not retain stale verdicts, fact ages or open surprises."""
        folder = self.root / 'memory' / 'episodes'
        folder.mkdir(parents=True)
        (self.root / 'memory' / 'facts.md').write_text('- [2026-11-01] example fact')
        episode = {'kind': 'live', 'ts': '2026-11-01T12:00:00', 'intent': {},
                   'finish': {'surprises': [{'expected': 'rain', 'observed': 'snow'}]}}
        (folder / 'example.jsonl').write_text(json.dumps(episode) + '\n')
        with patch.object(memory.clock, 'now', return_value=datetime(2026, 11, 8, 11)):
            docs = memory.corpus(self.root)
            self.assertIn('7 days old', docs[0][0])
            self.assertTrue(any('Status: open' in text for _, text in docs))
            (self.root / memory.VERDICTS).write_text(json.dumps(
                {'episode_ts': episode['ts'], 'criterion_met': True}) + '\n')
            with tempfile.TemporaryDirectory(dir=BASE) as other:
                memory.corpus(Path(other))
            self.assertTrue(any('criterion met True' in text for _, text in memory.corpus(self.root)))
        with patch.object(memory.clock, 'now', return_value=datetime(2026, 11, 8, 13)):
            self.assertTrue(any('Status: expired' in text for _, text in memory.corpus(self.root)))
        with patch.object(memory.clock, 'now', return_value=datetime(2026, 11, 9, 13)):
            self.assertIn('8 days old', memory.corpus(self.root)[0][0])

    def test_concurrent_migrations_leave_complete_binary_index(self):
        """Concurrent index writers must not expose a partial header or vector payload."""
        docs = memory.corpus(self.root)
        data = {memory.hashlib.sha1(t.encode()).hexdigest():
                base64.b64encode(array('f', concept_vector(t)).tobytes()).decode() for _, t in docs}
        (self.root / memory.INDEX).write_text(json.dumps(data))
        code = '''
import sys, os, time
from pathlib import Path
sys.path.insert(0, 'template')
from shaft import memory
root = Path(sys.argv[1])
(root / ('ready-' + str(os.getpid()))).touch()
deadline = time.monotonic() + 10
while len(list(root.glob('ready-*'))) < 4:
    assert time.monotonic() < deadline, 'writer barrier timed out'
    time.sleep(.01)
memory._embed = lambda texts, query: [[1., 0., 0., 0.]] if query else (_ for _ in ()).throw(AssertionError('re-embed'))
for _ in range(5):
    assert memory.recall(root, 'weather')
    assert not memory.semantic_error, memory.semantic_error
'''
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.root)], cwd=BASE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
        memory._vector_root = None
        with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
            self.assertEqual([v.tolist() for v in memory._vectors(self.root, [t for _, t in docs])],
                             [array('f', concept_vector(t)).tolist() for _, t in docs])

    def test_cold_append_reuses_unchanged_file_chunks(self):
        """A new episode must not make a fresh process reparse every old file."""
        memory.corpus(self.root)
        with tempfile.TemporaryDirectory(dir=BASE) as other:
            memory.corpus(Path(other))
        new = self.root / 'notes' / 'new.md'
        new.write_text('new synthetic observation')
        original = memory._file_docs
        reads = []
        def read(root, path, verdicts, rows):
            reads.append(path)
            return original(root, path, verdicts, rows)
        with patch.object(memory, '_file_docs', side_effect=read):
            docs = memory.corpus(self.root)
        self.assertEqual(reads, [new])
        self.assertEqual(docs[-1], ('notes/new.md', 'new synthetic observation'))

    def test_rounding_ties_and_short_vectors_preserve_old_order(self):
        """Extended product precision and unequal lengths must not reorder old dot-product ties."""
        docs = [('example', str(i)) for i in range(4)]
        vectors = [array('f', [0.5, -0.5, 1e-17]), array('f', [0., 0., 0.]),
                   array('f', [0.5, 1e-17, -0.5]), array('f', [0.])]
        qv = [0.5, 0.5, 0.5]
        with patch.object(memory, '_embed', return_value=[qv]), patch.object(memory, '_vectors', return_value=vectors):
            self.assertEqual(memory._semantic(self.root, docs, 'example', 4), [2, 0, 3, 1])
            self.assertEqual(memory._semantic(self.root, docs, 'example', 2), [2, 0])

    def test_log_appends_and_compacts_only_past_one_quarter_dead(self):
        """Two new vectors must append without rewriting live records; dead keys compact above 25%."""
        texts = [f'example {i}' for i in range(8)]
        with patch.object(memory, '_embed', side_effect=self.embed) as embed:
            memory._vectors(self.root, texts)
            path = self.root / memory.BINARY_INDEX
            original, inode = path.read_bytes(), path.stat().st_ino
            texts += ['example new a', 'example new b']
            memory._vectors(self.root, texts)
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.read_bytes()[:len(original)], original)
            self.assertEqual(path.stat().st_size - len(original), 2 * (24 + 4 * 4))
            memory._vectors(self.root, texts[:8])
            self.assertEqual(path.stat().st_ino, inode)  # 20% dead
            memory._vectors(self.root, texts[:7])
            self.assertNotEqual(path.stat().st_ino, inode)  # 30% dead
            self.assertEqual(path.stat().st_size, len(original) - (24 + 4 * 4))
            self.assertEqual(sum(len(c.args[0]) for c in embed.call_args_list), 10)
        memory._vector_root = None
        with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
            self.assertEqual(len(memory._vectors(self.root, texts[:7])), 7)

    def test_torn_last_record_is_ignored_then_repaired_before_append(self):
        """A crashed append must not hide old vectors or corrupt the next successful append."""
        texts = [f'example {i}' for i in range(8)]
        with patch.object(memory, '_embed', side_effect=self.embed):
            memory._vectors(self.root, texts)
            path = self.root / memory.BINARY_INDEX
            for tail in (b'partial header', bytes.fromhex('ab' * 20) + (4).to_bytes(4, 'little') + b'xx'):
                original = path.read_bytes()
                with path.open('ab') as f:
                    f.write(tail)
                memory._vector_root = None
                with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
                    self.assertEqual(len(memory._vectors(self.root, texts)), len(texts))
                texts.append(f'new example {len(texts)}')
                memory._vectors(self.root, texts)
                self.assertEqual(path.read_bytes()[:len(original)], original)
                self.assertEqual(path.stat().st_size, len(original) + 24 + 4 * 4)
                memory._vector_root = None
                with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
                    self.assertEqual(len(memory._vectors(self.root, texts)), len(texts))

    def test_only_edited_source_shard_is_rewritten(self):
        """Appending a source must leave every unchanged corpus shard untouched."""
        other = self.root / 'notes' / 'other.md'
        other.write_text('another synthetic note')
        memory.corpus(self.root)
        shards = self.root / memory.CORPUS_CACHE
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in shards.glob('*.json')}
        self.assertEqual(len(before), 2)
        with other.open('a') as f:
            f.write(' appended observation')
        memory.corpus(self.root)
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in shards.glob('*.json')}
        self.assertEqual(sum(before[k] != after[k] for k in before), 1)
        with tempfile.TemporaryDirectory(dir=BASE) as root:
            memory.corpus(Path(root))
        with patch.object(memory, '_file_docs', side_effect=AssertionError('re-read source')), \
             patch.object(memory, '_atomic_write', side_effect=AssertionError('rewrote shard')):
            self.assertIn(('notes/other.md', 'another synthetic note appended observation'), memory.corpus(self.root))

    def test_legacy_deleted_only_after_complete_migration(self):
        """A failed replacement must retain the legacy index and retry without re-embedding."""
        docs = memory.corpus(self.root)
        texts = [text for _, text in docs]
        encoded = {memory.hashlib.sha1(t.encode()).hexdigest():
                   base64.b64encode(array('f', concept_vector(t)).tobytes()).decode() for t in texts}
        legacy = self.root / memory.INDEX
        legacy.write_text(json.dumps(encoded))
        with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
            with patch.object(memory, '_atomic_write', side_effect=OSError('synthetic write failure')):
                with self.assertRaises(OSError):
                    memory._vectors(self.root, texts)
            self.assertEqual(json.loads(legacy.read_text()), encoded)
            self.assertFalse((self.root / memory.BINARY_INDEX).exists())
            expected = memory._vectors(self.root, texts)
            self.assertFalse(legacy.exists())
            memory._vector_root = None
            self.assertEqual(memory._vectors(self.root, texts), expected)

    def test_short_embedding_response_keeps_migration_source(self):
        """An incomplete service response must not delete the old index or poison the next retry."""
        texts = ['known example', 'new example']
        legacy = self.root / memory.INDEX
        legacy.parent.mkdir()
        vec = array('f', concept_vector(texts[0]))
        legacy.write_text(json.dumps({memory.hashlib.sha1(texts[0].encode()).hexdigest():
                                      base64.b64encode(vec.tobytes()).decode()}))
        with patch.object(memory, '_embed', return_value=[]):
            with self.assertRaises(KeyError):
                memory._vectors(self.root, texts)
        self.assertTrue(legacy.exists())
        self.assertFalse((self.root / memory.BINARY_INDEX).exists())
        with patch.object(memory, '_embed', side_effect=self.embed) as embed:
            self.assertEqual(len(memory._vectors(self.root, texts)), 2)
            embed.assert_called_once_with(['new example'], query=False)
        self.assertFalse(legacy.exists())

    def test_previous_binary_generation_upgrades_without_embedding(self):
        """The previous single-header binary format must upgrade without losing known vectors."""
        docs = memory.corpus(self.root)
        texts = [text for _, text in docs]
        keys = [memory.hashlib.sha1(t.encode()).hexdigest() for t in texts]
        vectors = [array('f', concept_vector(t)) for t in texts]
        path = self.root / memory.BINARY_INDEX
        with path.open('wb') as f:
            f.write(json.dumps(dict(version=1, byteorder=sys.byteorder,
                                   vectors=[(key, 4) for key in keys])).encode() + b'\n')
            for vec in vectors:
                f.write(vec.tobytes())
        with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
            self.assertEqual(memory._vectors(self.root, texts), vectors)
            with path.open('rb') as f:
                self.assertEqual(json.loads(f.readline())['version'], 2)
            memory._vector_root = None
            self.assertEqual(memory._vectors(self.root, texts), vectors)

    def test_concurrent_appends_preserve_both_writers_records(self):
        """Two appenders must not interleave records or truncate each other's new vectors."""
        texts = [f'example {i}' for i in range(8)]
        with patch.object(memory, '_embed', side_effect=self.embed):
            memory._vectors(self.root, texts)
        code = r"""
import sys, time
from pathlib import Path
from supervisor.tests.test_memory import memory, concept_vector
root, writer = Path(sys.argv[1]), sys.argv[2]
(root / ('ready-' + writer)).touch()
deadline = time.monotonic() + 10
while len(list(root.glob('ready-*'))) < 2:
    assert time.monotonic() < deadline
    time.sleep(.01)
def embed(texts, query):
    assert all(text.startswith('new ') for text in texts), 're-embedded known text'
    return [concept_vector(t) for t in texts]
memory._embed = embed
texts = [f'example {i}' for i in range(8)] + [f'new {writer} {i}' for i in range(2)]
assert len(memory._vectors(root, texts)) == 10
"""
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.root), str(i)], cwd=BASE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, (out, err))
        texts += [f'new {writer} {i}' for writer in range(2) for i in range(2)]
        with patch.object(memory, '_embed', side_effect=AssertionError('re-embed')):
            self.assertEqual(memory._vectors(self.root, texts), [array('f', concept_vector(t)) for t in texts])
            memory._vector_root = None
            self.assertEqual(len(memory._vectors(self.root, texts)), 12)

    def test_words_only_message_is_unchanged(self):
        """Embedding failures must still return word hits and the original diagnostic."""
        stderr = io.StringIO()
        with patch.object(memory, '_embed', side_effect=RuntimeError('offline')), contextlib.redirect_stderr(stderr):
            hits = memory.search(self.root, 'forecast')
        self.assertEqual(hits[0].text, 'rain forecast weather')
        self.assertEqual(memory.mode, 'words only: semantic search is unavailable — RuntimeError: offline')
        self.assertEqual(stderr.getvalue(), 'memory search: words only — semantic search unavailable (RuntimeError: offline)\n')


if __name__ == '__main__':
    unittest.main()
