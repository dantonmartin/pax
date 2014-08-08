__author__ = 'tunnell'

"""
test_pax_configuration
----------------------------------

Tests for `pax.configuration` module.
"""

import unittest

from pax import units


class TestPaxUnits(unittest.TestCase):
	def setUp(self):
		pass

	def test_parsing(self):
		self.assertAlmostEqual(units.Ohm, 1.6021765699999998e-10)

	def tearDown(self):
		pass


if __name__ == '__main__':
	unittest.main()