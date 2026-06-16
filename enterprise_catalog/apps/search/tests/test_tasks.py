"""
Tests for the Phase 3 incremental Algolia indexing batch tasks.
"""
from dataclasses import asdict
from datetime import timedelta
from unittest import mock
from uuid import uuid4

import ddt
from algoliasearch.exceptions import AlgoliaException
from django.test import TestCase
from django.test.utils import override_settings

from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    COURSE_RUN,
    LEARNER_PATHWAY,
    PROGRAM,
    VIDEO,
)
from enterprise_catalog.apps.catalog.models import EnterpriseCatalog
from enterprise_catalog.apps.catalog.tests.factories import (
    CatalogQueryFactory,
    ContentMetadataFactory,
)
from enterprise_catalog.apps.catalog.utils import localized_utcnow
from enterprise_catalog.apps.search import tasks as search_tasks
from enterprise_catalog.apps.search.indexing_mappings import IndexingMappings
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
from enterprise_catalog.apps.search.tasks import (
    BatchSummary,
    IndexingDecision,
    RecordOutcome,
    _build_objects_by_content_key,
    _chunked,
    _extract_program_keys,
    _finalize_decision,
    _get_catalog_membership_by_key,
    _get_content_modified_by_key,
    _get_indexable_keys_by_content_type,
    _get_indexing_state_by_key,
    _get_video_pks_for_dispatch,
    _group_aggregation_keys_by_content_type,
    _has_newer_child_index,
    _index_content_batch,
    _is_uuid_string,
    _should_retry_failed_record,
    dispatch_algolia_indexing,
    index_courses_batch_in_algolia,
    index_pathways_batch_in_algolia,
    index_programs_batch_in_algolia,
)
from enterprise_catalog.apps.search.tests.factories import (
    ContentMetadataIndexingStateFactory,
)
from enterprise_catalog.apps.video_catalog.tests.factories import VideoFactory


def _algolia_object(content_key, content_type=COURSE, shard_index=0):
    """
    Build a minimal Algolia object payload — enough fields for the batch task
    to do its work (objectID, aggregation_key) without pulling in the legacy
    generator's full output schema. ``aggregation_key`` is set to the
    ``"{content_type}:{content_key}"`` form the legacy generator actually emits.
    """
    return {
        'objectID': f'{content_key}-catalog-query-uuids-{shard_index}',
        'aggregation_key': f'{content_type}:{content_key}',
    }


def _program_content_key():
    """
    Build a UUID-formatted content_key for program records.
    """
    return str(uuid4())


@ddt.ddt
class TestIndexContentBatch(TestCase):
    """
    Tests for ``_index_content_batch`` and the three thin task wrappers
    that delegate to it.
    """

    def setUp(self):
        # Patch the legacy object generator + the algolia client + the cached
        # mappings at the module-import seam used by ``search.tasks``. Tests
        # configure return values per scenario.
        self.algolia_client = mock.MagicMock(name='algolia_client')
        self.algolia_client.get_object_ids_for_aggregation_key.return_value = []
        client_patcher = mock.patch.object(
            search_tasks, 'get_initialized_algolia_client', return_value=self.algolia_client,
        )
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

        self.mock_get_products = mock.patch.object(
            search_tasks, '_get_algolia_products_for_batch',
        ).start()
        self.addCleanup(mock.patch.stopall)

        self.mock_get_mappings = mock.patch.object(
            search_tasks, 'get_indexing_mappings',
        ).start()

        # Sensible default: every key the test creates is indexable.
        self.mock_get_mappings.return_value = IndexingMappings(
            program_to_course_keys={},
            pathway_to_program_course_keys={},
            all_indexable_content_keys=set(),
        )
        self.mock_get_products.return_value = []

    def _set_indexable(self, *content_keys):
        self.mock_get_mappings.return_value = IndexingMappings(
            program_to_course_keys={},
            pathway_to_program_course_keys={},
            all_indexable_content_keys=set(content_keys),
        )

    # --- Happy path ----------------------------------------------------

    def test_happy_path_indexes_all_records(self):
        """
        Three indexable, never-indexed courses → all three indexed via a
        single bulk ``save_objects_batch`` call carrying every shard, then
        each row's ``mark_as_indexed`` runs in pass 3.
        """
        c1 = ContentMetadataFactory(content_type=COURSE, content_key='course-A')
        c2 = ContentMetadataFactory(content_type=COURSE, content_key='course-B')
        c3 = ContentMetadataFactory(content_type=COURSE, content_key='course-C')
        self._set_indexable(c1.content_key, c2.content_key, c3.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(c1.content_key),
            _algolia_object(c2.content_key),
            _algolia_object(c3.content_key),
        ]

        result = _index_content_batch(
            [c1.content_key, c2.content_key, c3.content_key], COURSE,
        )

        self.assertEqual(result.indexed, 3)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.failed, 0)
        # One bulk save across all three records, not one per record.
        self.assertEqual(self.algolia_client.save_objects_batch.call_count, 1)
        bulk_objects = self.algolia_client.save_objects_batch.call_args.args[0]
        self.assertEqual(
            {obj['objectID'] for obj in bulk_objects},
            {f'{c.content_key}-catalog-query-uuids-0' for c in (c1, c2, c3)},
        )
        for content in (c1, c2, c3):
            state = ContentMetadataIndexingState.objects.get(content_metadata=content)
            self.assertIsNotNone(state.last_indexed_at)
            self.assertEqual(
                state.algolia_object_ids,
                [f'{content.content_key}-catalog-query-uuids-0'],
            )

    # --- Skip path -----------------------------------------------------

    def test_skip_when_already_indexed_at_current_version(self):
        """
        ``force=False`` and ``state.last_indexed_at >= content.modified`` → skipped,
        no Algolia calls for that record.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-skip')
        # Indexed in the future relative to content.modified.
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=content.modified + timedelta(seconds=60),
        )
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [_algolia_object(content.content_key)]

        result = _index_content_batch([content.content_key], COURSE, force=False)

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.indexed, 0)
        self.algolia_client.save_objects_batch.assert_not_called()

    def test_force_true_bypasses_skip(self):
        """
        Same scenario as skip, but with ``force=True`` → indexed anyway.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-force')
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=content.modified + timedelta(seconds=60),
        )
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [_algolia_object(content.content_key)]

        result = _index_content_batch([content.content_key], COURSE, force=True)

        self.assertEqual(result.indexed, 1)
        self.algolia_client.save_objects_batch.assert_called_once()

    def test_failed_records_are_retried_even_when_current(self):
        """
        A record with a fresh ``last_indexed_at`` but a recorded failure should
        be reprocessed so catalog-query retries can unstick it.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-retry-failed')
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=content.modified + timedelta(seconds=60),
            last_failure_at=localized_utcnow(),
        )
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [_algolia_object(content.content_key)]

        result = _index_content_batch([content.content_key], COURSE, force=False)

        self.assertEqual(result.indexed, 1)
        self.algolia_client.save_objects_batch.assert_called_once()

    # --- Remove path ---------------------------------------------------

    def test_now_nonindexable_removes_existing_shards(self):
        """
        Content was previously indexed (has stored ``algolia_object_ids``) but is
        no longer in ``all_indexable_content_keys`` → REMOVE path.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-gone')
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=localized_utcnow(),
            algolia_object_ids=['course-gone-catalog-query-uuids-0'],
        )
        # NOT in indexable set:
        self._set_indexable()  # empty
        self.mock_get_products.return_value = []

        result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.removed, 1)
        self.algolia_client.delete_objects_batch.assert_called_once_with(
            ['course-gone-catalog-query-uuids-0'], index_name=None,
        )
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.removed_from_index_at)

    def test_nonindexable_with_no_prior_shards_skips_delete_call(self):
        """
        Now-nonindexable record with no stored shards → still marks removed,
        but does not call delete_objects_batch (nothing to delete).
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-virgin')
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            algolia_object_ids=[],
        )
        self._set_indexable()  # empty
        self.mock_get_products.return_value = []

        result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.removed, 1)
        self.algolia_client.delete_objects_batch.assert_not_called()
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.removed_from_index_at)

    def test_indexable_with_zero_objects_routes_to_removed(self):
        """
        Drift case: ``content_key`` is in ``all_indexable_content_keys`` per
        the partition fn, but the legacy generator emits zero shards (e.g.
        every catalog membership was dropped without ContentMetadata.modified
        advancing). The task treats this as REMOVED — it deletes any stale
        shards from Algolia and stamps ``removed_from_index_at`` rather than
        marking INDEXED with an empty shard list (which would mis-report the
        row as currently indexed).
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-membership-dropped')
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
            algolia_object_ids=[f'{content.content_key}-catalog-query-uuids-0'],
        )
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = []  # generator emits nothing

        result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.removed, 1)
        self.assertEqual(result.indexed, 0)
        self.algolia_client.save_objects_batch.assert_not_called()
        # State row tracks the existing shard, so no browse is needed.
        self.algolia_client.get_object_ids_for_aggregation_key.assert_not_called()
        self.algolia_client.delete_objects_batch.assert_called_once_with(
            [f'{content.content_key}-catalog-query-uuids-0'], index_name=None,
        )
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.removed_from_index_at)

    def test_indexable_with_zero_objects_and_no_existing_shards_skips_delete(self):
        """
        Same drift case as above but Algolia has no existing shards either —
        still routes to REMOVED, but does not call ``delete_objects_batch``.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-never-indexed')
        self._set_indexable(content.content_key)
        self.algolia_client.get_object_ids_for_aggregation_key.return_value = []
        self.mock_get_products.return_value = []

        result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.removed, 1)
        self.algolia_client.delete_objects_batch.assert_not_called()
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.removed_from_index_at)

    # --- Orphan handling -----------------------------------------------

    def test_orphan_shards_get_deleted(self):
        """
        Existing shard IDs are read from the state row; new generation has
        fewer shards → the missing one is deleted as an orphan. Pins that
        the INDEXED path does NOT issue a browse call when the state row
        already tracks the shard IDs.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-shrink')
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
            algolia_object_ids=[
                f'{content.content_key}-catalog-query-uuids-0',
                f'{content.content_key}-catalog-query-uuids-1',
                f'{content.content_key}-catalog-query-uuids-2',
            ],
        )
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(content.content_key, shard_index=0),
            _algolia_object(content.content_key, shard_index=1),
        ]

        result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.indexed, 1)
        self.algolia_client.get_object_ids_for_aggregation_key.assert_not_called()
        self.algolia_client.delete_objects_batch.assert_called_once()
        deleted = self.algolia_client.delete_objects_batch.call_args.args[0]
        self.assertEqual(set(deleted), {f'{content.content_key}-catalog-query-uuids-2'})

    def test_indexed_path_browses_when_state_has_no_object_ids(self):
        """
        First-time index for a content (state row exists but
        ``algolia_object_ids`` is empty) → the INDEXED path falls back to a
        browse so any pre-existing shards (e.g. from the legacy reindexer)
        are still detected and orphaned correctly.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-virgin-index')
        # State row exists with no recorded shards (default for a fresh row).
        self._set_indexable(content.content_key)
        self.algolia_client.get_object_ids_for_aggregation_key.return_value = [
            f'{content.content_key}-legacy-shard-0',
        ]
        self.mock_get_products.return_value = [
            _algolia_object(content.content_key, shard_index=0),
        ]

        result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.indexed, 1)
        self.algolia_client.get_object_ids_for_aggregation_key.assert_called_once()
        # The legacy shard should be deleted as an orphan since it isn't in
        # the new generation.
        self.algolia_client.delete_objects_batch.assert_called_once()
        deleted = self.algolia_client.delete_objects_batch.call_args.args[0]
        self.assertEqual(set(deleted), {f'{content.content_key}-legacy-shard-0'})

    # --- Failure handling ----------------------------------------------

    def test_bulk_save_failure_falls_back_to_per_record_and_isolates_one_failure(self):
        """
        Bulk save raises ``AlgoliaException`` → the task falls back to
        per-record save calls. The bad record's per-record retry still
        raises; the two good ones succeed. Final state: 2 INDEXED, 1 FAILED.
        """
        c1 = ContentMetadataFactory(content_type=COURSE, content_key='course-ok-1')
        c2 = ContentMetadataFactory(content_type=COURSE, content_key='course-bad')
        c3 = ContentMetadataFactory(content_type=COURSE, content_key='course-ok-2')
        self._set_indexable(c1.content_key, c2.content_key, c3.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(c1.content_key),
            _algolia_object(c2.content_key),
            _algolia_object(c3.content_key),
        ]

        # First call (bulk across all 3 records) raises; subsequent per-record
        # calls only raise for the bad key.
        bulk_call_seen = {'count': 0}

        def save_side_effect(objects, index_name=None):  # pylint: disable=unused-argument
            bulk_call_seen['count'] += 1
            if bulk_call_seen['count'] == 1:
                raise AlgoliaException('bulk boom')
            if any(obj['aggregation_key'] == f'course:{c2.content_key}' for obj in objects):
                raise AlgoliaException('per-record boom')

        self.algolia_client.save_objects_batch.side_effect = save_side_effect

        result = _index_content_batch(
            [c1.content_key, c2.content_key, c3.content_key], COURSE,
        )

        # 1 bulk + 3 per-record retries = 4 calls
        self.assertEqual(self.algolia_client.save_objects_batch.call_count, 4)
        self.assertEqual(result.indexed, 2)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, [c2.content_key])
        bad_state = ContentMetadataIndexingState.objects.get(content_metadata=c2)
        self.assertIsNotNone(bad_state.last_failure_at)
        self.assertIn('per-record boom', bad_state.failure_reason)
        # Good records were still marked indexed via the per-record fallback.
        for good_content in (c1, c3):
            good_state = ContentMetadataIndexingState.objects.get(content_metadata=good_content)
            self.assertIsNotNone(good_state.last_indexed_at)

    def test_bulk_save_succeeds_no_fallback(self):
        """
        Bulk save succeeds → no per-record fallback calls; one ``save_objects_batch``
        call total.
        """
        c1 = ContentMetadataFactory(content_type=COURSE, content_key='course-A')
        c2 = ContentMetadataFactory(content_type=COURSE, content_key='course-B')
        self._set_indexable(c1.content_key, c2.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(c1.content_key),
            _algolia_object(c2.content_key),
        ]

        _index_content_batch([c1.content_key, c2.content_key], COURSE)

        self.assertEqual(self.algolia_client.save_objects_batch.call_count, 1)

    def test_per_record_save_failure_skips_orphan_delete_for_that_record(self):
        """
        When a record's bulk-save fallback fails, that record's orphan delete
        is skipped (its old shards stay in place as the partial-failure
        fallback). Other records' orphans are still bulk-deleted.
        """
        # Two records, both have existing shards being shrunk to one new shard.
        c_bad = ContentMetadataFactory(content_type=COURSE, content_key='course-save-fail')
        c_good = ContentMetadataFactory(content_type=COURSE, content_key='course-save-ok')
        ContentMetadataIndexingStateFactory(
            content_metadata=c_bad,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
            algolia_object_ids=[
                f'{c_bad.content_key}-catalog-query-uuids-0',
                f'{c_bad.content_key}-catalog-query-uuids-1',
            ],
        )
        ContentMetadataIndexingStateFactory(
            content_metadata=c_good,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
            algolia_object_ids=[
                f'{c_good.content_key}-catalog-query-uuids-0',
                f'{c_good.content_key}-catalog-query-uuids-1',
            ],
        )
        self._set_indexable(c_bad.content_key, c_good.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(c_bad.content_key, shard_index=0),
            _algolia_object(c_good.content_key, shard_index=0),
        ]

        # Bulk save raises, then bad record's per-record retry also raises.
        save_calls = {'count': 0}

        def save_side_effect(objects, index_name=None):  # pylint: disable=unused-argument
            save_calls['count'] += 1
            if save_calls['count'] == 1:
                raise AlgoliaException('bulk boom')
            if any(obj['aggregation_key'] == f'course:{c_bad.content_key}' for obj in objects):
                raise AlgoliaException('per-record boom')

        self.algolia_client.save_objects_batch.side_effect = save_side_effect

        result = _index_content_batch(
            [c_bad.content_key, c_good.content_key], COURSE,
        )

        self.assertEqual(result.indexed, 1)
        self.assertEqual(result.failed, 1)
        # Exactly one bulk delete call, carrying only the good record's orphan.
        self.assertEqual(self.algolia_client.delete_objects_batch.call_count, 1)
        deleted = self.algolia_client.delete_objects_batch.call_args.args[0]
        self.assertEqual(set(deleted), {f'{c_good.content_key}-catalog-query-uuids-1'})

    def test_bulk_delete_failure_falls_back_to_per_record(self):
        """
        Bulk delete raises → per-record delete fallback runs. A record whose
        delete fails individually is marked FAILED even though its save
        succeeded.
        """
        c1 = ContentMetadataFactory(content_type=COURSE, content_key='course-delete-bad')
        c2 = ContentMetadataFactory(content_type=COURSE, content_key='course-delete-ok')
        ContentMetadataIndexingStateFactory(
            content_metadata=c1,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
            algolia_object_ids=[
                f'{c1.content_key}-catalog-query-uuids-0',
                f'{c1.content_key}-catalog-query-uuids-1',
            ],
        )
        ContentMetadataIndexingStateFactory(
            content_metadata=c2,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
            algolia_object_ids=[
                f'{c2.content_key}-catalog-query-uuids-0',
                f'{c2.content_key}-catalog-query-uuids-1',
            ],
        )
        self._set_indexable(c1.content_key, c2.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(c1.content_key, shard_index=0),
            _algolia_object(c2.content_key, shard_index=0),
        ]

        delete_calls = {'count': 0}

        def delete_side_effect(ids, index_name=None):  # pylint: disable=unused-argument
            delete_calls['count'] += 1
            if delete_calls['count'] == 1:
                raise AlgoliaException('bulk delete boom')
            if f'{c1.content_key}-catalog-query-uuids-1' in ids:
                raise AlgoliaException('per-record delete boom')

        self.algolia_client.delete_objects_batch.side_effect = delete_side_effect

        result = _index_content_batch(
            [c1.content_key, c2.content_key], COURSE,
        )

        # 1 bulk + 2 per-record retries
        self.assertEqual(self.algolia_client.delete_objects_batch.call_count, 3)
        self.assertEqual(result.indexed, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, [c1.content_key])
        bad_state = ContentMetadataIndexingState.objects.get(content_metadata=c1)
        self.assertIsNotNone(bad_state.last_failure_at)

    def test_missing_content_metadata_counts_as_failed(self):
        """
        content_key passed to the task but no ContentMetadata exists for it
        (e.g. deleted between dispatch and execution) → counted as failed,
        no exception bubbles up.
        """
        result = _index_content_batch(['course-missing'], COURSE)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, ['course-missing'])

    def test_unexpected_exception_in_resolve_decision_counts_as_failed(self):
        """
        If an unexpected exception escapes inside ``_resolve_indexing_decision``
        (e.g. a DB error from ``get_or_create_for_content``), the record is
        counted as failed and the rest of the batch is unaffected. This covers
        the broad-except guard in ``_resolve_indexing_decision``.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-exploding-state')
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [_algolia_object(content.content_key)]

        with mock.patch.object(
            ContentMetadataIndexingState,
            'get_or_create_for_content',
            side_effect=RuntimeError('unexpected db error'),
        ):
            result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, [content.content_key])

    def test_indexed_record_orphan_delete_failure_mutates_outcome_to_failed(self):
        """
        A single record's save succeeds but its orphan delete fails (both
        bulk and per-record retry). The decision is mutated from INDEXED to
        FAILED, the state row carries the failure stamp, and the
        already-written new shards stay in Algolia (the next run will detect
        them via the state row's old IDs and clean up the orphan).

        Pinned in isolation rather than only via the multi-record bulk-delete
        test, so the INDEXED-to-FAILED mutation contract is explicit.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-orphan-fail')
        ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
            algolia_object_ids=[
                f'{content.content_key}-catalog-query-uuids-0',
                f'{content.content_key}-catalog-query-uuids-1',  # becomes the orphan
            ],
        )
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(content.content_key, shard_index=0),
        ]

        # Save succeeds; every delete attempt (bulk + per-record retry) fails.
        self.algolia_client.delete_objects_batch.side_effect = AlgoliaException('delete boom')

        result = _index_content_batch([content.content_key], COURSE)

        self.assertEqual(result.indexed, 0)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, [content.content_key])
        # 1 bulk save (succeeded), no per-record save retry (no save failure).
        self.algolia_client.save_objects_batch.assert_called_once()
        # 1 bulk delete (raised) + 1 per-record retry (also raised).
        self.assertEqual(self.algolia_client.delete_objects_batch.call_count, 2)
        # State row reflects the failure stamp; mark_as_indexed was not called.
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.last_failure_at)
        self.assertIn('delete boom', state.failure_reason)

    def test_finalize_step_failure_isolated_to_offending_record(self):
        """
        If pass 3's state-row update raises (e.g. DB hiccup in
        ``mark_as_indexed``), the failure is recorded in the batch summary
        and the rest of the batch still finalizes. Pinned because pass 3 is
        the only loop where a single record's exception can fan out and abort
        siblings if it's not wrapped per-iteration.

        We do NOT try to recover by also calling ``mark_as_failed`` here —
        that path could itself raise. The next run sees ``last_indexed_at``
        unchanged on the offending record and re-indexes idempotently.
        """
        c_ok = ContentMetadataFactory(content_type=COURSE, content_key='course-ok')
        c_explode = ContentMetadataFactory(content_type=COURSE, content_key='course-explode')
        self._set_indexable(c_ok.content_key, c_explode.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(c_ok.content_key),
            _algolia_object(c_explode.content_key),
        ]

        original_mark_as_indexed = ContentMetadataIndexingState.mark_as_indexed

        def mark_as_indexed_side_effect(self, *args, **kwargs):
            if self.content_metadata.content_key == c_explode.content_key:
                raise RuntimeError('db boom')
            return original_mark_as_indexed(self, *args, **kwargs)

        with mock.patch.object(
            ContentMetadataIndexingState,
            'mark_as_indexed',
            autospec=True,
            side_effect=mark_as_indexed_side_effect,
        ):
            result = _index_content_batch(
                [c_ok.content_key, c_explode.content_key], COURSE,
            )

        self.assertEqual(result.indexed, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, [c_explode.content_key])
        # Sibling still finalized cleanly.
        ok_state = ContentMetadataIndexingState.objects.get(content_metadata=c_ok)
        self.assertIsNotNone(ok_state.last_indexed_at)
        # Offending record's state row stays in its pre-finalize state — the
        # next run re-indexes idempotently.
        explode_state = ContentMetadataIndexingState.objects.get(content_metadata=c_explode)
        self.assertIsNone(explode_state.last_indexed_at)

    # --- Plumbing ------------------------------------------------------

    def test_index_name_threaded_through_to_client(self):
        """
        ``index_name`` is forwarded to every Algolia client call.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-v2')
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [_algolia_object(content.content_key)]

        _index_content_batch([content.content_key], COURSE, index_name='enterprise_catalog_v2')

        self.algolia_client.get_object_ids_for_aggregation_key.assert_called_with(
            f'course:{content.content_key}', index_name='enterprise_catalog_v2',
        )
        self.algolia_client.save_objects_batch.assert_called_with(
            mock.ANY, index_name='enterprise_catalog_v2',
        )

    def test_empty_content_keys_returns_zeroed_counts(self):
        """
        No content_keys → no DB or Algolia work; result is all zeroes.
        """
        result = _index_content_batch([], COURSE)
        self.assertEqual(result.indexed, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.failed, 0)
        self.mock_get_products.assert_not_called()
        self.algolia_client.save_objects_batch.assert_not_called()

    # --- Task wrappers --------------------------------------------------

    @ddt.data(
        ('course', index_courses_batch_in_algolia, COURSE),
        ('program', index_programs_batch_in_algolia, PROGRAM),
        ('pathway', index_pathways_batch_in_algolia, LEARNER_PATHWAY),
    )
    @ddt.unpack
    def test_task_wrappers_delegate_with_correct_content_type(self, _label, task, content_type):
        """
        Each ``index_<type>_batch_in_algolia`` task delegates to
        ``_index_content_batch`` with the right ``content_type``.
        """
        content = ContentMetadataFactory(content_type=content_type, content_key=f'{_label}-key')
        self._set_indexable(content.content_key)
        self.mock_get_products.return_value = [
            _algolia_object(content.content_key, content_type=content_type),
        ]

        result = task([content.content_key])

        self.assertEqual(result['content_type'], content_type)
        self.assertEqual(result['indexed'], 1)


@override_settings(ALGOLIA_INDEXING_BATCH_SIZE=2)
@ddt.ddt
class TestDispatchAlgoliaIndexing(TestCase):
    """
    Tests for the Phase 4a dispatcher task.
    """
    # pylint: disable=no-value-for-parameter

    def setUp(self):
        self.mock_get_mappings = mock.patch.object(
            search_tasks, 'get_indexing_mappings',
        ).start()
        self.mock_invalidate_cache = mock.patch.object(
            search_tasks, 'invalidate_indexing_mappings_cache',
        ).start()
        self.mock_course_si = mock.patch.object(
            search_tasks.index_courses_batch_in_algolia, 'si',
        ).start()
        self.mock_program_si = mock.patch.object(
            search_tasks.index_programs_batch_in_algolia, 'si',
        ).start()
        self.mock_pathway_si = mock.patch.object(
            search_tasks.index_pathways_batch_in_algolia, 'si',
        ).start()
        self.mock_video_si = mock.patch.object(
            search_tasks.index_videos_batch_in_algolia, 'si',
        ).start()
        # Prevent actual Celery chain/group dispatch
        self.mock_group = mock.patch.object(search_tasks, 'group').start()
        self.mock_chain = mock.patch.object(search_tasks, 'chain').start()
        self.addCleanup(mock.patch.stopall)

    def _set_mappings(
        self,
        *,
        all_indexable_content_keys,
        program_to_course_keys=None,
        pathway_to_program_course_keys=None,
    ):
        self.mock_get_mappings.return_value = IndexingMappings(
            program_to_course_keys=program_to_course_keys or {},
            pathway_to_program_course_keys=pathway_to_program_course_keys or {},
            all_indexable_content_keys=set(all_indexable_content_keys),
        )

    def test_dry_run_returns_summary_without_dispatching(self):
        course = ContentMetadataFactory(content_type=COURSE, content_key='course-dry-run')
        program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='pathway-dry-run')
        self._set_mappings(
            all_indexable_content_keys=[course.content_key, program.content_key, pathway.content_key],
        )

        result = dispatch_algolia_indexing(force=True, dry_run=True, index_name='enterprise_catalog_v2')

        self.assertEqual(result, {
            'force': True,
            'dry_run': True,
            'batch_size': 2,
            'index_name': 'enterprise_catalog_v2',
            'dispatched': {
                COURSE: {'records': 1, 'batches': 1},
                PROGRAM: {'records': 1, 'batches': 1},
                LEARNER_PATHWAY: {'records': 1, 'batches': 1},
                VIDEO: {'records': 0, 'batches': 0},
            },
        })
        self.mock_invalidate_cache.assert_called_once_with()
        self.mock_course_si.assert_not_called()
        self.mock_program_si.assert_not_called()
        self.mock_pathway_si.assert_not_called()

    def test_dry_run_with_video_records_does_not_dispatch_video_tasks(self):
        """
        dry_run=True with video records in the DB reports the correct summary
        but never calls index_videos_batch_in_algolia.si(). Covers the
        ``if not dry_run:`` False branch in _build_ordered_groups for videos.
        """
        course = ContentMetadataFactory(content_type=COURSE, content_key='course-video-dryrun')
        course_run = ContentMetadataFactory(
            content_type=COURSE_RUN,
            parent_content_key=course.content_key,
        )
        VideoFactory(parent_content_metadata=course_run)
        self._set_mappings(all_indexable_content_keys=[course.content_key])

        result = dispatch_algolia_indexing(force=True, dry_run=True)

        self.assertEqual(result['dispatched'][VIDEO]['records'], 1)
        self.assertEqual(result['dispatched'][VIDEO]['batches'], 1)
        self.mock_video_si.assert_not_called()

    def test_force_false_does_not_invalidate_cache(self):
        course = ContentMetadataFactory(content_type=COURSE, content_key='course-no-force')
        self._set_mappings(all_indexable_content_keys=[course.content_key])

        dispatch_algolia_indexing(force=False)

        self.mock_invalidate_cache.assert_not_called()

    def test_courses_dispatch_never_indexed_stale_and_failed_records(self):
        never_indexed = ContentMetadataFactory(content_type=COURSE, content_key='course-never-indexed')
        stale = ContentMetadataFactory(content_type=COURSE, content_key='course-stale')
        current = ContentMetadataFactory(content_type=COURSE, content_key='course-current')
        failed = ContentMetadataFactory(content_type=COURSE, content_key='course-failed')
        ContentMetadataIndexingStateFactory(
            content_metadata=stale,
            last_indexed_at=stale.modified - timedelta(hours=1),
        )
        ContentMetadataIndexingStateFactory(
            content_metadata=current,
            last_indexed_at=current.modified + timedelta(hours=1),
        )
        ContentMetadataIndexingStateFactory(
            content_metadata=failed,
            last_indexed_at=failed.modified + timedelta(hours=1),
            last_failure_at=localized_utcnow(),
        )
        self._set_mappings(
            all_indexable_content_keys=[
                never_indexed.content_key,
                stale.content_key,
                current.content_key,
                failed.content_key,
            ],
        )

        result = dispatch_algolia_indexing(force=False, include_failed=True)

        self.assertEqual(result['dispatched'][COURSE], {'records': 3, 'batches': 2})
        self.assertEqual(self.mock_course_si.call_count, 2)
        dispatched_batches = [
            call.kwargs['content_keys']
            for call in self.mock_course_si.call_args_list
        ]
        self.assertEqual(
            dispatched_batches,
            [
                ['course-failed', 'course-never-indexed'],
                ['course-stale'],
            ],
        )

    def test_failed_courses_can_be_excluded_from_retry(self):
        failed = ContentMetadataFactory(content_type=COURSE, content_key='course-failed-no-retry')
        ContentMetadataIndexingStateFactory(
            content_metadata=failed,
            last_indexed_at=failed.modified + timedelta(hours=1),
            last_failure_at=localized_utcnow(),
        )
        self._set_mappings(all_indexable_content_keys=[failed.content_key])

        result = dispatch_algolia_indexing(force=False, include_failed=False)

        self.assertEqual(result['dispatched'][COURSE], {'records': 0, 'batches': 0})
        self.mock_course_si.assert_not_called()

    def test_programs_dispatch_when_child_course_indexed_more_recently(self):
        child_course = ContentMetadataFactory(content_type=COURSE, content_key='course-program-child')
        program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        program_state = ContentMetadataIndexingStateFactory(
            content_metadata=program,
            last_indexed_at=localized_utcnow() - timedelta(hours=2),
        )
        ContentMetadataIndexingStateFactory(
            content_metadata=child_course,
            last_indexed_at=program_state.last_indexed_at + timedelta(hours=1),
        )
        self._set_mappings(
            all_indexable_content_keys=[child_course.content_key, program.content_key],
            program_to_course_keys={program.content_key: {child_course.content_key}},
        )

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'][PROGRAM], {'records': 1, 'batches': 1})
        self.mock_program_si.assert_called_once_with(
            content_keys=[program.content_key],
            force=False,
            index_name=None,
        )

    def test_pathways_dispatch_when_child_program_is_newer(self):
        child_program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='pathway-stale')
        pathway_state = ContentMetadataIndexingStateFactory(
            content_metadata=pathway,
            last_indexed_at=localized_utcnow() - timedelta(hours=2),
        )
        ContentMetadataIndexingStateFactory(
            content_metadata=child_program,
            last_indexed_at=pathway_state.last_indexed_at + timedelta(hours=1),
        )
        self._set_mappings(
            all_indexable_content_keys=[child_program.content_key, pathway.content_key],
            pathway_to_program_course_keys={
                pathway.content_key: {'course-not-a-uuid', child_program.content_key},
            },
        )

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'][LEARNER_PATHWAY], {'records': 1, 'batches': 1})
        self.mock_pathway_si.assert_called_once_with(
            content_keys=[pathway.content_key],
            force=False,
            index_name=None,
        )

    def test_programs_dispatched_on_child_staleness_when_own_metadata_current(self):
        """
        A program whose own metadata is current (last_indexed_at > modified) is
        still dispatched when a child course was indexed more recently than the
        program itself. This exercises the child-staleness branch in
        ``_get_program_keys_for_dispatch`` that is bypassed when
        ``modified > last_indexed_at`` fires first.
        """
        child_course = ContentMetadataFactory(content_type=COURSE, content_key='course-newer-child')
        program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        # Program's own metadata is fresh — last_indexed_at is ahead of modified.
        program_state = ContentMetadataIndexingStateFactory(
            content_metadata=program,
            last_indexed_at=program.modified + timedelta(hours=1),
        )
        # Child course was indexed after the program.
        ContentMetadataIndexingStateFactory(
            content_metadata=child_course,
            last_indexed_at=program_state.last_indexed_at + timedelta(hours=1),
        )
        self._set_mappings(
            all_indexable_content_keys=[child_course.content_key, program.content_key],
            program_to_course_keys={program.content_key: {child_course.content_key}},
        )

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'][PROGRAM], {'records': 1, 'batches': 1})
        self.mock_program_si.assert_called_once()

    def test_pathways_dispatched_on_child_staleness_when_own_metadata_current(self):
        """
        A pathway whose own metadata is current (last_indexed_at > modified) is
        still dispatched when a child program was indexed more recently. This
        exercises the child-staleness branch in ``_get_pathway_keys_for_dispatch``
        that is bypassed when ``modified > last_indexed_at`` fires first.
        """
        child_program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='pathway-fresh-own')
        # Pathway's own metadata is fresh.
        pathway_state = ContentMetadataIndexingStateFactory(
            content_metadata=pathway,
            last_indexed_at=pathway.modified + timedelta(hours=1),
        )
        # Child program was indexed after the pathway.
        ContentMetadataIndexingStateFactory(
            content_metadata=child_program,
            last_indexed_at=pathway_state.last_indexed_at + timedelta(hours=1),
        )
        self._set_mappings(
            all_indexable_content_keys=[child_program.content_key, pathway.content_key],
            pathway_to_program_course_keys={
                pathway.content_key: {child_program.content_key},
            },
        )

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'][LEARNER_PATHWAY], {'records': 1, 'batches': 1})
        self.mock_pathway_si.assert_called_once()

    def test_batching_uses_configured_batch_size(self):
        courses = [
            ContentMetadataFactory(content_type=COURSE, content_key=f'course-batch-{index}')
            for index in range(5)
        ]
        self._set_mappings(
            all_indexable_content_keys=[course.content_key for course in courses],
        )

        result = dispatch_algolia_indexing(force=True)

        self.assertEqual(result['dispatched'][COURSE], {'records': 5, 'batches': 3})
        self.assertEqual(self.mock_course_si.call_count, 3)
        self.assertEqual(
            [call.kwargs['content_keys'] for call in self.mock_course_si.call_args_list],
            [
                ['course-batch-0', 'course-batch-1'],
                ['course-batch-2', 'course-batch-3'],
                ['course-batch-4'],
            ],
        )

    def test_empty_mappings_dispatch_no_records(self):
        """
        Empty indexable mappings produce zero dispatches for all content types.
        """
        self._set_mappings(all_indexable_content_keys=[])

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'], {
            COURSE: {'records': 0, 'batches': 0},
            PROGRAM: {'records': 0, 'batches': 0},
            LEARNER_PATHWAY: {'records': 0, 'batches': 0},
            VIDEO: {'records': 0, 'batches': 0},
        })
        self.mock_course_si.assert_not_called()
        self.mock_program_si.assert_not_called()
        self.mock_pathway_si.assert_not_called()

    def test_programs_dispatch_when_never_indexed(self):
        """
        Programs with no indexing state are always dispatched.
        """
        child_course = ContentMetadataFactory(
            content_type=COURSE, content_key='course-program-child-never-indexed',
        )
        program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        self._set_mappings(
            all_indexable_content_keys=[child_course.content_key, program.content_key],
            program_to_course_keys={program.content_key: {child_course.content_key}},
        )

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'][PROGRAM], {'records': 1, 'batches': 1})
        self.mock_program_si.assert_called_once_with(
            content_keys=[program.content_key],
            force=False,
            index_name=None,
        )

    def test_programs_dispatch_failed_records_when_include_failed_true(self):
        """
        Failed program records are retried when ``include_failed=True``.
        """
        child_course = ContentMetadataFactory(
            content_type=COURSE, content_key='course-program-child-with-failed-parent',
        )
        program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        ContentMetadataIndexingStateFactory(
            content_metadata=program,
            last_indexed_at=localized_utcnow(),
            last_failure_at=localized_utcnow(),
        )
        self._set_mappings(
            all_indexable_content_keys=[child_course.content_key, program.content_key],
            program_to_course_keys={program.content_key: {child_course.content_key}},
        )

        result = dispatch_algolia_indexing(force=False, include_failed=True)

        self.assertEqual(result['dispatched'][PROGRAM], {'records': 1, 'batches': 1})
        self.mock_program_si.assert_called_once_with(
            content_keys=[program.content_key],
            force=False,
            index_name=None,
        )

    def test_pathways_dispatch_when_never_indexed(self):
        """
        Pathways with no indexing state are always dispatched.
        """
        child_program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='pathway-never-indexed')
        self._set_mappings(
            all_indexable_content_keys=[child_program.content_key, pathway.content_key],
            pathway_to_program_course_keys={pathway.content_key: {child_program.content_key}},
        )

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'][LEARNER_PATHWAY], {'records': 1, 'batches': 1})
        self.mock_pathway_si.assert_called_once_with(
            content_keys=[pathway.content_key],
            force=False,
            index_name=None,
        )

    def test_pathways_dispatch_failed_records_when_include_failed_true(self):
        """
        Failed pathway records are retried when ``include_failed=True``.
        """
        child_program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='pathway-failed')
        ContentMetadataIndexingStateFactory(
            content_metadata=pathway,
            last_indexed_at=localized_utcnow(),
            last_failure_at=localized_utcnow(),
        )
        self._set_mappings(
            all_indexable_content_keys=[child_program.content_key, pathway.content_key],
            pathway_to_program_course_keys={pathway.content_key: {child_program.content_key}},
        )

        result = dispatch_algolia_indexing(force=False, include_failed=True)

        self.assertEqual(result['dispatched'][LEARNER_PATHWAY], {'records': 1, 'batches': 1})
        self.mock_pathway_si.assert_called_once_with(
            content_keys=[pathway.content_key],
            force=False,
            index_name=None,
        )

    def test_pathways_not_dispatched_when_child_keys_are_non_uuid_courses_only(self):
        """
        Pathways with only non-UUID child keys are not dispatched as child updates.
        """
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='pathway-courses-only')
        ContentMetadataIndexingStateFactory(
            content_metadata=pathway,
            last_indexed_at=localized_utcnow(),
        )
        self._set_mappings(
            all_indexable_content_keys=[pathway.content_key],
            pathway_to_program_course_keys={
                pathway.content_key: {'course-v1:edX+DemoX+2024', 'course-v1:edX+TestX+2024'},
            },
        )

        result = dispatch_algolia_indexing(force=False)

        self.assertEqual(result['dispatched'][LEARNER_PATHWAY], {'records': 0, 'batches': 0})
        self.mock_pathway_si.assert_not_called()

    # ------------------------------------------------------------------
    # content_types filtering
    # ------------------------------------------------------------------

    def test_content_types_unknown_value_raises(self):
        """Passing an unrecognised content type raises ValueError immediately."""
        with self.assertRaises(ValueError, msg='bogus_type'):
            dispatch_algolia_indexing(content_types=['bogus_type'])

    # Each tuple: (content_types, exp_courses, exp_programs, exp_pathways)
    # exp_* == expected record count AND expected .si() call count (1 record per type, batch_size=2)
    @ddt.data(
        (None, 1, 1, 1),
        ([COURSE], 1, 0, 0),
        ([PROGRAM], 0, 1, 0),
        ([LEARNER_PATHWAY], 0, 0, 1),
        ([COURSE, PROGRAM], 1, 1, 0),
    )
    @ddt.unpack
    def test_content_types_filter(self, content_types, exp_courses, exp_programs, exp_pathways):
        """
        content_types restricts which content type batches are dispatched.
        """
        course = ContentMetadataFactory(content_type=COURSE, content_key='ct-course')
        program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='ct-pathway')
        self._set_mappings(
            all_indexable_content_keys=[course.content_key, program.content_key, pathway.content_key],
            pathway_to_program_course_keys={pathway.content_key: {program.content_key}},
        )

        result = dispatch_algolia_indexing(force=True, content_types=content_types)

        self.assertEqual(result['dispatched'][COURSE]['records'], exp_courses)
        self.assertEqual(result['dispatched'][PROGRAM]['records'], exp_programs)
        self.assertEqual(result['dispatched'][LEARNER_PATHWAY]['records'], exp_pathways)
        self.assertEqual(self.mock_course_si.call_count, exp_courses)
        self.assertEqual(self.mock_program_si.call_count, exp_programs)
        self.assertEqual(self.mock_pathway_si.call_count, exp_pathways)

    def test_content_types_dry_run_filters_summary_without_dispatching(self):
        """
        dry_run=True with content_types set reports zero for excluded types and never calls .si().
        """
        course = ContentMetadataFactory(content_type=COURSE, content_key='ct-dry-course')
        program = ContentMetadataFactory(content_type=PROGRAM, content_key=_program_content_key())
        pathway = ContentMetadataFactory(content_type=LEARNER_PATHWAY, content_key='ct-dry-pathway')
        self._set_mappings(
            all_indexable_content_keys=[course.content_key, program.content_key, pathway.content_key],
        )

        result = dispatch_algolia_indexing(force=True, dry_run=True, content_types=[COURSE])

        self.assertEqual(result['dispatched'][COURSE], {'records': 1, 'batches': 1})
        self.assertEqual(result['dispatched'][PROGRAM], {'records': 0, 'batches': 0})
        self.assertEqual(result['dispatched'][LEARNER_PATHWAY], {'records': 0, 'batches': 0})
        self.mock_course_si.assert_not_called()
        self.mock_program_si.assert_not_called()
        self.mock_pathway_si.assert_not_called()

    # ------------------------------------------------------------------
    # video dispatch wiring
    # ------------------------------------------------------------------

    def test_videos_dispatched_as_fourth_group_on_force(self):
        """
        ``force=True`` dispatches every Video as a fourth group after pathways,
        keyed by ``edx_video_id`` (not content_key), regardless of whether the
        parent course is otherwise stale. Pins the ``_build_ordered_groups``
        video-group wiring at the dispatcher level.
        """
        course = ContentMetadataFactory(content_type=COURSE, content_key='course-with-video')
        course_run = ContentMetadataFactory(
            content_type=COURSE_RUN, parent_content_key=course.content_key,
        )
        video = VideoFactory(parent_content_metadata=course_run)
        self._set_mappings(all_indexable_content_keys=[course.content_key])

        result = dispatch_algolia_indexing(force=True)

        self.assertEqual(result['dispatched'][VIDEO], {'records': 1, 'batches': 1})
        self.mock_video_si.assert_called_once_with(
            video_pks=[video.edx_video_id],
            index_name=None,
        )

    def test_video_only_run_still_derives_videos_from_stale_courses(self):
        """
        A ``content_types=['video']`` run with ``force=False`` must still
        compute course staleness and dispatch the videos whose parent course is
        stale — even though COURSE itself is filtered out of the dispatch set.

        This pins the reason the ``content_types`` filter is applied *after*
        ``_get_keys_to_dispatch_by_type``: video staleness piggybacks on the
        full stale-course set, so filtering COURSE out beforehand would starve
        the video derivation and dispatch nothing.
        """
        # A never-indexed (therefore stale) course, its run, and a video.
        course = ContentMetadataFactory(content_type=COURSE, content_key='course-stale-for-video')
        course_run = ContentMetadataFactory(
            content_type=COURSE_RUN, parent_content_key=course.content_key,
        )
        video = VideoFactory(parent_content_metadata=course_run)
        self._set_mappings(all_indexable_content_keys=[course.content_key])

        result = dispatch_algolia_indexing(force=False, content_types=[VIDEO])

        # COURSE is filtered out of dispatch, but its staleness still drove the
        # video derivation.
        self.assertEqual(result['dispatched'][COURSE], {'records': 0, 'batches': 0})
        self.assertEqual(result['dispatched'][VIDEO], {'records': 1, 'batches': 1})
        self.mock_course_si.assert_not_called()
        self.mock_video_si.assert_called_once_with(
            video_pks=[video.edx_video_id],
            index_name=None,
        )

    # ------------------------------------------------------------------
    # use_apply
    # ------------------------------------------------------------------

    def test_use_apply_calls_chain_apply(self):
        """
        use_apply=True causes the dispatched chain to be executed via .apply()
        rather than .apply_async(), making downstream tasks run synchronously.
        """
        course = ContentMetadataFactory(content_type=COURSE, content_key='ua-course')
        self._set_mappings(all_indexable_content_keys=[course.content_key])

        dispatch_algolia_indexing(force=True, use_apply=True)

        self.mock_chain.return_value.apply.assert_called_once()
        self.mock_chain.return_value.apply_async.assert_not_called()

    def test_use_apply_false_calls_chain_apply_async(self):
        """
        use_apply=False (default) dispatches via .apply_async().
        """
        course = ContentMetadataFactory(content_type=COURSE, content_key='ua-async-course')
        self._set_mappings(all_indexable_content_keys=[course.content_key])

        dispatch_algolia_indexing(force=True, use_apply=False)

        self.mock_chain.return_value.apply_async.assert_called_once()
        self.mock_chain.return_value.apply.assert_not_called()


@ddt.ddt
class TestDispatchAlgoliaIndexingHelpers(TestCase):
    """
    Focused tests for Phase 4a helper functions.
    """

    def test_chunked_with_empty_input_yields_no_batches(self):
        """
        ``_chunked`` returns an empty iterator when the input is empty.
        """
        self.assertEqual(list(_chunked([], 2)), [])

    def test_get_indexable_keys_by_content_type_returns_empty_defaults(self):
        """
        Empty ``all_indexable_content_keys`` returns empty lists for each type.
        """
        self.assertEqual(_get_indexable_keys_by_content_type(set()), {
            COURSE: [],
            PROGRAM: [],
            LEARNER_PATHWAY: [],
        })

    def test_get_content_modified_by_key_empty_input(self):
        """
        ``_get_content_modified_by_key`` short-circuits for empty key lists.
        """
        self.assertEqual(_get_content_modified_by_key([], COURSE), {})

    def test_get_indexing_state_by_key_empty_input(self):
        """
        ``_get_indexing_state_by_key`` short-circuits for empty key lists.
        """
        self.assertEqual(_get_indexing_state_by_key([]), {})

    def test_should_retry_failed_record_when_excluded_or_missing_state(self):
        """
        ``_should_retry_failed_record`` is false when retries are disabled or missing state.
        """
        self.assertFalse(_should_retry_failed_record({}, 'missing', include_failed=True))
        self.assertFalse(_should_retry_failed_record({
            'course-key': {'last_failure_at': localized_utcnow()},
        }, 'course-key', include_failed=False))

    def test_has_newer_child_index_ignores_children_without_last_indexed_at(self):
        """
        ``_has_newer_child_index`` ignores child records with no ``last_indexed_at``.
        """
        self.assertFalse(_has_newer_child_index(
            child_keys=['child-key'],
            child_state_by_key={'child-key': {'last_indexed_at': None}},
            parent_last_indexed_at=localized_utcnow(),
        ))

    @ddt.data(
        ('550e8400-e29b-41d4-a716-446655440000', True),
        ('course-v1:edX+DemoX+2024', False),
        (1234, False),
        (None, False),
    )
    @ddt.unpack
    def test_is_uuid_string_edge_cases(self, value, expected):
        """
        ``_is_uuid_string`` handles both string and non-string values safely.
        """
        self.assertEqual(_is_uuid_string(value), expected)

    def test_extract_program_keys_filters_non_uuid_values(self):
        """
        ``_extract_program_keys`` keeps only UUID-shaped values from mixed input.
        """
        program_key = '550e8400-e29b-41d4-a716-446655440000'
        self.assertEqual(_extract_program_keys([
            program_key,
            'course-v1:edX+DemoX+2024',
            1234,
            None,
        ]), [program_key])

    def test_group_aggregation_keys_skips_malformed_keys(self):
        """
        Keys with no ``':'`` separator are logged and excluded from all buckets.
        """
        result = _group_aggregation_keys_by_content_type({'malformed-key-no-colon'})
        self.assertEqual(result, {COURSE: [], PROGRAM: [], LEARNER_PATHWAY: []})

    def test_group_aggregation_keys_skips_unknown_content_types(self):
        """
        Keys whose content-type prefix is not one of the known types are
        logged and excluded.
        """
        result = _group_aggregation_keys_by_content_type({'video:video-key-123'})
        self.assertEqual(result, {COURSE: [], PROGRAM: [], LEARNER_PATHWAY: []})


@override_settings(ALGOLIA_INDEXING_BATCH_SIZE=2)
class TestDispatchAlgoliaIndexingForCatalogQuery(TestCase):
    """
    Tests for the catalog-query-specific dispatcher task.
    """
    # pylint: disable=no-value-for-parameter

    def setUp(self):
        self.algolia_client = mock.MagicMock(name='algolia_client')
        client_patcher = mock.patch.object(
            search_tasks, 'get_initialized_algolia_client', return_value=self.algolia_client,
        )
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

        self.mock_invalidate_cache = mock.patch.object(
            search_tasks, 'invalidate_indexing_mappings_cache',
        ).start()
        self.mock_course_si = mock.patch.object(
            search_tasks.index_courses_batch_in_algolia, 'si',
        ).start()
        self.mock_program_si = mock.patch.object(
            search_tasks.index_programs_batch_in_algolia, 'si',
        ).start()
        self.mock_pathway_si = mock.patch.object(
            search_tasks.index_pathways_batch_in_algolia, 'si',
        ).start()
        self.mock_group = mock.patch.object(search_tasks, 'group').start()
        self.mock_chain = mock.patch.object(search_tasks, 'chain').start()
        self.addCleanup(mock.patch.stopall)

        self.algolia_client.get_aggregation_keys_for_catalog_query.return_value = set()

    def _create_catalog_membership(
        self,
        content_type,
        content_key,
        *,
        catalog_query=None,
        last_indexed_at=None,
        last_failure_at=None,
    ):
        content = ContentMetadataFactory(content_type=content_type, content_key=content_key)
        catalog_query = catalog_query or CatalogQueryFactory()
        content.catalog_queries.add(catalog_query)
        if last_indexed_at is not None or last_failure_at is not None:
            ContentMetadataIndexingStateFactory(
                content_metadata=content,
                last_indexed_at=last_indexed_at,
                last_failure_at=last_failure_at,
            )
        return content, catalog_query

    def _set_catalog_query_diff(self, catalog_query, *extra_removed_aggregation_keys):
        db_keys = set(
            catalog_query.contentmetadata_set.values_list('content_type', 'content_key')
            .order_by('content_type', 'content_key')
        )
        aggregation_keys = {
            f'{content_type}:{content_key}'
            for content_type, content_key in db_keys
        }
        aggregation_keys.update(extra_removed_aggregation_keys)
        self.algolia_client.get_aggregation_keys_for_catalog_query.return_value = aggregation_keys

    def test_dry_run_returns_summary_without_dispatching(self):
        catalog_query = CatalogQueryFactory()
        _current_content, catalog_query = self._create_catalog_membership(
            COURSE, 'course-current',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
        )
        self._create_catalog_membership(
            COURSE, 'course-stale',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
        )
        self._create_catalog_membership(
            COURSE, 'course-failed',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
            last_failure_at=localized_utcnow(),
        )
        self._create_catalog_membership(
            PROGRAM, _program_content_key(),
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
        )
        self._create_catalog_membership(LEARNER_PATHWAY, 'pathway-new', catalog_query=catalog_query)
        self._set_catalog_query_diff(catalog_query, f'{COURSE}:course-removed')

        result = search_tasks.dispatch_algolia_indexing_for_catalog_query(
            catalog_query.id,
            dry_run=True,
            force=False,
            include_failed=True,
            index_name='enterprise_catalog_v2',
        )

        self.assertEqual(result, {
            'catalog_query_id': catalog_query.id,
            'force': False,
            'dry_run': True,
            'batch_size': 2,
            'index_name': 'enterprise_catalog_v2',
            'db_membership_count': 5,
            'algolia_membership_count': 6,
            'removed_count': 1,
            'added_count': 0,
            'dispatched': {
                COURSE: {'records': 3, 'batches': 2},
                PROGRAM: {'records': 1, 'batches': 1},
                LEARNER_PATHWAY: {'records': 1, 'batches': 1},
                VIDEO: {'records': 0, 'batches': 0},
            },
        })
        self.mock_invalidate_cache.assert_not_called()
        self.algolia_client.get_aggregation_keys_for_catalog_query.assert_called_once_with(
            catalog_query.uuid,
            index_name='enterprise_catalog_v2',
        )
        self.mock_course_si.assert_not_called()
        self.mock_program_si.assert_not_called()
        self.mock_pathway_si.assert_not_called()

    def test_force_false_excludes_failed_records_when_requested(self):
        catalog_query = CatalogQueryFactory()
        self._create_catalog_membership(
            COURSE, 'course-current',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
        )
        _failed_content, catalog_query = self._create_catalog_membership(
            COURSE, 'course-failed-only',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
            last_failure_at=localized_utcnow(),
        )
        self._set_catalog_query_diff(catalog_query)

        result = search_tasks.dispatch_algolia_indexing_for_catalog_query(
            catalog_query.id,
            force=False,
            include_failed=False,
        )

        self.assertEqual(result['dispatched'][COURSE], {'records': 0, 'batches': 0})
        self.mock_course_si.assert_not_called()

    def test_added_content_dispatched_even_when_last_indexed_at_is_current(self):
        """
        Content that is in the DB membership but NOT currently facet-tagged in
        Algolia (added to the catalog since the last index run) must be
        dispatched even if its own ``last_indexed_at`` is current (i.e. the
        staleness filter would otherwise skip it).

        This is the symmetric case to removed content:
        ``added = db_aggregation_keys - algolia_aggregation_keys``.
        """
        catalog_query = CatalogQueryFactory()
        # course-current is already indexed and fully up-to-date — normally skipped.
        self._create_catalog_membership(
            COURSE, 'course-current',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
        )
        # course-added is also "current" but Algolia has never written the
        # catalog-query facet for it. Must be dispatched to gain the new facet.
        self._create_catalog_membership(
            COURSE, 'course-added',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
        )
        # Algolia only knows about course-current for this catalog query.
        self.algolia_client.get_aggregation_keys_for_catalog_query.return_value = {
            f'{COURSE}:course-current',
        }

        result = search_tasks.dispatch_algolia_indexing_for_catalog_query(
            catalog_query.id,
            force=False,
            include_failed=False,
        )

        self.assertEqual(result['added_count'], 1)
        self.assertEqual(result['dispatched'][COURSE], {'records': 1, 'batches': 1})
        self.mock_course_si.assert_called_once_with(
            content_keys=['course-added'],
            force=False,
            index_name=None,
        )

    def test_force_true_dispatches_everything_in_the_catalog(self):
        catalog_query = CatalogQueryFactory()
        _current_content, catalog_query = self._create_catalog_membership(
            COURSE, 'course-current',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
        )
        self._create_catalog_membership(
            COURSE, 'course-stale',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() - timedelta(hours=1),
        )
        self._create_catalog_membership(
            COURSE, 'course-failed',
            catalog_query=catalog_query,
            last_indexed_at=localized_utcnow() + timedelta(hours=1),
            last_failure_at=localized_utcnow(),
        )
        removed_course_aggregation_key = f'{COURSE}:course-removed'
        self._set_catalog_query_diff(catalog_query, removed_course_aggregation_key)

        result = search_tasks.dispatch_algolia_indexing_for_catalog_query(
            catalog_query.id,
            force=True,
            index_name='enterprise_catalog_v2',
        )

        self.assertEqual(result['dispatched'][COURSE], {'records': 4, 'batches': 2})
        self.mock_invalidate_cache.assert_called_once_with()
        self.assertEqual(self.mock_course_si.call_count, 2)
        self.assertEqual(
            [call.kwargs['content_keys'] for call in self.mock_course_si.call_args_list],
            [
                ['course-current', 'course-failed'],
                ['course-removed', 'course-stale'],
            ],
        )
        self.assertEqual(
            [call.kwargs['index_name'] for call in self.mock_course_si.call_args_list],
            ['enterprise_catalog_v2', 'enterprise_catalog_v2'],
        )

    def test_catalog_query_not_found_returns_empty_summary(self):
        """
        If the CatalogQuery doesn't exist at execution time (e.g. deleted
        between enqueue and run), the task logs a warning and returns ``{}``
        without touching Algolia or dispatching any batch tasks.
        """
        result = search_tasks.dispatch_algolia_indexing_for_catalog_query(
            catalog_query_id=999999,
        )
        self.assertEqual(result, {})
        self.algolia_client.get_aggregation_keys_for_catalog_query.assert_not_called()
        self.mock_course_si.assert_not_called()
        self.mock_invalidate_cache.assert_not_called()


class TestGetVideoPksForDispatch(TestCase):
    """
    Tests for ``_get_video_pks_for_dispatch``.
    """

    def setUp(self):
        # A course-run ContentMetadata with parent_content_key pointing to a course.
        self.course_key = 'course-v1:Org+Course+Run'
        self.course_run = ContentMetadataFactory(
            content_type=COURSE_RUN,
            parent_content_key=self.course_key,
        )
        self.video = VideoFactory(parent_content_metadata=self.course_run)

    def test_force_returns_all_video_pks(self):
        pks = _get_video_pks_for_dispatch(stale_course_keys=[], force=True)
        self.assertIn(self.video.edx_video_id, pks)

    def test_force_false_with_matching_stale_course(self):
        pks = _get_video_pks_for_dispatch(
            stale_course_keys=[self.course_key],
            force=False,
        )
        self.assertIn(self.video.edx_video_id, pks)

    def test_force_false_with_no_matching_stale_course(self):
        pks = _get_video_pks_for_dispatch(
            stale_course_keys=['some-other-course'],
            force=False,
        )
        self.assertNotIn(self.video.edx_video_id, pks)

    def test_force_false_with_empty_stale_courses_returns_empty(self):
        pks = _get_video_pks_for_dispatch(stale_course_keys=[], force=False)
        self.assertEqual(pks, [])


class TestGetCatalogMembershipByKey(TestCase):
    """
    Tests for ``_get_catalog_membership_by_key``.
    """

    def test_empty_course_keys_returns_empty_dicts(self):
        customer_uuids, catalog_uuids, catalog_queries = _get_catalog_membership_by_key([])
        self.assertEqual(dict(customer_uuids), {})
        self.assertEqual(dict(catalog_uuids), {})
        self.assertEqual(dict(catalog_queries), {})

    def test_course_with_catalog_membership_is_resolved(self):
        catalog_query = CatalogQueryFactory()
        enterprise_catalog = EnterpriseCatalog.objects.create(
            enterprise_uuid=uuid4(),
            catalog_query=catalog_query,
            title='Test Catalog',
        )

        course_key = 'course-v1:Org+Membership+2024'
        course = ContentMetadataFactory(content_type='course', content_key=course_key)
        course.catalog_queries.set([catalog_query])

        customer_uuids, catalog_uuids, catalog_queries = _get_catalog_membership_by_key(
            [course_key]
        )

        self.assertIn(str(enterprise_catalog.enterprise_uuid), customer_uuids[course_key])
        self.assertIn(str(enterprise_catalog.uuid), catalog_uuids[course_key])
        expected_query_tuple = (str(catalog_query.uuid), catalog_query.title)
        self.assertIn(expected_query_tuple, catalog_queries[course_key])

    def test_course_run_membership_accumulates_under_parent_course_key(self):
        """
        Membership attached to a COURSE_RUN record (not the course itself) is
        accumulated under the parent course's content_key. This is the branch
        that actually matters for videos: a video's parent is always a
        course-run, and catalog queries may be associated at the run level.

        Exercises the ``Q(parent_content_key__in=..., content_type=COURSE_RUN)``
        leg of ``_get_catalog_membership_by_key``, which the course-level test
        above does not touch.
        """
        catalog_query = CatalogQueryFactory()
        enterprise_catalog = EnterpriseCatalog.objects.create(
            enterprise_uuid=uuid4(),
            catalog_query=catalog_query,
            title='Run-Level Catalog',
        )

        course_key = 'course-v1:Org+RunMembership+2024'
        # No COURSE record carries the query — only the course-run does.
        course_run = ContentMetadataFactory(
            content_type=COURSE_RUN,
            parent_content_key=course_key,
        )
        course_run.catalog_queries.set([catalog_query])

        customer_uuids, catalog_uuids, catalog_queries = _get_catalog_membership_by_key(
            [course_key]
        )

        # Membership keyed by the parent course, sourced entirely from the run.
        self.assertIn(str(enterprise_catalog.enterprise_uuid), customer_uuids[course_key])
        self.assertIn(str(enterprise_catalog.uuid), catalog_uuids[course_key])
        self.assertIn(
            (str(catalog_query.uuid), catalog_query.title),
            catalog_queries[course_key],
        )


class TestIndexVideoBatchInAlgolia(TestCase):
    """
    Tests for ``index_videos_batch_in_algolia``.
    """
    # pylint: disable=no-value-for-parameter

    def setUp(self):
        self.course_key = 'course-v1:Org+Video+2024'
        self.course_run = ContentMetadataFactory(
            content_type=COURSE_RUN,
            parent_content_key=self.course_key,
        )
        self.video = VideoFactory(parent_content_metadata=self.course_run)

        self.mock_algolia_client = mock.MagicMock()
        self.mock_membership = mock.patch(
            'enterprise_catalog.apps.search.tasks._get_catalog_membership_by_key',
            return_value=(
                {self.course_key: {'customer-uuid-1'}},
                {self.course_key: {'catalog-uuid-1'}},
                {self.course_key: {('query-uuid-1', 'Query Title')}},
            ),
        )
        self.mock_add_video = mock.patch(
            'enterprise_catalog.apps.search.tasks.add_video_to_algolia_objects',
            side_effect=lambda v, products, *a, **kw: products.update(
                {f'{v.edx_video_id}-obj': {'objectID': f'{v.edx_video_id}-obj'}}
            ),
        )
        self.mock_get_client = mock.patch(
            'enterprise_catalog.apps.search.tasks.get_initialized_algolia_client',
            return_value=self.mock_algolia_client,
        )

    def test_happy_path_calls_save_objects_batch(self):
        with self.mock_membership, self.mock_add_video, self.mock_get_client:
            result = search_tasks.index_videos_batch_in_algolia(
                video_pks=[self.video.edx_video_id],
                index_name='test-index',
            )

        self.assertEqual(result['indexed'], 1)
        self.assertEqual(result['skipped'], 0)
        self.mock_algolia_client.save_objects_batch.assert_called_once()
        _, call_kwargs = self.mock_algolia_client.save_objects_batch.call_args
        self.assertEqual(call_kwargs['index_name'], 'test-index')

    def test_empty_video_pks_returns_early_without_db_query(self):
        with mock.patch(
            'enterprise_catalog.apps.search.tasks._get_catalog_membership_by_key'
        ) as mock_membership:
            result = search_tasks.index_videos_batch_in_algolia(video_pks=[])

        self.assertEqual(result['indexed'], 0)
        mock_membership.assert_not_called()

    def test_unknown_video_pks_returns_without_algolia_call(self):
        with self.mock_membership, self.mock_get_client:
            result = search_tasks.index_videos_batch_in_algolia(
                video_pks=['nonexistent-video-id'],
            )

        self.assertEqual(result['indexed'], 0)
        self.assertEqual(result['skipped'], 1)
        self.mock_algolia_client.save_objects_batch.assert_not_called()

    def test_video_with_no_parent_content_metadata_is_skipped(self):
        """
        A video whose parent_content_metadata is None (dangling FK via DO_NOTHING)
        is counted as skipped and does not call add_video_to_algolia_objects.
        The valid sibling in the same batch still produces an Algolia object.
        """
        mock_orphan = mock.MagicMock()
        mock_orphan.edx_video_id = 'orphan-vid'
        mock_orphan.parent_content_metadata = None

        mock_valid = mock.MagicMock()
        mock_valid.edx_video_id = self.video.edx_video_id
        mock_valid.parent_content_metadata = self.course_run

        with mock.patch.object(search_tasks.Video, 'objects') as mock_mgr:
            mock_mgr.filter.return_value.select_related.return_value = [mock_orphan, mock_valid]
            with self.mock_membership, self.mock_add_video, self.mock_get_client:
                result = search_tasks.index_videos_batch_in_algolia(
                    video_pks=['orphan-vid', self.video.edx_video_id],
                )

        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['indexed'], 1)
        self.mock_algolia_client.save_objects_batch.assert_called_once()

    def test_no_algolia_objects_generated_returns_early(self):
        """
        When add_video_to_algolia_objects produces no objects (e.g. the video
        has no catalog membership), the task returns early without calling
        save_objects_batch.
        """
        with self.mock_membership, mock.patch(
            'enterprise_catalog.apps.search.tasks.add_video_to_algolia_objects',
        ):
            result = search_tasks.index_videos_batch_in_algolia(
                video_pks=[self.video.edx_video_id],
            )

        self.assertEqual(result['indexed'], 0)
        self.assertEqual(result['skipped'], 1)  # len(video_pks)
        self.mock_algolia_client.save_objects_batch.assert_not_called()

    def test_algolia_exception_propagates_for_retry(self):
        """
        ``AlgoliaException`` from ``save_objects_batch`` propagates out of the
        task so that ``_LoggedTaskWithRetry.autoretry_for`` can catch it and
        schedule a retry.
        """
        self.mock_algolia_client.save_objects_batch.side_effect = AlgoliaException('algolia boom')
        with self.mock_membership, self.mock_add_video, self.mock_get_client:
            with self.assertRaises(AlgoliaException):
                search_tasks.index_videos_batch_in_algolia(
                    video_pks=[self.video.edx_video_id],
                )


class TestRecordOutcome(TestCase):
    """
    Pinning tests for the ``RecordOutcome`` StrEnum.
    """

    def test_members_compare_equal_to_their_string_value(self):
        self.assertEqual(RecordOutcome.INDEXED, 'indexed')
        self.assertEqual(RecordOutcome.SKIPPED, 'skipped')
        self.assertEqual(RecordOutcome.REMOVED, 'removed')
        self.assertEqual(RecordOutcome.FAILED, 'failed')


class TestBatchSummary(TestCase):
    """
    Pinning tests for the ``BatchSummary`` dataclass — the dispatch
    helpers (``increment``, ``record_failure``) and the ``asdict()``
    conversion that the task wrappers rely on.
    """

    def test_increment_dispatches_via_outcome_member(self):
        """
        ``RecordOutcome`` member values must match ``BatchSummary`` attribute
        names so ``setattr(results, outcome, ...)`` lands on the right field.
        """
        results = BatchSummary(content_type='course')
        results.increment(RecordOutcome.INDEXED)
        results.increment(RecordOutcome.INDEXED)
        results.increment(RecordOutcome.SKIPPED)
        results.increment(RecordOutcome.REMOVED)

        self.assertEqual(results.indexed, 2)
        self.assertEqual(results.skipped, 1)
        self.assertEqual(results.removed, 1)
        self.assertEqual(results.failed, 0)

    def test_record_failure_increments_counter_and_tracks_key(self):
        results = BatchSummary(content_type='course')
        results.record_failure('course-bad-1')
        results.record_failure('course-bad-2')

        self.assertEqual(results.failed, 2)
        self.assertEqual(results.failed_keys, ['course-bad-1', 'course-bad-2'])

    def test_asdict_produces_celery_safe_dict(self):
        """
        Task wrappers convert ``BatchSummary`` to a dict so the on-the-wire
        Celery payload stays JSON-serializable.
        """
        results = BatchSummary(content_type='course')
        results.increment(RecordOutcome.INDEXED)
        results.record_failure('course-bad')

        as_dict = asdict(results)
        self.assertEqual(as_dict, {
            'content_type': 'course',
            'indexed': 1,
            'skipped': 0,
            'removed': 0,
            'failed': 1,
            'failed_keys': ['course-bad'],
        })


class TestIndexingDecisionConstructors(TestCase):
    """
    The classmethod constructors are the public way to build an
    ``IndexingDecision``; these pin the per-outcome field invariants so
    callers can rely on them.
    """

    def test_skipped_carries_no_objects_or_ids(self):
        decision = IndexingDecision.skipped(
            content_key='c-1', content=mock.sentinel.content, state=mock.sentinel.state,
        )
        self.assertEqual(decision.desired_outcome, RecordOutcome.SKIPPED)
        self.assertEqual(decision.outcome, RecordOutcome.SKIPPED)
        self.assertEqual(decision.new_objects, [])
        self.assertEqual(decision.new_object_ids, [])
        self.assertEqual(decision.ids_to_delete, [])
        self.assertIsNone(decision.failure_reason)

    def test_removed_copies_ids_to_delete_into_a_list(self):
        decision = IndexingDecision.removed(
            content_key='c-1', content=mock.sentinel.content, state=mock.sentinel.state,
            ids_to_delete=('id-0', 'id-1'),
        )
        self.assertEqual(decision.desired_outcome, RecordOutcome.REMOVED)
        self.assertEqual(decision.outcome, RecordOutcome.REMOVED)
        self.assertEqual(decision.ids_to_delete, ['id-0', 'id-1'])
        self.assertEqual(decision.new_objects, [])

    def test_indexed_carries_objects_and_orphan_ids(self):
        new_objects = [{'objectID': 'id-0'}, {'objectID': 'id-1'}]
        decision = IndexingDecision.indexed(
            content_key='c-1', content=mock.sentinel.content, state=mock.sentinel.state,
            new_objects=new_objects,
            new_object_ids=['id-0', 'id-1'],
            ids_to_delete=['orphan-0'],
        )
        self.assertEqual(decision.desired_outcome, RecordOutcome.INDEXED)
        self.assertEqual(decision.outcome, RecordOutcome.INDEXED)
        self.assertEqual(decision.new_objects, new_objects)
        self.assertEqual(decision.new_object_ids, ['id-0', 'id-1'])
        self.assertEqual(decision.ids_to_delete, ['orphan-0'])

    def test_failed_allows_missing_content_and_state(self):
        exc = ValueError('boom')
        decision = IndexingDecision.failed(content_key='c-1', failure_reason=exc)
        self.assertEqual(decision.desired_outcome, RecordOutcome.FAILED)
        self.assertEqual(decision.outcome, RecordOutcome.FAILED)
        self.assertIsNone(decision.content)
        self.assertIsNone(decision.state)
        self.assertIs(decision.failure_reason, exc)

    def test_per_record_save_failure_mutates_outcome_only_not_desired(self):
        """
        Pass 2's per-record save fallback updates ``outcome`` to FAILED but
        leaves ``desired_outcome`` as INDEXED so the original plan stays
        readable for debugging.
        """
        decision = IndexingDecision.indexed(
            content_key='c-1', content=mock.sentinel.content, state=mock.sentinel.state,
            new_objects=[{'objectID': 'id-0'}],
            new_object_ids=['id-0'],
            ids_to_delete=[],
        )
        # Simulate the fallback mutation.
        decision.outcome = RecordOutcome.FAILED
        self.assertEqual(decision.desired_outcome, RecordOutcome.INDEXED)
        self.assertEqual(decision.outcome, RecordOutcome.FAILED)

    def test_explicitly_set_outcome_is_not_overwritten_by_post_init(self):
        """
        Constructing IndexingDecision directly with outcome= already set
        leaves that value untouched — __post_init__ only fills in the default
        when outcome is None. Covers the ``outcome is not None`` branch.
        """
        decision = IndexingDecision(
            content_key='c-1',
            desired_outcome=RecordOutcome.INDEXED,
            outcome=RecordOutcome.FAILED,
        )
        self.assertEqual(decision.desired_outcome, RecordOutcome.INDEXED)
        self.assertEqual(decision.outcome, RecordOutcome.FAILED)


class TestBuildObjectsByContentKey(TestCase):
    """
    Tests for ``_build_objects_by_content_key``.
    """

    def test_objects_with_mismatched_aggregation_key_are_dropped(self):
        """
        The legacy generator sometimes includes objects for related content
        (e.g. courses pulled into a program batch). Objects whose
        aggregation_key doesn't match any of the batch's content keys are
        silently dropped. Covers the ``agg_key not in aggregation_key_to_content_key``
        branch.
        """
        target_key = 'course-v1:Org+Target+Run'
        mappings = IndexingMappings(
            program_to_course_keys={},
            pathway_to_program_course_keys={},
            all_indexable_content_keys={target_key},
        )
        unrelated_obj = {
            'objectID': 'unrelated-0',
            'aggregation_key': 'course:unrelated-key',
        }
        target_obj = {
            'objectID': 'target-0',
            'aggregation_key': f'course:{target_key}',
        }

        with mock.patch(
            'enterprise_catalog.apps.search.tasks._get_algolia_products_for_batch',
            return_value=[unrelated_obj, target_obj],
        ):
            result = _build_objects_by_content_key([target_key], COURSE, mappings)

        self.assertEqual(result[target_key], [target_obj])
        self.assertNotIn('unrelated-key', result)


class TestFinalizeDecision(TestCase):
    """
    Direct unit tests for ``_finalize_decision``.
    """

    def test_unknown_outcome_is_a_silent_noop(self):
        """
        ``_finalize_decision`` silently falls through when outcome is not one
        of the four RecordOutcome members. Covers the False branch of the
        final ``elif`` (outcome == INDEXED).
        """
        results = BatchSummary(content_type=COURSE)
        decision = IndexingDecision.skipped(
            content_key='c-1', content=None, state=None,
        )
        decision.outcome = 'unexpected'

        _finalize_decision(decision, results)

        self.assertEqual(results.indexed, 0)
        self.assertEqual(results.skipped, 0)
        self.assertEqual(results.removed, 0)
        self.assertEqual(results.failed, 0)
