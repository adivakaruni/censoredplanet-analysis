"""Beam pipeline helper for flattening measurements lines into bq format."""

from __future__ import absolute_import

import json
import logging
from typing import Tuple, Iterator
import uuid

from pipeline.metadata.flatten_base import Row
from pipeline.metadata.flatten_satellite import FlattenSatelliteMixin, SATELLITE_PATH_COMPONENT
from pipeline.metadata.flatten_hyperquack import FlattenHyperquackMixin

# The structure of this class's inheritance is
#
#                    beam.DoFn
#                       |
#              FlattenMeasurementBase
#                   /         \
# FlattenHyperquackMixin   FlattenSatelliteMixin
#                   \         /
#               FlattenMeasurement
#


class FlattenMeasurement(FlattenSatelliteMixin, FlattenHyperquackMixin):
  """DoFn class for flattening lines of json text into Rows."""

  def process(self, element: Tuple[str, str]) -> Iterator[Row]:
    """Flatten a measurement string into several roundtrip Rows.

    Args:
      element: Tuple(filepath, line)
        filename: a filepath string
        line: a json string describing a censored planet measurement. example
        {'Keyword': 'test.com',
        'Server': '1.2.3.4',
        'Results': [{'Success': true},
                    {'Success': false}]}

    Yields:
      Row dicts containing individual roundtrip information
      {'column_name': field_value}
      examples:
      {'domain': 'test.com', 'ip': '1.2.3.4', 'success': true}
      {'domain': 'test.com', 'ip': '1.2.3.4', 'success': false}
    """
    (filename, line) = element

    # pylint: disable=too-many-branches
    try:
      scan = json.loads(line)
    except json.decoder.JSONDecodeError as e:
      logging.warning('JSONDecodeError: %s\nFilename: %s\n%s\n', e, filename,
                      line)
      return

    # Add a unique id per-measurement so single retry rows can be reassembled
    random_measurement_id = uuid.uuid4().hex

    if SATELLITE_PATH_COMPONENT in filename:
      yield from self._process_satellite(filename, scan, random_measurement_id)
    else:
      yield from self._process_hyperquack(filename, scan, random_measurement_id)
