# -*- coding: utf-8 -*-
#
# This file is part of INSPIRE.
# Copyright (C) 2014-2018 CERN.
#
# INSPIRE is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# INSPIRE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with INSPIRE. If not, see <http://www.gnu.org/licenses/>.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization
# or submit itself to any jurisdiction.

from __future__ import absolute_import, division, print_function

from time import sleep

import click
import click_spinner
import csv
import json
import pprint

from os import path, makedirs
from datetime import datetime

from invenio_db import db
from invenio_files_rest.models import ObjectVersion
from invenio_pidstore.models import PersistentIdentifier, PIDStatus
from flask import current_app
from flask.cli import with_appcontext
from invenio_records_files.models import RecordsBuckets

from inspirehep.modules.records.utils import get_citations_from_es
from inspirehep.utils.record_getter import get_db_record, get_es_record, \
    RecordGetterError
from .checkers import check_unlinked_references
from .tasks import batch_reindex

from invenio_records.models import RecordMetadata

from sqlalchemy import (
    String,
    cast,
    type_coerce,
    or_,
    not_
)

from sqlalchemy.dialects.postgresql import JSONB


@click.group()
def check():
    """Commands to perform checks on records"""


@check.command()
@click.argument('doi_file_name', type=click.File('w', encoding='utf-8'), default='missing_cited_dois.txt')
@click.argument('arxiv_file_name', type=click.File('w', encoding='utf-8'), default='missing_cited_arxiv_eprints.txt')
@with_appcontext
def unlinked_references(doi_file_name, arxiv_file_name):
    """Find often cited literature that is not on INSPIRE.

    It generates two files with a list of DOI/arxiv ids respectively,
    in which each line has the respective identifier, folowed by two numbers,
    representing the amount of times the literature has been cited
    by a core and a non-core article respectively.
    The lists are ordered by an internal measure of relevance."""
    with click_spinner.spinner():
        click.echo('Looking up unlinked references...')
        result_doi, result_arxiv = check_unlinked_references()

    click.echo('Done!')
    click.echo(u'Output written to "{}" and "{}"'.format(doi_file_name.name, arxiv_file_name.name))

    for item in result_doi:
        doi_file_name.write(u'{i[0]}: {i[1]}\n'.format(i=item))

    for item in result_arxiv:
        arxiv_file_name.write(u'{i[0]}: {i[1]}\n'.format(i=item))


def next_batch(iterator, batch_size):
    """Get first batch_size elements from the iterable, or remaining if less.

    :param iterator: the iterator for the iterable
    :param batch_size: size of the requested batch
    :return: batch (list)
    """
    batch = []

    try:
        for idx in range(batch_size):
            batch.append(next(iterator))
    except StopIteration:
        pass

    return batch


@click.command()
@click.option('--yes-i-know', is_flag=True)
@click.option('-t', '--pid-type', multiple=True, required=True)
@click.option('-s', '--batch-size', default=200)
@click.option('-q', '--queue-name', default='indexer_task')
@click.option('-l', '--log-path', default='/tmp/inspire/')
@with_appcontext
def simpleindex(yes_i_know, pid_type, batch_size, queue_name, log_path):
    """Bulk reindex all records in a parallel manner.

    :param yes_i_know: if True, skip confirmation screen
    :param pid_type: array of PID types, allowed: lit, con, exp, jou, aut, job, ins
    :param batch_size: number of documents per batch sent to workers.
    :param queue_name: name of the celery queue
    """
    if not yes_i_know:
        click.confirm(
            'Do you really want to reindex the record?',
            abort=True,
        )

    click.secho('Sending record UUIDs to the indexing queue...', fg='green')

    query = (
        db.session.query(PersistentIdentifier.object_uuid).join(RecordMetadata, type_coerce(PersistentIdentifier.object_uuid, String) == type_coerce(RecordMetadata.id, String))
        .filter(
            PersistentIdentifier.pid_type.in_(pid_type),
            PersistentIdentifier.object_type == 'rec',
            PersistentIdentifier.status == PIDStatus.REGISTERED,
            or_(
                not_(
                    type_coerce(RecordMetadata.json, JSONB).has_key('deleted')
                ),
                RecordMetadata.json["deleted"] == cast(False, JSONB)
            )
            # noqa: F401
        )
    )

    request_timeout = current_app.config.get('INDEXER_BULK_REQUEST_TIMEOUT')
    all_tasks = []
    records_per_tasks = {}

    with click.progressbar(
        query.yield_per(2000),
        length=query.count(),
        label='Scheduling indexing tasks'
    ) as items:
        batch = next_batch(items, batch_size)

        while batch:
            uuids = [str(item[0]) for item in batch]
            indexer_task = batch_reindex.apply_async(
                kwargs={
                    'uuids': uuids,
                    'request_timeout': request_timeout,
                },
                queue=queue_name,
            )

            records_per_tasks[indexer_task.id] = uuids
            all_tasks.append(indexer_task)
            batch = next_batch(items, batch_size)

    click.secho('Created {} tasks.'.format(len(all_tasks)), fg='green')

    with click.progressbar(
        length=len(all_tasks),
        label='Indexing records'
    ) as progressbar:
        def _finished_tasks_count():
            return len(filter(lambda task: task.ready(), all_tasks))

        while len(all_tasks) != _finished_tasks_count():
            sleep(0.5)
            # this is so click doesn't divide by 0:
            progressbar.pos = _finished_tasks_count() or 1
            progressbar.update(0)

    failures = []
    successes = 0
    batch_errors = []

    for task in all_tasks:
        result = task.result
        if task.failed():
            batch_errors.append({
                'task_id': task.id,
                'error': result,
            })
        else:
            successes += result['success']
            failures += result['failures']

    color = 'red' if failures or batch_errors else 'green'
    click.secho(
        'Reindexing finished: {} failed, {} succeeded, additionally {} batches errored.'.format(
            len(failures),
            successes,
            len(batch_errors),
        ),
        fg=color,
    )

    failures_log_path = path.join(log_path, 'records_index_failures.log')
    errors_log_path = path.join(log_path, 'records_index_errors.log')
    _prepare_logdir(failures_log_path)
    _prepare_logdir(errors_log_path)

    if failures:
        failures_json = []
        for failure in failures:
            try:
                failures_json.append({
                    'id': failure['index']['_id'],
                    'error': failure['index']['error'],
                })
            except Exception:
                failures_json.append({
                    'error': repr(failure),
                })
        with open(failures_log_path, 'w') as log:
            json.dump(failures_json, log)

        click.secho('You can see the index failures in %s' % failures_log_path)

    if batch_errors:
        errors_json = []
        for error in batch_errors:
            task_id = error['task_id']
            failed_uuids = records_per_tasks[task_id]
            errors_json.append({
                'ids': failed_uuids,
                'error': repr(error['error']),
            })
        with open(errors_log_path, 'w') as log:
            json.dump(errors_json, log)

        click.secho('You can see the errors in %s' % errors_log_path)


@click.command()
@click.option('--remove-no-control-number', is_flag=True)
@click.option('--remove-duplicates', is_flag=True)
@click.option('--remove-not-in-pidstore', is_flag=True)
@click.option('-c', '--print-without-control-number', is_flag=True)
@click.option('-p', '--print-pid-not-in-pidstore', is_flag=True)
@click.option('-d', '--print-duplicates', is_flag=True)
@with_appcontext
def handle_duplicates(remove_no_control_number, remove_duplicates,
                      print_without_control_number, print_pid_not_in_pidstore,
                      print_duplicates, remove_not_in_pidstore):
    """Find duplicates and handle them properly"""
    query = RecordMetadata.query.with_entities(
            RecordMetadata.id,
            RecordMetadata.json['control_number']
    ).outerjoin(
        PersistentIdentifier,
        PersistentIdentifier.object_uuid == RecordMetadata.id
    ).filter(
        PersistentIdentifier.object_uuid == None  # noqa: E711
    )
    out = query.all()

    recs_no_control_number = []
    recs_no_in_pid_store = []
    others = []

    click.echo("Processing %s records:" % len(out))
    with click.progressbar(out) as data:
        for rec in data:
            cn = rec[1]
            if not cn:
                recs_no_control_number.append(rec)
            elif not PersistentIdentifier.query.filter(
                    PersistentIdentifier.pid_value == str(cn)).one_or_none():
                recs_no_in_pid_store.append(rec)
            else:
                others.append(rec)

    click.secho("Found %s records not in PID store" % len(out))
    click.secho("\t%s records without control number" % len(recs_no_control_number))
    click.secho("\t%s records with their PID not in pidstore" % (
        len(recs_no_in_pid_store)))
    click.secho("\t%s records which are duplicates of records in pid store" % (
        len(others)))

    if print_without_control_number:
        click.secho("Records which are missing control number:\n%s" % (
            pprint.pformat(recs_no_control_number)))
    if print_pid_not_in_pidstore:
        click.secho("Records missing in PID store:\n%s" % (
            pprint.pformat(recs_no_in_pid_store)))
    if print_duplicates:
        click.secho("Duplicates:\n%s" % (pprint.pformat(others)))

    if remove_no_control_number:
        click.secho("Removing records which do not have control number (%s)" % (
            len(recs_no_control_number)))
        removed_records, _, _ = _remove_records(recs_no_control_number)
        click.secho("Removed %s out of %s records which did not have." % (
            removed_records, len(recs_no_control_number)))

    if remove_not_in_pidstore:
        click.secho("Removing records which PID is not in PID store but they are no duplicates (%s)" % (
            len(recs_no_in_pid_store)))
        removed_records, _, _ = _remove_records(recs_no_in_pid_store)
        click.secho("Removed %s out of %s records which PID was missing from PID store." % (
            removed_records, len(recs_no_in_pid_store)))

    if remove_duplicates:
        click.secho("Removing records which looks to be duplicates (%s)" % (
            len(others)))
        removed_records, _, _ = _remove_records(others)
        click.secho("Removed %s out of %s records which looks to be duplicates." % (
            removed_records, len(others)))
    db.session.commit()


def _remove_records(records_ids):
    """ This method is only a helper for removal of records which are not in PID store.
        If you will use it for records which are in PID store it will fail as it not removes data from PID store itself.
    Args:
        records_ids: List of tuples with record.id and record.control_number

    Returns: Tuple with information how many records, buckets and objects was removed

    """
    records_ids = [str(r[0]) for r in records_ids]
    recs = RecordMetadata.query.filter(
        RecordMetadata.id.in_(records_ids)
    )
    recs_buckets = RecordsBuckets.query.filter(
        RecordsBuckets.record_id.in_(records_ids)
    )

    # as in_ is not working for relationships...
    buckets_ids = [str(bucket.bucket_id) for bucket in recs_buckets]
    objects = ObjectVersion.query.filter(
        ObjectVersion.bucket_id.in_(buckets_ids)
    )

    removed_objects = objects.delete(synchronize_session=False)
    removed_buckets = recs_buckets.delete(synchronize_session=False)
    removed_records = recs.delete(synchronize_session=False)

    return(removed_records, removed_buckets, removed_objects)


def _prepare_logdir(log_path):
    if not path.exists(path.dirname(log_path)):
        makedirs(path.dirname(log_path))


def _gen_query(query, page_start=1, page_end=-1, window_size=100):
    query = query.paginate(page_start, window_size)
    while query and (page_start <= page_end or page_end == -1):
        for item in query.items:
            yield item
        if query.has_next:
            query = query.next()
            page_start += 1
        else:
            query = None


@check.command()
@click.option('-o', '--data-output', default='/tmp/inspire/missing_records.txt')
@with_appcontext
def check_missing_records_in_es(data_output):
    """Checks if all not deleted records from pidstore are also in ElasticSearch"""
    all_records = int(PersistentIdentifier.query.filter(
        PersistentIdentifier.pid_type == 'lit').count())
    _prepare_logdir(data_output)
    click.echo("All missing records pids will be saved in %s file" % data_output)
    missing = 0
    _query = _gen_query(PersistentIdentifier.query.filter(
        PersistentIdentifier.pid_type == 'lit'))
    with click.progressbar(_query,
                           length=all_records,
                           label="Processing pids (%s pids)..." % all_records) as pidstore:
        with open(data_output, 'w') as data_file:
            for pid in pidstore:
                db_rec = get_db_record('lit', pid.pid_value)
                if db_rec.get('deleted'):
                    continue
                try:
                    get_es_record('lit', pid.pid_value)
                except RecordGetterError:
                    missing += 1
                    data_file.write("%s\n" % pid.pid_value)
                    data_file.flush()
    click.echo("%s records are missing from es" % missing)


@check.command()
@click.option('-s', '--from-page', default=1)
@click.option('-e', '--to-page', default='-1')
@click.option('-s', '--pagesize', default=100)
@click.option('-o', '--data-output', default='/tmp/inspire/db_benchmark.csv')
@with_appcontext
def benchmark_citations(from_page, to_page, pagesize, data_output):
    """Process all records from db and logs its time of getting data from db
    and of counting citations."""
    click.echo("All benchmark data will be saved in %s csv file" % data_output)
    processed_record_counter = 0
    tmp_data = []
    all_recs = int(PersistentIdentifier.query.filter(PersistentIdentifier.pid_type == 'lit').count())
    all_pages = int(all_recs / pagesize) + 1
    if to_page > -1 and to_page < all_pages:
        all_recs = (all_pages - (all_pages - to_page)) * pagesize
    if from_page > 1:
        all_recs -= int((from_page - 1) * pagesize)
    with click.progressbar(length=all_recs,
                           label="Benchmarking db (%s records)..." % all_recs) as bar:
        with open(data_output, 'w') as data_file:
            keys = ['pid', 'get_record_time', 'get_citations_count_time',
                    'citations_count']
            out = csv.DictWriter(data_file, keys)
            out.writeheader()
            _query = _gen_query(
                PersistentIdentifier.query.filter(PersistentIdentifier.pid_type == 'lit'),
                from_page,
                to_page,
                pagesize
            )
            for pid in _query:
                get_db_record_start = datetime.now()
                rec = get_db_record('lit', pid.pid_value)
                get_db_record_time = (datetime.now() - get_db_record_start).total_seconds()

                get_cits_count_start = datetime.now()
                cits_count = rec.get_citations_count()
                get_cits_count_time = (datetime.now() - get_cits_count_start).total_seconds()
                data = {'pid': pid.pid_value,
                        'get_record_time': get_db_record_time,
                        'get_citations_count_time': get_cits_count_time,
                        'citations_count': int(cits_count)
                        }
                tmp_data.append(data)
                bar.update(1)
                processed_record_counter += 1
                if processed_record_counter % 100 == 0:
                    # Save data every 100 records to file.
                    out.writerows(tmp_data)
                    tmp_data = []
                    data_file.flush()
            if tmp_data:
                out.writerows(tmp_data)


@check.command()
@click.option('-s', '--from-page', default=1)
@click.option('-e', '--to-page', default='-1')
@click.option('-s', '--pagesize', default=100)
@click.option('-o', '--output', default='/tmp/inspire/citations_inconsistencies.txt')
@with_appcontext
def find_citations_inconsistencies(from_page, to_page, pagesize, output):
    """Process all non deleted records and check if citation in ES
    are the same like in DB"""
    ok = 0
    fail = 0
    no_cits = 0
    deleted = 0

    all_recs = int(PersistentIdentifier.query.filter(
        PersistentIdentifier.pid_type == 'lit').count())
    all_pages = int(all_recs / pagesize) + 1
    if -1 < to_page < all_pages:
        all_recs = (all_pages - (all_pages - to_page)) * pagesize
    if from_page > 1:
        all_recs -= int((from_page - 1) * pagesize)
    with click.progressbar(length=all_recs,
                           label="Processing %s records..." % all_recs) as bar:
        _prepare_logdir(output)
        with open(output, 'w') as data_file:
            keys = ['pid_value', 'db_citations_count',
                    'es_citations_count', 'es_citations_field']
            out_data = csv.DictWriter(data_file, keys)
            out_data.writeheader()
            _query = _gen_query(
                PersistentIdentifier.query.filter(PersistentIdentifier.pid_type == 'lit'),
                from_page,
                to_page,
                pagesize
            )
            for pid in _query:
                rec = get_db_record('lit', pid.pid_value)
                if rec.get('deleted'):
                    ok += 1
                    deleted += 1
                    continue

                try:
                    es_cits = get_citations_from_es(rec).total
                    es_citation_count_field = get_es_record('lit', pid.pid_value).get('citation_count')
                    db_cits = rec.get_citations_count()
                except Exception as err:
                    click.echo("Cannot prepare data for %s record. %s", pid.pid_value, err)
                    fail += 1
                    continue

                if es_cits == db_cits == es_citation_count_field:
                    if es_cits == 0:
                        no_cits += 1
                    ok += 1
                else:
                    fail += 1
                    data = {'pid_value': pid.pid_value,
                            'db_citations_count': db_cits,
                            'es_citations_count': es_cits,
                            'es_citations_field': es_citation_count_field}
                    out_data.writerow(data)
                    data_file.flush()
                bar.update(1)
            output_msg = "\nProcessed {all_recs} records. {ok} were ok, {failed}"\
                         " had difference between db an es ctations count!"\
                         "\n{no_citations} records had no citations"\
                         " at all.\n{deleted} records"\
                         " were deleted\n".format(all_recs=all_recs,
                                                ok=ok, failed=fail,
                                                no_citations=no_cits,
                                                deleted=deleted)
            click.echo(output_msg)
            click.echo("Additional statistics for incosistent records"\
                       "was saved in %s file" % output)
