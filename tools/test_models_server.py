"""Run: .venv-models/bin/python -m unittest discover tools (no models needed)."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from models_server import Gliner, MicroBatcher


class MicroBatcherTest(unittest.TestCase):
    def test_concurrent_calls_share_a_batch_and_get_their_own_result(self):
        sizes = []

        def run(items):
            sizes.append(len(items))
            time.sleep(0.01)
            return [i * 10 for i in items]

        batcher = MicroBatcher(run, max_batch=16, max_wait=0.02)
        with ThreadPoolExecutor(16) as pool:
            results = list(pool.map(batcher.submit, range(16)))
        self.assertEqual(results, [i * 10 for i in range(16)])
        self.assertLess(len(sizes), 16)  # batched, not one call per item
        self.assertLessEqual(max(sizes), 16)

    def test_a_failing_batch_fails_its_callers_and_the_batcher_keeps_working(self):
        calls = threading.Event()

        def run(items):
            if not calls.is_set():
                calls.set()
                raise RuntimeError("model crashed")
            return items

        batcher = MicroBatcher(run, max_wait=0)
        with self.assertRaises(RuntimeError):
            batcher.submit(1)
        self.assertEqual(batcher.submit(2), 2)


class NativeExtractionTest(unittest.TestCase):
    def test_native_entities_relations_keep_all_spans_and_legacy_fields_still_flatten(self):
        native = {"entities": {"person": [{"text": "Luca", "start": 0, "end": 4, "confidence": .9},
                                          {"text": "Anna", "start": 12, "end": 16, "confidence": .8}]},
                  "relation_extraction": {"greets": [{"head": {"text": "Luca"}, "tail": {"text": "Anna"},
                                                        "confidence": .9}]}}
        schemas = []
        class Schema:
            native = False
            def structure(self, name):
                return self
            def field(self, *args, **kwargs):
                return self
            def entities(self, labels, **kwargs):
                self.native = True
                schemas.append(("entities", labels, kwargs))
                return self
            def relations(self, labels, **kwargs):
                schemas.append(("relations", labels, kwargs))
                return self
        class Model:
            def create_schema(self):
                return Schema()
            def batch_extract(self, texts, structures, **kwargs):
                assert kwargs["include_spans"] is True
                return [native if structure.native else {"record": [{"name": {"text": "Luca", "confidence": .9}}]}
                        for structure in structures]
        gliner = Gliner.__new__(Gliner)
        gliner.model = Model()
        gliner.batcher = MicroBatcher(gliner._run, max_wait=.03)
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(gliner.extract, "Luca saluta Anna", entities={"person": "A named person"},
                            relations={"greets": "A person greets another person"}, threshold=.7)
            b = pool.submit(gliner.extract, "Luca", [{"name": "name"}], .5)
            self.assertEqual(a.result(), native)
            self.assertEqual(b.result(), {"name": {"value": "Luca", "confidence": .9}})
        self.assertEqual(schemas[-1][2]["threshold"], .7)

    def test_rejects_invalid_native_schema_without_running_a_model(self):
        gliner = Gliner.__new__(Gliner)
        for kwargs in ({"entities": ["person"]}, {"relations": {"a": 123}}, {"threshold": float("nan")}):
            with self.assertRaises(ValueError):
                gliner.extract("A sentence", **kwargs)


if __name__ == "__main__":
    unittest.main()
