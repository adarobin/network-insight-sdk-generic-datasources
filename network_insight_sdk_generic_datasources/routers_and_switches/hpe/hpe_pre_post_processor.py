# Copyright 2026 The Regents of the University of Michigan
# SPDX-License-Identifier: BSD-2-Clause

"""Parsers for the HPE Virtual Connect SE 100Gb F32 Module for Synergy.

The module's read-only "netops" CLI is the Aricent/ISS switching stack behind an
'OneView>' prompt, with Virtual Connect extensions. It is not the c-Class Virtual
Connect Manager command set.

Three things about the platform shape the parsing here:

* 'show interfaces information' is the only command that enumerates every port
  (252 of them: ethernet uplinks, S-Channel server downlinks, the port-channel,
  plus mgmt and vlan interfaces). 'show interfaces status' covers only the 100
  ethernet ports, so it is not used.
* S-Channel blocks are minimal - no hardware address, MTU, speed or duplex. The
  rule engine yields an empty string for a rule that does not match, so those
  fields are simply absent rather than an error.
* VLAN membership wraps across continuation lines in both 'show port vlanlist'
  and 'show vlan brief', so both need unwrapping before the rules run.
"""

import re

from netaddr import IPAddress
from netaddr import IPNetwork
from netaddr.core import AddrFormatError

from network_insight_sdk_generic_datasources.parsers.text.pre_post_processor import PrePostProcessor
from network_insight_sdk_generic_datasources.parsers.text.table_processor import TableProcessor

# The device reports this only in its login banner, and the banner is not part of
# any command output, so it is pinned to the model this YAML describes.
MODEL = 'Virtual Connect SE 100Gb F32 Module for Synergy'
VENDOR = 'HPE'
DEFAULT_VRF = 'default'

# vRNI's documented contract is bits per second. Note the SDK's own sample CSVs
# disagree and carry Kbps (Cisco pipes 'BW <n> Kbit' through unchanged), so if
# port speeds read 1000x low in the product, this is the single place to change.
SPEED_MULTIPLIERS = {'kbps': 10 ** 3, 'mbps': 10 ** 6, 'gbps': 10 ** 9, 'tbps': 10 ** 12}

# Route code letters from 'show ip route' mapped to vRNI route types. The sample
# CSVs use lower case for these.
ROUTE_TYPES = {'C': 'direct', 'S': 'static', 'R': 'rip', 'B': 'bgp', 'O': 'ospf',
               'I': 'isis', 'E': 'ecmp'}

# Interfaces that are routed rather than switched, and so belong in
# router-interfaces.csv instead of switch-ports.csv.
ROUTED_INTERFACE = re.compile(r'^(mgmt|vlan\d+)$', re.IGNORECASE)
PORT_CHANNEL_INTERFACE = re.compile(r'^po\d+$', re.IGNORECASE)


def normalise_status(value):
    """'up' -> 'UP'. Anything unrecognised becomes an empty string."""
    value = (value or '').strip().lower()
    if value in ('up', 'down'):
        return value.upper()
    return ''


def speed_to_bits_per_second(value, unit):
    """('10', 'Gbps') -> '10000000000'."""
    multiplier = SPEED_MULTIPLIERS.get((unit or '').strip().lower())
    if multiplier is None or not value:
        return ''
    try:
        return str(int(float(value) * multiplier))
    except ValueError:
        return ''


def expand_vlan_list(text):
    """'vlan 21,42-44,52' -> '21,42,43,44,52'.

    vRNI wants a flat comma separated list of VLAN ids, but the CLI abbreviates
    runs as ranges and prefixes the list with 'vlan'.
    """
    text = (text or '').strip()
    if not text or text.lower() == 'none':
        return ''
    text = re.sub(r'(?i)\bvlans?\b', ' ', text)
    vlans = []
    for token in text.replace(' ', '').split(','):
        if not token:
            continue
        if '-' in token:
            bounds = token.split('-')
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                vlans.extend(range(int(bounds[0]), int(bounds[1]) + 1))
                continue
        if token.isdigit():
            vlans.append(int(token))
    return ','.join(str(v) for v in sorted(set(vlans)))


def unwrap_continuation_lines(data, key_pattern):
    """Fold wrapped continuation lines back onto the line that started them.

    A line matching key_pattern starts a logical line; anything that follows and
    matches neither key_pattern nor a new record header is a continuation of it.
    """
    lines = []
    for line in data.splitlines():
        if not line.strip():
            continue
        if re.match(key_pattern, line.strip()) or not lines:
            lines.append(line.strip())
        elif ':' in line or re.match(r'^\s*\S+\s+\S+', line):
            # A wrapped value: no colon of its own, so it belongs to the previous
            # logical line. Lines that do carry a colon start a new field.
            if ':' in line:
                lines.append(line.strip())
            else:
                lines[-1] = lines[-1] + ',' + line.strip()
        else:
            lines[-1] = lines[-1] + ',' + line.strip()
    return '\n'.join(lines)


class HpeSwitchTableProcessor(TableProcessor):
    """Fold the several identity commands into the single switch.csv row.

    No one command carries everything vRNI wants, so hostname, firmware and
    serial are collected separately and merged here.
    """

    def process_tables(self, tables):
        row = {'vendor': VENDOR, 'model': MODEL, 'haState': 'ACTIVE'}
        for table in tables.values():
            for parsed in table:
                for key, value in parsed.items():
                    if value:
                        row[key] = value
        # PhysicalDevice indexes table[0] for the 'switch' table and would raise
        # rather than warn, so make the failure legible.
        if 'hostname' not in row:
            raise ValueError('Could not determine hostname; check "show host" output')
        row.setdefault('name', row['hostname'])
        return [row]


class HpeInterfacePrePostProcessor(PrePostProcessor):
    """Normalise one 'show interfaces information' block into vRNI's vocabulary."""

    def post_process(self, data):
        results = []
        for parsed in data:
            name = (parsed.get('name') or '').strip()
            if not name:
                continue
            admin = normalise_status(parsed.get('administrativeStatus'))
            operational = normalise_status(parsed.get('operationalStatus'))
            row = {
                'name': name,
                'administrativeStatus': admin,
                'operationalStatus': operational,
                # The CLI states this directly as '(connected)' / '(not connect)'.
                'connected': 'true' if 'not' not in (parsed.get('connected') or 'not') else 'false',
                'mtu': (parsed.get('mtu') or '').strip(),
                'hardwareAddress': (parsed.get('hardwareAddress') or '').strip(),
                'interfaceSpeed': speed_to_bits_per_second(parsed.get('speedValue'),
                                                           parsed.get('speedUnit')),
                'duplex': (parsed.get('duplex') or '').strip().upper(),
            }
            row['operationalSpeed'] = row['interfaceSpeed']
            # Retained for the table processors that split switch ports from
            # router interfaces and port channels; not written to any CSV.
            row['bridgePortType'] = (parsed.get('bridgePortType') or '').strip()
            row['ipAddress'] = ''
            results.append(row)
        return results


class HpeSwitchPortsTableProcessor(TableProcessor):
    """switch-ports.csv: the switched ports, with VLAN membership joined on."""

    def process_tables(self, tables):
        interfaces = tables['showInterfaces']
        vlan_membership = {row['name']: row for row in tables.get('showPortVlanList', [])}

        results = []
        for row in interfaces:
            name = row['name']
            if ROUTED_INTERFACE.match(name) or PORT_CHANNEL_INTERFACE.match(name):
                continue
            membership = vlan_membership.get(name, {})
            port = dict(row)
            port.pop('bridgePortType', None)
            port.pop('ipAddress', None)
            port['vlans'] = membership.get('vlans', '')
            port['accessVlan'] = membership.get('accessVlan', '')
            port['switchPortMode'] = membership.get('switchPortMode', 'OTHER')
            results.append(port)
        return results


class HpePortChannelsTableProcessor(TableProcessor):
    """port-channels.csv: the same shape as switch ports, restricted to po*."""

    def process_tables(self, tables):
        interfaces = tables['showInterfaces']
        vlan_membership = {row['name']: row for row in tables.get('showPortVlanList', [])}

        results = []
        for row in interfaces:
            if not PORT_CHANNEL_INTERFACE.match(row['name']):
                continue
            membership = vlan_membership.get(row['name'], {})
            channel = dict(row)
            channel.pop('bridgePortType', None)
            channel.pop('ipAddress', None)
            channel['vlans'] = membership.get('vlans', '')
            channel['accessVlan'] = membership.get('accessVlan', '')
            channel['switchPortMode'] = membership.get('switchPortMode', 'OTHER')
            results.append(channel)
        return results


class HpePortVlanListPrePostProcessor(PrePostProcessor):
    """'show port vlanlist' - one block per port, VLAN lists unwrapped."""

    def pre_process(self, data):
        return unwrap_continuation_lines(data, r'^(Port\s+\S+|Member|Untagged)')

    def post_process(self, data):
        results = []
        for parsed in data:
            name = (parsed.get('name') or '').strip()
            if not name:
                continue
            tagged = expand_vlan_list(parsed.get('memberVlans'))
            untagged = expand_vlan_list(parsed.get('untaggedVlans'))
            if tagged:
                mode = 'TRUNK'
            elif untagged:
                mode = 'ACCESS'
            else:
                mode = 'OTHER'
            all_vlans = expand_vlan_list(','.join(v for v in (tagged, untagged) if v))
            results.append({'name': name,
                            'vlans': all_vlans,
                            'accessVlan': untagged.split(',')[0] if untagged else '',
                            'switchPortMode': mode})
        return results


class HpeVlanPrePostProcessor(PrePostProcessor):
    """l2bridges.csv from 'show vlan brief'."""

    def pre_process(self, data):
        return unwrap_continuation_lines(data, r'^(Vlan ID|Member Ports|Untagged Ports|'
                                               r'Forbidden Ports|Name|Status)')

    def post_process(self, data):
        results = []
        for parsed in data:
            vlan_id = (parsed.get('vlans') or '').strip()
            if not vlan_id.isdigit():
                continue
            # VLAN names on an OneView-managed module are generated GUIDs, so
            # fall back to the id when the name is absent.
            name = (parsed.get('name') or '').strip() or 'VLAN{}'.format(vlan_id)
            results.append({'name': name, 'vlans': vlan_id})
        return results


class HpeMacAddressTablePrePostProcessor(PrePostProcessor):
    """mac-address-table.csv - drops the header row the table parser yields."""

    def post_process(self, data):
        results = []
        for parsed in data:
            mac = (parsed.get('macAddress') or '').strip()
            vlan = (parsed.get('vlan') or '').strip()
            if not vlan.isdigit() or not re.match(r'^[0-9a-fA-F:.-]{12,}$', mac):
                continue
            results.append({'macAddress': mac,
                            'vlan': vlan,
                            'switchPort': (parsed.get('switchPort') or '').strip()})
        return results


class HpeLldpNeighborsPrePostProcessor(PrePostProcessor):
    """neighbors.csv from 'show lldp neighbors detail'.

    The non-detail output truncates chassis ids with an ellipsis and leaves the
    capability column blank on some rows, which would shift a columnar parse, so
    the detail form is used instead. It also carries the peer's real System Name.
    """

    def post_process(self, data):
        results = []
        for parsed in data:
            local = (parsed.get('localInterface') or '').strip()
            remote_interface = (parsed.get('remoteInterface') or '').strip()
            if not local or not remote_interface:
                continue
            remote = (parsed.get('remoteDevice') or '').strip()
            if not remote or remote == '-':
                # No System Name advertised; the chassis id is the only identity.
                remote = (parsed.get('chassisId') or '').strip()
            results.append({'localInterface': local,
                            'remoteDevice': remote,
                            'remoteInterface': remote_interface})
        return results


class HpeRouterInterfacesPrePostProcessor(PrePostProcessor):
    """router-interfaces.csv from 'show ip interface'."""

    def post_process(self, data):
        results = []
        for parsed in data:
            name = (parsed.get('name') or '').strip()
            address = (parsed.get('ipAddress') or '').strip()
            if not name or not address:
                continue
            operational = normalise_status(parsed.get('operationalStatus'))
            vlan_match = re.match(r'^vlan(\d+)$', name, re.IGNORECASE)
            results.append({
                'name': name,
                'ipAddress': address,
                'vrf': DEFAULT_VRF,
                'vlan': vlan_match.group(1) if vlan_match else '',
                'administrativeStatus': normalise_status(parsed.get('administrativeStatus')),
                'operationalStatus': operational,
                'connected': 'true' if operational == 'UP' else 'false',
            })
        return results


class HpeRoutesPrePostProcessor(PrePostProcessor):
    """routes.csv from 'show ip route'.

    Two line shapes have to be handled, so this parses the text directly rather
    than going through the rule engine:

        S 0.0.0.0/0  [0/100] via 10.244.130.1
        C 10.244.130.0/23 is directly connected, mgmt
    """

    VRF_PATTERN = re.compile(r'^Vrf Name:\s*(\S+)')
    CONNECTED_PATTERN = re.compile(r'^(\w+) (\S+) is directly connected,\s*(\S+)')
    VIA_PATTERN = re.compile(r'^(\w+) (\S+)\s+\[[^\]]*\]\s*via\s+(\S+)(?:,\s*(\S+))?')

    def parse(self, data):
        routes = []
        vrf = DEFAULT_VRF
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            vrf_match = self.VRF_PATTERN.match(line)
            if vrf_match:
                vrf = vrf_match.group(1)
                continue

            connected = self.CONNECTED_PATTERN.match(line)
            if connected:
                code, network, interface = connected.groups()
                routes.append({'name': network, 'network': network, 'nextHop': 'DIRECT',
                               'routeType': ROUTE_TYPES.get(code[0], code.lower()),
                               'interfaceName': interface, 'vrf': vrf})
                continue

            via = self.VIA_PATTERN.match(line)
            if via:
                code, network, next_hop, interface = via.groups()
                routes.append({'name': network, 'network': network, 'nextHop': next_hop,
                               'routeType': ROUTE_TYPES.get(code[0], code.lower()),
                               'interfaceName': interface or '', 'vrf': vrf})
        return self.resolve_egress_interfaces(routes)

    @staticmethod
    def resolve_egress_interfaces(routes):
        """Fill in interfaceName for routes the CLI reports without one.

        'show ip route' names the egress interface for connected routes but not
        for a route stated as 'via <next-hop>'. vRNI wants interfaceName on every
        route, and it is derivable: the next hop is reachable over whichever
        connected subnet contains it.
        """
        connected = []
        for route in routes:
            if route['nextHop'] == 'DIRECT' and route['interfaceName']:
                try:
                    connected.append((IPNetwork(route['network']), route['interfaceName']))
                except (AddrFormatError, ValueError):
                    continue

        for route in routes:
            if route['interfaceName'] or route['nextHop'] == 'DIRECT':
                continue
            try:
                next_hop = IPAddress(route['nextHop'])
            except (AddrFormatError, ValueError):
                continue
            for network, interface in connected:
                if next_hop in network:
                    route['interfaceName'] = interface
                    break
        return routes


class HpeVrfPrePostProcessor(PrePostProcessor):
    """vrfs.csv from the 'Vrf Name:' headings in 'show ip route'.

    There is no bare 'show vrf' on this platform, so the routing table is the
    only place a VRF is named.
    """

    def parse(self, data):
        names = []
        for line in data.splitlines():
            match = re.match(r'^Vrf Name:\s*(\S+)', line.strip())
            if match and match.group(1) not in names:
                names.append(match.group(1))
        return [{'name': name} for name in names] or [{'name': DEFAULT_VRF}]
