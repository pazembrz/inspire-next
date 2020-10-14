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

from __future__ import absolute_import, division, print_function

from test._mock_backport import MagicMock, patch

import pytest

from inspire_schemas.readers import LiteratureReader
from invenio_workflows import ObjectStatus, workflow_object_class
from invenio_records.api import RecordMetadata

from inspirehep.modules.workflows.tasks.manual_merging import save_roots, store_records
from inspirehep.modules.workflows.utils import (
    insert_wf_record_source,
    read_wf_record_source,
)
from inspirehep.modules.records.api import InspireRecord
from inspirehep.modules.workflows.workflows.manual_merge import start_merger
from inspirehep.utils.record_getter import get_db_record, RecordGetterError

from calls import do_resolve_manual_merge_wf
from invenio_workflows.errors import WorkflowsError


def fake_record(title, rec_id):
    return {
        '$schema': 'http://localhost:5000/schemas/records/hep.json',
        'titles': [{'title': title}],
        '_collections': ['Literature'],
        'document_type': ['article'],
        'acquisition_source': {'source': 'arxiv'},
        'arxiv_eprints': [{'value': '1701.01431', 'categories': ['cs']}],
        'control_number': rec_id
    }


def test_manual_merge_existing_records(workflow_app):
    with patch.dict(workflow_app.config, {
        'FEATURE_FLAG_ENABLE_REST_RECORD_MANAGEMENT': False,
    }):
        json_head = fake_record('This is the HEAD', 1)
        json_update = fake_record('While this is the update', 2)

        # this two fields will create a merging conflict
        json_head['core'] = True
        json_update['core'] = False

        head = InspireRecord.create_or_update(json_head, skip_files=False)
        head.commit()
        update = InspireRecord.create_or_update(json_update, skip_files=False)
        update.commit()
        head_id = head.id
        update_id = update.id

        obj_id = start_merger(
            head_id=1,
            update_id=2,
            current_user_id=1,
        )

        do_resolve_manual_merge_wf(workflow_app, obj_id)

        # retrieve it again, otherwise Detached Instance Error
        obj = workflow_object_class.get(obj_id)

        assert obj.status == ObjectStatus.COMPLETED
        assert obj.extra_data['approved'] is True
        assert obj.extra_data['auto-approved'] is False

        # no root present before
        last_root = read_wf_record_source(head_id, 'arxiv')
        assert last_root is None

        update_source = LiteratureReader(update).source
        root_update = read_wf_record_source(update_id, update_source)
        assert root_update is None

        # check that head's content has been replaced by merged
        deleted_record = RecordMetadata.query.filter_by(id=update_id).one()

        latest_record = get_db_record('lit', 1)

        assert deleted_record.json['deleted'] is True

        # check deleted record is linked in the latest one
        deleted_rec_ref = {'$ref': 'http://localhost:5000/api/literature/2'}
        assert [deleted_rec_ref] == latest_record['deleted_records']

        # check the merged record is linked in the deleted one
        new_record_metadata = {'$ref': 'http://localhost:5000/api/literature/1'}
        assert new_record_metadata == deleted_record.json['new_record']

        del latest_record['deleted_records']
        assert latest_record == obj.data  # -> resulted merged record


def test_manual_merge_with_none_record(workflow_app):
    with patch.dict(workflow_app.config, {
        'FEATURE_FLAG_ENABLE_REST_RECORD_MANAGEMENT': False,
    }):
        json_head = fake_record('This is the HEAD', 1)

        head = InspireRecord.create_or_update(json_head, skip_files=False)
        head.commit()
        non_existing_id = 123456789

        with pytest.raises(RecordGetterError):
            start_merger(
                head_id=1,
                update_id=non_existing_id,
                current_user_id=1,
            )


def test_save_roots(workflow_app):
    with patch.dict(workflow_app.config, {
        'FEATURE_FLAG_ENABLE_REST_RECORD_MANAGEMENT': False,
    }):
        head = InspireRecord.create_or_update(fake_record('title1', 123), skip_files=False)
        head.commit()
        update = InspireRecord.create_or_update(fake_record('title2', 456), skip_files=False)
        update.commit()

        obj = workflow_object_class.create(
            data={},
            data_type='hep'
        )
        obj.extra_data['head_uuid'] = str(head.id)
        obj.extra_data['update_uuid'] = str(update.id)
        obj.save()

        # Union: keep the most recently created/updated root from each source.
        insert_wf_record_source(json={'version': 'original'}, record_uuid=head.id, source='arxiv')

        insert_wf_record_source(json={'version': 'updated'}, record_uuid=update.id, source='arxiv')

        insert_wf_record_source(json={'version': 'updated'}, record_uuid=update.id, source='publisher')

        save_roots(obj, None)

        arxiv_rec = read_wf_record_source(head.id, 'arxiv')
        assert arxiv_rec.json == {'version': 'updated'}

        pub_rec = read_wf_record_source(head.id, 'publisher')
        assert pub_rec.json == {'version': 'updated'}

        assert not read_wf_record_source(update.id, 'arxiv')
        assert not read_wf_record_source(update.id, 'publisher')


def test_store_record_inspirehep_api_manual_merge_accepts(workflow_app, requests_mock=None):
    json_head = fake_record('This is the HEAD', 1)
    json_update = fake_record('While this is the update', 2)

    # this two fields will create a merging conflict
    json_head['core'] = True
    json_update['core'] = False

    head = InspireRecord.create_or_update(json_head, skip_files=False)
    head.commit()
    update = InspireRecord.create_or_update(json_update, skip_files=False)
    update.commit()

    with patch.dict(workflow_app.config, {
        'FEATURE_FLAG_ENABLE_REST_RECORD_MANAGEMENT': True,
        'INSPIREHEP_URL': "http://web:8000"
    }):
        with requests_mock.Mocker() as requests_mocker:
            requests_mocker.register_uri(
                'POST', '{url}/authors'.format(
                    url=workflow_app.config.get("INSPIREHEP_URL")),
                headers={'content-type': 'application/json'},
                status_code=401,
                json={
                    "message": "Something"
                }
            )
            obj_id = start_merger(
                head_id=1,
                update_id=2,
                current_user_id=1,
            )

            do_resolve_manual_merge_wf(workflow_app, obj_id)
        # retrieve it again, otherwise Detached Instance Error
        obj = workflow_object_class.get(obj_id)

        assert obj.status == ObjectStatus.COMPLETED
        assert obj.extra_data['approved'] is True
        assert obj.extra_data['auto-approved'] is False


def test_store_record_inspirehep_api_manual_merge_error_code(workflow_app, requests_mock=None):
    json_head = fake_record('This is the HEAD', 1)
    json_update = fake_record('While this is the update', 2)

    # this two fields will create a merging conflict
    json_head['core'] = True
    json_update['core'] = False

    head = InspireRecord.create_or_update(json_head, skip_files=False)
    head.commit()
    update = InspireRecord.create_or_update(json_update, skip_files=False)
    update.commit()

    with patch.dict(workflow_app.config, {
        'FEATURE_FLAG_ENABLE_REST_RECORD_MANAGEMENT': True,
        'INSPIREHEP_URL': "http://web:8000"
    }):
        with requests_mock.Mocker() as requests_mocker:
            requests_mocker.register_uri(
                'POST', '{url}/authors'.format(
                    url=workflow_app.config.get("INSPIREHEP_URL")),
                headers={'content-type': 'application/json'},
                status_code=401,
                json={
                    "message": "Something"
                }
            )
            with pytest.raises(WorkflowsError):
                obj_id = start_merger(
                    head_id=1,
                    update_id=2,
                    current_user_id=1,
                )

                do_resolve_manual_merge_wf(workflow_app, obj_id)
                # retrieve it again, otherwise Detached Instance Error
    obj = workflow_object_class.get(obj_id)
    assert obj.status == ObjectStatus.ERROR
