"""Module to initialize Maxmind databases and lookup IP metadata."""

import logging
import os
import tempfile
from typing import Optional, Tuple

import geoip2.database
from geoip2.database import MODE_MEMORY
import apache_beam.io.filesystems as apache_filesystems

from pipeline.metadata.ip_metadata_interface import IpMetadataInterface

MAXMIND_CITY = 'GeoLite2-City.mmdb'
MAXMIND_ASN = 'GeoLite2-ASN.mmdb'


def _maxmind_reader(filepath: str) -> geoip2.database.Reader:
  """Return a reader for the Maxmind database.

    Args:
      filepath: gcs or local fileststem path to Maxmind .mmdb file

    Returns:
      geoip2.database.Reader
  """
  f = apache_filesystems.FileSystems.open(filepath)

  # MaxMind Reader will only take a filepath,
  # so we need to write the file to local disk
  with tempfile.NamedTemporaryFile() as disk_file:
    disk_file.write(f.read())
    disk_filename = disk_file.name
    database = geoip2.database.Reader(disk_filename, mode=MODE_MEMORY)
    return database


class MaxmindIpMetadata(IpMetadataInterface):
  """Lookup database for Maxmind ASN and country metadata."""

  def __init__(self, maxmind_folder: str) -> None:
    """Create a Maxmind Database.

      Args:
        maxmind_folder: a folder containing maxmind files.
          Either a gcs filepath or a local system folder.
    """
    maxmind_city_path = os.path.join(maxmind_folder, MAXMIND_CITY)
    maxmind_asn_path = os.path.join(maxmind_folder, MAXMIND_ASN)

    self.maxmind_city = _maxmind_reader(maxmind_city_path)
    self.maxmind_asn = _maxmind_reader(maxmind_asn_path)

  def lookup(
      self, ip: str
  ) -> Tuple[Optional[str], int, Optional[str], None, None, Optional[str]]:
    """Lookup metadata infomation about an IP.

      Args:
        ip: string of the format 1.1.1.1 (ipv4 only)

      Returns:
        Tuple(netblock, asn, as_name, as_full_name, as_type, country)
        ("1.0.0.1/24", 13335, "CLOUDFLARENET", None, None, "AU")
        The final 4 fields may be None
        Maxmind never has as_full_name or as_type info.

      Raises:
        KeyError: when the IP's ASN can't be found
    """
    (asn, as_name, netblock) = self._get_maxmind_asn(ip)
    country = self._get_country_code(ip)

    if not asn:
      raise KeyError("No Maxmind entry for {}".format(ip))

    return (netblock, asn, as_name, None, None, country)

  def _get_country_code(self, vp_ip: str) -> Optional[str]:
    """Get country code for IP address.

      Args:
        vp_ip: IP address of vantage point (as string)

      Returns:
        2-letter ISO country code
    """
    try:
      vp_info = self.maxmind_city.city(vp_ip)
      return vp_info.country.iso_code
    except (ValueError, geoip2.errors.AddressNotFoundError) as e:
      logging.warning('Maxmind: %s\n', e)
    return None

  def _get_maxmind_asn(
      self, vp_ip: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Get ASN information for IP address.

      Args:
        vp_ip: IP address of vantage point (as string)

      Returns:
        Tuple containing AS num, AS org, and netblock
    """
    try:
      vp_info = self.maxmind_asn.asn(vp_ip)
      asn = vp_info.autonomous_system_number
      as_name = vp_info.autonomous_system_organization
      if vp_info.network:
        netblock: Optional[str] = vp_info.network.with_prefixlen
      else:
        netblock = None
      return asn, as_name, netblock
    except (ValueError, geoip2.errors.AddressNotFoundError) as e:
      logging.warning('Maxmind: %s\n', e)
    return None, None, None


class FakeMaxmindIpMetadata(IpMetadataInterface):
  """A fake lookup table for testing MaxmindIpMetadata."""

  def __init__(self, _: str) -> None:
    super()

  # pylint: disable=no-self-use
  def lookup(
      self, _: str
  ) -> Tuple[str, int, Optional[str], Optional[str], Optional[str],
             Optional[str]]:
    return ('101.103.0.0/16', 1221, 'ASN-TELSTRA', None, None, 'AU')
