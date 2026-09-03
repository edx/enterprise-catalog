"""
Admin-only debug view for diagnosing content-catalog membership issues.
"""
import logging
import uuid

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from edx_rest_framework_extensions.auth.jwt.authentication import (
    JwtAuthentication,
)
from rest_framework import permissions, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView

from enterprise_catalog.apps.api.v1.serializers import (
    BaseErrorSerializer,
    ContentMembershipDebugResponseSerializer,
)
from enterprise_catalog.apps.catalog.models import (
    ContentMetadata,
    EnterpriseCatalog,
)
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


logger = logging.getLogger(__name__)


def _content_block(content_metadata):
    """
    Local state of the ContentMetadata record itself, including when it last changed.
    """
    if not content_metadata:
        # Same keys either way, so consumers don't have to branch on presence.
        return {
            'exists': False,
            'content_type': None,
            'content_uuid': None,
            'parent_content_key': None,
            'created': None,
            'modified': None,
            'discovery_modified': None,
        }
    return {
        'exists': True,
        'content_type': content_metadata.content_type,
        'content_uuid': content_metadata.content_uuid,
        'parent_content_key': content_metadata.parent_content_key,
        'created': content_metadata.created,
        'modified': content_metadata.modified,
        # Discovery's own modified time, mirrored into json_metadata for courses only.
        'discovery_modified': (content_metadata.json_metadata or {}).get('modified'),
    }


def _algolia_block(indexing_state):
    """
    Algolia indexing state from the database only.

    ``ContentMetadataIndexingState`` records *that* the content was indexed and which
    object ID shards were written, but not the ``enterprise_customer_uuids`` /
    ``enterprise_catalog_uuids`` payload that went with them, so nothing here confirms a
    given customer or catalog was present in the indexed record. The per-catalog
    ``indexed_after_last_membership_change`` flag is the DB-only substitute.
    """
    if not indexing_state:
        return {
            'indexing_state_exists': False,
            'last_indexed_at': None,
            'removed_from_index_at': None,
            'last_failure_at': None,
            'failure_reason': None,
            'is_stale': None,
            'algolia_object_ids': [],
        }
    return {
        'indexing_state_exists': True,
        'last_indexed_at': indexing_state.last_indexed_at,
        'removed_from_index_at': indexing_state.removed_from_index_at,
        'last_failure_at': indexing_state.last_failure_at,
        'failure_reason': indexing_state.failure_reason,
        'is_stale': indexing_state.is_stale,
        'algolia_object_ids': indexing_state.algolia_object_ids,
    }


def _catalog_block(catalog, content_key, last_indexed_at):
    """
    Membership of ``content_key`` in a single catalog, with and without restricted runs.
    """
    catalog_query = catalog.catalog_query

    # Two calls on purpose: this reuses the exact production membership path rather than
    # reimplementing it. A staff debug endpoint doesn't need the query count optimized.
    matching = catalog.get_matching_content([content_key])
    matching_with_restricted = catalog.get_matching_content([content_key], include_restricted=True)

    contains = matching.exists()
    # Avoid values_list() here: the include_restricted queryset carries a Prefetch(to_attr=...),
    # which values_list() does not support.
    matched_content_keys = sorted({metadata.content_key for metadata in matching_with_restricted})
    contains_with_restricted = bool(matched_content_keys)

    query_modified = catalog_query.modified if catalog_query else None
    indexed_after_last_membership_change = None
    if last_indexed_at and query_modified:
        indexed_after_last_membership_change = last_indexed_at > query_modified

    return {
        'catalog_uuid': catalog.uuid,
        'title': catalog.title,
        'catalog_query_id': catalog.catalog_query_id,
        'catalog_query_uuid': catalog_query.uuid if catalog_query else None,
        'contains_content': contains,
        'contains_content_including_restricted': contains_with_restricted,
        # The diagnostic: in the catalog only by way of a restricted run.
        'restricted_only': contains_with_restricted and not contains,
        'matched_content_keys': matched_content_keys,
        'restricted_runs_allowed': catalog_query.restricted_runs_allowed if catalog_query else None,
        # The closest available stand-ins for "when did membership last change". The
        # CatalogQuery <-> ContentMetadata M2M has no through model and no history, and every
        # sync rewrites it wholesale via set(clear=True), so no per-membership timestamp
        # exists to return. Both fields are named for exactly what they are.
        'catalog_query_modified': query_modified,
        'enterprise_catalog_modified': catalog.modified,
        'indexed_after_last_membership_change': indexed_after_last_membership_change,
    }


class ContentMembershipDebugView(APIView):
    """
    Staff-only view reporting why a content key is (or isn't) available to an enterprise customer.

    For a given customer and content key, returns:
      1. which of the customer's catalogs contain the content
      2. how that answer changes when restricted course runs are included
      3. when the membership was last updated (proxy timestamps only, see below)
      4. when the content metadata was last updated
      5. when the content was last indexed in Algolia

    Two caveats are surfaced in the payload rather than papered over: there is no true
    membership timestamp in the schema, and the stored indexing state does not record which
    customer/catalog UUIDs were written to Algolia.
    """
    authentication_classes = [JwtAuthentication, SessionAuthentication]
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary='Debug why content is or is not available to an enterprise customer',
        description=(
            'Staff-only. For one enterprise customer and one content key, reports which of the '
            "customer's catalogs contain the content, how that answer changes when restricted "
            'course runs are included, when the content metadata last changed, and when the '
            'content was last indexed in Algolia.'
            '\n\n'
            'Read `restricted_only` first. When `contains_content` is false but '
            '`contains_content_including_restricted` is true, the content **is** in the catalog '
            'and is being hidden because the run is restricted, which is a different problem from '
            'the content not being in the catalog at all.'
            '\n\n'
            'Two limits are worth knowing. There is no true "membership last changed" '
            'timestamp: the CatalogQuery/ContentMetadata M2M has no through model, no history, '
            'and is rewritten wholesale on every sync, so `catalog_query_modified` and '
            '`enterprise_catalog_modified` are the closest stand-ins. And nothing here confirms '
            'which customer or catalog UUIDs were actually written to Algolia: the stored '
            'indexing state records the object ID shards but not their payload, and this '
            'endpoint makes no outbound Algolia call. Use '
            '`indexed_after_last_membership_change` instead.'
            '\n\n'
            'Routed identically in v1 and v2. Unlike other v2 endpoints there is no '
            '`include_restricted` difference, because both answers are always returned.'
        ),
        parameters=[
            OpenApiParameter(
                name='enterprise_uuid',
                type=str,
                location=OpenApiParameter.PATH,
                description='UUID of the enterprise customer whose catalogs are searched.',
            ),
            OpenApiParameter(
                name='content_key',
                type=str,
                location=OpenApiParameter.PATH,
                description=(
                    'Content key to look up, e.g. `edX+DemoX` or `course-v1:edX+DemoX+1T2024`. '
                    'Content uuids are not accepted.'
                ),
            ),
        ],
        responses={
            200: ContentMembershipDebugResponseSerializer,
            400: OpenApiResponse(
                response=BaseErrorSerializer,
                description='The enterprise_uuid path parameter is not a valid UUID.',
            ),
        },
        tags=['Enterprise Customer'],
        examples=[
            OpenApiExample(
                'Content hidden by a restricted run',
                value={
                    'enterprise_uuid': '8b1b1b1e-0000-4000-8000-000000000001',
                    'content_key': 'course-v1:edX+DemoX+3T2024',
                    'content': {
                        'exists': True,
                        'content_type': 'courserun',
                        'content_uuid': '3f2b1b1e-0000-4000-8000-000000000002',
                        'parent_content_key': 'edX+DemoX',
                        'created': '2026-07-14T09:20:00Z',
                        'modified': '2026-09-01T04:11:00Z',
                        'discovery_modified': '2026-09-01T04:10:58Z',
                    },
                    'catalogs': [{
                        'catalog_uuid': '2c4d1b1e-0000-4000-8000-000000000003',
                        'title': 'Demo Catalog',
                        'catalog_query_id': 12,
                        'catalog_query_uuid': '9a7c1b1e-0000-4000-8000-000000000004',
                        'contains_content': False,
                        'contains_content_including_restricted': True,
                        'restricted_only': True,
                        'matched_content_keys': ['course-v1:edX+DemoX+3T2024'],
                        'restricted_runs_allowed': {
                            'edX+DemoX': ['course-v1:edX+DemoX+3T2024'],
                        },
                        'catalog_query_modified': '2026-08-30T11:02:00Z',
                        'enterprise_catalog_modified': '2026-07-14T09:20:00Z',
                        'indexed_after_last_membership_change': False,
                    }],
                    'catalogs_containing_content': [],
                    'catalogs_containing_content_including_restricted': [
                        '2c4d1b1e-0000-4000-8000-000000000003',
                    ],
                    'algolia': {
                        'indexing_state_exists': True,
                        'last_indexed_at': '2026-08-29T02:15:00Z',
                        'removed_from_index_at': None,
                        'last_failure_at': None,
                        'failure_reason': None,
                        'is_stale': True,
                        'algolia_object_ids': ['course-abc123-catalog-uuids-0'],
                    },
                },
                response_only=True,
            ),
        ],
    )
    def get(self, request, enterprise_uuid, content_key):
        """
        Return the membership debug report for ``content_key`` under ``enterprise_uuid``.
        """
        try:
            uuid.UUID(enterprise_uuid)
        except ValueError:
            logger.warning(
                'content-membership-debug called with unparseable enterprise uuid: %s',
                enterprise_uuid,
            )
            error = {
                'user_message': 'Invalid enterprise customer uuid.',
                'developer_message': f'Could not parse "{enterprise_uuid}" as a UUID.',
            }
            return Response(
                BaseErrorSerializer(error).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Deliberately not a 404 when the content is unknown locally: "never synced from
        # Discovery" is the answer to the question being asked, not an error.
        content_metadata = ContentMetadata.objects.filter(content_key=content_key).first()
        indexing_state = None
        if content_metadata:
            indexing_state = ContentMetadataIndexingState.objects.filter(
                content_metadata=content_metadata,
            ).first()
        last_indexed_at = indexing_state.last_indexed_at if indexing_state else None

        catalogs = EnterpriseCatalog.objects.filter(
            enterprise_uuid=enterprise_uuid,
        ).select_related('catalog_query')

        catalog_blocks = [
            _catalog_block(catalog, content_key, last_indexed_at)
            for catalog in catalogs
        ]

        response_data = {
            'enterprise_uuid': enterprise_uuid,
            'content_key': content_key,
            'content': _content_block(content_metadata),
            'catalogs': catalog_blocks,
            'catalogs_containing_content': [
                block['catalog_uuid'] for block in catalog_blocks if block['contains_content']
            ],
            'catalogs_containing_content_including_restricted': [
                block['catalog_uuid'] for block in catalog_blocks
                if block['contains_content_including_restricted']
            ],
            'algolia': _algolia_block(indexing_state),
        }
        return Response(ContentMembershipDebugResponseSerializer(response_data).data)
