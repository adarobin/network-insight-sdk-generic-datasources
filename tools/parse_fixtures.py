#!/usr/bin/env python
# Copyright 2026 The Regents of the University of Michigan
# SPDX-License-Identifier: BSD-2-Clause

"""Run the real collection pipeline against captured fixtures, with no device.

Substitutes a fixture-backed stand-in for SSHConnectHandler, so the YAML, block
parsers, rules, pre/post processors, table processors and CSV writer all execute
exactly as they would against hardware. That makes parser development possible
without network access to the module, and gives a fast regression check after any
YAML or parser edit.

    python tools/parse_fixtures.py -d hpe -m vc-se-100gb-f32

With -z it also writes the zip vRNI ingests, which makes this a complete offline
route to an uploadable package: capture by hand, split the log, parse, upload. No
connectivity from this machine to the device is needed at any point.

    python tools/parse_fixtures.py -z /tmp/hpe-vc-10.244.130.138.zip \
        -i 10.244.130.138

Fixture files are matched to commands by the same slug capture_vc_cli.py uses, so
a command with no fixture is reported rather than silently returning nothing.
"""

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import network_insight_sdk_generic_datasources.common.physical_device as physical_device_module
import network_insight_sdk_generic_datasources.common.yaml_utilities as yaml_utilities
from network_insight_sdk_generic_datasources.common.constants import GENERATION_DIRECTORY_KEY
from network_insight_sdk_generic_datasources.common.constants import RESULT_WRITER_KEY
from network_insight_sdk_generic_datasources.common.constants import TABLE_JOINERS_KEY
from network_insight_sdk_generic_datasources.common.constants import TABLE_ID_KEY
from network_insight_sdk_generic_datasources.common.constants import WORKLOADS_KEY
from network_insight_sdk_generic_datasources.archive.zip_archiver import ZipArchiver


def slugify(command):
    if command.strip() == '?':
        return 'question-mark'
    return re.sub(r'[^a-z0-9]+', '-', command.strip().lower()).strip('-') or 'command'


class FixtureConnectHandler(object):
    """Answers commands from files instead of a device."""

    missing = []

    def __init__(self, fixture_dir):
        self.fixture_dir = fixture_dir

    def execute_command(self, command=None):
        path = os.path.join(self.fixture_dir, '%s.txt' % slugify(command))
        if not os.path.exists(path):
            FixtureConnectHandler.missing.append(command)
            return ''
        with open(path, errors='replace') as handle:
            return handle.read()

    def close_connection(self):
        pass


class Credentials(object):
    def __init__(self, ip_or_fqdn):
        self.ip_or_fqdn = ip_or_fqdn
        self.username = 'fixture'
        self.password = 'fixture'
        self.device_type = 'LINUX'
        self.port = '22'


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-d', '--device', default='hpe', help='Vendor directory name')
    parser.add_argument('-m', '--model', default='vc-se-100gb-f32', help='Model key in the YAML')
    parser.add_argument('-f', '--fixtures', help='Fixture directory (default derives from -d/-m)')
    parser.add_argument('-i', '--ip_or_fqdn', default='192.0.2.1',
                        help='Address recorded in switch.csv (default a documentation address)')
    parser.add_argument('-o', '--output-dir', help='Where to write CSVs (default a temp directory)')
    parser.add_argument('-z', '--output-zip',
                        help='Also write the zip to upload to VCF Operations for Networks. Pass -i '
                             'with the real management address, since that is recorded in '
                             'switch.csv and the product matches the data source on it.')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_arguments(argv)
    fixture_dir = args.fixtures or os.path.join(
        REPO_ROOT, 'test', 'fixtures', args.device, args.model)
    if not os.path.isdir(fixture_dir):
        sys.stderr.write('No fixture directory at %s\n' % fixture_dir)
        return 2

    yaml_path = os.path.join(REPO_ROOT, 'network_insight_sdk_generic_datasources',
                             'routers_and_switches', args.device, '%s.yml' % args.device)
    with open(yaml_path) as handle:
        configuration = yaml_utilities.altered_safe_load(handle)

    model = configuration[args.model]
    output_dir = args.output_dir or os.path.join(
        configuration[GENERATION_DIRECTORY_KEY], 'fixtures-%s' % args.model)

    # Swap the SSH layer for the fixture reader before PhysicalDevice builds one.
    physical_device_module.SSHConnectHandler = lambda **kwargs: FixtureConnectHandler(fixture_dir)

    device = physical_device_module.PhysicalDevice(
        args.device, args.model, model[WORKLOADS_KEY], Credentials(args.ip_or_fqdn),
        model.get(TABLE_JOINERS_KEY), model[RESULT_WRITER_KEY], output_dir)
    device.process()

    print('\nRows per table')
    for table_id in model[RESULT_WRITER_KEY]['table_id']:
        rows = device.result_map.get(table_id)
        print('  %-22s %s' % (table_id, len(rows) if rows is not None else 'MISSING'))

    if FixtureConnectHandler.missing:
        print('\nCommands with no fixture: %s' % ', '.join(sorted(set(FixtureConnectHandler.missing))))
    print('\nCSVs written to %s' % output_dir)

    if args.output_zip:
        ZipArchiver(False, args.output_zip, output_dir,
                    model[RESULT_WRITER_KEY][TABLE_ID_KEY]).zipdir()
        print('Zip written to %s' % args.output_zip)
        if args.ip_or_fqdn == parse_arguments([]).ip_or_fqdn:
            print('WARNING: switch.csv records the default placeholder address. Re-run with -i '
                  '<real management address> before uploading.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
