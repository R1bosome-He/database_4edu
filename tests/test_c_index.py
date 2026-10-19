import random
import unittest

from edudb.c_api import RangeSpec
from edudb.index.bplus import BPlusTree
from edudb.models import RecordId


class CBPlusTreeTest(unittest.TestCase):
    def test_random_insert_delete_rebalance_against_reference(self):
        for order in [3, 4, 8]:
            with self.subTest(order=order):
                rng = random.Random(1927)
                tree = BPlusTree(order)
                reference = set()
                for i in range(240):
                    key = rng.randrange(25)
                    rid = RecordId(i // 10, i % 10)
                    tree.insert(key, rid)
                    reference.add((key, rid.page_id, rid.slot_id))
                    tree.validate()
                self.assertEqual(list(tree.search(RangeSpec())), sorted(reference))
                entries = list(reference)
                rng.shuffle(entries)
                for key, page, slot in entries:
                    tree.delete(key, RecordId(page, slot))
                    reference.remove((key, page, slot))
                    tree.validate()
                    self.assertEqual(list(tree.search(RangeSpec())), sorted(reference))
                self.assertTrue(tree.nodes[tree.root].leaf)

    def test_range_endpoints_and_serialization(self):
        tree = BPlusTree(3)
        for i in range(50):
            tree.insert(i // 5, RecordId(0, i))
        restored = BPlusTree.load(tree.order, tree.root, tree.next_id, tree.dump())
        for lo_inc in [True, False]:
            for hi_inc in [True, False]:
                result = list(restored.search(RangeSpec(3, 6, lo_inc, hi_inc)))
                expected = [(i // 5, 0, i) for i in range(50)
                            if (i // 5 >= 3 if lo_inc else i // 5 > 3) and (i // 5 <= 6 if hi_inc else i // 5 < 6)]
                self.assertEqual(result, expected)
        self.assertEqual(list(restored.search(RangeSpec(9, 2))), [])

    def test_duplicate_rid_is_idempotent_and_null_is_omitted(self):
        tree = BPlusTree()
        tree.insert(None, RecordId(0, 0))
        tree.insert(1, RecordId(0, 1))
        tree.insert(1, RecordId(0, 1))
        self.assertEqual(list(tree.search(RangeSpec())), [(1, 0, 1)])
