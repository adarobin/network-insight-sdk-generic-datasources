#!/usr/bin/env python
# Copyright 2019 VMware, Inc.
# SPDX-License-Identifier: BSD-2-Clause

"""Capture raw CLI output from an HPE Virtual Connect SE module.

Phase 0a discovery helper. The netops CLI on the Virtual Connect SE 100Gb F32 is
thinly documented, so before any parser can be written we need to know which
commands exist and what their output actually looks like. This connects once,
runs a list of read-only probe commands, and saves each raw response as a fixture
file that the parser unit tests can then assert against.

Individual command failures are expected and never fatal: on a read-only CLI most
of the probe list will not exist. Every response is saved regardless, and a
manifest records which commands looked supported.

Example:

    python tools/capture_vc_cli.py -i 10.1.1.10 -u readonly_user

Re-run after a firmware upgrade to refresh the fixtures and catch output drift.
"""

import argparse
import datetime
import getpass
import json
import os
import re
import sys
import time

from netmiko import ConnectHandler

DEFAULT_OUTPUT_DIR = os.path.join('test', 'fixtures', 'hpe', 'vc-se-100gb-f32')

# Verified against the `help` output of a Virtual Connect SE 100Gb F32 (OneView>
# prompt, Aricent/ISS CLI with Virtual Connect extensions). Grouped by the vRNI
# CSV each one feeds. Note the c-Class VCM commands (show enet-connection,
# show profile, show server, show domain, show mac-cache) do not exist here.
PROBE_COMMANDS = [
    # Command set itself, re-captured unwrapped for reference.
    'help',
    # switch.csv - device identity.
    'show system information',
    'show serial-number',
    'show hardware',
    'show firmware version normal',
    'show switch detail',
    'show enclosure details',
    'show mgmt interface',
    'show host',
    'show clock',
    # switch-ports.csv - per-port state.
    'show interfaces status',
    'show interfaces description',
    'show interfaces mtu',
    'show interfaces capabilities',
    'show interfaces information',
    'show transceiver-info all',
    # switch-ports.csv - VLAN membership per port.
    'show port vlanlist',
    'show vlan port config',
    'show vlan port info',
    # Virtual Connect specific uplink constructs.
    'show uplinkset summary',
    'show uplinkport all',
    # port-channels.csv.
    'show etherchannel summary',
    'show etherchannel detail',
    'show interfaces etherchannel',
    'show lacp neighbor detail',
    # l2bridges.csv / VLAN inventory.
    'show vlan brief',
    'show vlan summary',
    # mac-address-table.csv.
    'show mac-address-table',
    'show mac-address-table count',
    # neighbors.csv.
    'show lldp',
    'show lldp neighbors',
    'show lldp neighbors detail',
    'show lldp local mgmt-addr',
    # router-interfaces.csv / routes.csv. There is no bare 'show vrf' on this
    # platform, so vrfs.csv will most likely be a single default row.
    'show ip interface',
    'show ip route',
    # Large, but the most complete single description of the configuration.
    'show running-config',
]

# Best-effort attempts to stop a pager from stalling the session. Both are real
# commands on this platform; harmless if rejected.
PAGING_COMMANDS = [
    'no pagination',
    'set cli pagination off',
]

# Substrings that suggest the device rejected the command rather than answering.
REJECTION_MARKERS = [
    'invalid input',
    'invalid command',
    'unknown command',
    'unrecognized command',
    'syntax error',
    'incomplete command',
    'command not found',
    'not supported',
    'permission denied',
    '% error',
]

READ_ONLY_PREFIXES = ('show', 'display', 'get', 'list', 'help', '?', 'version', 'dir')


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description='Capture raw CLI output from an HPE Virtual Connect SE module.')
    parser.add_argument('-i', '--ip', required=True, help='Management IP or FQDN of the module')
    parser.add_argument('-u', '--username', required=True, help='Username (a read-only account)')
    parser.add_argument('-p', '--password',
                        help='Password. Prompted for if omitted; VC_PASSWORD is also honoured.')
    parser.add_argument('-P', '--port', type=int, default=22, help='SSH port (default 22)')
    parser.add_argument('-s', '--device-type', default='generic',
                        help="netmiko platform name (default 'generic'; try hp_procurve, "
                             "hp_comware, generic_termserver if output looks mangled)")
    parser.add_argument('-o', '--output-dir', default=DEFAULT_OUTPUT_DIR,
                        help='Directory for fixture files (default %s)' % DEFAULT_OUTPUT_DIR)
    parser.add_argument('-t', '--timeout', type=float, default=60.0,
                        help='Per-command read timeout in seconds (default 60)')
    parser.add_argument('--commands-file',
                        help='File of commands to run, one per line, instead of the built-in probes')
    parser.add_argument('--allow-any-command', action='store_true',
                        help='Permit commands that are not obviously read-only. Off by default so '
                             'a stray line in --commands-file cannot reconfigure the module.')
    return parser.parse_args(argv)


def resolve_password(args):
    if args.password:
        return args.password
    if os.environ.get('VC_PASSWORD'):
        return os.environ['VC_PASSWORD']
    return getpass.getpass('Password for %s@%s: ' % (args.username, args.ip))


def load_commands(args):
    if not args.commands_file:
        return list(PROBE_COMMANDS)
    commands = []
    with open(args.commands_file) as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith('#'):
                commands.append(line)
    return commands


def is_read_only(command):
    return command.strip().lower().startswith(READ_ONLY_PREFIXES)


def slugify(command):
    if command.strip() == '?':
        return 'question-mark'
    slug = re.sub(r'[^a-z0-9]+', '-', command.strip().lower()).strip('-')
    return slug or 'command'


def looks_rejected(output):
    lowered = output.lower()
    return any(marker in lowered for marker in REJECTION_MARKERS)


def run_command(connection, command, timeout):
    """Return (output, error). send_command_timing avoids needing to know the prompt."""
    try:
        return connection.send_command_timing(command, read_timeout=timeout), None
    except Exception as timing_error:  # noqa: BLE001 - discovery tool, report and continue
        try:
            return connection.send_command(command, read_timeout=timeout), None
        except Exception as error:  # noqa: BLE001
            return '', '%s / %s' % (timing_error, error)


def main(argv=None):
    args = parse_arguments(argv)
    commands = load_commands(args)

    if not args.allow_any_command:
        rejected = [c for c in commands if not is_read_only(c)]
        if rejected:
            sys.stderr.write(
                'Refusing to run commands that are not obviously read-only: %s\n'
                'Pass --allow-any-command if this is intended.\n' % ', '.join(rejected))
            return 2

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    print('Connecting to %s:%s as %s (platform %s)'
          % (args.ip, args.port, args.username, args.device_type))
    connection = ConnectHandler(device_type=args.device_type, host=args.ip, port=args.port,
                                username=args.username, password=resolve_password(args))

    manifest = {
        'captured_at': datetime.datetime.now().isoformat(),
        'ip': args.ip,
        'device_type': args.device_type,
        'commands': [],
    }

    try:
        try:
            manifest['prompt'] = connection.find_prompt()
            print('Prompt: %r' % manifest['prompt'])
        except Exception as error:  # noqa: BLE001
            manifest['prompt'] = None
            print('Could not determine prompt: %s' % error)

        manifest['paging_attempts'] = []
        for paging_command in PAGING_COMMANDS:
            output, error = run_command(connection, paging_command, 15.0)
            manifest['paging_attempts'].append({
                'command': paging_command,
                'accepted': error is None and not looks_rejected(output),
            })

        for command in commands:
            started = time.time()
            output, error = run_command(connection, command, args.timeout)
            elapsed = time.time() - started

            filename = '%s.txt' % slugify(command)
            with open(os.path.join(args.output_dir, filename), 'w') as fixture:
                fixture.write(output)

            entry = {
                'command': command,
                'file': filename,
                'bytes': len(output),
                'seconds': round(elapsed, 2),
                'error': error,
                'rejected': looks_rejected(output),
            }
            manifest['commands'].append(entry)

            if error:
                status = 'ERROR'
            elif entry['rejected']:
                status = 'rejected'
            elif not output.strip():
                status = 'empty'
            else:
                status = 'ok'
            print('  %-28s %-9s %6d bytes  %5.1fs' % (command, status, entry['bytes'], elapsed))
    finally:
        connection.disconnect()

    manifest_path = os.path.join(args.output_dir, '_manifest.json')
    with open(manifest_path, 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    supported = [c for c in manifest['commands']
                 if not c['error'] and not c['rejected'] and c['bytes'] > 0]
    print('\n%d of %d commands returned output. Manifest: %s'
          % (len(supported), len(commands), manifest_path))
    if supported:
        print('Supported: %s' % ', '.join(c['command'] for c in supported))
    return 0


if __name__ == '__main__':
    sys.exit(main())
