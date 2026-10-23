"""Launch Core with an explicit Android network adapter bridge.

Android may deny getifaddrs/netlink to an application UID, including in PRoot.
Only EPERM/EACCES trigger this bridge. Use actual ConnectivityManager data,
never an empty adapter list or a blanket suppression of unrelated errors.
"""
import errno
import ipaddress
import json
import logging
from pathlib import Path
import runpy
import time


def adapters_from_snapshot(module, path):
    with Path(path).open() as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('Android network snapshot exceeds size limit')
    data = json.loads(raw)
    if data['schema'] != 1 or abs(time.time() - data['updatedAt']) > 120:
        raise ValueError('Android network snapshot is unsupported or stale')
    adapters = []
    for interface in data['interfaces']:
        name, index = interface['name'], interface['index']
        if not isinstance(name, str) or not name or not isinstance(index, int) or index < 1:
            raise ValueError('Invalid Android network interface')
        addresses = []
        for item in interface['addresses']:
            address = ipaddress.ip_address(item['address'])
            prefix = item['prefix']
            if not isinstance(prefix, int) or not 0 <= prefix <= address.max_prefixlen:
                raise ValueError('Invalid Android address prefix')
            ip = str(address) if address.version == 4 else (str(address), 0, index if address.is_link_local else 0)
            addresses.append(module.IP(ip=ip, network_prefix=prefix, nice_name=name))
        adapters.append(module.Adapter(name=name, nice_name=name, ips=addresses, index=index))
    if not adapters:
        raise ValueError('Android did not supply any network interfaces')
    return adapters


def install_bridge(module=None, path='/run/habab-network.json'):
    if module is None:
        import ifaddr as module
    native = module.get_adapters
    warned = False

    def get_adapters(*args, **kwargs):
        nonlocal warned
        try:
            return native(*args, **kwargs)
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.EACCES):
                raise
            adapters = adapters_from_snapshot(module, path)
            if not warned:
                logging.getLogger('habab.android').warning(
                    'Android denies native interface enumeration; using ConnectivityManager adapter snapshot'
                )
                warned = True
            return adapters

    module.get_adapters = get_adapters


if __name__ == '__main__':
    install_bridge()
    runpy.run_module('homeassistant', run_name='__main__')
