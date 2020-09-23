
from os import path
import sys
sys.path.append(path.dirname(path.realpath(__file__)))

import unittest


class TestThings(unittest.TestCase):

    def test_one(self):
        self.assertEqual(1 + 1, 2)

    def test_two(self):
        self.assertEqual(2 + 2, 4)

    def test_three(self):
        self.assertEqual(3 + 3, 6)

    def test_deps(self):
        import check_mathis_accuracy
        import cl_args
        import correlation
        import defaults
        import fet_hists
        import gen_training_data
        import graph_one
        import hyper
        import models
        import parse_dumbbell
        import prepare_data
        import sim
        import test
        import train
        import training_param_sweep
        import utils


if __name__ == "__main__":
    unittest.main()
