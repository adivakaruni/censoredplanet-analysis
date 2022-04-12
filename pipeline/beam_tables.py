# Copyright 2020 Jigsaw Operations LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Beam pipeline for converting json scan files into bigquery tables."""

from __future__ import absolute_import

import datetime
import logging
import pathlib
import re
import json
from typing import BinaryIO, Callable, Optional, Tuple, Dict, List, Any, Iterator, Iterable

import apache_beam as beam
from apache_beam.io.gcp.gcsfilesystem import GCSFileSystem
from apache_beam.io.filesystem import CompressionTypes
from apache_beam.io.fileio import WriteToFiles, FileMetadata, FileSink
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.coders import coders
from google.cloud import bigquery as cloud_bigquery  # type: ignore
from google.cloud import storage

from pipeline.metadata.beam_metadata import DateIpKey, IP_METADATA_PCOLLECTION_NAME, ROWS_PCOLLECION_NAME, make_date_ip_key, merge_metadata_with_rows
from pipeline.metadata.schema import BigqueryRow, IpMetadataWithKeys
from pipeline.metadata import schema
from pipeline.metadata import flatten_base
from pipeline.metadata import flatten
from pipeline.metadata import satellite
from pipeline.metadata.ip_metadata_chooser import IpMetadataChooserFactory

# Tables have names like 'echo_scan' and 'http_scan
BASE_TABLE_NAME = 'scan'
# Prod data goes in the `firehook-censoredplanet:base' dataset
PROD_DATASET_NAME = 'base'

# Mapping of each scan type to the zone to run its pipeline in.
# This adds more parallelization when running all pipelines.
SCAN_TYPES_TO_ZONES = {
    'https': 'us-east1',  # https has the most data, so it gets the best zone.
    'http': 'us-east4',
    'echo': 'us-west1',
    'discard': 'us-west2',
    'satellite': 'us-central1'  # us-central1 has 160 shuffle slots by default
}

ALL_SCAN_TYPES = SCAN_TYPES_TO_ZONES.keys()

# Data files for the non-Satellite pipelines
SCAN_FILES = ['results.json']

# An empty json file is 0 bytes when unzipped, but 33 bytes when zipped
EMPTY_GZIPPED_FILE_SIZE = 33


class JsonGzSink(FileSink):
  """A sink to a GCS or local .json.gz file."""

  def __init__(self) -> None:
    """Initialize a JsonGzSink."""
    self.coder = coders.ToBytesCoder()
    self.file_handle: BinaryIO

  def create_metadata(self, destination: str,
                      full_file_name: str) -> FileMetadata:
    """Returns the file metadata as tuple (mime_type, compression_type)."""
    return FileMetadata(
        mime_type="application/json", compression_type=CompressionTypes.GZIP)

  def open(self, fh: BinaryIO) -> None:
    """Prepares the sink file for writing."""
    self.file_handle = fh

  def write(self, record: str) -> None:
    """Writes a single record."""
    self.file_handle.write(self.coder.encode(record) + b'\n')

  def flush(self) -> None:
    """Flushes the sink file."""
    # This method must be implemented for FileSink subclasses,
    # but calling self.file_handle.flush() here triggers a zlib error.
    pass  # pylint: disable=unnecessary-pass


def _get_existing_datasources(destination: str, project: str) -> List[str]:
  """Given a BigQuery or GCS destination return all contributing sources.

  Args:
    destination: name of a bigquery table like
      'firehook-censoredplanet:echo_results.scan_test'
      or a GCS folder like 'gs://firehook-test/avirkud'
    project: project to use for query like 'firehook-censoredplanet'

  Returns:
    List of data sources. ex ['CP_Quack-echo-2020-08-23-06-01-02']
  """
  # The client needs to be created locally
  # because bigquery and storage client objects are unpickleable.
  # So passing in a client to the class breaks the pickling beam uses
  # to send state to remote machines.
  if destination.startswith('gs://'):
    client = storage.Client(project=project)

    # The first path component after gs:// is the bucket, the rest is the subdir.
    path_split = destination[5:].split('/', maxsplit=1)
    bucket_name = path_split[0]
    folder = path_split[1] if len(path_split) > 1 else None

    # We expect filenames in format gs://bucket/.../source={source}/.../file
    bucket = client.get_bucket(bucket_name)
    matches = [
        re.findall(r'source=[^/]*', file.name)
        for file in client.list_blobs(bucket, prefix=folder)
    ]
    sources = [match[0][7:] for match in matches if match]
    return sources

  client = cloud_bigquery.Client(project=project)

  # Bigquery table names are of the format project:dataset.table
  # but this library wants the format project.dataset.table
  fixed_table_name = destination.replace(':', '.')

  query = f'SELECT DISTINCT(source) AS source FROM `{fixed_table_name}`'
  rows = client.query(query)
  sources = [row.source for row in rows]
  return sources


def _make_tuple(line: str, filename: str) -> Tuple[str, str]:
  """Helper method for making a tuple from two args."""
  return (filename, line)


def _read_scan_text(
    p: beam.Pipeline,
    filenames: List[str]) -> beam.pvalue.PCollection[Tuple[str, str]]:
  """Read in all individual lines for the given data sources.

  Args:
    p: beam pipeline object
    filenames: List of files to read from

  Returns:
    A PCollection[Tuple[filename, line]] of all the lines in the files keyed
    by filename
  """
  # PCollection[filename]
  pfilenames = (p | beam.Create(filenames))

  # PCollection[Tuple(filename, line)]
  lines = (
      pfilenames | "read files" >> beam.io.ReadAllFromText(with_filename=True))

  return lines


def _between_dates(filename: str,
                   start_date: Optional[datetime.date] = None,
                   end_date: Optional[datetime.date] = None) -> bool:
  """Return true if a filename is between (or matches either of) two dates.

  Args:
    filename: string of the format
    "gs://firehook-scans/http/CP_Quack-http-2020-05-11-01-02-08/results.json"
    start_date: date object or None
    end_date: date object or None

  Returns:
    boolean
  """
  date = datetime.date.fromisoformat(
      re.findall(r'\d\d\d\d-\d\d-\d\d', filename)[0])
  if start_date and end_date:
    return start_date <= date <= end_date
  if start_date:
    return start_date <= date
  if end_date:
    return date <= end_date
  return True


def _filename_matches(filepath: str, allowed_filenames: List[str]) -> bool:
  """Find if a filepath matches a list of filenames.

  Args:
    filepath, zipped or unzipped ex: path/results.js, path/results.json.gz
    allowed_filenames: List of filenames, all unzipped
      ex: [resolvers.json, blockpages.json]

  Returns:
    boolean, whether the filepath matches one of the list.
    Zipped matches to unzipped names count.
  """
  filename = pathlib.PurePosixPath(filepath).name

  if '.gz' in pathlib.PurePosixPath(filename).suffixes:
    filename = pathlib.PurePosixPath(filename).stem

  return filename in allowed_filenames


def _get_partition_params() -> Dict[str, Any]:
  """Returns additional partitioning params to pass with the bigquery load.

  Args:
    scan_type: data type, one of 'echo', 'discard', 'http', 'https',
      'satellite' or 'page_fetch'

  Returns: A dict of query params, See:
  https://beam.apache.org/releases/pydoc/2.14.0/apache_beam.io.gcp.bigquery.html#additional-parameters-for-bigquery-tables
  https://cloud.google.com/bigquery/docs/reference/rest/v2/tables#resource:-table
  """
  partition_params = {
      'timePartitioning': {
          'type': 'DAY',
          'field': 'date'
      },
      'clustering': {
          'fields': ['country', 'asn']
      }
  }
  return partition_params


def get_job_name(table_name: str, incremental_load: bool) -> str:
  """Creates the job name for the beam pipeline.

  Pipelines with the same name cannot run simultaneously.

  Args:
    table_name: a dataset.table name like 'base.scan_echo'
    incremental_load: boolean. whether the job is incremental.

  Returns:
    A string like 'write-base-scan-echo'
  """
  # no underscores or periods are allowed in beam job names
  fixed_table_name = table_name.replace('_', '-').replace('.', '-')
  fixed_table_name = fixed_table_name.replace('/', '-')
  if incremental_load:
    return 'append-' + fixed_table_name
  return 'write-' + fixed_table_name


def get_table_name(dataset_name: str, scan_type: str,
                   base_table_name: str) -> str:
  """Construct a bigquery table name.

  Args:
    dataset_name: dataset name like 'base' or 'laplante'
    scan_type: data type, one of 'echo', 'discard', 'http', 'https'
    base_table_name: table name like 'scan'

  Returns:
    a dataset.table name like 'base.echo_scan'
  """
  return f'{dataset_name}.{scan_type}_{base_table_name}'


def get_gcs_folder(dataset_name: str, bucket_name: str) -> str:
  """Construct a gcs folder name.

  Args:
    dataset_name: dataset name like 'base' or 'laplante'
    bucket_name: bucket name like 'firehook-test'

  Returns:
    a GCS folder like 'gs://firehook-test/avirkud'
  """
  return f'gs://{bucket_name}/{dataset_name}'


def _raise_exception_if_zero(num: int) -> None:
  if num == 0:
    raise Exception("Zero rows were created even though there were new files.")


def _raise_error_if_collection_empty(
    rows: beam.pvalue.PCollection[BigqueryRow]) -> beam.pvalue.PCollection:
  count_collection = (
      rows | "Count" >> beam.combiners.Count.Globally() |
      "Error if empty" >> beam.Map(_raise_exception_if_zero))
  return count_collection


class ScanDataBeamPipelineRunner():
  """A runner to collect cloud values and run a corrosponding beam pipeline."""

  def __init__(self, project: str, bucket: str, staging_location: str,
               temp_location: str,
               metadata_chooser_factory: IpMetadataChooserFactory) -> None:
    """Initialize a pipeline runner.

    Args:
      project: google cluod project name
      bucket: gcs bucket name
      staging_location: gcs bucket name, used for staging beam data
      temp_location: gcs bucket name, used for temp beam data
      metadata_chooser: factory to create a metadata chooser
    """
    self.project = project
    self.bucket = bucket
    self.staging_location = staging_location
    self.temp_location = temp_location
    self.metadata_chooser_factory = metadata_chooser_factory

  def _get_full_table_name(self, table_name: str) -> str:
    """Get a full project:dataset.table name.

    Args:
      table_name: a dataset.table name

    Returns:
      project:dataset.table name
    """
    return self.project + ':' + table_name

  def _data_to_load(self,
                    gcs: GCSFileSystem,
                    scan_type: str,
                    incremental_load: bool,
                    destination: str,
                    start_date: Optional[datetime.date] = None,
                    end_date: Optional[datetime.date] = None) -> List[str]:
    """Select the right files to read.

    Args:
      gcs: GCSFileSystem object
      scan_type: one of 'echo', 'discard', 'http', 'https', 'satellite'
      incremental_load: boolean. If true, only read the latest new data
      table_name: dataset.table name like 'base.scan_echo'
      start_date: date object, only files after or at this date will be read
      end_date: date object, only files at or before this date will be read

    Returns:
      A List of filename strings. ex
       ['gs://firehook-scans/echo/CP_Quack-echo-2020-08-22-06-08-03/results.json',
        'gs://firehook-scans/echo/CP_Quack-echo-2020-08-23-06-01-02/results.json']
    """
    if incremental_load:
      if not destination.startswith('gs://'):
        destination = self._get_full_table_name(destination)
      existing_sources = _get_existing_datasources(destination, self.project)
    else:
      existing_sources = []

    if scan_type == schema.SCAN_TYPE_SATELLITE:
      files_to_load = satellite.SATELLITE_FILES
    else:
      files_to_load = SCAN_FILES

    # Filepath like `gs://firehook-scans/echo/**/*'
    files_regex = f'{self.bucket}{scan_type}/**/*'
    file_metadata = [m.metadata_list for m in gcs.match([files_regex])][0]

    filepaths = [metadata.path for metadata in file_metadata]
    file_sizes = [metadata.size_in_bytes for metadata in file_metadata]

    filtered_filenames = [
        filepath for (filepath, file_size) in zip(filepaths, file_sizes)
        if (_between_dates(filepath, start_date, end_date) and
            _filename_matches(filepath, files_to_load) and
            flatten_base.source_from_filename(filepath) not in existing_sources
            and file_size > EMPTY_GZIPPED_FILE_SIZE)
    ]

    return filtered_filenames

  def _add_metadata(
      self, rows: beam.pvalue.PCollection[BigqueryRow]
  ) -> beam.pvalue.PCollection[BigqueryRow]:
    """Add ip metadata to a collection of roundtrip rows.

    Args:
      rows: beam.PCollection[BigqueryRow]

    Returns:
      PCollection[BigqueryRow]
      The same rows as above with with additional metadata columns added.
    """

    # PCollection[Tuple[DateIpKey,BigqueryRow]]
    rows_keyed_by_ip_and_date = (
        rows | 'key by ips and dates' >>
        beam.Map(lambda row: (make_date_ip_key(row), row)).with_output_types(
            Tuple[DateIpKey, BigqueryRow]))

    # PCollection[DateIpKey]
    # pylint: disable=no-value-for-parameter
    ips_and_dates = (
        rows_keyed_by_ip_and_date | 'get ip and date keys per row' >>
        beam.Keys().with_output_types(DateIpKey))

    # PCollection[DateIpKey]
    deduped_ips_and_dates = (
        # pylint: disable=no-value-for-parameter
        ips_and_dates | 'dedup' >> beam.Distinct().with_output_types(DateIpKey))

    # PCollection[Tuple[date,List[ip]]]
    grouped_ips_by_dates = (
        deduped_ips_and_dates | 'group by date' >>
        beam.GroupByKey().with_output_types(Tuple[str, Iterable[str]]))

    # PCollection[Tuple[DateIpKey,IpMetadataWithKeys]]
    ips_with_metadata = (
        grouped_ips_by_dates | 'get ip metadata' >> beam.FlatMapTuple(
            self._add_ip_metadata).with_output_types(Tuple[DateIpKey,
                                                           IpMetadataWithKeys]))

    # PCollection[Tuple[Tuple[date,ip],Dict[input_name_key,List[BigqueryRow|IpMetadataWithKeys]]]]
    grouped_metadata_and_rows = (({
        IP_METADATA_PCOLLECTION_NAME: ips_with_metadata,
        ROWS_PCOLLECION_NAME: rows_keyed_by_ip_and_date
    }) | 'group by keys' >> beam.CoGroupByKey())

    # PCollection[BigqueryRow]
    rows_with_metadata = (
        grouped_metadata_and_rows |
        'merge metadata with rows' >> beam.FlatMapTuple(
            merge_metadata_with_rows).with_output_types(BigqueryRow))

    return rows_with_metadata

  def _add_ip_metadata(
      self, date: str,
      ips: List[str]) -> Iterator[Tuple[DateIpKey, IpMetadataWithKeys]]:
    """Add Autonymous System metadata for ips in the given rows.

    Args:
      date: a 'YYYY-MM-DD' date key
      ips: a list of ips

    Yields:
      Tuples (DateIpKey, IpMetadataWithKeys)
    """
    ip_metadata_chooser = self.metadata_chooser_factory.make_chooser(
        datetime.date.fromisoformat(date))

    for ip in ips:
      metadata_key = (date, ip)
      metadata_values = ip_metadata_chooser.get_metadata(ip)

      yield (metadata_key, metadata_values)

  def _write_to_bigquery(self, scan_type: str,
                         rows: beam.pvalue.PCollection[BigqueryRow],
                         table_name: str, incremental_load: bool) -> None:
    """Write out row to a bigquery table.

    Args:
      scan_type: one of 'echo', 'discard', 'http', 'https',
        or 'satellite'
      rows: PCollection[BigqueryRow] of data to write.
      table_name: dataset.table name like 'base.echo_scan' Determines which
        tables to write to.
      incremental_load: boolean. If true, only load the latest new data, if
        false reload all data.

    Raises:
      Exception: if any arguments are invalid.
    """
    bq_schema = schema.get_beam_bigquery_schema(
        schema.get_bigquery_schema(scan_type))

    if incremental_load:
      write_mode = beam.io.BigQueryDisposition.WRITE_APPEND
    else:
      write_mode = beam.io.BigQueryDisposition.WRITE_TRUNCATE

    # Pcollection[Dict[str, Any]]
    dict_rows = (
        rows | f'dataclass to dicts {scan_type}' >> beam.Map(
            schema.flatten_for_bigquery).with_output_types(Dict[str, Any]))

    (dict_rows | f'Write {scan_type}' >> beam.io.WriteToBigQuery(  # pylint: disable=expression-not-assigned
        self._get_full_table_name(table_name),
        schema=bq_schema,
        create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
        write_disposition=write_mode,
        additional_bq_parameters=_get_partition_params()))

  def _write_to_gcs(  # pylint: disable=no-self-use
      self, scan_type: str, rows: beam.pvalue.PCollection[BigqueryRow],
      gcs_folder: str) -> None:
    """Write out rows to GCS folder with hive-partioned format.

    Rows are written to .json.gz files organized by scan_type, source, and country:
    '{gcs_folder}/{scan_type}/source={source}/country={country}/results.json.gz'

    Args:
      scan_type: one of 'echo', 'discard', 'http', 'https',
        or 'satellite'
      rows: PCollection[BigqueryRow] of data to write.
      gcs_folder: GCS folder gs://bucket/folder/... like
        'gs://firehook-test/scans' to write output files to.

    Raises:
      Exception: if any arguments are invalid.
    """

    def get_destination(record: str) -> str:
      """Returns the hive-format dest folder for a measurement record str."""
      record_dict = json.loads(record)
      return f'{scan_type}/source={record_dict["source"]}/country={record_dict["country"]}/results'

    def custom_file_naming(suffix: str = None) -> Callable:
      """Returns custom function to name destination files."""

      # Beam requires the returned function to have the following arguments,
      # see beam.io.filename.destination_prefix_naming.
      def _inner(window: Any, pane: Any, shard_index: Optional[int],
                 total_shards: Optional[int], compression: Optional[str],
                 destination: Optional[str]) -> str:
        # Get the filename with the destination_prefix_naming format:
        # '{prefix}-{start}-{end}-{pane}-{shard:05d}-of-{total_shards:05d}{suffix}{compression}'
        # where prefix = destination.
        filename = beam.io.fileio.destination_prefix_naming(suffix)(
            window, pane, shard_index, total_shards, compression, destination)
        # Remove shard component from filename if there is only one shard.
        filename = filename.replace('-00000-of-00001', '')
        logging.info('%s %s %s %s', destination, shard_index, total_shards,
                     filename)
        return filename

      return _inner

    # Pcollection[Dict[str, Any]]
    dict_rows = (
        rows | f'dataclass to dicts {scan_type}' >> beam.Map(
            schema.flatten_for_bigquery).with_output_types(Dict[str, Any]))

    # PCollection[str]
    json_rows = (
        dict_rows |
        'dicts to json' >> beam.Map(json.dumps).with_output_types(str))

    # Set shards=1 and max_writers_per_bundle=0 to avoid sharding output.
    (json_rows | 'Write to GCS files' >> WriteToFiles(  # pylint: disable=expression-not-assigned
        path=gcs_folder,
        destination=get_destination,
        sink=lambda dest: JsonGzSink(),
        shards=1,
        max_writers_per_bundle=0,
        file_naming=custom_file_naming(suffix='.json.gz')))

  def _get_pipeline_options(self, scan_type: str,
                            job_name: str) -> PipelineOptions:
    """Sets up pipeline options for a beam pipeline.

    Args:
      scan_type: one of 'echo', 'discard', 'http', 'https'
      job_name: a name for the dataflow job

    Returns:
      PipelineOptions
    """
    pipeline_options = PipelineOptions(
        runner='DataflowRunner',
        project=self.project,
        region=SCAN_TYPES_TO_ZONES[scan_type],
        staging_location=self.staging_location,
        temp_location=self.temp_location,
        job_name=job_name,
        runtime_type_check=False,  # slow in prod
        experiments=[
            'enable_execution_details_collection',
            'use_monitoring_state_manager'
        ],
        setup_file='./pipeline/setup.py')
    pipeline_options.view_as(SetupOptions).save_main_session = True
    return pipeline_options

  def run_beam_pipeline(self, scan_type: str, incremental_load: bool,
                        job_name: str, destination: str,
                        start_date: Optional[datetime.date],
                        end_date: Optional[datetime.date],
                        export_gcs: bool) -> None:
    """Run a single apache beam pipeline to load json data into bigquery.

    Args:
      scan_type: one of 'echo', 'discard', 'http', 'https' or 'satellite'
      incremental_load: boolean. If true, only load the latest new data, if
        false reload all data.
      job_name: string name for this pipeline job.
      destination: either dataset.table name like 'base.scan_echo' if writing to
        BigQuery, or gs://bucket/folder/... name like 'gs://firehook-test/scans'
        if writing to Google Cloud Storage.
      start_date: date object, only files after or at this date will be read.
        Mostly only used during development.
      end_date: date object, only files at or before this date will be read.
        Mostly only used during development.
      export_gcs: boolean. If true, write to Google Cloud Storage, if false
        write to BigQuery.

    Raises:
      Exception: if any arguments are invalid or the pipeline fails.
    """
    logging.getLogger().setLevel(logging.INFO)

    is_gcs_destination = destination.startswith('gs://')
    if export_gcs and not is_gcs_destination:
      raise Exception('Destination must start with gs:// when writing to GCS.')
    if not export_gcs and is_gcs_destination:
      raise Exception(
          'Destination must be a dataset.table name when writing to BigQuery.')

    pipeline_options = self._get_pipeline_options(scan_type, job_name)
    gcs = GCSFileSystem(pipeline_options)

    new_filenames = self._data_to_load(gcs, scan_type, incremental_load,
                                       destination, start_date, end_date)
    if not new_filenames:
      logging.info('No new files to load')
      return

    with beam.Pipeline(options=pipeline_options) as p:
      # PCollection[Tuple[filename,line]]
      lines = _read_scan_text(p, new_filenames)

      if scan_type == schema.SCAN_TYPE_SATELLITE:
        # PCollection[SatelliteRow]
        rows = satellite.process_satellite_lines(lines)

      else:  # Hyperquack scans
        # PCollection[HyperquackRow]
        rows = (
            lines | 'flatten json' >> beam.ParDo(
                flatten.FlattenMeasurement()).with_output_types(BigqueryRow))

      # PCollection[HyperquackRow|SatelliteRow]
      rows_with_metadata = self._add_metadata(rows)

      _raise_error_if_collection_empty(rows_with_metadata)

      if export_gcs:
        self._write_to_gcs(scan_type, rows_with_metadata, destination)
      else:
        self._write_to_bigquery(scan_type, rows_with_metadata, destination,
                                incremental_load)
