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
    BatchSummary,
    IndexingDecision,
    RecordOutcome,
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
