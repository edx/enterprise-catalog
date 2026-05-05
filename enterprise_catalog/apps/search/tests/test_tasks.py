"""
Tests for the Phase 3 incremental Algolia indexing batch tasks.
"""
from dataclasses import asdict
from datetime import timedelta
from unittest import mock

import ddt
from algoliasearch.exceptions import AlgoliaException
from django.test import TestCase

from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.tests.factories import (
    ContentMetadataFactory,
)
from enterprise_catalog.apps.catalog.utils import localized_utcnow
from enterprise_catalog.apps.search import tasks as search_tasks
from enterprise_catalog.apps.search.indexing_mappings import IndexingMappings
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
from enterprise_catalog.apps.search.tasks import (
    BatchOutcome,
    BatchResults,
    _index_content_batch,
    index_courses_batch_in_algolia,
    index_pathways_batch_in_algolia,
    index_programs_batch_in_algolia,
)
from enterprise_catalog.apps.search.tests.factories import (
    ContentMetadataIndexingStateFactory,
)


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
        Three indexable, never-indexed courses → all three indexed; save +
        mark_as_indexed called per record.
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
        self.assertEqual(self.algolia_client.save_objects_batch.call_count, 3)
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

    def test_per_record_algolia_failure_marks_failed_and_continues(self):
        """
        AlgoliaException on save for one record → that record marked failed,
        the other records still index successfully.
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

        # Fail on the bad key only.
        def save_side_effect(objects, index_name=None):  # pylint: disable=unused-argument
            if objects[0]['aggregation_key'] == f'course:{c2.content_key}':
                raise AlgoliaException('boom')
        self.algolia_client.save_objects_batch.side_effect = save_side_effect

        result = _index_content_batch(
            [c1.content_key, c2.content_key, c3.content_key], COURSE,
        )

        self.assertEqual(result.indexed, 2)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, [c2.content_key])
        bad_state = ContentMetadataIndexingState.objects.get(content_metadata=c2)
        self.assertIsNotNone(bad_state.last_failure_at)
        self.assertIn('boom', bad_state.failure_reason)

    def test_missing_content_metadata_counts_as_failed(self):
        """
        content_key passed to the task but no ContentMetadata exists for it
        (e.g. deleted between dispatch and execution) → counted as failed,
        no exception bubbles up.
        """
        result = _index_content_batch(['course-missing'], COURSE)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_keys, ['course-missing'])

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


class TestBatchOutcome(TestCase):
    """
    Pinning tests for the ``BatchOutcome`` StrEnum.
    """

    def test_members_compare_equal_to_their_string_value(self):
        self.assertEqual(BatchOutcome.INDEXED, 'indexed')
        self.assertEqual(BatchOutcome.SKIPPED, 'skipped')
        self.assertEqual(BatchOutcome.REMOVED, 'removed')
        self.assertEqual(BatchOutcome.FAILED, 'failed')


class TestBatchResults(TestCase):
    """
    Pinning tests for the ``BatchResults`` dataclass — the dispatch
    helpers (``increment``, ``record_failure``) and the ``asdict()``
    conversion that the task wrappers rely on.
    """

    def test_increment_dispatches_via_outcome_member(self):
        """
        ``BatchOutcome`` member values must match ``BatchResults`` attribute
        names so ``setattr(results, outcome, ...)`` lands on the right field.
        """
        results = BatchResults(content_type='course')
        results.increment(BatchOutcome.INDEXED)
        results.increment(BatchOutcome.INDEXED)
        results.increment(BatchOutcome.SKIPPED)
        results.increment(BatchOutcome.REMOVED)

        self.assertEqual(results.indexed, 2)
        self.assertEqual(results.skipped, 1)
        self.assertEqual(results.removed, 1)
        self.assertEqual(results.failed, 0)

    def test_record_failure_increments_counter_and_tracks_key(self):
        results = BatchResults(content_type='course')
        results.record_failure('course-bad-1')
        results.record_failure('course-bad-2')

        self.assertEqual(results.failed, 2)
        self.assertEqual(results.failed_keys, ['course-bad-1', 'course-bad-2'])

    def test_asdict_produces_celery_safe_dict(self):
        """
        Task wrappers convert ``BatchResults`` to a dict so the on-the-wire
        Celery payload stays JSON-serializable.
        """
        results = BatchResults(content_type='course')
        results.increment(BatchOutcome.INDEXED)
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


class TestSafelyMarkFailed(TestCase):
    """
    The state-update path must not itself blow up the batch.
    """
    # pylint: disable=protected-access

    def test_swallows_exceptions_from_mark_as_failed(self):
        """
        If ``mark_as_failed`` raises, the helper logs and returns rather than
        propagating — callers shouldn't have their batch failed by the
        failure-recording path.
        """
        content = ContentMetadataFactory(content_type=COURSE, content_key='course-explode')
        with mock.patch.object(
            ContentMetadataIndexingState, 'mark_as_failed',
            side_effect=RuntimeError('db down'),
        ):
            # Should not raise.
            search_tasks._safely_mark_failed(content, ValueError('original'))
