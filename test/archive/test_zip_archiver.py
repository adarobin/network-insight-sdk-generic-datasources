# Copyright 2026 The Regents of the University of Michigan
# SPDX-License-Identifier: BSD-2-Clause

import os
import shutil
import tempfile
import unittest
import zipfile

from network_insight_sdk_generic_datasources.archive.zip_archiver import ZipArchiver
from network_insight_sdk_generic_datasources.writers.csv_writer import CsvWriter


class ZipArchiverTestCase(unittest.TestCase):

    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.generation_dir = os.path.join(self.base_dir, 'gen')
        os.makedirs(self.generation_dir)
        self.zip_path = os.path.join(self.base_dir, 'out.zip')

    def tearDown(self):
        shutil.rmtree(self.base_dir)

    def names_in_zip(self):
        with zipfile.ZipFile(self.zip_path) as archive:
            return sorted(archive.namelist())

    def test_csvs_are_stored_at_archive_root(self):
        CsvWriter.write(self.generation_dir, 'switch', [{'name': 'vc-1'}])
        ZipArchiver(False, self.zip_path, self.generation_dir, ['switch']).zipdir()
        self.assertEqual(['switch.csv'], self.names_in_zip())

    def test_files_not_declared_by_result_writer_are_excluded(self):
        CsvWriter.write(self.generation_dir, 'switch', [{'name': 'vc-1'}])
        with open(os.path.join(self.generation_dir, 'stale.csv'), 'w') as stale:
            stale.write('left,over\n')
        ZipArchiver(False, self.zip_path, self.generation_dir, ['switch']).zipdir()
        self.assertEqual(['switch.csv'], self.names_in_zip())

    def test_everything_is_archived_when_no_filter_is_given(self):
        CsvWriter.write(self.generation_dir, 'switch', [{'name': 'vc-1'}])
        with open(os.path.join(self.generation_dir, 'stale.csv'), 'w') as stale:
            stale.write('left,over\n')
        ZipArchiver(False, self.zip_path, self.generation_dir).zipdir()
        self.assertEqual(['stale.csv', 'switch.csv'], self.names_in_zip())

    def test_empty_table_produces_no_csv(self):
        # CsvWriter skips empty tables, so a table with no rows silently yields no
        # file. Mandatory vRNI CSVs therefore need at least one synthesised row.
        CsvWriter.write(self.generation_dir, 'routes', [])
        self.assertFalse(os.path.exists(os.path.join(self.generation_dir, 'routes.csv')))


if __name__ == '__main__':
    unittest.main()
