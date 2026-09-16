# Copyright 2026 The Regents of the University of Michigan
# SPDX-License-Identifier: BSD-2-Clause

"""Tests for the HPE Virtual Connect SE datasource.

Sample output is sanitised and inlined rather than read from test/fixtures, because
real captures contain internal addresses, serials and neighbour hostnames and are
deliberately untracked. See test/fixtures/README.md.

The rules under test are loaded from hpe.yml rather than restated here, so these
exercise the YAML's regexes and not a copy of them.
"""

import os
import unittest

import network_insight_sdk_generic_datasources.common.yaml_utilities as yaml_utilities
from network_insight_sdk_generic_datasources.common.physical_device import PhysicalDevice
from network_insight_sdk_generic_datasources.routers_and_switches.hpe.hpe_pre_post_processor import (
    HpeRoutesPrePostProcessor,
    HpeSwitchTableProcessor,
    HpeVrfPrePostProcessor,
    expand_vlan_list,
    speed_to_bits_per_second,
)

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    'network_insight_sdk_generic_datasources', 'routers_and_switches', 'hpe', 'hpe.yml')
MODEL = 'vc-se-100gb-f32'


def load_workloads():
    with open(YAML_PATH) as handle:
        return {w['table_id']: w for w in yaml_utilities.altered_safe_load(handle)[MODEL]['workloads']}


def run_workload(table_id, text):
    """Parse text through the real pipeline for one workload, as PhysicalDevice would."""
    workload = load_workloads()[table_id]
    device = PhysicalDevice('hpe', MODEL, [], None, None, None, '/tmp')
    return device.parse_command_output(workload, text)


class HelperTestCase(unittest.TestCase):

    def test_speed_is_converted_to_bits_per_second(self):
        self.assertEqual('10000000000', speed_to_bits_per_second('10', 'Gbps'))
        self.assertEqual('100000000000', speed_to_bits_per_second('100', 'Gbps'))
        self.assertEqual('1000000', speed_to_bits_per_second('1', 'Mbps'))

    def test_speed_is_empty_when_the_block_did_not_report_one(self):
        # S-Channel interfaces carry no speed line at all.
        self.assertEqual('', speed_to_bits_per_second('', ''))
        self.assertEqual('', speed_to_bits_per_second('10', 'furlongs'))

    def test_vlan_ranges_are_expanded(self):
        self.assertEqual('21,42,43,44,52', expand_vlan_list('vlan 21,42-44,52'))

    def test_vlan_list_handles_none_and_duplicates(self):
        self.assertEqual('', expand_vlan_list('None'))
        self.assertEqual('', expand_vlan_list(''))
        self.assertEqual('7', expand_vlan_list('vlan 7,7'))


class InterfaceParsingTestCase(unittest.TestCase):

    ETHERNET_PORT = """ethernet0/0/3 up, line protocol is up (connected)
Bridge Port Type: Customer Bridge Port

Interface SubType: hundredGigE
Interface Alias: ethernet0/0/3

Hardware Address is 00:00:5e:00:53:01
MTU  9394 bytes,
Port Role:Uplink

Full duplex, 100 Gbps,  No-Negotiation
Auto-MDIX on
"""

    ADMIN_DOWN_PORT = """ethernet0/0/9 down, line protocol is down (not connect)
Bridge Port Type: Customer Bridge Port

Hardware Address is 00:00:5e:00:53:02
MTU  9394 bytes,

Full duplex, 10 Gbps,  No-Negotiation
"""

    S_CHANNEL_PORT = """S-Channel0/1/1:2 up, line protocol is up (connected)
Bridge Port Type: Station Facing Bridge Port

Interface SubType: Not Applicable
Interface Alias: S-Channel0/1/1:2
"""

    def test_ethernet_port_is_fully_parsed(self):
        rows = run_workload('showInterfaces', self.ETHERNET_PORT)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual('ethernet0/0/3', row['name'])
        self.assertEqual('UP', row['administrativeStatus'])
        self.assertEqual('UP', row['operationalStatus'])
        self.assertEqual('true', row['connected'])
        self.assertEqual('9394', row['mtu'])
        self.assertEqual('00:00:5e:00:53:01', row['hardwareAddress'])
        self.assertEqual('100000000000', row['interfaceSpeed'])
        self.assertEqual('FULL', row['duplex'])

    def test_admin_down_port_reports_both_states_and_is_not_connected(self):
        row = run_workload('showInterfaces', self.ADMIN_DOWN_PORT)[0]
        self.assertEqual('DOWN', row['administrativeStatus'])
        self.assertEqual('DOWN', row['operationalStatus'])
        self.assertEqual('false', row['connected'])

    def test_s_channel_parses_despite_missing_fields(self):
        # The rule engine yields empty strings for rules that do not match, so a
        # minimal block must still produce a usable row rather than raising.
        row = run_workload('showInterfaces', self.S_CHANNEL_PORT)[0]
        self.assertEqual('S-Channel0/1/1:2', row['name'])
        self.assertEqual('UP', row['operationalStatus'])
        self.assertEqual('true', row['connected'])
        self.assertEqual('', row['interfaceSpeed'])
        self.assertEqual('', row['hardwareAddress'])

    def test_multiple_interfaces_are_split_into_separate_rows(self):
        rows = run_workload('showInterfaces', self.ETHERNET_PORT + self.S_CHANNEL_PORT)
        self.assertEqual(['ethernet0/0/3', 'S-Channel0/1/1:2'], [r['name'] for r in rows])


class PortVlanListTestCase(unittest.TestCase):

    VLANLIST = """Port VLAN List
-------------------------------
Port ethernet0/0/3

 Member Vlan's                       : vlan 92
 Untagged Vlan's                     : None
Port S-Channel0/1/1:2

 Member Vlan's                       : vlan 21,42-44,
                                           92,111
 Untagged Vlan's                     : None
Port ethernet0/0/9

 Member Vlan's                       : None
 Untagged Vlan's                     : vlan 5
"""

    def test_tagged_membership_is_a_trunk(self):
        rows = {r['name']: r for r in run_workload('showPortVlanList', self.VLANLIST)}
        self.assertEqual('TRUNK', rows['ethernet0/0/3']['switchPortMode'])
        self.assertEqual('92', rows['ethernet0/0/3']['vlans'])

    def test_wrapped_vlan_list_is_unwrapped_and_expanded(self):
        rows = {r['name']: r for r in run_workload('showPortVlanList', self.VLANLIST)}
        self.assertEqual('21,42,43,44,92,111', rows['S-Channel0/1/1:2']['vlans'])

    def test_untagged_only_membership_is_an_access_port(self):
        rows = {r['name']: r for r in run_workload('showPortVlanList', self.VLANLIST)}
        self.assertEqual('ACCESS', rows['ethernet0/0/9']['switchPortMode'])
        self.assertEqual('5', rows['ethernet0/0/9']['accessVlan'])


class MacAddressTableTestCase(unittest.TestCase):

    MAC_TABLE = """Vlan    Mac                Type    Ports                       Hit
------------------------------------------------------------------
109     00:00:5e:00:53:10  Learnt  S-Channel1/1/8:2            Yes
134     00:00:5e:00:53:11  Learnt  po2                         No
"""

    def test_rows_are_parsed_and_the_header_is_dropped(self):
        rows = run_workload('mac-address-table', self.MAC_TABLE)
        self.assertEqual(2, len(rows))
        self.assertEqual({'macAddress': '00:00:5e:00:53:10', 'vlan': '109',
                          'switchPort': 'S-Channel1/1/8:2'}, rows[0])
        self.assertEqual('po2', rows[1]['switchPort'])


class LldpNeighborsTestCase(unittest.TestCase):

    NEIGHBOUR = """Chassis Id SubType            : Mac Address
Chassis Id                    : 00:00:5e:00:53:20
Port Id SubType               : Interface Name
Port Id                       : xe-0/0/30
Port Description              : xe-0/0/30
System Name                   : switch-a
System Desc                   : Example Networks, version 1.0
Local Intf                    : ethernet0/0/1:1
Time Remaining                : 111 Seconds
"""

    ANONYMOUS_NEIGHBOUR = """Chassis Id SubType            : Mac Address
Chassis Id                    : 00:00:5e:00:53:21
Port Id SubType               : Mac Address
Port Id                       : 00:00:5e:00:53:22
Local Intf                    : ethernet0/1/1
"""

    def test_system_name_is_used_as_the_remote_device(self):
        row = run_workload('neighbors', self.NEIGHBOUR)[0]
        self.assertEqual({'localInterface': 'ethernet0/0/1:1', 'remoteDevice': 'switch-a',
                          'remoteInterface': 'xe-0/0/30'}, row)

    def test_port_id_subtype_is_not_mistaken_for_the_port_id(self):
        row = run_workload('neighbors', self.NEIGHBOUR)[0]
        self.assertEqual('xe-0/0/30', row['remoteInterface'])

    def test_chassis_id_is_the_fallback_when_no_system_name_is_advertised(self):
        row = run_workload('neighbors', self.ANONYMOUS_NEIGHBOUR)[0]
        self.assertEqual('00:00:5e:00:53:21', row['remoteDevice'])


class RouterInterfaceTestCase(unittest.TestCase):

    IP_INTERFACES = """vlan4095 is up, line protocol is up
Internet Address is 169.254.254.1/30
Broadcast Address  169.254.254.3

mgmt is up, line protocol is up
Internet Address is 192.0.2.10/24
Broadcast Address  192.0.2.255
Gateway Address    192.0.2.1
"""

    def test_addresses_and_vlan_ids_are_extracted(self):
        rows = {r['name']: r for r in run_workload('router-interfaces', self.IP_INTERFACES)}
        self.assertEqual('169.254.254.1/30', rows['vlan4095']['ipAddress'])
        self.assertEqual('4095', rows['vlan4095']['vlan'])
        self.assertEqual('192.0.2.10/24', rows['mgmt']['ipAddress'])
        self.assertEqual('', rows['mgmt']['vlan'])
        self.assertEqual('true', rows['mgmt']['connected'])


class RoutesTestCase(unittest.TestCase):

    ROUTES = """Codes: C - connected, S - static, R - rip, B - bgp, O - ospf

Vrf Name:          default
---------
S 0.0.0.0/0  [0/100] via 192.0.2.1
C 192.0.2.0/24 is directly connected, mgmt
C 169.254.254.0/30 is directly connected, vlan4095
"""

    def test_connected_and_static_routes_are_parsed(self):
        routes = {r['network']: r for r in HpeRoutesPrePostProcessor().parse(self.ROUTES)}
        self.assertEqual('DIRECT', routes['192.0.2.0/24']['nextHop'])
        self.assertEqual('direct', routes['192.0.2.0/24']['routeType'])
        self.assertEqual('mgmt', routes['192.0.2.0/24']['interfaceName'])
        self.assertEqual('192.0.2.1', routes['0.0.0.0/0']['nextHop'])
        self.assertEqual('static', routes['0.0.0.0/0']['routeType'])

    def test_egress_interface_is_resolved_from_the_containing_connected_subnet(self):
        # The CLI does not name an interface for a 'via' route, but vRNI requires
        # one, and the next hop sits inside mgmt's connected subnet.
        routes = {r['network']: r for r in HpeRoutesPrePostProcessor().parse(self.ROUTES)}
        self.assertEqual('mgmt', routes['0.0.0.0/0']['interfaceName'])

    def test_vrf_is_taken_from_the_routing_table_heading(self):
        self.assertEqual([{'name': 'default'}], HpeVrfPrePostProcessor().parse(self.ROUTES))

    def test_vrf_falls_back_to_default_when_absent(self):
        self.assertEqual([{'name': 'default'}], HpeVrfPrePostProcessor().parse('no vrfs here'))


class SwitchTableTestCase(unittest.TestCase):

    def test_identity_commands_are_merged_into_one_row(self):
        rows = HpeSwitchTableProcessor().process_tables({
            'showHost': [{'hostname': 'interconnect-a'}],
            'showSystemInformation': [{'os': '2.9.1-1001'}],
            'showSerialNumber': [{'serial': 'ABC123'}],
        })
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual('interconnect-a', row['hostname'])
        self.assertEqual('interconnect-a', row['name'])
        self.assertEqual('2.9.1-1001', row['os'])
        self.assertEqual('ABC123', row['serial'])
        self.assertEqual('HPE', row['vendor'])
        self.assertEqual('ACTIVE', row['haState'])

    def test_missing_hostname_raises_rather_than_producing_an_unusable_row(self):
        # PhysicalDevice indexes table[0] for the switch table without guarding,
        # so failing here gives a legible error instead of an IndexError later.
        with self.assertRaises(ValueError):
            HpeSwitchTableProcessor().process_tables({'showHost': [{'hostname': ''}]})


if __name__ == '__main__':
    unittest.main()
