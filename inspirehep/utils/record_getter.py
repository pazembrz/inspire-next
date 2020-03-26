# -*- coding: utf-8 -*-
#
# This file is part of INSPIRE.
# Copyright (C) 2014-2017 CERN.
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

"""Resource-aware json reference loaders to be used with jsonref."""

from __future__ import absolute_import, division, print_function

from functools import wraps

import requests
from flask import current_app
from sqlalchemy import tuple_
from werkzeug.utils import import_string

from inspire_dojson.utils import get_recid_from_ref
from inspire_utils.record import get_value
from invenio_pidstore.models import PersistentIdentifier
from invenio_records.models import RecordMetadata

from inspirehep.modules.pidstore.utils import get_endpoint_from_pid_type


class RecordGetterError(Exception):

    def __init__(self, message, cause):
        super(RecordGetterError, self).__init__(message)
        self.cause = cause

    def __repr__(self):
        return '{} caused by {}'.format(
            super(RecordGetterError, self).__repr__(),
            repr(self.cause)
        )

    def __str__(self):
        return repr(self)


def raise_record_getter_error_and_log(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            current_app.logger.exception("Can't load recid %s", args)
            raise RecordGetterError(str(e), e)

    return wrapper


@raise_record_getter_error_and_log
def get_es_record(pid_type, recid, **kwargs):
    pid = PersistentIdentifier.get(pid_type, recid)

    endpoint = get_endpoint_from_pid_type(pid_type)
    search_conf = current_app.config['RECORDS_REST_ENDPOINTS'][endpoint]
    search_class = import_string(search_conf['search_class'])()

    return search_class.get_source(pid.object_uuid, **kwargs)


def get_es_records(pid_type, recids, **kwargs):
    """Get a list of recids from ElasticSearch."""
    recids = [str(recid) for recid in recids]
    uuids = PersistentIdentifier.query.filter(
        PersistentIdentifier.pid_value.in_(recids),
        PersistentIdentifier.pid_type == pid_type
    ).all()
    uuids = [str(uuid.object_uuid) for uuid in uuids]

    endpoint = get_endpoint_from_pid_type(pid_type)
    search_conf = current_app.config['RECORDS_REST_ENDPOINTS'][endpoint]
    search_class = import_string(search_conf['search_class'])()

    return search_class.mget(uuids, **kwargs)


@raise_record_getter_error_and_log
def get_es_record_by_uuid(uuid):
    pid = PersistentIdentifier.query.filter_by(object_uuid=uuid).one()

    endpoint = get_endpoint_from_pid_type(pid.pid_type)
    search_conf = current_app.config['RECORDS_REST_ENDPOINTS'][endpoint]
    search_class = import_string(search_conf['search_class'])()

    return search_class.get_source(uuid)


@raise_record_getter_error_and_log
def get_record_from_hep(pid_type=None, recid=None, uuid=None):
    if (not pid_type and not recid) and not uuid:
        raise RecordGetterError("No record pid or uuid provided")

    from inspirehep.modules.records.api import InspireRecord
    headers = {
        "content-type": "application/json",
        "Authorization": "Bearer {token}".format(
            token=current_app.config['AUTHENTICATION_TOKEN']
        ),
    }
    inspirehep_url = current_app.config.get("INSPIREHEP_URL")
    if pid_type and recid:
        response = requests.get(
            "{inspirehep_url}{endpoint}/{control_number}".format(
                inspirehep_url=inspirehep_url,
                endpoint=current_app.config["PID_TYPES_TO_ENDPOINTS"][pid_type],
                control_number=recid
            ),
            headers=headers,
        )
    else:
        response = requests.get(
            "{inspirehep_url}by_uuid/{uuid}".format(
                inspirehep_url=inspirehep_url,
                uuid=uuid
            ),
            headers=headers,
        )

    if response.status_code == 200 and response.json():
        response_data = response.json()
        record_data = response_data.get('metadata')
        uuid = response_data['uuid']
        if not record_data:
            raise RecordGetterError("Record is empty", "No record")
        record = InspireRecord(record_data, uuid)
        return record
    else:
        raise RecordGetterError("Cannot download record from hep", response.reason or response.text)



@raise_record_getter_error_and_log
def get_db_record(pid_type, recid):
    from inspirehep.modules.records.api import InspireRecord
    pid = PersistentIdentifier.get(pid_type, recid)
    return InspireRecord.get_record(pid.object_uuid)


def get_db_records(pids):
    """Get an iterator on record metadata from the DB.

    Args:
        pids (Iterable[Tuple[str, Union[str, int]]): a list of (pid_type, pid_value) tuples.

    Yields:
        dict: metadata of a record found in the database.

    Warning:
        The order in which records are returned is different from the order of
        the input.
    """
    pids = [(pid_type, str(pid_value)) for (pid_type, pid_value) in pids]

    if not pids:
        return

    query = RecordMetadata.query.join(
        PersistentIdentifier, RecordMetadata.id == PersistentIdentifier.object_uuid
    ).filter(
        PersistentIdentifier.object_type == 'rec',  # So it can use the 'idx_object' index
        tuple_(PersistentIdentifier.pid_type, PersistentIdentifier.pid_value).in_(pids)
    )

    for record in query.yield_per(100):
        yield record.json


def get_conference_record(record, default=None):
    """Return the first Conference record associated with a record.

    Queries the database to fetch the first Conference record referenced
    in the ``publication_info`` of the record.

    Args:
        record(InspireRecord): a record.
        default: value to be returned if no conference record present/found

    Returns:
        InspireRecord: the first Conference record associated with the record.

    Examples:
    >>> record = {
    ...     'publication_info': [
    ...         {
    ...             'conference_record': {
    ...                 '$ref': '/api/conferences/972464',
    ...             },
    ...         },
    ...     ],
    ... }
    >>> conference_record = get_conference_record(record)
    >>> conference_record['control_number']
    972464

    """
    pub_info = get_value(record, 'publication_info.conference_record[0]')
    if not pub_info:
        return default

    conferences = get_db_records([('con', get_recid_from_ref(pub_info))])
    return list(conferences)[0]
